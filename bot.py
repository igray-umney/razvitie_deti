import os
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import aiohttp
import uuid
import base64
import psycopg2
from psycopg2.extras import RealDictCursor

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
YOOKASSA_SHOP_ID = os.getenv('YOOKASSA_SHOP_ID')
YOOKASSA_SECRET_KEY = os.getenv('YOOKASSA_SECRET_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
DATABASE_URL = os.getenv('DATABASE_URL')  # PostgreSQL URL от Railway

# Тарифы
TARIFFS = {
    'trial': {'name': 'Пробный период', 'days': 2, 'price': 0, 'old_price': 0},
    '1month': {'name': '1 месяц', 'days': 30, 'price': 190, 'old_price': 380},
    '3months': {'name': '3 месяца', 'days': 90, 'price': 450, 'old_price': 1140},
    '6months': {'name': '6 месяцев', 'days': 180, 'price': 690, 'old_price': 2280},
    'forever': {'name': 'Навсегда', 'days': 36500, 'price': 900, 'old_price': 4560}
}

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# База данных PostgreSQL
def get_db_connection():
    """Создает подключение к PostgreSQL"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Инициализация таблиц в PostgreSQL"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id BIGINT PRIMARY KEY,
                  username TEXT,
                  subscription_until TIMESTAMP,
                  tariff TEXT,
                  created_at TIMESTAMP)''')
    
    cur.execute('''CREATE TABLE IF NOT EXISTS payments
                 (payment_id TEXT PRIMARY KEY,
                  user_id BIGINT,
                  amount REAL,
                  tariff TEXT,
                  status TEXT,
                  yookassa_id TEXT,
                  created_at TIMESTAMP)''')
    
    conn.commit()
    cur.close()
    conn.close()

def add_user(user_id, username, days, tariff):
    """Добавление/обновление пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    subscription_until = datetime.now() + timedelta(days=days)
    created_at = datetime.now()
    
    cur.execute('''INSERT INTO users 
                 (user_id, username, subscription_until, tariff, created_at)
                 VALUES (%s, %s, %s, %s, %s)
                 ON CONFLICT (user_id) 
                 DO UPDATE SET subscription_until = %s, tariff = %s''',
              (user_id, username, subscription_until, tariff, created_at, 
               subscription_until, tariff))
    
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    """Получение данных пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def is_subscription_active(user_id):
    """Проверка активности подписки"""
    user = get_user(user_id)
    if not user:
        return False
    return datetime.now() < user['subscription_until']

def create_payment(user_id, amount, tariff, yookassa_id):
    """Создание записи о платеже"""
    conn = get_db_connection()
    cur = conn.cursor()
    payment_id = f"{user_id}_{int(datetime.now().timestamp())}"
    created_at = datetime.now()
    
    cur.execute('''INSERT INTO payments 
                 (payment_id, user_id, amount, tariff, status, yookassa_id, created_at)
                 VALUES (%s, %s, %s, %s, %s, %s, %s)''',
              (payment_id, user_id, amount, tariff, 'pending', yookassa_id, created_at))
    
    conn.commit()
    cur.close()
    conn.close()
    return payment_id

def update_payment_status(yookassa_id, status):
    """Обновление статуса платежа"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE payments SET status = %s WHERE yookassa_id = %s', 
                (status, yookassa_id))
    conn.commit()
    cur.close()
    conn.close()

def get_payment_by_yookassa_id(yookassa_id):
    """Получение платежа по ID ЮКассы"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM payments WHERE yookassa_id = %s', (yookassa_id,))
    payment = cur.fetchone()
    cur.close()
    conn.close()
    return payment

def get_expired_users():
    """Получение пользователей с истекшей подпиской"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''SELECT user_id, username FROM users 
                   WHERE subscription_until < %s''', (datetime.now(),))
    expired = cur.fetchall()
    cur.close()
    conn.close()
    return expired

def was_notified_recently(user_id):
    """Проверка, было ли уведомление отправлено недавно (за последние 24 часа)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''SELECT last_notified FROM notifications 
                       WHERE user_id = %s''', (user_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        
        if not result:
            return False
        
        last_notified = result['last_notified']
        time_diff = datetime.now() - last_notified
        return time_diff.total_seconds() < 86400  # 24 часа = 86400 секунд
    except Exception as e:
        logging.warning(f"Table notifications doesn't exist yet: {e}")
        return False

def mark_as_notified(user_id):
    """Отметить что пользователь был уведомлен"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''INSERT INTO notifications (user_id, last_notified)
                   VALUES (%s, %s)
                   ON CONFLICT (user_id)
                   DO UPDATE SET last_notified = %s''',
                (user_id, datetime.now(), datetime.now()))
    conn.commit()
    cur.close()
    conn.close()

def get_trial_users_for_funnel():
    """Получение пользователей в пробном периоде для воронки"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Пользователи с пробным доступом
    cur.execute('''SELECT user_id, username, subscription_until, created_at 
                   FROM users 
                   WHERE tariff = %s 
                   AND subscription_until > %s''',
                ('trial', datetime.now()))
    
    trial_users = cur.fetchall()
    cur.close()
    conn.close()
    return trial_users

def get_expired_trial_users():
    """Получение пользователей с истекшим пробным периодом"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''SELECT user_id, username, subscription_until, created_at 
                   FROM users 
                   WHERE tariff = %s 
                   AND subscription_until < %s''',
                ('trial', datetime.now()))
    
    expired_users = cur.fetchall()
    cur.close()
    conn.close()
    return expired_users

def get_funnel_message_sent(user_id, message_type):
    """Проверка, было ли отправлено сообщение воронки"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Создаем таблицу для отслеживания сообщений воронки если её нет
    cur.execute('''CREATE TABLE IF NOT EXISTS funnel_messages
                   (id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    message_type TEXT,
                    sent_at TIMESTAMP,
                    UNIQUE(user_id, message_type))''')
    
    cur.execute('''SELECT sent_at FROM funnel_messages 
                   WHERE user_id = %s AND message_type = %s''',
                (user_id, message_type))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result

def mark_funnel_message_sent(user_id, message_type):
    """Отметить что сообщение воронки отправлено"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''INSERT INTO funnel_messages (user_id, message_type, sent_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, message_type)
                   DO UPDATE SET sent_at = %s''',
                (user_id, message_type, datetime.now(), datetime.now()))
    
    conn.commit()
    cur.close()
    conn.close()

async def sales_funnel():
    """Воронка продаж - автоматические сообщения"""
    while True:
        try:
            logging.info("Running sales funnel check...")
            
            # Получаем пользователей с пробным периодом
            trial_users = get_trial_users_for_funnel()
            
            for user in trial_users:
                user_id = user['user_id']
                created_at = user['created_at']
                subscription_until = user['subscription_until']
                
                hours_since_start = (datetime.now() - created_at).total_seconds() / 3600
                hours_until_end = (subscription_until - datetime.now()).total_seconds() / 3600
                
                try:
                    # ДЕНЬ 0: Приветствие через 5 минут (0.08 часа)
                    if 0.08 <= hours_since_start < 0.5:
                        if not get_funnel_message_sent(user_id, 'welcome'):
                            await bot.send_message(
                                user_id,
                                "🎉 Поздравляем! Вы в клубе \"Развитие для детей\"!\n\n"
                                "Ваш бесплатный доступ активен на 2 дня.\n\n"
                                "📚 Что делать дальше:\n\n"
                                "1️⃣ Изучите материалы в группе\n"
                                "2️⃣ Попробуйте задания с ребенком\n"
                                "3️⃣ Посмотрите результаты уже сегодня!\n\n"
                                "💡 Совет: начните с популярных разделов - там самые любимые материалы!\n\n"
                                "🎥 Видеообзор материалов → https://t.me/instrukcii_baza/32\n\n"
                                "Приятного знакомства! 🌟"
                            )
                            mark_funnel_message_sent(user_id, 'welcome')
                    
                    # ДЕНЬ 1: Утро (18-20 часов)
                    if 18 <= hours_since_start < 22:
                        if not get_funnel_message_sent(user_id, 'day1_morning'):
                            await bot.send_message(
                                user_id,
                                "☀️ Доброе утро!\n\n"
                                "Как вам первые материалы? Уже попробовали что-то с ребенком?\n\n"
                                "👨‍👩‍👧‍👦 Кстати, всего в клубе уже 500+ активных родителей.\n\n"
                                "💬 Что говорят другие:\n\n"
                                "\"За первый день дочка освоила 5 новых слов! Спасибо за игры!\" - Мария\n\n"
                                "\"Сын в восторге от заданий на логику!\" - Андрей\n\n"
                                "📌 У вас остался 1 день пробного доступа.\n\n"
                                "Вопросы? Пишите @razvitie_dety 💬"
                            )
                            mark_funnel_message_sent(user_id, 'day1_morning')
                    
                    # ДЕНЬ 1: Вечер (28-32 часа)
                    if 28 <= hours_since_start < 32:
                        if not get_funnel_message_sent(user_id, 'day1_evening'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="Развивающие игры 🎮", callback_data="survey_games")],
                                [InlineKeyboardButton(text="Творчество 🎨", callback_data="survey_creative")],
                                [InlineKeyboardButton(text="Обучение 📚", callback_data="survey_learning")]
                            ])
                            await bot.send_message(
                                user_id,
                                "🌙 Добрый вечер!\n\n"
                                "Быстрый вопрос: какие материалы понравились больше всего?\n\n"
                                "⏰ Кстати, завтра последний день пробного периода.\n\n"
                                "💡 Успеете попробовать творческие мастер-классы? Дети обожают их!",
                                reply_markup=keyboard
                            )
                            mark_funnel_message_sent(user_id, 'day1_evening')
                    
                    # ДЕНЬ 2: За 8 часов до конца
                    if 6 <= hours_until_end < 10:
                        if not get_funnel_message_sent(user_id, 'day2_8hours'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📋 Выбрать подписку", callback_data="show_tariffs")]
                            ])
                            await bot.send_message(
                                user_id,
                                "⏰ Осталось 8 часов пробного доступа!\n\n"
                                "Мы заметили, что вы активно используете материалы - это здорово! 👏\n\n"
                                "🎯 Специально для вас:\n\n"
                                "Оформите подписку сегодня и получите:\n"
                                "✅ Скидку до 80% (действует только 7 дней)\n"
                                "✅ Бонусную подборку материалов\n\n"
                                "📊 Ваша экономия:\n\n"
                                "1 месяц: 380₽ → 190₽ (экономия 190₽)\n"
                                "3 месяца: 1140₽ → 450₽ (экономия 690₽)\n"
                                "6 месяцев: 2280₽ → 690₽ (экономия 1590₽)\n"
                                "Навсегда: 4560₽ → 900₽ (экономия 3660₽!)\n\n"
                                "P.S. После окончания пробного периода цены вернутся к обычным.",
                                reply_markup=keyboard
                            )
                            mark_funnel_message_sent(user_id, 'day2_8hours')
                    
                    # ДЕНЬ 2: За 2 часа до конца
                    if 1 <= hours_until_end < 3:
                        if not get_funnel_message_sent(user_id, 'day2_2hours'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Продолжить развитие", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/razvitie_dety")]
                            ])
                            await bot.send_message(
                                user_id,
                                "⏰ Осталось 2 часа!\n\n"
                                "Представьте: завтра ваш ребенок спросит: \"Мама/Папа, а где наши игры?\"\n\n"
                                "🎉 Или завтра вы продолжите вместе:\n"
                                "• Развивать речь через игры\n"
                                "• Создавать поделки\n"
                                "• Учиться через творчество\n\n"
                                "🔥 Специальная цена действует еще 5 дней!\n\n"
                                "190₽ = всего 6₽ в день для развития ребенка\n"
                                "☕ Меньше чем чашка кофе!\n\n"
                                "❓ Есть вопросы? Ответим за 5 минут!",
                                reply_markup=keyboard
                            )
                            mark_funnel_message_sent(user_id, 'day2_2hours')
                
                except Exception as e:
                    logging.error(f"Error sending funnel message to {user_id}: {e}")
            
            # Обработка пользователей с истекшим пробным периодом
            expired_users = get_expired_trial_users()
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_until = user['subscription_until']
                hours_since_expired = (datetime.now() - subscription_until).total_seconds() / 3600
                
                try:
                    # Сразу после истечения (0-2 часа)
                    if 0 <= hours_since_expired < 2:
                        if not get_funnel_message_sent(user_id, 'expired_immediate'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Вернуться в клуб", callback_data="show_tariffs")]
                            ])
                            await bot.send_message(
                                user_id,
                                "😔 Ваш пробный доступ истек\n\n"
                                "Надеемся, материалы понравились вам и вашему ребенку.\n\n"
                                "🎁 Хорошая новость:\n\n"
                                "Специально для вас мы сохранили скидку еще на 5 дней!\n\n"
                                "Вернуться можно прямо сейчас:\n"
                                "• 190₽ за месяц (вместо 380₽)\n"
                                "• Или выбрать выгодный тариф на 3-6 месяцев\n\n"
                                "📊 Что вы потеряете без подписки:\n"
                                "❌ 200+ развивающих игр\n"
                                "❌ Еженедельные новинки\n"
                                "❌ Поддержку экспертов\n\n"
                                "P.S. Скидка действует 5 дней.",
                                reply_markup=keyboard
                            )
                            mark_funnel_message_sent(user_id, 'expired_immediate')
                    
                    # ДЕНЬ 3 (через 24 часа после истечения)
                    if 22 <= hours_since_expired < 26:
                        if not get_funnel_message_sent(user_id, 'expired_day3'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📋 Выбрать тариф", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/razvitie_dety")]
                            ])
                            await bot.send_message(
                                user_id,
                                "💬 Посмотрите, что говорят родители:\n\n"
                                "\"Вернулись после пробного периода и не жалеем! Ребенок с нетерпением ждет новых заданий!\" - Елена\n\n"
                                "\"За месяц сын научился считать до 20 и выучил все буквы!\" - Мария\n\n"
                                "А вы все еще думаете? 🤔\n\n"
                                "⏰ Осталось 4 дня специальной цены!\n\n"
                                "💡 Знаете ли вы:\n"
                                "• 87% родителей продлевают подписку\n"
                                "• Родители экономят 2-3 часа в неделю на поиске материалов\n\n"
                                "🎯 3 месяца = всего 5₽ в день!\n\n"
                                "❓ Не уверены? Напишите нам - расскажем подробнее!",
                                reply_markup=keyboard
                            )
                            mark_funnel_message_sent(user_id, 'expired_day3')
                    
                    # ДЕНЬ 5 (через 72 часа)
                    if 70 <= hours_since_expired < 74:
                        if not get_funnel_message_sent(user_id, 'expired_day5'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="Слишком дорого 💰", callback_data="feedback_expensive")],
                                [InlineKeyboardButton(text="Не понравился контент", callback_data="feedback_content")],
                                [InlineKeyboardButton(text="Нужно больше времени ⏰", callback_data="feedback_time")],
                                [InlineKeyboardButton(text="Другое", callback_data="feedback_other")]
                            ])
                            await bot.send_message(
                                user_id,
                                "Можем узнать ваше мнение? 🤔\n\n"
                                "Мы заметили, что вы не продлили подписку после пробного периода.\n\n"
                                "Что вас остановило?\n\n"
                                "💡 За честный ответ - специальный бонус от нас!",
                                reply_markup=keyboard
                            )
                            mark_funnel_message_sent(user_id, 'expired_day5')
                
                except Exception as e:
                    logging.error(f"Error sending expired funnel message to {user_id}: {e}")
            
            # Проверяем каждые 30 минут
            await asyncio.sleep(1800)
            
        except Exception as e:
            logging.error(f"Error in sales funnel: {e}")
            await asyncio.sleep(1800)

async def check_and_remove_expired():
    """Фоновая задача: проверка и удаление пользователей с истекшей подпиской"""
    while True:
        try:
            logging.info("Checking for expired subscriptions...")
            expired_users = get_expired_users()
            
            for user in expired_users:
                user_id = user['user_id']
                username = user['username']
                
                # Проверяем, не уведомляли ли мы пользователя за последние 24 часа
                if was_notified_recently(user_id):
                    logging.info(f"User {user_id} was already notified recently, skipping...")
                    continue
                
                try:
                    # Удаляем пользователя из группы
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    # Сразу разбаниваем чтобы мог вернуться при покупке
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                    
                    logging.info(f"Removed expired user: {username} (ID: {user_id})")
                    
                    # Уведомляем пользователя
                    try:
                        await bot.send_message(
                            user_id,
                            "⏰ Ваша подписка истекла!\n\n"
                            "Продлите доступ чтобы продолжить пользоваться материалами.",
                            reply_markup=get_main_menu()
                        )
                        # Отмечаем что уведомили
                        mark_as_notified(user_id)
                        logging.info(f"Notified user {user_id} about expiration")
                    except Exception as e:
                        logging.error(f"Could not notify user {user_id}: {e}")
                    
                except Exception as e:
                    logging.error(f"Error removing user {user_id}: {e}")
            
            # Проверяем каждый час, но уведомляем только раз в 24 часа
            await asyncio.sleep(3600)
            
        except Exception as e:
            logging.error(f"Error in check_and_remove_expired: {e}")
            await asyncio.sleep(3600)

# ЮKassa API
async def create_yookassa_payment(amount, description, user_id):
    """Создание платежа в ЮKassa"""
    url = "https://api.yookassa.ru/v3/payments"
    
    idempotence_key = str(uuid.uuid4())
    auth_string = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}"
    auth_bytes = auth_string.encode('utf-8')
    auth_b64 = base64.b64encode(auth_bytes).decode('utf-8')
    
    headers = {
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_b64}"
    }
    
    data = {
        "amount": {
            "value": f"{amount:.2f}",
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": f"https://t.me/{(await bot.get_me()).username}"
        },
        "capture": True,
        "description": description,
        "metadata": {
            "user_id": str(user_id)
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data, headers=headers) as response:
            if response.status == 200:
                result = await response.json()
                return result
            else:
                logging.error(f"YooKassa error: {response.status}, {await response.text()}")
                return None

async def check_yookassa_payment(payment_id):
    """Проверка статуса платежа в ЮKassa"""
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    
    auth_string = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}"
    auth_bytes = auth_string.encode('utf-8')
    auth_b64 = base64.b64encode(auth_bytes).decode('utf-8')
    
    headers = {
        "Authorization": f"Basic {auth_b64}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                result = await response.json()
                return result
            else:
                logging.error(f"YooKassa check error: {response.status}")
                return None

# Клавиатуры
def get_main_menu():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Попробовать бесплатно (2 дня)", callback_data="trial")],
        [InlineKeyboardButton(text="📋 Выбрать подписку", callback_data="show_tariffs")],
        [InlineKeyboardButton(text="ℹ️ Мой статус", callback_data="status")],
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")]
    ])
    return keyboard

def get_tariffs_menu():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 1 месяц - 380₽ → 190₽ (скидка 50%)", callback_data="1month")],
        [InlineKeyboardButton(text="📅 3 месяца - 1140₽ → 450₽ (скидка 61%)", callback_data="3months")],
        [InlineKeyboardButton(text="📅 6 месяцев - 2280₽ → 690₽ (скидка 70%)", callback_data="6months")],
        [InlineKeyboardButton(text="♾️ Навсегда - 4560₽ → 900₽ (скидка 80%)", callback_data="forever")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    return keyboard

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = f"""
👋 Привет, {message.from_user.first_name}!

Добро пожаловать в бот закрытой группы с развивающими материалами для детей!

🎁 Попробуй бесплатно 2 дня! После пробного периода выбери удобную подписку и развивайся вместе с нами 👇
"""
    
    await message.answer(welcome_text, reply_markup=get_main_menu())

@dp.callback_query(F.data == "trial")
async def process_trial(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or "unknown"
    
    user = get_user(user_id)
    if user:
        await callback.answer("❌ Вы уже использовали пробный период!", show_alert=True)
        return
    
    add_user(user_id, username, TARIFFS['trial']['days'], 'trial')
    
    try:
        invite_link = await bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1,
            expire_date=datetime.now() + timedelta(days=2)
        )
        
        await callback.message.edit_text(
            f"🎉 Отлично! Ты получил пробный доступ на 2 дня!\n\n"
            f"Переходи по ссылке: {invite_link.invite_link}\n\n"
            f"⏰ Доступ истечет через 2 дня.\n"
            f"После этого выбери подходящий тариф!\n\n"
            f"💡 Это ссылка для присоединения к закрытой группе.",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        logging.error(f"Error adding user to channel: {e}")
        await callback.message.edit_text(
            "❌ Произошла ошибка. Обратитесь к администратору.",
            reply_markup=get_main_menu()
        )
    
    await callback.answer()

@dp.callback_query(F.data.in_(['1month', '3months', '6months', 'forever']))
async def process_tariff(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    tariff_code = callback.data
    tariff = TARIFFS[tariff_code]
    
    await callback.answer("⏳ Создаем платеж...", show_alert=False)
    
    payment = await create_yookassa_payment(
        amount=tariff['price'],
        description=f"Подписка: {tariff['name']}",
        user_id=user_id
    )
    
    if not payment:
        await callback.message.edit_text(
            "❌ Ошибка создания платежа. Попробуйте позже.",
            reply_markup=get_main_menu()
        )
        return
    
    create_payment(user_id, tariff['price'], tariff_code, payment['id'])
    confirmation_url = payment['confirmation']['confirmation_url']
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=confirmation_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_{payment['id']}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    
    await callback.message.edit_text(
        f"📦 Вы выбрали: **{tariff['name']}**\n"
        f"💰 Полная цена: {tariff['old_price']}₽\n"
        f"💳 К оплате: **{tariff['price']}₽**\n\n"
        f"1️⃣ Нажмите 'Оплатить'\n"
        f"2️⃣ Завершите оплату\n"
        f"3️⃣ Вернитесь и нажмите 'Проверить оплату'\n\n"
        f"⚠️ Доступ откроется автоматически после оплаты!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("check_"))
async def check_payment(callback: types.CallbackQuery):
    yookassa_payment_id = callback.data.replace("check_", "")
    await callback.answer("⏳ Проверяем оплату...", show_alert=False)
    
    payment_info = await check_yookassa_payment(yookassa_payment_id)
    
    if not payment_info:
        await callback.answer("❌ Ошибка проверки платежа", show_alert=True)
        return
    
    status = payment_info.get('status')
    
    if status == 'succeeded':
        payment = get_payment_by_yookassa_id(yookassa_payment_id)
        if payment:
            user_id = payment['user_id']
            tariff_code = payment['tariff']
            tariff = TARIFFS[tariff_code]
            username = callback.from_user.username or "unknown"
            
            update_payment_status(yookassa_payment_id, 'completed')
            add_user(user_id, username, tariff['days'], tariff_code)
            
            try:
                if tariff_code == 'forever':
                    invite_link = await bot.create_chat_invite_link(
                        CHANNEL_ID,
                        member_limit=1
                    )
                else:
                    invite_link = await bot.create_chat_invite_link(
                        CHANNEL_ID,
                        member_limit=1,
                        expire_date=datetime.now() + timedelta(days=tariff['days'])
                    )
                
                await callback.message.edit_text(
                    f"✅ **Оплата прошла успешно!**\n\n"
                    f"🎉 Поздравляем! Вы получили доступ.\n"
                    f"📅 Тариф: {tariff['name']}\n\n"
                    f"Переходите в группу: {invite_link.invite_link}",
                    reply_markup=get_main_menu(),
                    parse_mode="Markdown"
                )
                
                if ADMIN_ID:
                    await bot.send_message(
                        ADMIN_ID,
                        f"💰 Новая оплата!\n"
                        f"👤 User: @{username} (ID: {user_id})\n"
                        f"📦 Тариф: {tariff['name']}\n"
                        f"💵 Сумма: {tariff['price']}₽"
                    )
                
            except Exception as e:
                logging.error(f"Error creating invite: {e}")
                await callback.message.edit_text(
                    "✅ Оплата получена!\n"
                    "❌ Ошибка создания приглашения.\n"
                    "Обратитесь к администратору.",
                    reply_markup=get_main_menu()
                )
        
    elif status == 'pending':
        await callback.answer(
            "⏳ Платеж в обработке. Попробуйте проверить через минуту.",
            show_alert=True
        )
    else:
        await callback.answer(
            f"❌ Статус платежа: {status}. Попробуйте снова.",
            show_alert=True
        )

@dp.callback_query(F.data == "status")
async def check_status(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id)
    
    if not user:
        await callback.answer(
            "❌ У вас нет активной подписки. Попробуйте бесплатно!",
            show_alert=True
        )
        return
    
    subscription_until = user['subscription_until']
    is_active = datetime.now() < subscription_until
    
    if is_active:
        days_left = (subscription_until - datetime.now()).days
        tariff_info = TARIFFS.get(user['tariff'], {})
        
        if user['tariff'] == 'forever':
            status_text = (
                f"✅ **Ваша подписка активна!**\n\n"
                f"📅 Тариф: {tariff_info.get('name', 'Неизвестно')}\n"
                f"♾️ Бессрочная подписка"
            )
        else:
            status_text = (
                f"✅ **Ваша подписка активна!**\n\n"
                f"📅 Тариф: {tariff_info.get('name', 'Неизвестно')}\n"
                f"⏰ Осталось дней: {days_left}\n"
                f"📆 Действует до: {subscription_until.strftime('%d.%m.%Y')}"
            )
    else:
        status_text = (
            f"❌ **Подписка истекла**\n\n"
            f"Продлите подписку, чтобы продолжить доступ к материалам!"
        )
    
    await callback.message.edit_text(
        status_text,
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "back")
async def go_back(callback: types.CallbackQuery):
    await callback.message.edit_text(
        f"👋 Привет, {callback.from_user.first_name}!\n\n"
        "Добро пожаловать в бот закрытой группы с развивающими материалами для детей!\n\n"
        "🎁 Попробуй бесплатно 2 дня! После пробного периода выбери удобную подписку и развивайся вместе с нами 👇",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs(callback: types.CallbackQuery):
    """Показать список тарифов"""
    await callback.message.edit_text(
        "📋 **Выберите подходящую подписку:**\n\n"
        "🔥 Специальные цены 7 дней!\n"
        "Обычная цена → Цена со скидкой\n\n"
        "💡 Чем дольше тариф - тем больше экономия!",
        reply_markup=get_tariffs_menu(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq")
async def show_faq(callback: types.CallbackQuery):
    """Показать FAQ"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Как продлить подписку?", callback_data="faq_1")],
        [InlineKeyboardButton(text="2️⃣ Что делать если оплата не прошла?", callback_data="faq_2")],
        [InlineKeyboardButton(text="3️⃣ Как узнать срок окончания подписки?", callback_data="faq_3")],
        [InlineKeyboardButton(text="4️⃣ Можно ли вернуть деньги?", callback_data="faq_4")],
        [InlineKeyboardButton(text="5️⃣ Что входит в подписку?", callback_data="faq_5")],
        [InlineKeyboardButton(text="6️⃣ Как изменить тариф?", callback_data="faq_6")],
        [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    
    await callback.message.edit_text(
        "❓ **Часто задаваемые вопросы**\n\n"
        "Выберите интересующий вас вопрос:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_1")
async def faq_answer_1(callback: types.CallbackQuery):
    """Ответ на вопрос 1"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎥 Видеоинструкция", url="https://t.me/instrukcii_baza/33")],
        [InlineKeyboardButton(text="💳 Продлить сейчас", callback_data="show_tariffs")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**1. Как продлить подписку?**\n\n"
        "🎥 **Смотрите видеоинструкцию** - там всё понятно показано!\n\n"
        "После окончания подписки вы получите уведомление от бота с предложением продлить.\n\n"
        "Также вы можете продлить в любой момент:\n"
        "• Введите /start\n"
        "• Выберите нужный тариф\n"
        "• Оплатите удобным способом\n\n"
        "⚠️ **Важно:** Автоматическое продление отключено. Вам нужно будет продлевать подписку вручную каждый период.\n\n"
        "💡 **Совет:** За 3-7 дней до окончания подписки вы получите напоминание!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_2")
async def faq_answer_2(callback: types.CallbackQuery):
    """Ответ на вопрос 2"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="back")],
        [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**2. Что делать, если оплата не прошла?**\n\n"
        "Оплата может не пройти по следующим причинам:\n\n"
        "💳 **Проблемы с картой:**\n"
        "• Недостаточно средств на счёте\n"
        "• Карта заблокирована или просрочена\n"
        "• Превышен лимит по операциям\n\n"
        "🏦 **Проблемы с банком:**\n"
        "• Банк отклонил транзакцию\n"
        "• Включены ограничения на онлайн-платежи\n"
        "• Требуется подтверждение через SMS\n\n"
        "**Что делать:**\n"
        "1. Проверьте баланс карты\n"
        "2. Убедитесь что карта не заблокирована\n"
        "3. Попробуйте другую карту\n"
        "4. Свяжитесь с банком для уточнения\n\n"
        "Если проблема не решилась - напишите в поддержку @razvitie_dety",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_3")
async def faq_answer_3(callback: types.CallbackQuery):
    """Ответ на вопрос 3"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ℹ️ Проверить мой статус", callback_data="status")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**3. Как узнать срок окончания подписки?**\n\n"
        "Чтобы проверить свою подписку:\n\n"
        "1️⃣ Введите команду /start\n"
        "2️⃣ Нажмите кнопку **'ℹ️ Мой статус'**\n\n"
        "Вы увидите:\n"
        "• Текущий тариф\n"
        "• Дату окончания подписки\n"
        "• Количество оставшихся дней\n\n"
        "📱 Также бот отправит вам уведомление за несколько дней до окончания!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_4")
async def faq_answer_4(callback: types.CallbackQuery):
    """Ответ на вопрос 4"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**4. Можно ли вернуть деньги?**\n\n"
        "🎁 **Пробный период:**\n"
        "Воспользуйтесь бесплатным доступом на 2 дня, чтобы оценить качество материалов перед покупкой!\n\n"
        "💰 **Возврат средств:**\n"
        "Возврат возможен в течение 3 дней после оплаты, если:\n"
        "• Вы не получили доступ к материалам\n"
        "• Возникли технические проблемы\n"
        "• Контент не соответствует описанию\n\n"
        "Для оформления возврата свяжитесь с поддержкой @razvitie_dety\n\n"
        "⚠️ **Обратите внимание:**\n"
        "После использования материалов возврат не предусмотрен согласно законодательству об информационных услугах.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_5")
async def faq_answer_5(callback: types.CallbackQuery):
    """Ответ на вопрос 5"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎥 Видео: Обзор материалов", url="https://t.me/instrukcii_baza/32")],
        [InlineKeyboardButton(text="🎥 Видео: Как пользоваться", url="https://t.me/instrukcii_baza/34")],
        [InlineKeyboardButton(text="🎁 Попробовать бесплатно", callback_data="trial")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**5. Что входит в подписку?**\n\n"
        "🎥 **Смотрите видеообзоры** - наглядно покажем что внутри!\n\n"
        "📚 **Доступ к материалам:**\n"
        "• Развивающие игры и задания\n"
        "• Образовательный контент по возрастам\n"
        "• Творческие мастер-классы\n"
        "• Методические материалы для родителей\n\n"
        "👥 **Закрытая группа:**\n"
        "• Общение с другими родителями\n"
        "• Регулярные обновления контента\n"
        "• Поддержка и советы экспертов\n\n"
        "🎁 **Бонусы:**\n"
        "• Эксклюзивные материалы для подписчиков\n"
        "• Раннее получение новинок\n"
        "• Специальные акции и скидки\n\n"
        "💡 Попробуйте бесплатно 2 дня, чтобы оценить все возможности!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_6")
async def faq_answer_6(callback: types.CallbackQuery):
    """Ответ на вопрос 6"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Посмотреть тарифы", callback_data="back")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**6. Как изменить тариф?**\n\n"
        "📈 **Повышение тарифа:**\n"
        "Вы можете в любой момент перейти на более длительную подписку:\n"
        "• Выберите новый тариф\n"
        "• Оплатите разницу\n"
        "• Доступ продлится с учетом оставшихся дней\n\n"
        "📉 **Понижение тарифа:**\n"
        "• Текущая подписка действует до конца оплаченного периода\n"
        "• После окончания выберите другой тариф\n\n"
        "♾️ **Тариф 'Навсегда':**\n"
        "• Бессрочный доступ без ограничений\n"
        "• Самая выгодная цена\n"
        "• Скидка 80%!\n\n"
        "💡 **Совет:** Длительные тарифы выгоднее - экономия до 80%!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

# Команда /faq
@dp.message(Command("faq"))
async def cmd_faq(message: types.Message):
    """Команда для показа FAQ"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Как продлить подписку?", callback_data="faq_1")],
        [InlineKeyboardButton(text="2️⃣ Что делать если оплата не прошла?", callback_data="faq_2")],
        [InlineKeyboardButton(text="3️⃣ Как узнать срок окончания подписки?", callback_data="faq_3")],
        [InlineKeyboardButton(text="4️⃣ Можно ли вернуть деньги?", callback_data="faq_4")],
        [InlineKeyboardButton(text="5️⃣ Что входит в подписку?", callback_data="faq_5")],
        [InlineKeyboardButton(text="6️⃣ Как изменить тариф?", callback_data="faq_6")],
        [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")]
    ])
    
    await message.answer(
        "❓ **Часто задаваемые вопросы**\n\n"
        "Выберите интересующий вас вопрос:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('SELECT COUNT(*) as count FROM users')
    total_users = cur.fetchone()['count']
    
    cur.execute('SELECT COUNT(*) as count FROM users WHERE subscription_until > %s', 
                (datetime.now(),))
    active_users = cur.fetchone()['count']
    
    cur.execute('SELECT COALESCE(SUM(amount), 0) as total FROM payments WHERE status = %s',
                ('completed',))
    total_revenue = cur.fetchone()['total']
    
    cur.execute('SELECT COUNT(*) as count FROM payments WHERE status = %s', ('pending',))
    pending_payments = cur.fetchone()['count']
    
    cur.close()
    conn.close()
    
    stats_text = f"""
📊 **Статистика бота**

👥 Всего пользователей: {total_users}
✅ Активных подписок: {active_users}
💰 Общий доход: {total_revenue}₽
⏳ Ожидают оплаты: {pending_payments}
"""
    
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("cleardb"))
async def admin_clear_db(message: types.Message):
    """Очистка базы данных (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    # Спрашиваем подтверждение
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, очистить", callback_data="confirm_clear")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_clear")]
    ])
    
    await message.answer(
        "⚠️ **ВНИМАНИЕ!**\n\n"
        "Вы действительно хотите очистить ВСЮ базу данных?\n"
        "Это удалит:\n"
        "• Всех пользователей\n"
        "• Все платежи\n"
        "• Все уведомления\n\n"
        "**Это действие нельзя отменить!**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "confirm_clear")
async def confirm_clear_db(callback: types.CallbackQuery):
    """Подтверждение очистки БД"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Очищаем таблицы (если существуют)
        tables_cleared = []
        
        # Пытаемся очистить notifications
        try:
            cur.execute('DELETE FROM notifications')
            tables_cleared.append('notifications')
        except Exception as e:
            logging.warning(f"Table notifications doesn't exist or error: {e}")
        
        # Очищаем payments
        try:
            cur.execute('DELETE FROM payments')
            tables_cleared.append('payments')
        except Exception as e:
            logging.warning(f"Error clearing payments: {e}")
        
        # Очищаем users
        try:
            cur.execute('DELETE FROM users')
            tables_cleared.append('users')
        except Exception as e:
            logging.warning(f"Error clearing users: {e}")
        
        conn.commit()
        cur.close()
        conn.close()
        
        await callback.message.edit_text(
            "✅ **База данных успешно очищена!**\n\n"
            f"Очищенные таблицы: {', '.join(tables_cleared)}\n\n"
            "Можете начинать тестирование заново! 🚀"
        )
        
        logging.info(f"Database cleared by admin {callback.from_user.id}")
        
    except Exception as e:
        logging.error(f"Error clearing database: {e}")
        await callback.message.edit_text(
            "❌ **Ошибка при очистке базы данных!**\n\n"
            f"Детали: {str(e)}"
        )
    
    await callback.answer()

@dp.callback_query(F.data == "cancel_clear")
async def cancel_clear_db(callback: types.CallbackQuery):
    """Отмена очистки БД"""
    await callback.message.edit_text("✅ Очистка отменена. База данных не изменена.")
    await callback.answer()

@dp.callback_query(F.data.startswith("survey_"))
async def handle_survey(callback: types.CallbackQuery):
    """Обработка ответов на опрос"""
    survey_type = callback.data.replace("survey_", "")
    
    responses = {
        'games': 'Развивающие игры',
        'creative': 'Творчество',
        'learning': 'Обучение'
    }
    
    await callback.answer(
        f"Спасибо! Рады что вам нравится раздел '{responses.get(survey_type)}'!",
        show_alert=True
    )
    
    await callback.message.edit_text(
        f"🎁 Отлично! Вы выбрали: {responses.get(survey_type)}\n\n"
        f"В этом разделе еще много материалов доступно по подписке!\n\n"
        f"💡 Успейте оформить подписку по специальной цене - скидка действует еще несколько дней!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Выбрать подписку", callback_data="show_tariffs")]
        ])
    )

@dp.callback_query(F.data.startswith("feedback_"))
async def handle_feedback(callback: types.CallbackQuery):
    """Обработка обратной связи"""
    feedback_type = callback.data.replace("feedback_", "")
    user_id = callback.from_user.id
    
    if feedback_type == 'expensive':
        # Персональная скидка
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Активировать скидку", callback_data="show_tariffs")]
        ])
        await callback.message.edit_text(
            "Спасибо за честность! 💙\n\n"
            "Специально для вас - персональная скидка 30%:\n\n"
            "1 месяц: 190₽ → 133₽\n"
            "3 месяца: 450₽ → 315₽\n\n"
            "Это предложение только для вас и действует 24 часа!",
            reply_markup=keyboard
        )
        mark_funnel_message_sent(user_id, 'personal_discount')
        
    elif feedback_type == 'time':
        # Дополнительный пробный период
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")]
        ])
        await callback.message.edit_text(
            "Понимаем вас! 🤗\n\n"
            "Напишите нам @razvitie_dety - возможно мы сможем продлить ваш пробный период!\n\n"
            "А еще подготовили для вас:\n"
            "📝 Гайд \"Как получить максимум от материалов\"\n"
            "🎥 Видео-обзор всех разделов\n"
            "💬 Консультацию с экспертом (бесплатно)",
            reply_markup=keyboard
        )
        
    elif feedback_type == 'content':
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Рассказать подробнее", url="https://t.me/razvitie_dety")]
        ])
        await callback.message.edit_text(
            "Нам очень важно ваше мнение! 🙏\n\n"
            "Расскажите, что именно не понравилось?\n"
            "Мы постоянно улучшаем материалы и ваш отзыв поможет нам стать лучше!\n\n"
            "Напишите @razvitie_dety",
            reply_markup=keyboard
        )
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать нам", url="https://t.me/razvitie_dety")]
        ])
        await callback.message.edit_text(
            "Спасибо за ответ! 💙\n\n"
            "Расскажите подробнее о причине - возможно мы сможем помочь!\n\n"
            "Напишите @razvitie_dety",
            reply_markup=keyboard
        )
    
    await callback.answer()

async def main():
    init_db()
    logging.info("Bot started successfully!")
    
    # Запускаем фоновую задачу проверки подписок
    asyncio.create_task(check_and_remove_expired())
    
    # Запускаем воронку продаж
    asyncio.create_task(sales_funnel())
    
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
