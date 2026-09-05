import os
import json
import base64
import sqlite3
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BufferedInputFile

# --- Конфигурация из переменных окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "707417409"))
CHANNEL_STORAGE_ID = int(os.getenv("CHANNEL_STORAGE_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-username.github.io/cs-market-app/")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Локальная база данных SQLite ---
def init_db():
    conn = sqlite3.connect("market_bot.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            referred_by INTEGER DEFAULT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id INTEGER,
            referee_username TEXT,
            earned_bonus REAL DEFAULT 0,
            PRIMARY KEY (referrer_id, referee_username)
        )
    """)
    conn.commit()
    conn.close()

init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # При старте запускаем вебхук или фоновое чтение
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# Разрешаем запросы из Mini App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Стартовое сообщение бота с кнопкой запуска Mini App
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    conn = sqlite3.connect("market_bot.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (user.id, (user.username or "").lower(), user.first_name)
    )
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔥 Открыть CS:GO Market", url="https://t.me/market_02_bot/app")]
])
    ])
    await message.answer(
        f"Привет, {user.first_name}! Добро пожаловать в CS:GO Market.\n\n"
        f"Здесь вы можете приобрести скины, донат, игры или забрать бесплатные скины за простые задания!",
        reply_markup=kb
    )

# Приём скриншотов и отправка в закрытый канал
@app.post("/api/upload_proof")
async def upload_proof(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = data.get("username", "client")
    task_title = data.get("taskTitle", "Задание")
    img_base64 = data.get("screenshot")

    if not img_base64:
        return {"ok": False, "error": "No image"}

    # Преобразуем base64 в бинарный файл
    header, encoded = img_base64.split(",", 1) if "," in img_base64 else ("", img_base64)
    image_bytes = base64.b64decode(encoded)
    file_payload = BufferedInputFile(image_bytes, filename="proof.jpg")

    caption = (
        f"📸 <b>Новый скриншот на проверку!</b>\n\n"
        f"👤 Клиент: @{username} (ID: <code>{user_id}</code>)\n"
        f"🎯 Задание: <b>{task_title}</b>"
    )

    # Отправляем фото в закрытый канал-хранилище
    msg = await bot.send_photo(chat_id=CHANNEL_STORAGE_ID, photo=file_payload, caption=caption, parse_mode="HTML")
    photo_file_id = msg.photo[-1].file_id

    # Присылаем уведомление админу в ЛС
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{username}")]
    ])
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🔔 <b>Новая заявка на проверку!</b>\nЗадание: <b>{task_title}</b> от @{username}\nСкриншот загружен в канал архива.",
        reply_markup=admin_kb,
        parse_mode="HTML"
    )

    return {"ok": True, "fileId": photo_file_id}

# Проверка реферала (заходил ли друг в бота)
@app.post("/api/check_referral")
async def check_referral(request: Request):
    data = await request.json()
    ref_username = (data.get("username") or "").replace("@", "").lower().strip()

    conn = sqlite3.connect("market_bot.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, first_name FROM users WHERE username = ?", (ref_username,))
    row = cur.fetchone()
    conn.close()

    if row:
        return {"exists": True, "userId": row[0], "name": row[1]}
    return {"exists": False}
