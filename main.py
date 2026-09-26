import os
import json
import base64
import sqlite3
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID", "707417409")
CHANNEL_STORAGE_ID = int(os.getenv("CHANNEL_STORAGE_ID", "-1003931747114"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_db():
    conn = sqlite3.connect("market_bot.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_active TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            user_id INTEGER,
            task_key TEXT,
            status TEXT DEFAULT 'idle',
            reason TEXT DEFAULT '',
            idea_title TEXT DEFAULT '',
            idea_desc TEXT DEFAULT '',
            reward_requested INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (user_id, task_key)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            service_key TEXT,
            service_title TEXT,
            comment TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS live_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            user_name TEXT,
            text TEXT,
            tag TEXT,
            icon TEXT,
            created_at TEXT,
            timestamp INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER,
            friend_username TEXT,
            friend_name TEXT,
            earned REAL DEFAULT 0,
            created_at TEXT,
            PRIMARY KEY (user_id, friend_username)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok"}

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_active=excluded.last_active
    """, (user.id, (user.username or "").lower(), user.first_name, now_time))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, ?, 'Запустил бота в Telegram', 'bot_start', 'smart_toy', ?, ?)
    """, (user.id, (user.username or "").lower(), user.first_name, now_time, now_ts))
    conn.commit()
    conn.close()

    try:
        clear_msg = await message.answer("...", reply_markup=types.ReplyKeyboardRemove())
        await clear_msg.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Открыть CS:GO Market", url="https://t.me/market_02_bot/app")]
    ])
    await message.answer(
        f"Привет, {user.first_name}! Добро пожаловать в CS:GO Market.\n\n"
        f"Здесь вы можете приобрести скины, донат, игры или забрать бесплатные скины за простые задания!",
        reply_markup=kb
    )

@app.post("/api/sync")
async def sync_all(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "user").replace("@", "").lower().strip()
    first_name = data.get("firstName", "Пользователь")
    is_admin_req = str(user_id) == str(ADMIN_ID) or data.get("isAdmin", False)
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_active=excluded.last_active
    """, (user_id, username, first_name, now_time))

    cur.execute("SELECT timestamp FROM live_feed WHERE user_id = ? AND tag = 'login' ORDER BY id DESC LIMIT 1", (user_id,))
    last_log = cur.fetchone()
    if not last_log or (now_ts - last_log["timestamp"]) > 900000:
        cur.execute("""
            INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
            VALUES (?, ?, ?, 'Открыл приложение CS:GO Market', 'login', 'login', ?, ?)
        """, (user_id, username, first_name, now_time, now_ts))
    conn.commit()

    # Задания текущего юзера
    cur.execute("SELECT task_key, status, reason, idea_title, idea_desc, reward_requested FROM tasks WHERE user_id = ?", (user_id,))
    tasks_db = {r["task_key"]: {"status": r["status"], "reason": r["reason"], "ideaTitle": r["idea_title"], "ideaDesc": r["idea_desc"], "rewardRequested": bool(r["reward_requested"])} for r in cur.fetchall()}
        
    # Выгрузка модерации для админа (если админ)
    admin_tasks_db = []
    if is_admin_req:
        cur.execute("""
            SELECT t.user_id, t.task_key, t.status, t.idea_title, t.idea_desc, t.reward_requested, u.username, u.first_name, t.updated_at
            FROM tasks t LEFT JOIN users u ON t.user_id = u.user_id
            WHERE t.status IN ('in_progress', 'accepted') OR t.reward_requested = 1
            ORDER BY t.updated_at DESC
        """)
        for r in cur.fetchall():
            admin_tasks_db.append({
                "userId": r["user_id"], "taskKey": r["task_key"], "status": r["status"],
                "ideaTitle": r["idea_title"], "ideaDesc": r["idea_desc"], "rewardRequested": bool(r["reward_requested"]),
                "username": r["username"] or "", "firstName": r["first_name"] or "Клиент", "time": r["updated_at"]
            })

    cur.execute("SELECT friend_username, friend_name, earned, created_at FROM referrals WHERE user_id = ?", (user_id,))
    ref_rows = cur.fetchall()
    refs_db = [{"username": f"@{r['friend_username']}", "tasksCount": 0, "earned": r["earned"], "date": r["created_at"]} for r in ref_rows]
    total_ref_earned = sum(float(r["earned"] or 0) for r in ref_rows)

    cur.execute("SELECT value FROM app_settings WHERE key = ?", (f"withdrawn_{user_id}",))
    w_row = cur.fetchone()
    withdrawn = float(w_row["value"]) if w_row and w_row["value"] else 0.0
    ref_balance = max(0.0, round(total_ref_earned - withdrawn, 2))

    cur.execute("SELECT id, user_id, username, first_name, service_key, service_title, comment, status, created_at FROM market_orders WHERE status = 'new' ORDER BY id DESC LIMIT 50")
    orders_db = [{"id": r["id"], "user": r["first_name"], "userTag": f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}", "serviceKey": r["service_key"], "serviceTitle": r["service_title"], "comment": r["comment"], "status": r["status"], "time": r["created_at"]} for r in cur.fetchall()]

    cur.execute("SELECT user_name, username, user_id, text, tag, icon, created_at, timestamp FROM live_feed ORDER BY id DESC LIMIT 50")
    feed_db = [{"user": r["user_name"], "userTag": f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}", "text": r["text"], "tag": r["tag"], "icon": r["icon"], "time": r["created_at"], "timestamp": r["timestamp"]} for r in cur.fetchall()]

    cur.execute("SELECT key, value FROM app_settings")
    settings_db = {r["key"]: r["value"] for r in cur.fetchall()}

    ozon_card = {
        "status": settings_db.get("ozon_status", "idle"),
        "link": settings_db.get("ozon_link", "https://finance.ozon.ru/promo/card"),
        "readyTimestamp": int(settings_db.get("ozon_ready_timestamp", 0))
    }
    conn.close()

    return {
        "ok": True,
        "tasksState": tasks_db,
        "adminTasks": admin_tasks_db,
        "marketOrders": orders_db,
        "liveFeed": feed_db,
        "referrals": refs_db,
        "referralBalance": ref_balance,
        "appSettings": settings_db,
        "ozonCard": ozon_card
    }

@app.post("/api/admin/settings")
async def save_settings(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.cursor()
    for k, v in data.items():
        if k not in ["userId", "username", "firstName"]:
            cur.execute("""
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (k, str(v)))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/ozon/request")
async def request_ozon(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    first_name = data.get("firstName", "Клиент")
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES ('ozon_status', 'requested')
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """)
    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, ?, 'Запросил персональную ссылку Ozon (72ч)', 'request', 'shopping_basket', ?, ?)
    """, (user_id, username, first_name, now_time, now_ts))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]])
        await bot.send_message(chat_id=ADMIN_ID, text=f"⏳ <b>Запрос ссылки Ozon Карты!</b>\nКлиент: @{username} (ID: <code>{user_id}</code>)\nПерейдите в Админку ➔ Выдача данных и выдайте ссылку.", reply_markup=admin_kb, parse_mode="HTML")
    except Exception:
        pass
    return {"ok": True}

@app.post("/api/ozon/update")
async def update_ozon(request: Request):
    data = await request.json()
    status = data.get("status", "ready")
    link = data.get("link", "https://finance.ozon.ru/promo/card")
    ready_ts = data.get("readyTimestamp", int(datetime.now().timestamp() * 1000))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES ('ozon_status', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (status,))
    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES ('ozon_link', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (link,))
    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES ('ozon_ready_timestamp', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (str(ready_ts),))
    
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)
    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, 'admin', 'Админ', 'Выдал клиентам ссылку Ozon (72ч)', 'ozon', 'link', ?, ?)
    """, (ADMIN_ID, now_time, now_ts))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/check_referral")
async def check_referral(request: Request):
    data = await request.json()
    ref_username = (data.get("username") or "").replace("@", "").lower().strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, first_name, username FROM users WHERE LOWER(username) = ?", (ref_username,))
    row = cur.fetchone()
    conn.close()
    if row: return {"exists": True, "userId": row["user_id"], "name": row["first_name"]}
    return {"exists": False}

@app.post("/api/referral/add")
async def add_referral(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    friend_username = (data.get("friendUsername") or "").replace("@", "").lower().strip()
    friend_name = data.get("friendName", friend_username)
    now_date = datetime.now().strftime("%d.%m.%Y")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO referrals (user_id, friend_username, friend_name, earned, created_at) VALUES (?, ?, ?, 0, ?)", (user_id, friend_username, friend_name, now_date))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/referral/withdraw")
async def withdraw_referral(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    first_name = data.get("firstName", "Клиент")
    amount = float(data.get("amount", 0))

    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = ?", (f"withdrawn_{user_id}",))
    row = cur.fetchone()
    cur_w = float(row["value"]) if row and row["value"] else 0.0
    new_w = cur_w + amount

    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (f"withdrawn_{user_id}", str(new_w)))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, ?, ?, 'withdraw', 'card_giftcard', ?, ?)
    """, (user_id, username, first_name, f"Запросил вывод бонуса {amount} ₽", now_time, now_ts))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]])
        await bot.send_message(chat_id=ADMIN_ID, text=f"💰 <b>Запрос на вывод реф. бонуса!</b>\nКлиент: @{username} (ID: <code>{user_id}</code>)\nСумма: <b>{amount} ₽</b> скином.", reply_markup=admin_kb, parse_mode="HTML")
    except Exception:
        pass
    return {"ok": True}

@app.post("/api/task/update")
async def update_task(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    task_key = data.get("taskKey")
    status = data.get("status", "idle")
    reason = data.get("reason", "")
    idea_title = data.get("ideaTitle", "")
    idea_desc = data.get("ideaDesc", "")
    reward_requested = 1 if data.get("rewardRequested") else 0
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (user_id, task_key, status, reason, idea_title, idea_desc, reward_requested, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, task_key) DO UPDATE SET 
            status=excluded.status, 
            reason=excluded.reason, 
            idea_title=excluded.idea_title, 
            idea_desc=excluded.idea_desc, 
            reward_requested=excluded.reward_requested, 
            updated_at=excluded.updated_at
    """, (user_id, task_key, status, reason, idea_title, idea_desc, reward_requested, now_time))

    if status == 'in_progress':
        cur.execute("""
            INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
            VALUES (?, ?, 'Клиент', ?, 'task', 'checklist', ?, ?)
        """, (user_id, username, f"Отправил задание '{task_key}' на проверку", now_time, now_ts))
    elif reward_requested or status == 'accepted':
        cur.execute("""
            INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
            VALUES (?, ?, 'Клиент', ?, 'claim', 'card_giftcard', ?, ?)
        """, (user_id, username, f"Запросил получение скина за '{task_key}'", now_time, now_ts))

    conn.commit()
    conn.close()

    try:
        if reward_requested:
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]])
            await bot.send_message(chat_id=ADMIN_ID, text=f"🎁 <b>Запрос на выдачу скина!</b>\nКлиент: @{username}\nЗадание: <b>{task_key}</b>", reply_markup=admin_kb, parse_mode="HTML")
        elif status == 'accepted':
            await bot.send_message(chat_id=user_id, text=f"🎉 <b>Задание одобрено!</b>\nЗадание: <b>{task_key}</b>\nОткройте мини-апп и нажмите кнопку <b>Получить скин</b>!", parse_mode="HTML")
        elif status == 'rejected':
            await bot.send_message(chat_id=user_id, text=f"❌ <b>Задание отклонено</b>\nЗадание: <b>{task_key}</b>\nПричина: {reason}\nВы можете исправить и попробовать снова.", parse_mode="HTML")
        elif status == 'completed':
            await bot.send_message(chat_id=user_id, text=f"✅ <b>Скин передан!</b>\nЗадание: <b>{task_key}</b> успешно закрыто. Спасибо за участие!", parse_mode="HTML")
    except Exception:
        pass

    return {"ok": True}

@app.post("/api/task/submit_proof")
async def submit_proof(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    task_key = data.get("taskKey")
    task_title = data.get("taskTitle", "Задание")
    img_base64 = data.get("screenshot")
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    if img_base64:
        try:
            header, encoded = img_base64.split(",", 1) if "," in img_base64 else ("", img_base64)
            image_bytes = base64.b64decode(encoded)
            file_payload = BufferedInputFile(image_bytes, filename="proof.jpg")
            await bot.send_photo(chat_id=CHANNEL_STORAGE_ID, photo=file_payload, caption=f"📸 <b>Скриншот задания!</b>\n👤 @{username} (ID: <code>{user_id}</code>)\n🎯 <b>{task_title}</b>", parse_mode="HTML")
        except Exception:
            pass

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (user_id, task_key, status, updated_at) 
        VALUES (?, ?, 'in_progress', ?) 
        ON CONFLICT(user_id, task_key) DO UPDATE SET status='in_progress', updated_at=excluded.updated_at
    """, (user_id, task_key, now_time))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, 'Клиент', ?, 'proof', 'photo', ?, ?)
    """, (user_id, username, f"Прикрепил скриншот к '{task_title}'", now_time, now_ts))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]])
        await bot.send_message(chat_id=ADMIN_ID, text=f"🔔 <b>Новая заявка на проверку (со скрином)!</b>\nЗадание: <b>{task_title}</b>\nОт: @{username}", reply_markup=admin_kb, parse_mode="HTML")
    except Exception:
        pass
    return {"ok": True}

@app.post("/api/order/create")
async def create_order(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    first_name = data.get("firstName", "Клиент")
    service_title = data.get("serviceTitle", "Товар")
    comment = data.get("comment", "Без комментария")
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO market_orders (user_id, username, first_name, service_key, service_title, comment, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'new', ?)", (user_id, username, first_name, data.get("serviceKey", "custom"), service_title, comment, now_time))
    order_id = cur.lastrowid

    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, ?, ?, 'order', 'shopping_cart', ?, ?)
    """, (user_id, username, first_name, f"Оформил заказ: {service_title}", now_time, now_ts))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать в ЛС", url=f"https://t.me/{username}")]])
        await bot.send_message(chat_id=ADMIN_ID, text=f"🛒 <b>Новый заказ в Маркете!</b>\nУслуга: <b>{service_title}</b>\nКлиент: @{username}\nКомментарий: {comment}", reply_markup=admin_kb, parse_mode="HTML")
    except Exception:
        pass
    return {"ok": True, "orderId": order_id}

@app.post("/api/order/complete")
async def complete_order(request: Request):
    order_id = (await request.json()).get("orderId")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE market_orders SET status = 'completed' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/activity/log")
async def log_activity(request: Request):
    data = await request.json()
    now_time, now_ts = datetime.now().strftime("%H:%M"), int(datetime.now().timestamp() * 1000)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (data.get("userId"), data.get("username", "").replace("@", ""), data.get("user", "Клиент"), data.get("text", ""), data.get("tag", "action"), data.get("icon", "bolt"), now_time, now_ts))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/activity/clear")
async def clear_activity():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM live_feed")
    conn.commit()
    conn.close()
    return {"ok": True}
