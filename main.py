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

# --- Конфигурация ---
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
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0,
            last_active TEXT
        )
    """)
    # Таблица статусов заданий
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            task_key TEXT,
            task_title TEXT,
            status TEXT DEFAULT 'in_progress',
            screenshot_id TEXT,
            reject_reason TEXT,
            updated_at TEXT,
            UNIQUE(user_id, task_key) ON CONFLICT REPLACE
        )
    """)
    # Таблица заказов маркета (товары, донат, пополнения)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            title TEXT,
            comment TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)
    # Единая лайв-лента всех пользователей
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
    conn.commit()
    conn.close()

init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Фоновый опрос Telegram бота без конфликта с сервером
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

# Хелсчек для UptimeRobot и Render (200 OK)
@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok"}

# Ответ бота на команду /start
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

# ================= СЕРВЕРНЫЕ API ЭНДПОИНТЫ =================

# 1. Полная синхронизация профиля + АВТОМИГРАЦИЯ старых данных
@app.post("/api/sync")
async def sync_all(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "user").replace("@", "").lower().strip()
    first_name = data.get("firstName", "Пользователь")
    now_time = datetime.now().strftime("%H:%M")
    now_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cur = conn.cursor()

    # Фиксируем активность юзера
    cur.execute("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_active=excluded.last_active
    """, (user_id, username, first_name, now_full))

    # Добавляем вход в общую лайв-ленту
    cur.execute("""
        INSERT INTO live_feed (user_id, username, action_type, title, created_at)
        VALUES (?, ?, 'login', 'Открыл приложение CS:GO Market', ?)
    """, (user_id, username, now_time))

    # --- АВТОМИГРАЦИЯ СТАРЫХ ДАННЫХ С ТЕЛЕФОНА ---
    # Если на устройстве найдены локальные задания (например, выполненный VPN), сохраняем их в базу
    migrate_tasks = data.get("migrateTasks", {})
    if isinstance(migrate_tasks, dict):
        for t_key, t_val in migrate_tasks.items():
            t_status = t_val if isinstance(t_val, str) else t_val.get("status", "completed")
            cur.execute("""
                INSERT OR IGNORE INTO tasks (user_id, username, task_key, task_title, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, username, t_key, f"Задание: {t_key}", t_status, now_time))

    # Миграция старых локальных заказов
    migrate_orders = data.get("migrateOrders", [])
    if isinstance(migrate_orders, list):
        for o in migrate_orders:
            o_title = o.get("title") or o.get("name") or "Товар"
            cur.execute("""
                INSERT OR IGNORE INTO market_orders (user_id, username, title, comment, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
            """, (user_id, username, o_title, o.get("comment", ""), now_time))

    conn.commit()

    # --- СЧИТЫВАЕМ АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ЕДИНОЙ БАЗЫ ---
    # Все задания юзера
    cur.execute("SELECT task_key, status, reject_reason FROM tasks WHERE user_id = ?", (user_id,))
    tasks = {row["task_key"]: {"status": row["status"], "reason": row["reject_reason"]} for row in cur.fetchall()}

    # Все активные заказы Маркета для админки
    cur.execute("SELECT id, user_id, username, title, comment, status, created_at FROM market_orders ORDER BY id DESC")
    orders = [dict(r) for r in cur.fetchall()]

    # Последние 50 событий единой лайв-ленты
    cur.execute("SELECT id, user_id, username, action_type, title, created_at FROM live_feed ORDER BY id DESC LIMIT 50")
    feed = [dict(r) for r in cur.fetchall()]

    conn.close()
    return {"ok": True, "tasks": tasks, "orders": orders, "liveFeed": feed}

# 2. Действия с заданиями (старт / выполнение)
@app.post("/api/task/action")
async def task_action(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    task_key = data.get("taskKey")
    task_title = data.get("taskTitle", "Задание")
    status = data.get("status", "in_progress")
    now_time = datetime.now().strftime("%H:%M")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (user_id, username, task_key, task_title, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, task_key) DO UPDATE SET
            status=excluded.status,
            updated_at=excluded.updated_at
    """, (user_id, username, task_key, task_title, status, now_time))

    action_label = "Начал задание" if status == "in_progress" else "Выполнил задание"
    cur.execute("""
        INSERT INTO live_feed (user_id, username, action_type, title, created_at)
        VALUES (?, ?, 'task', ?, ?)
    """, (user_id, username, f"{action_label}: {task_title}", now_time))

    conn.commit()
    conn.close()
    return {"ok": True}

# 3. Сдача задания / отправка скриншота на проверку
@app.post("/api/task/submit")
async def submit_task(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    task_key = data.get("taskKey")
    task_title = data.get("taskTitle", "Задание")
    img_base64 = data.get("screenshot")
    now_time = datetime.now().strftime("%H:%M")

    photo_file_id = None
    if img_base64:
        try:
            header, encoded = img_base64.split(",", 1) if "," in img_base64 else ("", img_base64)
            image_bytes = base64.b64decode(encoded)
            file_payload = BufferedInputFile(image_bytes, filename="proof.jpg")
            caption = f"📸 <b>Скриншот на проверку!</b>\n👤 @{username} (ID: <code>{user_id}</code>)\n🎯 <b>{task_title}</b>"
            msg = await bot.send_photo(chat_id=CHANNEL_STORAGE_ID, photo=file_payload, caption=caption, parse_mode="HTML")
            photo_file_id = msg.photo[-1].file_id
        except Exception as e:
            print("Ошибка отправки скриншота в канал:", e)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (user_id, username, task_key, task_title, status, screenshot_id, updated_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(user_id, task_key) DO UPDATE SET
            status='pending',
            screenshot_id=excluded.screenshot_id,
            updated_at=excluded.updated_at
    """, (user_id, username, task_key, task_title, photo_file_id, now_time))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, action_type, title, created_at)
        VALUES (?, ?, 'task', ?, ?)
    """, (user_id, username, f"Отправил на проверку: {task_title}", now_time))

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

# 4. Создание заказа в Маркете
@app.post("/api/order/create")
async def create_order(request: Request):
    data = await request.json()
    user_id = data.get("userId")
    username = (data.get("username") or "client").replace("@", "")
    title = data.get("title", "Товар")
    comment = data.get("comment", "Без комментария")
    now_time = datetime.now().strftime("%H:%M")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO market_orders (user_id, username, title, comment, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (user_id, username, title, comment, now_time))

    cur.execute("""
        INSERT INTO live_feed (user_id, username, action_type, title, created_at)
        VALUES (?, ?, 'order', ?, ?)
    """, (user_id, username, f"Оформил заказ в Маркете: «{title}»", now_time))
    conn.commit()
    conn.close()

    try:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать в ЛС", url=f"https://t.me/{username}")]
        ])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🛒 <b>Новый заказ в Маркете!</b>\nТовар: <b>{title}</b>\nКлиент: @{username}\nКомментарий: {comment}",
            reply_markup=admin_kb,
            parse_mode="HTML"
        )
    except Exception:
        pass

    return {"ok": True}

# 5. Обработка заказа админом (кнопка «Выполнено»)
@app.post("/api/admin/order/complete")
async def admin_complete_order(request: Request):
    data = await request.json()
    order_id = data.get("orderId")
    now_time = datetime.now().strftime("%H:%M")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, title FROM market_orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE market_orders SET status = 'completed' WHERE id = ?", (order_id,))
        cur.execute("""
            INSERT INTO live_feed (user_id, username, action_type, title, created_at)
            VALUES (?, ?, 'admin', ?, ?)
        """, (row["user_id"], row["username"], f"Заказ выполнен: «{row['title']}»", now_time))
        conn.commit()
    conn.close()
    return {"ok": True}

# 6. Проверка реферала (проверяет реальное присутствие в базе)
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
