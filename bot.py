import os
import asyncio
import json
import uuid
import secrets
import sqlite3
import ssl
import subprocess
from io import BytesIO
from datetime import datetime, timedelta
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import qrcode
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
XUI_DB = os.getenv("XUI_DB", "/etc/x-ui/x-ui.db")
SUB_BASE_URL = os.getenv("SUB_BASE_URL", "").rstrip("/")
PANEL_API_BASE_URL = os.getenv("PANEL_API_BASE_URL", "").rstrip("/")
PANEL_API_TOKEN = os.getenv("PANEL_API_TOKEN", "").strip()
PANEL_API_SKIP_TLS_VERIFY = os.getenv("PANEL_API_SKIP_TLS_VERIFY", "false").lower() == "true"
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_OWNER = os.getenv("CARD_OWNER", "")
RESTART_XUI_AFTER_APPROVE = os.getenv("RESTART_XUI_AFTER_APPROVE", "true").lower() == "true"

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

BOT_DB = "/opt/xui-user-bot/bot.sqlite"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
REASSIGN_ALL_LOCK = asyncio.Lock()

PACKAGES = {
    "unlimited_300": {
        "title": "نامحدود",
        "price": 300000,
        "price_text": "300,000 تومان",
        "traffic_gb": 0,
        "traffic_text": "نامحدود",
        "days": 30,
    },
    "50gb_50": {
        "title": "50 گیگ",
        "price": 50000,
        "price_text": "50,000 تومان",
        "traffic_gb": 50,
        "traffic_text": "50 گیگ",
        "days": 30,
    },
    "100gb_90": {
        "title": "100 گیگ",
        "price": 90000,
        "price_text": "90,000 تومان",
        "traffic_gb": 100,
        "traffic_text": "100 گیگ",
        "days": 30,
    },
}

def main_menu(user_id=None):
    keyboard = [
        [KeyboardButton(text="🛒 خرید اشتراک")],
        [KeyboardButton(text="🧩 مدیریت اشتراک‌ها")],
        [KeyboardButton(text="ℹ️ راهنما")],
    ]

    if user_id is not None and is_admin(user_id):
        keyboard.append([KeyboardButton(text="🛠 ری‌اساین همه")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

menu = main_menu()

def now_ms():
    return int(datetime.now().timestamp() * 1000)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def xui_conn():
    conn = sqlite3.connect(XUI_DB)
    conn.row_factory = sqlite3.Row
    return conn

def xui_api_enabled():
    return bool(PANEL_API_BASE_URL and PANEL_API_TOKEN)

def xui_api_request(method, path, payload=None):
    if not xui_api_enabled():
        raise RuntimeError("Panel API is not configured.")

    body = None
    headers = {
        "Authorization": f"Bearer {PANEL_API_TOKEN}",
        "Accept": "application/json",
    }

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib_request.Request(
        url=f"{PANEL_API_BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method.upper(),
    )

    context = None
    if PANEL_API_BASE_URL.startswith("https://") and PANEL_API_SKIP_TLS_VERIFY:
        context = ssl._create_unverified_context()

    try:
        with urllib_request.urlopen(request, context=context, timeout=25) as response:
            raw_body = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Panel API HTTP {exc.code}: {details}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Panel API connection failed: {exc}") from exc

    if not raw_body:
        return None

    data = json.loads(raw_body)
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(data.get("msg") or "Panel API request failed.")

    if isinstance(data, dict) and "obj" in data:
        return data["obj"]

    return data

def get_table_columns(conn, table_name):
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
    except Exception:
        return []
    return [row["name"] for row in rows]

def has_unique_index_on_columns(conn, table_name, target_columns):
    try:
        index_rows = conn.execute(f"PRAGMA index_list({table_name});").fetchall()
    except Exception:
        return False

    normalized_target = tuple(target_columns)

    for index_row in index_rows:
        if not index_row["unique"]:
            continue

        try:
            info_rows = conn.execute(f"PRAGMA index_info({index_row['name']});").fetchall()
        except Exception:
            continue

        index_columns = tuple(row["name"] for row in info_rows)
        if index_columns == normalized_target:
            return True

    return False

def bot_conn():
    conn = sqlite3.connect(BOT_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_bot_db():
    conn = bot_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            tg_username TEXT,
            tg_full_name TEXT,
            package_key TEXT NOT NULL,
            status TEXT NOT NULL,
            receipt_file_id TEXT,
            xui_email TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def gb_to_bytes(gb: int) -> int:
    if gb <= 0:
        return 0
    return gb * 1024 * 1024 * 1024

def bytes_to_gb(value):
    value = int(value or 0)
    return f"{value / 1024 / 1024 / 1024:.2f} GB"

def format_time(ms):
    ms = int(ms or 0)
    if ms <= 0:
        return "نامحدود"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

def safe_token(length=16):
    return secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:length]

def restart_xui():
    if not RESTART_XUI_AFTER_APPROVE:
        return
    try:
        subprocess.run(
            ["systemctl", "restart", "x-ui"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
        )
    except Exception:
        pass

def get_all_inbound_ids():
    if xui_api_enabled():
        rows = xui_api_request("GET", "/panel/api/inbounds/options") or []
        return [int(row["id"]) for row in rows]

    conn = xui_conn()
    rows = conn.execute("SELECT id FROM inbounds").fetchall()
    conn.close()
    return [int(row["id"]) for row in rows]

def build_api_client_payload(email, tg_id, total_bytes, expiry_ms, base_client=None):
    payload = {
        "email": email,
        "totalGB": int(total_bytes),
        "expiryTime": int(expiry_ms),
        "tgId": int(tg_id or 0),
        "limitIp": 0,
        "enable": True,
        "comment": "",
        "reset": 0,
    }

    if not base_client:
        return payload

    payload.update(
        {
            "limitIp": int(base_client["limit_ip"] or 0),
            "comment": base_client["comment"] or "",
            "reset": int(base_client["reset"] or 0),
            "flow": base_client["flow"] or "",
            "security": base_client["security"] or "",
            "subId": base_client["sub_id"] or "",
            "password": base_client["password"] or "",
            "auth": base_client["auth"] or "",
            "uuid": base_client["uuid"] or "",
            "group": base_client["group_name"] or "",
            "reverse": None,
        }
    )
    return payload

def xui_api_attach_client_to_inbounds(email, inbound_ids):
    if not inbound_ids:
        return None

    encoded_email = urllib_parse.quote(email, safe="")
    return xui_api_request(
        "POST",
        f"/panel/api/clients/{encoded_email}/attach",
        {"inboundIds": inbound_ids},
    )

def maybe_insert_client_inbound(conn, client_id, inbound_id):
    try:
        col_names = get_table_columns(conn, "client_inbounds")

        if "client_id" in col_names and "inbound_id" in col_names:
            exists = conn.execute(
                "SELECT id FROM client_inbounds WHERE client_id = ? AND inbound_id = ? LIMIT 1",
                (client_id, inbound_id),
            ).fetchone()

            if not exists:
                conn.execute(
                    "INSERT INTO client_inbounds (client_id, inbound_id) VALUES (?, ?)",
                    (client_id, inbound_id),
                )
    except Exception:
        pass

def ensure_client_traffic_rows(conn, cur, inbound_ids, email, expiry_ms, total_bytes):
    col_names = get_table_columns(conn, "client_traffics")
    has_inbound_id = "inbound_id" in col_names
    email_is_unique = has_unique_index_on_columns(conn, "client_traffics", ("email",))
    writable_values = {
        "enable": 1,
        "email": email,
        "up": 0,
        "down": 0,
        "expiry_time": expiry_ms,
        "total": total_bytes,
        "reset": 0,
        "last_online": 0,
    }

    def insert_traffic_row(target_inbound_id):
        insert_values = dict(writable_values)
        if "inbound_id" in col_names:
            insert_values["inbound_id"] = target_inbound_id

        filtered_items = [(key, value) for key, value in insert_values.items() if key in col_names]
        fields = ", ".join(key for key, _ in filtered_items)
        placeholders = ", ".join("?" for _ in filtered_items)
        values = [value for _, value in filtered_items]

        cur.execute(
            f"INSERT INTO client_traffics ({fields}) VALUES ({placeholders})",
            values,
        )

    def update_traffic_row(where_clause, where_params):
        update_fields = [
            (key, value)
            for key, value in (
                ("enable", 1),
                ("expiry_time", expiry_ms),
                ("total", total_bytes),
            )
            if key in col_names
        ]

        if not update_fields:
            return

        set_clause = ", ".join(f"{key} = ?" for key, _ in update_fields)
        params = [value for _, value in update_fields] + list(where_params)
        cur.execute(
            f"UPDATE client_traffics SET {set_clause} WHERE {where_clause}",
            params,
        )

    if has_inbound_id and inbound_ids and not email_is_unique:
        for inbound_id in inbound_ids:
            traffic_exists = cur.execute(
                "SELECT id FROM client_traffics WHERE email = ? AND inbound_id = ? LIMIT 1",
                (email, inbound_id),
            ).fetchone()

            if not traffic_exists:
                insert_traffic_row(inbound_id)
            else:
                update_traffic_row("email = ? AND inbound_id = ?", (email, inbound_id))
        return

    traffic_exists = cur.execute(
        "SELECT id FROM client_traffics WHERE email = ? LIMIT 1",
        (email,),
    ).fetchone()

    primary_inbound_id = inbound_ids[0] if inbound_ids else 1

    if not traffic_exists:
        insert_traffic_row(primary_inbound_id)
    else:
        update_traffic_row("email = ?", (email,))

def assign_client_to_all_inbounds(conn, cur, client_id, email, expiry_ms, total_bytes):
    inbound_ids = get_all_inbound_ids()

    for inbound_id in inbound_ids:
        maybe_insert_client_inbound(conn, client_id, inbound_id)

    ensure_client_traffic_rows(conn, cur, inbound_ids, email, expiry_ms, total_bytes)

def create_order(tg_id, tg_username, tg_full_name, package_key):
    conn = bot_conn()
    cur = conn.cursor()
    t = now_ms()
    cur.execute(
        """
        INSERT INTO orders (
            tg_id, tg_username, tg_full_name, package_key, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'waiting_payment_method', ?, ?)
        """,
        (tg_id, tg_username, tg_full_name, package_key, t, t),
    )
    order_id = cur.lastrowid
    conn.commit()
    conn.close()
    return order_id

def get_order(order_id):
    conn = bot_conn()
    order = conn.execute("SELECT * FROM orders WHERE id = ? LIMIT 1", (order_id,)).fetchone()
    conn.close()
    return order

def update_order_status(order_id, status):
    conn = bot_conn()
    conn.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_ms(), order_id),
    )
    conn.commit()
    conn.close()

def save_receipt(order_id, file_id):
    conn = bot_conn()
    conn.execute(
        """
        UPDATE orders
        SET receipt_file_id = ?, status = 'waiting_admin_approval', updated_at = ?
        WHERE id = ?
        """,
        (file_id, now_ms(), order_id),
    )
    conn.commit()
    conn.close()

def save_order_xui_email(order_id, email):
    conn = bot_conn()
    conn.execute(
        "UPDATE orders SET xui_email = ?, updated_at = ? WHERE id = ?",
        (email, now_ms(), order_id),
    )
    conn.commit()
    conn.close()

def get_latest_waiting_receipt_order(tg_id):
    conn = bot_conn()
    order = conn.execute(
        """
        SELECT * FROM orders
        WHERE tg_id = ? AND status = 'waiting_receipt'
        ORDER BY id DESC
        LIMIT 1
        """,
        (tg_id,),
    ).fetchone()
    conn.close()
    return order

def get_clients_by_tg_id(tg_id):
    conn = xui_conn()
    rows = conn.execute(
        "SELECT * FROM clients WHERE tg_id = ? ORDER BY id DESC",
        (tg_id,),
    ).fetchall()
    conn.close()
    return rows

def get_client_by_id_and_tg(client_id, tg_id):
    conn = xui_conn()
    row = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND tg_id = ? LIMIT 1",
        (client_id, tg_id),
    ).fetchone()
    conn.close()
    return row

def get_client_by_email_and_tg(email, tg_id):
    conn = xui_conn()
    row = conn.execute(
        "SELECT * FROM clients WHERE email = ? AND tg_id = ? LIMIT 1",
        (email, tg_id),
    ).fetchone()
    conn.close()
    return row

def get_client_by_tg_latest(tg_id):
    conn = xui_conn()
    row = conn.execute(
        "SELECT * FROM clients WHERE tg_id = ? ORDER BY id DESC LIMIT 1",
        (tg_id,),
    ).fetchone()
    conn.close()
    return row

def get_traffic(email):
    conn = xui_conn()
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(up), 0) AS up,
            COALESCE(SUM(down), 0) AS down,
            COALESCE(MAX(last_online), 0) AS last_online
        FROM client_traffics
        WHERE email = ?
        """,
        (email,),
    ).fetchone()
    conn.close()
    return row

def subscription_link(client):
    if not client or not client["sub_id"]:
        return None
    return f"{SUB_BASE_URL}/sub/{client['sub_id']}"

def create_qr_image(text):
    img = qrcode.make(text)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return BufferedInputFile(bio.read(), filename="subscription_qr.png")

def package_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{pkg['title']} - {pkg['price_text']}", callback_data=f"buy:{key}")]
            for key, pkg in PACKAGES.items()
        ]
    )

def payment_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 کارت به کارت", callback_data=f"pay_card:{order_id}")]
        ]
    )

def admin_order_keyboard(order_id, status):
    if status == "approved":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ تایید شده", callback_data="noop")]]
        )
    if status == "rejected":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ رد شده", callback_data="noop")]]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تایید و ساخت اشتراک", callback_data=f"admin_approve:{order_id}")],
            [InlineKeyboardButton(text="❌ رد رسید", callback_data=f"admin_reject:{order_id}")],
        ]
    )

def services_keyboard(clients):
    rows = []
    for c in clients:
        traffic = get_traffic(c["email"])
        used = int(traffic["up"] or 0) + int(traffic["down"] or 0)
        total = int(c["total_gb"] or 0)
        remain = "نامحدود" if total <= 0 else bytes_to_gb(max(total - used, 0))
        rows.append([
            InlineKeyboardButton(
                text=f"{c['email']} | باقی‌مانده: {remain}",
                callback_data=f"svc:{c['id']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def service_actions_keyboard(client_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 حجم سرویس", callback_data=f"svc_usage:{client_id}"),
                InlineKeyboardButton(text="🔋 حجم باقی‌مانده", callback_data=f"svc_remain:{client_id}"),
            ],
            [
                InlineKeyboardButton(text="📷 دریافت QR", callback_data=f"svc_qr:{client_id}"),
                InlineKeyboardButton(text="🔗 لینک اشتراک", callback_data=f"svc_link:{client_id}"),
            ],
            [InlineKeyboardButton(text="♻️ تغییر لینک اشتراک", callback_data=f"svc_reset_link:{client_id}")],
            [
                InlineKeyboardButton(text="⏰ تاریخ انقضا", callback_data=f"svc_expiry:{client_id}"),
                InlineKeyboardButton(text="🔁 تمدید اشتراک", callback_data=f"svc_renew:{client_id}"),
            ],
            [InlineKeyboardButton(text="⬅️ برگشت به لیست", callback_data="manage_services")],
        ]
    )

def create_new_xui_client(tg_id, package_key, order_id):
    pkg = PACKAGES[package_key]
    total_bytes = gb_to_bytes(pkg["traffic_gb"])
    expiry_ms = int((datetime.now() + timedelta(days=pkg["days"])).timestamp() * 1000)

    email = f"connectme-{tg_id}-{order_id}"

    if xui_api_enabled():
        inbound_ids = get_all_inbound_ids()
        payload = {
            "client": build_api_client_payload(email, tg_id, total_bytes, expiry_ms),
            "inboundIds": inbound_ids,
        }
        xui_api_request("POST", "/panel/api/clients/add", payload)
        save_order_xui_email(order_id, email)
        return email

    sub_id = safe_token(16)
    user_uuid = str(uuid.uuid4())
    password = safe_token(16)
    auth = safe_token(16)

    conn = xui_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO clients (
            email, sub_id, uuid, password, auth, flow, security, reverse,
            limit_ip, total_gb, expiry_time, enable, tg_id, group_name,
            comment, reset, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, '', 'auto', '', 0, ?, ?, 1, ?, '', ?, 0, ?, ?)
        """,
        (
            email,
            sub_id,
            user_uuid,
            password,
            auth,
            total_bytes,
            expiry_ms,
            tg_id,
            f"Created by Telegram bot - order #{order_id}",
            now_ms(),
            now_ms(),
        ),
    )

    client_id = cur.lastrowid
    assign_client_to_all_inbounds(conn, cur, client_id, email, expiry_ms, total_bytes)

    conn.commit()
    conn.close()

    save_order_xui_email(order_id, email)
    restart_xui()
    return email

def renew_xui_client(client_id, tg_id, package_key, order_id):
    pkg = PACKAGES[package_key]
    total_bytes = gb_to_bytes(pkg["traffic_gb"])
    new_expiry_ms = int((datetime.now() + timedelta(days=pkg["days"])).timestamp() * 1000)

    conn = xui_conn()
    cur = conn.cursor()

    client = cur.execute(
        "SELECT * FROM clients WHERE id = ? AND tg_id = ? LIMIT 1",
        (client_id, tg_id),
    ).fetchone()

    if not client:
        conn.close()
        return None

    email = client["email"]

    if xui_api_enabled():
        payload = build_api_client_payload(email, tg_id, total_bytes, new_expiry_ms, client)
        xui_api_request(
            "POST",
            f"/panel/api/clients/update/{urllib_parse.quote(email, safe='')}",
            payload,
        )
        xui_api_attach_client_to_inbounds(email, get_all_inbound_ids())
        conn.close()
        save_order_xui_email(order_id, email)
        return email

    cur.execute(
        """
        UPDATE clients
        SET total_gb = ?, expiry_time = ?, enable = 1, updated_at = ?
        WHERE id = ?
        """,
        (total_bytes, new_expiry_ms, now_ms(), client_id),
    )

    cur.execute(
        """
        UPDATE client_traffics
        SET up = 0, down = 0, total = ?, expiry_time = ?, enable = 1
        WHERE email = ?
        """,
        (total_bytes, new_expiry_ms, email),
    )

    assign_client_to_all_inbounds(conn, cur, client_id, email, new_expiry_ms, total_bytes)

    conn.commit()
    conn.close()

    save_order_xui_email(order_id, email)
    restart_xui()
    return email

def reset_subscription_link(client_id, tg_id):
    conn = xui_conn()
    cur = conn.cursor()

    client = cur.execute(
        "SELECT * FROM clients WHERE id = ? AND tg_id = ? LIMIT 1",
        (client_id, tg_id),
    ).fetchone()

    if not client:
        conn.close()
        return None

    new_sub_id = safe_token(16)

    cur.execute(
        "UPDATE clients SET sub_id = ?, updated_at = ? WHERE id = ?",
        (new_sub_id, now_ms(), client_id),
    )

    conn.commit()
    conn.close()

    restart_xui()
    return get_client_by_id_and_tg(client_id, tg_id)

def reassign_all_clients_to_all_inbounds():
    if xui_api_enabled():
        clients = xui_api_request("GET", "/panel/api/clients/list") or []
        inbound_ids = get_all_inbound_ids()

        emails = [client.get("email") for client in clients if client.get("email")]
        if not emails or not inbound_ids:
            return 0, 0

        result = xui_api_request(
            "POST",
            "/panel/api/clients/bulkAttach",
            {"emails": emails, "inboundIds": inbound_ids},
        ) or {}

        errors = result.get("errors") if isinstance(result, dict) else None
        error_count = len(errors) if isinstance(errors, list) else 0
        return len(emails) - error_count, error_count

    conn = xui_conn()
    cur = conn.cursor()
    clients = cur.execute(
        "SELECT id, email, expiry_time, total_gb FROM clients ORDER BY id ASC"
    ).fetchall()

    reassigned = 0
    skipped = 0

    for client in clients:
        email = client["email"]
        if not email:
            skipped += 1
            continue

        assign_client_to_all_inbounds(
            conn=conn,
            cur=cur,
            client_id=client["id"],
            email=email,
            expiry_ms=int(client["expiry_time"] or 0),
            total_bytes=int(client["total_gb"] or 0),
        )
        reassigned += 1

    conn.commit()
    conn.close()
    restart_xui()
    return reassigned, skipped

async def run_admin_reassign_all(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("دسترسی ندارید.", reply_markup=main_menu(message.from_user.id))
        return

    if REASSIGN_ALL_LOCK.locked():
        await message.answer(
            "ری‌اساین همه همین الان در حال اجراست. چند لحظه بعد دوباره بررسی کنید.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    try:
        async with REASSIGN_ALL_LOCK:
            reassigned, skipped = reassign_all_clients_to_all_inbounds()
        await message.answer(
            f"ری‌اساین همه انجام شد.\n\n"
            f"کلاینت‌های پردازش‌شده: {reassigned}\n"
            f"ردشده/ناقص: {skipped}",
            reply_markup=main_menu(message.from_user.id),
        )
    except Exception:
        await message.answer(
            "هنگام ری‌اساین همه خطا رخ داد. لاگ سرور را بررسی کنید.",
            reply_markup=main_menu(message.from_user.id),
        )
        raise

async def send_final_config_to_user(tg_id, client, pkg):
    link = subscription_link(client)

    if not link:
        await bot.send_message(tg_id, "اشتراک ساخته شد ولی لینک اشتراک پیدا نشد.", reply_markup=menu)
        return

    qr = create_qr_image(link)

    caption = (
        f"✅ سفارش جدید شما\n"
        f"🔮 نام سرویس: {client['email']}\n"
        f"🔋 حجم سرویس: {pkg['traffic_text']}\n"
        f"⏰ مدت سرویس: {pkg['days']} روز\n"
        f"🌟 از اعتماد شما سپاسگزار هستیم.\n\n\n"
        f"🩸 Your License :\n"
        f"{link}"
    )

    await bot.send_photo(tg_id, photo=qr, caption=caption, reply_markup=menu)

async def send_packages(message):
    text = "🛒 لطفاً پکیج موردنظر خود را انتخاب کنید:\n\n"
    for pkg in PACKAGES.values():
        text += (
            f"📦 {pkg['title']}\n"
            f"حجم: {pkg['traffic_text']}\n"
            f"مدت: {pkg['days']} روز\n"
            f"قیمت: {pkg['price_text']}\n\n"
        )
    await message.answer(text, reply_markup=package_keyboard())

async def send_services(message):
    clients = get_clients_by_tg_id(message.from_user.id)

    if not clients:
        await message.answer(
            "هنوز هیچ اشتراکی برای شما ثبت نشده.\n\n"
            "برای خرید از دکمه «🛒 خرید اشتراک» استفاده کنید.",
            reply_markup=menu,
        )
        return

    await message.answer("🧩 اشتراک موردنظر را انتخاب کنید:", reply_markup=services_keyboard(clients))

async def send_service_panel(callback, client_id):
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    traffic = get_traffic(client["email"])
    used = int(traffic["up"] or 0) + int(traffic["down"] or 0)
    total = int(client["total_gb"] or 0)
    remain = "نامحدود" if total <= 0 else bytes_to_gb(max(total - used, 0))

    text = (
        f"🧩 مدیریت سرویس\n\n"
        f"🔮 نام سرویس: {client['email']}\n"
        f"وضعیت: {'فعال ✅' if bool(client['enable']) else 'غیرفعال ❌'}\n"
        f"مصرف‌شده: {bytes_to_gb(used)}\n"
        f"باقی‌مانده: {remain}\n"
        f"انقضا: {format_time(client['expiry_time'])}\n\n"
        f"یکی از گزینه‌های زیر را انتخاب کنید:"
    )

    await callback.message.edit_text(text, reply_markup=service_actions_keyboard(client_id))

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "سلام 👋\n\n"
        "به ربات مدیریت اشتراک خوش آمدید.\n"
        "از منوی پایین می‌توانید خرید کنید یا اشتراک‌های خود را مدیریت کنید.",
        reply_markup=main_menu(message.from_user.id),
    )

@dp.message(Command("buy"))
async def buy(message: types.Message):
    await send_packages(message)

@dp.message(Command("manage"))
async def manage(message: types.Message):
    await send_services(message)

@dp.message(Command("reassignall"))
async def admin_reassign_all_cmd(message: types.Message):
    await run_admin_reassign_all(message)

@dp.message(F.text == "🛒 خرید اشتراک")
async def buy_btn(message: types.Message):
    await send_packages(message)

@dp.message(F.text == "🧩 مدیریت اشتراک‌ها")
async def manage_btn(message: types.Message):
    await send_services(message)

@dp.message(F.text == "ℹ️ راهنما")
async def help_btn(message: types.Message):
    await message.answer(
        "راهنما:\n\n"
        "🛒 خرید اشتراک: انتخاب پکیج، کارت به کارت، ارسال رسید\n"
        "🧩 مدیریت اشتراک‌ها: مشاهده حجم، QR، لینک، تغییر لینک، تاریخ انقضا و تمدید\n\n"
        "بعد از تایید رسید توسط ادمین، اشتراک به صورت خودکار ساخته می‌شود.",
        reply_markup=main_menu(message.from_user.id),
    )

@dp.message(F.text == "🛠 ری‌اساین همه")
async def admin_reassign_all_btn(message: types.Message):
    await run_admin_reassign_all(message)

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == "manage_services")
async def manage_services_callback(callback: CallbackQuery):
    clients = get_clients_by_tg_id(callback.from_user.id)

    if not clients:
        await callback.message.edit_text("هیچ اشتراکی برای شما پیدا نشد.")
        await callback.answer()
        return

    await callback.message.edit_text(
        "🧩 اشتراک موردنظر را انتخاب کنید:",
        reply_markup=services_keyboard(clients),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("svc:"))
async def service_selected(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    await send_service_panel(callback, client_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("svc_usage:"))
async def svc_usage(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    traffic = get_traffic(client["email"])
    up = int(traffic["up"] or 0)
    down = int(traffic["down"] or 0)
    used = up + down

    await callback.answer(
        f"آپلود: {bytes_to_gb(up)}\nدانلود: {bytes_to_gb(down)}\nکل مصرف: {bytes_to_gb(used)}",
        show_alert=True,
    )

@dp.callback_query(F.data.startswith("svc_remain:"))
async def svc_remain(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    traffic = get_traffic(client["email"])
    used = int(traffic["up"] or 0) + int(traffic["down"] or 0)
    total = int(client["total_gb"] or 0)

    remain = "نامحدود" if total <= 0 else bytes_to_gb(max(total - used, 0))
    await callback.answer(f"حجم باقی‌مانده: {remain}", show_alert=True)

@dp.callback_query(F.data.startswith("svc_expiry:"))
async def svc_expiry(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    await callback.answer(f"تاریخ انقضا:\n{format_time(client['expiry_time'])}", show_alert=True)

@dp.callback_query(F.data.startswith("svc_link:"))
async def svc_link(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    link = subscription_link(client)

    if not link:
        await callback.answer("لینک پیدا نشد.", show_alert=True)
        return

    await callback.message.answer(f"🔗 لینک اشتراک:\n\n{link}", reply_markup=menu)
    await callback.answer()

@dp.callback_query(F.data.startswith("svc_qr:"))
async def svc_qr(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    link = subscription_link(client)

    if not link:
        await callback.answer("لینک پیدا نشد.", show_alert=True)
        return

    qr = create_qr_image(link)
    caption = (
        f"📷 QR اشتراک شما\n\n"
        f"🔮 نام سرویس: {client['email']}\n"
        f"🩸 Your License:\n"
        f"{link}"
    )

    await callback.message.answer_photo(photo=qr, caption=caption, reply_markup=menu)
    await callback.answer()

@dp.callback_query(F.data.startswith("svc_reset_link:"))
async def svc_reset_link(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = reset_subscription_link(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    link = subscription_link(client)
    qr = create_qr_image(link)

    caption = (
        f"♻️ لینک اشتراک شما تغییر کرد.\n\n"
        f"از این لحظه لینک قبلی دیگر معتبر نیست.\n\n"
        f"🔮 نام سرویس: {client['email']}\n\n"
        f"🩸 Your License:\n"
        f"{link}"
    )

    await callback.message.answer_photo(photo=qr, caption=caption, reply_markup=menu)
    await callback.answer("لینک تغییر کرد ✅", show_alert=True)

@dp.callback_query(F.data.startswith("svc_renew:"))
async def svc_renew(callback: CallbackQuery):
    client_id = int(callback.data.split(":", 1)[1])
    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    rows = []
    for key, pkg in PACKAGES.items():
        rows.append([
            InlineKeyboardButton(
                text=f"{pkg['title']} - {pkg['price_text']}",
                callback_data=f"renew:{client_id}:{key}",
            )
        ])

    await callback.message.answer(
        f"🔁 تمدید اشتراک\n\n"
        f"سرویس انتخابی: {client['email']}\n\n"
        f"پکیج تمدید را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery):
    package_key = callback.data.split(":", 1)[1]

    if package_key not in PACKAGES:
        await callback.answer("پکیج نامعتبر است.", show_alert=True)
        return

    order_id = create_order(
        tg_id=callback.from_user.id,
        tg_username=callback.from_user.username or "",
        tg_full_name=callback.from_user.full_name or "",
        package_key=package_key,
    )

    pkg = PACKAGES[package_key]

    await callback.message.answer(
        f"🛒 سفارش شما ثبت شد.\n\n"
        f"شماره سفارش: {order_id}\n"
        f"نوع سفارش: خرید سرویس جدید\n"
        f"پکیج: {pkg['title']}\n"
        f"حجم: {pkg['traffic_text']}\n"
        f"مدت: {pkg['days']} روز\n"
        f"مبلغ: {pkg['price_text']}\n\n"
        f"لطفاً روش پرداخت را انتخاب کنید:",
        reply_markup=payment_keyboard(order_id),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("renew:"))
async def renew_callback(callback: CallbackQuery):
    _, client_id_str, package_key = callback.data.split(":", 2)
    client_id = int(client_id_str)

    client = get_client_by_id_and_tg(client_id, callback.from_user.id)

    if not client:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    if package_key not in PACKAGES:
        await callback.answer("پکیج نامعتبر است.", show_alert=True)
        return

    order_id = create_order(
        tg_id=callback.from_user.id,
        tg_username=callback.from_user.username or "",
        tg_full_name=callback.from_user.full_name or "",
        package_key=package_key,
    )

    conn = bot_conn()
    conn.execute(
        "UPDATE orders SET xui_email = ?, updated_at = ? WHERE id = ?",
        (client["email"], now_ms(), order_id),
    )
    conn.commit()
    conn.close()

    pkg = PACKAGES[package_key]

    await callback.message.answer(
        f"🔁 سفارش تمدید ثبت شد.\n\n"
        f"شماره سفارش: {order_id}\n"
        f"سرویس: {client['email']}\n"
        f"پکیج: {pkg['title']}\n"
        f"حجم: {pkg['traffic_text']}\n"
        f"مدت: {pkg['days']} روز\n"
        f"مبلغ: {pkg['price_text']}\n\n"
        f"لطفاً روش پرداخت را انتخاب کنید:",
        reply_markup=payment_keyboard(order_id),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_card:"))
async def pay_card(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    if order["tg_id"] != callback.from_user.id:
        await callback.answer("این سفارش متعلق به شما نیست.", show_alert=True)
        return

    update_order_status(order_id, "waiting_receipt")
    pkg = PACKAGES[order["package_key"]]

    await callback.message.answer(
        f"💳 پرداخت کارت به کارت\n\n"
        f"شماره سفارش: {order_id}\n"
        f"مبلغ: {pkg['price_text']}\n\n"
        f"شماره کارت:\n"
        f"{CARD_NUMBER}\n\n"
        f"به نام:\n"
        f"{CARD_OWNER}\n\n"
        f"بعد از واریز، عکس رسید پرداخت را همینجا ارسال کنید.\n"
        f"بعد از تایید ادمین، سرویس به صورت خودکار ساخته یا تمدید می‌شود ✅",
        reply_markup=menu,
    )
    await callback.answer()

@dp.message(F.photo)
async def receipt_photo(message: types.Message):
    order = get_latest_waiting_receipt_order(message.from_user.id)

    if not order:
        await message.answer(
            "رسیدی در انتظار پرداخت برای شما پیدا نشد.\n\n"
            "اول یک سفارش خرید یا تمدید ثبت کنید.",
            reply_markup=menu,
        )
        return

    file_id = message.photo[-1].file_id
    save_receipt(order["id"], file_id)

    pkg = PACKAGES[order["package_key"]]

    await message.answer(
        "✅ رسید شما دریافت شد.\n\n"
        "بعد از بررسی ادمین، نتیجه به شما اعلام می‌شود.",
        reply_markup=menu,
    )

    order_type = "تمدید اشتراک" if order["xui_email"] else "خرید سرویس جدید"

    caption = (
        f"🧾 رسید پرداخت جدید\n\n"
        f"وضعیت: ⏳ در انتظار بررسی\n"
        f"شماره سفارش: {order['id']}\n"
        f"نوع سفارش: {order_type}\n"
        f"کاربر: {order['tg_full_name']}\n"
        f"Username: @{order['tg_username'] if order['tg_username'] else 'ندارد'}\n"
        f"Telegram ID: {order['tg_id']}\n\n"
        f"سرویس تمدیدی: {order['xui_email'] if order['xui_email'] else '-'}\n\n"
        f"پکیج: {pkg['title']}\n"
        f"حجم: {pkg['traffic_text']}\n"
        f"مدت: {pkg['days']} روز\n"
        f"مبلغ: {pkg['price_text']}\n\n"
        f"برای تایید یا رد، از دکمه‌های زیر استفاده کنید."
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                photo=file_id,
                caption=caption,
                reply_markup=admin_order_keyboard(order["id"], "waiting_admin_approval"),
            )
        except Exception:
            pass

@dp.callback_query(F.data.startswith("admin_approve:"))
async def admin_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    if order["status"] == "approved":
        await callback.answer("قبلاً تایید شده.", show_alert=True)
        return

    if order["status"] != "waiting_admin_approval":
        await callback.answer("این سفارش آماده تایید نیست.", show_alert=True)
        return

    try:
        if order["xui_email"]:
            target_client = get_client_by_email_and_tg(order["xui_email"], order["tg_id"])
            if target_client:
                email = renew_xui_client(target_client["id"], order["tg_id"], order["package_key"], order_id)
            else:
                email = create_new_xui_client(order["tg_id"], order["package_key"], order_id)
        else:
            email = create_new_xui_client(order["tg_id"], order["package_key"], order_id)

        update_order_status(order_id, "approved")

        client = get_client_by_email_and_tg(email, order["tg_id"])
        pkg = PACKAGES[order["package_key"]]

        if client:
            await send_final_config_to_user(order["tg_id"], client, pkg)

        new_caption = (
            f"🧾 رسید پرداخت\n\n"
            f"وضعیت: ✅ تایید شده\n"
            f"شماره سفارش: {order_id}\n"
            f"X-UI Service: {email}\n\n"
            f"توسط ادمین تایید شد."
        )

        try:
            await callback.message.edit_caption(
                caption=new_caption,
                reply_markup=admin_order_keyboard(order_id, "approved"),
            )
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=admin_order_keyboard(order_id, "approved"))

        await callback.answer("تایید شد ✅")

    except Exception as e:
        await callback.answer("خطا موقع ساخت اشتراک. لاگ سرور را ببین.", show_alert=True)
        raise e

@dp.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.split(":", 1)[1])
    order = get_order(order_id)

    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    update_order_status(order_id, "rejected")

    try:
        await bot.send_message(
            order["tg_id"],
            "❌ رسید پرداخت شما تایید نشد.\n\n"
            "لطفاً رسید صحیح ارسال کنید یا با پشتیبانی در ارتباط باشید.",
            reply_markup=menu,
        )
    except Exception:
        pass

    new_caption = (
        f"🧾 رسید پرداخت\n\n"
        f"وضعیت: ❌ رد شده\n"
        f"شماره سفارش: {order_id}\n\n"
        f"توسط ادمین رد شد."
    )

    try:
        await callback.message.edit_caption(
            caption=new_caption,
            reply_markup=admin_order_keyboard(order_id, "rejected"),
        )
    except Exception:
        await callback.message.edit_reply_markup(reply_markup=admin_order_keyboard(order_id, "rejected"))

    await callback.answer("رد شد ❌")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("از منوی پایین استفاده کنید 👇", reply_markup=menu)

if __name__ == "__main__":
    import asyncio
    init_bot_db()
    asyncio.run(dp.start_polling(bot))
