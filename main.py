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
            balance REAL DEFAULT 0,
            last_active TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            task_key TEXT,
            task_title TEXT,
            status TEXT DEFAULT 'pending',
            screenshot_id TEXT,
            reject_reason TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS live_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action_type TEXT,
            title TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    polling_task = asyncio.create_task(dp.start_polling(bot))
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

@app.get("/")
async def root():
    return {"status": "ok"}

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_active=excluded.last_active
    """, (user.id, (user.username or "").lower(), user.first_name, now))
    conn.commit()
    conn.close()

    clear_msg = await message.answer("...", reply_markup=types.ReplyKeyboardRemove())
    await clear_msg.delete()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Открыть CS:GO Market", url="https://t.me/market_02_bot/app")]
    ])
    await message.answer(
        f"Привет, {user.first_name}! Добро пожаловать в CS:GO Market.\n\n"
        f"Здесь вы можете приобрести скины, донат, игры или забрать бесплатные скины за простые задания!",
        reply_markup=kb
    )

# 1. Синхронизация профиля при открытии Mini App с любого устройства
@app.post("/api/user/sync")
async def sync_user(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "user").replace("@", "").lower().strip()
    first_name = data.get("firstName", "Клиент")
    now = datetime.now().strftime("%H:%M")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_active=excluded.last_active
    """, (user_id, username, first_name, now))

    # Запись события входа в общую лайв-ленту
    cur.execute("""
        INSERT INTO live_feed (user_id, username, action_type, title, created_at)
        VALUES (?, ?, 'login', 'Вход в приложение', ?)
    """, (user_id, username, now))

    # Получаем все задачи пользователя
    cur.execute("SELECT task_key, status, reject_reason FROM tasks WHERE user_id = ?", (user_id,))
    user_tasks = {row["task_key"]: {"status": row["status"], "reason": row["reject_reason"]} for row in cur.fetchall()}

    # Получаем настройки ссылок
    cur.execute("SELECT key, value FROM settings")
    settings = {row["key"]: row["value"] for row in cur.fetchall()}

    conn.commit()
    conn.close()

    return {"ok": True, "tasks": user_tasks, "settings": settings}

# 2. Отправка задания / скрина (без закрытия WebApp)
@app.post("/api/task/submit")
async def submit_task(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    task_key = data.get("taskKey")
    task_title = data.get("taskTitle", "Задание")
    img_base64 = data.get("screenshot")
    now = datetime.now().strftime("%H:%M")

    photo_file_id = None
    if img_base64:
        header, encoded = img_base64.split(",", 1) if "," in img_base64 else ("", img_base64)
        image_bytes = base64.b64decode(encoded)
        file_payload = BufferedInputFile(image_bytes, filename="proof.jpg")
        caption = f"📸 <b>Скриншот на проверку!</b>\n👤 @{username} (ID: <code>{user_id}</code>)\n🎯 <b>{task_title}</b>"
        msg = await bot.send_photo(chat_id=CHANNEL_STORAGE_ID, photo=file_payload, caption=caption, parse_mode="HTML")
        photo_file_id = msg.photo[-1].file_id

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (user_id, username, task_key, task_title, status, screenshot_id, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
    """, (user_id, username, task_key, task_title, photo_file_id, now))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, action_type, title, created_at)
        VALUES (?, ?, 'task', ?, ?)
    """, (user_id, username, f"Сдал задание: {task_title}", now))
    conn.commit()
    conn.close()

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать", url=f"https://t.me/{username}")]
    ])
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🔔 <b>Новая заявка на проверку!</b>\nЗадание: <b>{task_title}</b>\nОт: @{username}",
        reply_markup=admin_kb,
        parse_mode="HTML"
    )

    return {"ok": True}

# 3. Получение общей лайв-ленты для админки
@app.get("/api/admin/live_feed")
async def get_live_feed():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, action_type, title, created_at FROM live_feed ORDER BY id DESC LIMIT 40")
    rows = cur.fetchall()
    conn.close()
    return [{"userId": r["user_id"], "username": r["username"], "type": r["action_type"], "title": r["title"], "time": r["created_at"]} for r in rows]

# 4. Проверка реферала
@app.post("/api/check_referral")
async def check_referral(request: Request):
    data = await request.json()
    ref_username = (data.get("username") or "").replace("@", "").lower().strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, first_name FROM users WHERE username = ?", (ref_username,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"exists": True, "userId": row["user_id"], "name": row["first_name"]}
    return {"exists": False}
