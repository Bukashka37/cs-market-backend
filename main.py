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
ADMIN_ID = int(os.getenv("ADMIN_ID", "707417409"))
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

    # Очистка дубликатов заказов, если они накопились
    cur.execute("""
        DELETE FROM market_orders 
        WHERE id NOT IN (
            SELECT MIN(id) FROM market_orders GROUP BY user_id, service_key, comment, status
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

# Фиксация пользователей при команде /start
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

# ГЛАВНЫЙ СИНХРОНИЗАТОР
@app.post("/api/sync")
async def sync_all(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "user").replace("@", "").lower().strip()
    first_name = data.get("firstName", "Пользователь")
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()

    # 1. Запись пользователя в базу
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_active=excluded.last_active
    """, (user_id, username, first_name, now_time))

    # 2. Автоматическая запись входа в лайв-ленту (не чаще 1 раза в 15 минут)
    cur.execute("SELECT timestamp FROM live_feed WHERE user_id = ? AND tag = 'login' ORDER BY id DESC LIMIT 1", (user_id,))
    last_log = cur.fetchone()
    if not last_log or (now_ts - last_log["timestamp"]) > 900000:
        cur.execute("""
            INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
            VALUES (?, ?, ?, 'Открыл приложение CS:GO Market', 'login', 'login', ?, ?)
        """, (user_id, username, first_name, now_time, now_ts))

    # 3. Получение заданий пользователя
    cur.execute("SELECT task_key, status, reason, idea_title, idea_desc, reward_requested FROM tasks WHERE user_id = ?", (user_id,))
    tasks_db = {}
    for r in cur.fetchall():
        tasks_db[r["task_key"]] = {
            "status": r["status"],
            "reason": r["reason"],
            "ideaTitle": r["idea_title"],
            "ideaDesc": r["idea_desc"],
            "rewardRequested": bool(r["reward_requested"])
        }

    # 4. Получение рефералов
    cur.execute("SELECT friend_username, friend_name, earned, created_at FROM referrals WHERE user_id = ?", (user_id,))
    refs_db = [{"username": f"@{r['friend_username']}", "tasksCount": 0, "earned": r["earned"], "date": r["created_at"]} for r in cur.fetchall()]

    # 5. Получение заказов для админки
    cur.execute("SELECT id, user_id, username, first_name, service_key, service_title, comment, status, created_at FROM market_orders WHERE status = 'new' ORDER BY id DESC LIMIT 50")
    orders_db = []
    for r in cur.fetchall():
        orders_db.append({
            "id": r["id"],
            "user": r["first_name"],
            "userTag": f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}",
            "serviceKey": r["service_key"],
            "serviceTitle": r["service_title"],
            "comment": r["comment"],
            "status": r["status"],
            "time": r["created_at"]
        })

    # 6. Получение единой лайв-ленты всех пользователей
    cur.execute("SELECT user_name, username, user_id, text, tag, icon, created_at, timestamp FROM live_feed ORDER BY id DESC LIMIT 40")
    feed_db = []
    for r in cur.fetchall():
        feed_db.append({
            "user": r["user_name"],
            "userTag": f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}",
            "text": r["text"],
            "tag": r["tag"],
            "icon": r["icon"],
            "time": r["created_at"],
            "timestamp": r["timestamp"]
        })

    cur.execute("SELECT value FROM app_settings WHERE key = 'ozon_card'")
    ozon_row = cur.fetchone()
    ozon_data = json.loads(ozon_row["value"]) if ozon_row else None

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "tasksState": tasks_db,
        "marketOrders": orders_db,
        "liveFeed": feed_db,
        "referrals": refs_db,
        "ozonCard": ozon_data
    }

# Запрос ссылки Ozon (НЕ переводит задание на проверку!)
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
    cur.execute("SELECT value FROM app_settings WHERE key = 'ozon_card'")
    row = cur.fetchone()
    current_ozon = json.loads(row["value"]) if row and row["value"] else {}
    current_ozon["status"] = "requested"
    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES ('ozon_card', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (json.dumps(current_ozon),))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, ?, 'Запросил персональную ссылку Ozon (72ч)', 'request', 'shopping_basket', ?, ?)
    """, (user_id, username, first_name, now_time, now_ts))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⏳ <b>Запрос ссылки Ozon Карты!</b>\nКлиент: @{username} (ID: <code>{user_id}</code>)\nПерейдите в Админку ➔ Выдача данных и выдайте ссылку.",
            reply_markup=admin_kb,
            parse_mode="HTML"
        )
    except Exception:
        pass

    return {"ok": True}

# Проверка реферала (поиск в SQLite без учёта регистра)
@app.post("/api/check_referral")
async def check_referral(request: Request):
    data = await request.json()
    ref_username = (data.get("username") or "").replace("@", "").lower().strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, first_name, username FROM users WHERE LOWER(username) = ?", (ref_username,))
    row = cur.fetchone()
    conn.close()

    if row:
        return {"exists": True, "userId": row["user_id"], "name": row["first_name"]}
    return {"exists": False}

# Добавление друга в рефералы
@app.post("/api/referral/add")
async def add_referral(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    friend_username = (data.get("friendUsername") or "").replace("@", "").lower().strip()
    friend_name = data.get("friendName", friend_username)
    now_date = datetime.now().strftime("%d.%m.%Y")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO referrals (user_id, friend_username, friend_name, earned, created_at)
        VALUES (?, ?, ?, 0, ?)
    """, (user_id, friend_username, friend_name, now_date))
    conn.commit()
    conn.close()
    return {"ok": True}

# Обновление статуса задания
@app.post("/api/task/update")
async def update_task(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    task_key = data.get("taskKey")
    status = data.get("status", "idle")
    reason = data.get("reason", "")
    idea_title = data.get("ideaTitle", "")
    idea_desc = data.get("ideaDesc", "")
    reward_requested = 1 if data.get("rewardRequested") else 0
    now_time = datetime.now().strftime("%H:%M")

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
    conn.commit()
    conn.close()
    return {"ok": True}

# Отправка скриншота
@app.post("/api/task/submit_proof")
async def submit_proof(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    task_key = data.get("taskKey")
    task_title = data.get("taskTitle", "Задание")
    img_base64 = data.get("screenshot")
    now_time = datetime.now().strftime("%H:%M")

    if img_base64:
        try:
            header, encoded = img_base64.split(",", 1) if "," in img_base64 else ("", img_base64)
            image_bytes = base64.b64decode(encoded)
            file_payload = BufferedInputFile(image_bytes, filename="proof.jpg")
            caption = f"📸 <b>Скриншот задания!</b>\n👤 @{username} (ID: <code>{user_id}</code>)\n🎯 <b>{task_title}</b>"
            await bot.send_photo(chat_id=CHANNEL_STORAGE_ID, photo=file_payload, caption=caption, parse_mode="HTML")
        except Exception as e:
            print("Ошибка отправки скриншота:", e)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (user_id, task_key, status, updated_at)
        VALUES (?, ?, 'in_progress', ?)
        ON CONFLICT(user_id, task_key) DO UPDATE SET
            status='in_progress',
            updated_at=excluded.updated_at
    """, (user_id, task_key, now_time))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 <b>Новая заявка на проверку!</b>\nЗадание: <b>{task_title}</b>\nОт: @{username}",
            reply_markup=admin_kb,
            parse_mode="HTML"
        )
    except Exception:
        pass

    return {"ok": True}

# Создание заказа маркета
@app.post("/api/order/create")
async def create_order(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    first_name = data.get("firstName", "Клиент")
    service_key = data.get("serviceKey", "custom")
    service_title = data.get("serviceTitle", "Товар")
    comment = data.get("comment", "Без комментария")
    now_time = datetime.now().strftime("%H:%M")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_orders (user_id, username, first_name, service_key, service_title, comment, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'new', ?)
    """, (user_id, username, first_name, service_key, service_title, comment, now_time))
    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать в ЛС", url=f"https://t.me/{username}")]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🛒 <b>Новый заказ в Маркете!</b>\nУслуга: <b>{service_title}</b>\nКлиент: @{username}\nКомментарий: {comment}",
            reply_markup=admin_kb,
            parse_mode="HTML"
        )
    except Exception:
        pass

    return {"ok": True, "orderId": order_id}

# Завершение заказа админом
@app.post("/api/order/complete")
async def complete_order(request: Request):
    data = await request.json()
    order_id = data.get("orderId")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE market_orders SET status = 'completed' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# Выдача / обновление ссылки Ozon админом
@app.post("/api/ozon/update")
async def update_ozon(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO app_settings (key, value) VALUES ('ozon_card', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (json.dumps(data),))
    conn.commit()
    conn.close()
    return {"ok": True}

# Логирование активности
@app.post("/api/activity/log")
async def log_activity(request: Request):
    data = await request.json()
    now_time = datetime.now().strftime("%H:%M")
    now_ts = int(datetime.now().timestamp() * 1000)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO live_feed (user_id, username, user_name, text, tag, icon, created_at, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (data.get("userId"), data.get("username", "").replace("@", ""), data.get("user", "Клиент"), data.get("text", ""), data.get("tag", "action"), data.get("icon", "bolt"), now_time, now_ts))
    conn.commit()
    conn.close()
    return {"ok": True}
