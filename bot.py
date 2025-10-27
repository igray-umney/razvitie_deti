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

from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

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
    'trial': {'name': '7 дней бесплатно', 'days': 7, 'price': 0},
    '1month': {'name': '1 месяц', 'days': 30, 'price': 99, 'old_price': 299},
    '3months': {'name': '3 месяца', 'days': 90, 'price': 249, 'old_price': 897},
    '6months': {'name': '6 месяцев', 'days': 180, 'price': 399, 'old_price': 1794},
    'forever': {'name': 'Навсегда', 'days': 36500, 'price': 599, 'old_price': 2990}
}

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Импорт системы обратной связи
import feedback_broadcast

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
    
    # Добавляем недостающие таблицы
    cur.execute('''CREATE TABLE IF NOT EXISTS notifications
                 (user_id BIGINT PRIMARY KEY,
                  last_notified TIMESTAMP)''')
    
    cur.execute('''CREATE TABLE IF NOT EXISTS funnel_messages
                 (id SERIAL PRIMARY KEY,
                  user_id BIGINT,
                  message_type TEXT,
                  sent_at TIMESTAMP,
                  UNIQUE(user_id, message_type))''')

    cur.execute('''CREATE TABLE IF NOT EXISTS welcome_messages
                 (user_id BIGINT PRIMARY KEY,
                  sent_at TIMESTAMP,
                  opened BOOLEAN DEFAULT FALSE)''')
    
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

def get_expired_users_for_funnel():
    """Получение пользователей с ИСТЕКШЕЙ подпиской для воронки expired_day3 и expired_day5"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''SELECT user_id, username, subscription_until, created_at 
                   FROM users 
                   WHERE tariff = %s 
                   AND subscription_until < %s''',  # 👈 Ищем тех у кого ИСТЕКЛА подписка
                ('trial', datetime.now()))
    
    expired_users = cur.fetchall()
    cur.close()
    conn.close()
    return expired_users

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

# Добавьте эту функцию ПЕРЕД async def sales_funnel():
async def send_safe_funnel_message(user_id, text, reply_markup=None):
    """Безопасная отправка сообщений воронки с обработкой блокировки"""
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
        return True
    except Exception as e:
        if "bot was blocked by the user" in str(e) or "Forbidden" in str(e):
            logging.info(f"User {user_id} blocked the bot, skipping")
            return False
        else:
            logging.error(f"Error sending message to {user_id}: {e}")
            return False

# ОБНОВЛЕННАЯ ВОРОНКА ДЛЯ 7-ДНЕВНОГО TRIAL ПЕРИОДА
# Заменить функцию sales_funnel() в bot.py (примерно строки 240-496)

async def sales_funnel():
    """Фоновая задача: автоматическая отправка сообщений воронки продаж"""
    logging.info("Sales funnel started!")
    
    while True:
        try:
            await asyncio.sleep(1800)  # Проверка каждые 30 минут
            
            # Получаем активных пользователей с пробным периодом
            trial_users = get_active_trial_users()
            
            for user in trial_users:
                user_id = user['user_id']
                created_at = user['created_at']
                subscription_until = user['subscription_until']
                
                # Сколько часов прошло с регистрации
                hours_since_registration = (datetime.now() - created_at).total_seconds() / 3600
                # Сколько часов осталось до конца trial
                hours_until_end = (subscription_until - datetime.now()).total_seconds() / 3600
                
                try:
                    # ========== ДЕНЬ 1 (20-28 часов после регистрации) ==========
                    if 20 <= hours_since_registration < 28:
                        if not get_funnel_message_sent(user_id, 'day1'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🎮 Игры и задания", callback_data="survey_games")],
                                [InlineKeyboardButton(text="🎨 Творчество", callback_data="survey_creative")],
                                [InlineKeyboardButton(text="📚 Обучение", callback_data="survey_learning")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "Привет! 👋\n\n"
                                "Мы рады, что вы с нами! Уже попробовали наши материалы?\n\n"
                                "💡 Совет: начните с раздела игр - там самые популярные активности!\n\n"
                                "🎯 У вас еще 6 дней чтобы:\n"
                                "• Протестировать все материалы\n"
                                "• Увидеть прогресс ребенка\n"
                                "• Понять что вам нужно\n\n"
                                "❓ Что интересует больше всего?",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day1')
                                logging.info(f"Sent day1 message to user {user_id}")
                    
                    # ========== ДЕНЬ 3 (68-76 часов) ==========
                    if 68 <= hours_since_registration < 76:
                        if not get_funnel_message_sent(user_id, 'day3'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📋 Смотреть тарифы", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/razvitie_dety")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "Уже 3 дня вместе! 🎉\n\n"
                                "Надеемся, материалы вам нравятся!\n\n"
                                "📊 Интересный факт:\n"
                                "Родители которые занимаются с детьми по нашим материалам всего 15-20 минут в день, "
                                "замечают результаты уже через неделю!\n\n"
                                "✨ Осталось 4 дня пробного периода\n\n"
                                "🎁 Специальная цена действует только для пробников:\n"
                                "• 1 месяц - всего 99₽ (вместо 299₽)\n"
                                "• 3 месяца - 249₽ (самый популярный!)\n"
                                "• Навсегда - 599₽ (одним платежом)\n\n"
                                "💡 Совет: большинство выбирают тариф на 3 месяца - "
                                "как раз чтобы пройти полный курс развития!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day3')
                                logging.info(f"Sent day3 message to user {user_id}")
                    
                    # ========== ДЕНЬ 5 (116-124 часа) ==========
                    if 116 <= hours_since_registration < 124:
                        if not get_funnel_message_sent(user_id, 'day5'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="❓ Вопросы", callback_data="faq")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "💬 Что говорят другие родители:\n\n"
                                "\"Дочка за неделю выучила 10 букв! Спасибо за материалы!\" - Анна\n\n"
                                "\"Сын теперь сам просит позаниматься. Это невероятно!\" - Олег\n\n"
                                "\"Лучшая инвестиция в развитие ребенка!\" - Елена\n\n"
                                "📊 Наши результаты:\n"
                                "• 87% родителей продлевают подписку\n"
                                "• 1000+ довольных семей\n"
                                "• 5000+ развивающих материалов\n\n"
                                "⏰ Осталось 2 дня пробного периода!\n\n"
                                "🎁 Успейте оформить со скидкой 50-70%\n\n"
                                "🛡️ Гарантия: не понравится - вернём деньги в течение 14 дней!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day5')
                                logging.info(f"Sent day5 message to user {user_id}")
                    
                    # ========== ДЕНЬ 7 - ЗА 8 ЧАСОВ ДО КОНЦА (160-164 часа от начала) ==========
                    if 8 <= hours_until_end < 12:
                        if not get_funnel_message_sent(user_id, 'day7_8hours'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🔥 Продолжить со скидкой", callback_data="show_tariffs")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "⏰ Осталось меньше 8 часов!\n\n"
                                "Завтра доступ к материалам закроется...\n\n"
                                "🎁 Но у вас еще есть время оформить подписку со СКИДКОЙ:\n\n"
                                "💰 Специальные цены (только для пробников):\n"
                                "1 месяц: 299₽ → 99₽ (экономия 200₽)\n"
                                "3 месяца: 897₽ → 249₽ (экономия 648₽!) 🔥\n"
                                "6 месяцев: 1794₽ → 399₽ (экономия 1395₽)\n"
                                "Навсегда: 2990₽ → 599₽ (экономия 2391₽!)\n\n"
                                "⚠️ После окончания пробного периода эти цены исчезнут!\n\n"
                                "P.S. Не теряйте то, что уже начали строить вместе с ребенком 💚",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day7_8hours')
                                logging.info(f"Sent day7_8hours message to user {user_id}")
                    
                    # ========== ДЕНЬ 7 - ЗА 2 ЧАСА ДО КОНЦА (166-170 часов от начала) ==========
                    if 1 <= hours_until_end < 3:
                        if not get_funnel_message_sent(user_id, 'day7_2hours'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Продолжить сейчас!", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Вопросы", url="https://t.me/razvitie_dety")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "🚨 ПОСЛЕДНИЕ 2 ЧАСА!\n\n"
                                "Представьте: завтра ваш ребенок спросит:\n"
                                "\"Мама/Папа, а где наши игры?\"\n\n"
                                "🎉 Или завтра вы продолжите вместе:\n"
                                "• Развивать речь через игры\n"
                                "• Создавать поделки\n"
                                "• Учиться через творчество\n\n"
                                "💰 99₽ в месяц = всего 3₽ в день\n"
                                "☕ Меньше чем чашка кофе!\n\n"
                                "🔥 Скидка 50-70% действует только до конца пробного периода!\n\n"
                                "⏰ Не упустите момент - осталось меньше 2 часов!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day7_2hours')
                                logging.info(f"Sent day7_2hours message to user {user_id}")
                
                except Exception as e:
                    logging.error(f"Error sending funnel message to {user_id}: {e}")
            
            # ========== ОБРАБОТКА ИСТЕКШИХ ПОЛЬЗОВАТЕЛЕЙ ==========
            expired_users = get_expired_trial_users()
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_until = user['subscription_until']
                hours_since_expired = (datetime.now() - subscription_until).total_seconds() / 3600
                
                try:
                    # ========== СРАЗУ ПОСЛЕ ИСТЕЧЕНИЯ (0-2 часа) ==========
                    if 0 <= hours_since_expired < 2:
                        if not get_funnel_message_sent(user_id, 'expired_immediate'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Вернуться в клуб", callback_data="show_tariffs")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "😔 Ваш пробный доступ истек\n\n"
                                "Надеемся, материалы понравились вам и вашему ребенку.\n\n"
                                "🎁 Хорошая новость:\n\n"
                                "Специально для вас мы сохранили скидку еще на 7 дней!\n\n"
                                "Вернуться можно прямо сейчас:\n"
                                "• 99₽ за месяц (вместо 299₽)\n"
                                "• 249₽ за 3 месяца (вместо 897₽) 🔥\n"
                                "• Или выбрать тариф на 6 месяцев/навсегда\n\n"
                                "📊 Что вы потеряете без подписки:\n"
                                "❌ 5000+ развивающих материалов\n"
                                "❌ Еженедельные новинки\n"
                                "❌ Поддержку и советы\n\n"
                                "P.S. Скидка действует 7 дней, потом цены вернутся к обычным.",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'expired_immediate')
                                logging.info(f"Sent expired_immediate message to user {user_id}")
                    
                    # ========== ДЕНЬ 2 ПОСЛЕ ИСТЕЧЕНИЯ (46-50 часов) ==========
                    if 46 <= hours_since_expired < 50:
                        if not get_funnel_message_sent(user_id, 'expired_day2'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📋 Выбрать тариф", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/razvitie_dety")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "💬 Посмотрите, что говорят родители:\n\n"
                                "\"Вернулись после пробного и не жалеем! Ребенок с нетерпением ждет новых заданий!\" - Елена\n\n"
                                "\"За месяц сын научился считать до 20 и выучил все буквы!\" - Мария\n\n"
                                "\"Пожалела что не продлила сразу, пришлось платить по полной цене 😔\" - Ольга\n\n"
                                "А вы все еще думаете? 🤔\n\n"
                                "⏰ Осталось 5 дней специальной цены!\n\n"
                                "💡 Знаете ли вы:\n"
                                "• 87% родителей продлевают подписку\n"
                                "• Родители экономят 2-3 часа в неделю на поиске материалов\n"
                                "• Средний результат: +10 новых навыков за месяц\n\n"
                                "🎯 3 месяца = всего 2.7₽ в день!\n\n"
                                "❓ Не уверены? Напишите нам - расскажем подробнее!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'expired_day2')
                                logging.info(f"Sent expired_day2 message to user {user_id}")
                    
                    # ========== ДЕНЬ 5 ПОСЛЕ ИСТЕЧЕНИЯ (118-122 часа) ==========
                    if 118 <= hours_since_expired < 122:
                        if not get_funnel_message_sent(user_id, 'expired_day5'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="Слишком дорого 💰", callback_data="feedback_expensive")],
                                [InlineKeyboardButton(text="Не понравился контент", callback_data="feedback_content")],
                                [InlineKeyboardButton(text="Нужно больше времени ⏰", callback_data="feedback_time")],
                                [InlineKeyboardButton(text="Другое", callback_data="feedback_other")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "Можем узнать ваше мнение? 🤔\n\n"
                                "Мы заметили, что вы не продлили подписку после пробного периода.\n\n"
                                "Что вас остановило?\n\n"
                                "💡 За честный ответ - специальный бонус:\n"
                                "Скидка 30% на любой тариф!\n\n"
                                "P.S. Нам действительно важно ваше мнение, это поможет нам стать лучше 💚",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'expired_day5')
                                logging.info(f"Sent expired_day5 message to user {user_id}")
                
                except Exception as e:
                    logging.error(f"Error sending expired funnel message to {user_id}: {e}")
            
        except Exception as e:
            logging.error(f"Error in sales funnel: {e}")
            await asyncio.sleep(1800)

async def expired_users_funnel():
    """Фоновая задача для отправки сообщений ИСТЕКШИМ пользователям (day2, day5)"""
    logging.info("Запущена воронка для истекших пользователей")
    
    while True:
        try:
            await asyncio.sleep(3600)  # Проверка каждый час
            
            expired_users = get_expired_users_for_funnel()
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_until = user['subscription_until']
                
                # Считаем сколько часов прошло с момента истечения подписки
                hours_since_expired = (datetime.now() - subscription_until).total_seconds() / 3600
                
                # ========== ДЕНЬ 2 ПОСЛЕ ИСТЕЧЕНИЯ (46-50 часов) ==========
                if 46 <= hours_since_expired < 50:
                    if not get_funnel_message_sent(user_id, 'expired_day2'):
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="📅 Выбрать тариф", callback_data="show_tariffs")],
                            [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/razvitie_dety")]
                        ])
                        
                        success = await send_safe_funnel_message(
                            user_id,
                            "💬 Посмотрите реальные отзывы родителей:\n\n"
                            "\"Вернулись после пробного и не жалеем! Ребенок каждый день ждет новых заданий!\" - Елена, 2 детей\n\n"
                            "\"За месяц дочка научилась считать до 50 и выучила весь алфавит. Результат превзошел ожидания!\" - Мария\n\n"
                            "\"Пожалела что сразу не продлила - потом пришлось платить полную цену 😔\" - Ольга\n\n"
                            "🤔 А вы все еще думаете?\n\n"
                            "⏰ Осталось 5 дней со скидкой 50-70%!\n\n"
                            "📊 Статистика говорит сама за себя:\n"
                            "✅ 87% родителей продлевают подписку\n"
                            "✅ Экономия 2-3 часа в неделю на поиске материалов\n"
                            "✅ В среднем +10 новых навыков за месяц\n\n"
                            "💰 Всего 2.7₽ в день = тариф на 3 месяца\n"
                            "Это дешевле одной шоколадки!\n\n"
                            "❓ Есть вопросы? Ответим за 5 минут!",
                            reply_markup=keyboard
                        )
                        
                        if success:
                            mark_funnel_message_sent(user_id, 'expired_day2')
                            logging.info(f"Отправлено сообщение expired_day2 пользователю {user_id}")
                
                # ========== ДЕНЬ 5 ПОСЛЕ ИСТЕЧЕНИЯ (118-122 часа) ==========
                if 118 <= hours_since_expired < 122:
                    if not get_funnel_message_sent(user_id, 'expired_day5'):
                        keyboard = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💰 Слишком дорого", callback_data="feedback_expensive")],
                            [InlineKeyboardButton(text="📚 Не понравился контент", callback_data="feedback_content")],
                            [InlineKeyboardButton(text="⏰ Нужно больше времени", callback_data="feedback_time")],
                            [InlineKeyboardButton(text="💬 Другая причина", callback_data="feedback_other")]
                        ])
                        
                        success = await send_safe_funnel_message(
                            user_id,
                            "🙏 Нам жаль что вы не с нами...\n\n"
                            "Можете рассказать почему не продлили подписку?\n"
                            "Ваш отзыв поможет нам стать лучше! 💚\n\n"
                            "🎁 За честный ответ - специальный бонус:\n"
                            "Промокод на скидку 30% на любой тариф!\n\n"
                            "📩 А может быть есть что-то, что мы можем исправить прямо сейчас?\n"
                            "Напишите нам - мы всегда на связи!",
                            reply_markup=keyboard
                        )
                        
                        if success:
                            mark_funnel_message_sent(user_id, 'expired_day5')
                            logging.info(f"Отправлено сообщение expired_day5 пользователю {user_id}")
            
        except Exception as e:
            logging.error(f"Ошибка в expired_users_funnel: {e}")
            await asyncio.sleep(3600)

async def check_and_remove_expired():
    """Фоновая задача: проверка и удаление пользователей с истекшей подпиской"""
    while True:
        try:
            logging.info("Checking for expired subscriptions...")
            expired_users = get_expired_users()
            
            for user in expired_users:
                user_id = user['user_id']
                username = user['username']
                
                # Пропускаем админа
                if user_id == ADMIN_ID:
                    logging.info(f"Skipping admin {user_id}")
                    continue
                
                # Проверяем, не уведомляли ли мы пользователя за последние 24 часа
                if was_notified_recently(user_id):
                    logging.info(f"User {user_id} was already notified recently, skipping...")
                    continue
                
                try:
                    # Получаем информацию о пользователе в канале
                    try:
                        chat_member = await bot.get_chat_member(CHANNEL_ID, user_id)
                        
                        # Если это владелец или администратор - пропускаем
                        if chat_member.status in ['creator', 'administrator']:
                            logging.info(f"User {user_id} is admin/owner, skipping removal")
                            continue
                    except Exception as e:
                        logging.warning(f"Could not get chat member info for {user_id}: {e}")
                    
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
        [InlineKeyboardButton(text="🎁 Попробовать бесплатно (7 дней)", callback_data="trial")],
        [InlineKeyboardButton(text="📋 Выбрать подписку", callback_data="show_tariffs")],
        [InlineKeyboardButton(text="ℹ️ Мой статус", callback_data="status")],
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")]
    ])
    return keyboard

# Улучшенная функция get_tariffs_menu() для bot.py

def get_tariffs_menu():
    """Меню выбора тарифов с выделением популярного"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"1️⃣ 1 месяц - {TARIFFS['1month']['price']}₽ (вместо {TARIFFS['1month']['old_price']}₽)",
            callback_data="1month"
        )],
        [InlineKeyboardButton(
            text=f"🔥 3 месяца - {TARIFFS['3months']['price']}₽ (ПОПУЛЯРНЫЙ!) 🔥",  # 👈 ВЫДЕЛИЛИ
            callback_data="3months"
        )],
        [InlineKeyboardButton(
            text=f"6️⃣ 6 месяцев - {TARIFFS['6months']['price']}₽ (вместо {TARIFFS['6months']['old_price']}₽)",
            callback_data="6months"
        )],
        [InlineKeyboardButton(
            text=f"♾️ НАВСЕГДА - {TARIFFS['forever']['price']}₽ 💎 ЛУЧШАЯ ЦЕНА",
            callback_data="forever"
        )],
        [InlineKeyboardButton(text="❓ Вопросы", callback_data="faq")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    return keyboard

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Проверяем есть ли пользователь в базе
    user = get_user(user_id)
    
    if not user:
        # Новый пользователь - показываем приветствие
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}!\n\n"
            "Добро пожаловать в бот закрытой группы с развивающими материалами для детей!\n\n"
            "🎁 Попробуй бесплатно 7 дней! После пробного периода выбери удобную подписку и развивайся вместе с нами 👇",
            reply_markup=get_main_menu()
        )
    else:
        # Существующий пользователь
        if is_subscription_active(user_id):
            await message.answer(
                f"👋 С возвращением, {message.from_user.first_name}!\n\n"
                "Твоя подписка активна! 🎉",
                reply_markup=get_main_menu()
            )
        else:
            await message.answer(
                f"👋 Привет, {message.from_user.first_name}!\n\n"
                "Твоя подписка истекла 😔\n\n"
                "Продли подписку чтобы продолжить пользоваться материалами!",
                reply_markup=get_main_menu()
            )

@dp.callback_query(F.data == "trial")
async def process_trial(callback: types.CallbackQuery):
    """Обработчик кнопки 'Попробовать бесплатно'"""
    user_id = callback.from_user.id
    username = callback.from_user.username
    
    # Проверяем не брал ли уже пробный период
    user = get_user(user_id)
    
    if user:
        await callback.answer(
            "Вы уже использовали пробный период! 😊",
            show_alert=True
        )
        return
    
    # Добавляем пользователя с пробным периодом
    add_user(user_id, username, TARIFFS['trial']['days'], 'trial')
    
    try:
        # Создаём инвайт-ссылку
        invite_link = await bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1,
            expire_date=datetime.now() + timedelta(days=TARIFFS['trial']['days'])
        )
        
        await callback.message.edit_text(
            f"🎉 <b>Поздравляем!</b>\n\n"
            f"Вам активирован пробный период на {TARIFFS['trial']['days']} дней!\n\n"
            f"<b>ВАЖНО: Сохрани эту ссылку!</b>\n\n"
            f"Переходи по ссылке: {invite_link.invite_link}\n\n"
            f"⏰ Доступ истечет через {TARIFFS['trial']['days']} дней.\n"
            f"После этого выбери подходящий тариф!\n\n"
            f"💡 Это ссылка для присоединения к закрытой группе.",
        )
        
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error adding user to channel: {e}")
        await callback.message.edit_text(
            "❌ Произошла ошибка. Обратитесь к администратору.",
            reply_markup=get_main_menu()
        )
    
    await callback.answer()

# И изменить текст при показе тарифов:
@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs(callback: types.CallbackQuery):
    """Показать список тарифов"""
    await callback.message.edit_text(
        "📋 **Выберите подходящую подписку:**\n\n"
        "🎁 Попробуйте 7 дней БЕСПЛАТНО, потом:\n\n"
        "💰 1 месяц - 99₽\n"
        "🔥 3 месяца - 249₽ (самый популярный!)\n"
        "💎 6 месяцев - 399₽\n"
        "♾️ Навсегда - 599₽ (разовый платёж)\n\n"
        "⚡️ Цены действуют только сейчас!",
        reply_markup=get_tariffs_menu(),
        parse_mode="Markdown"
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
        "🎁 Попробуй бесплатно 7 дней! После пробного периода выбери удобную подписку и развивайся вместе с нами 👇",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs(callback: types.CallbackQuery):
    """Показать список тарифов"""
    await callback.message.edit_text(
        "📋 **Выберите подходящую подписку:**\n\n"
        "🔥 Специальные цены 5 дней!\n"
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
    
    # Убираем parse_mode или используем HTML вместо Markdown
    await callback.message.edit_text(
        "4. Можно ли вернуть деньги?\n\n"
        "🎁 Пробный период:\n"
        "Воспользуйтесь бесплатным доступом на 7 дней, чтобы оценить качество материалов перед покупкой!\n\n"
        "💰 Возврат средств:\n"
        "Возврат возможен в течение 3 дней после оплаты, если:\n"
        "• Вы не получили доступ к материалам\n"
        "• Возникли технические проблемы\n"
        "• Контент не соответствует описанию\n\n"
        "Для оформления возврата свяжитесь с поддержкой @razvitie_dety\n\n"
        "⚠️ Обратите внимание:\n"
        "После использования материалов возврат не предусмотрен согласно законодательству об информационных услугах.",
        reply_markup=keyboard
        # УДАЛИТЕ parse_mode="Markdown" отсюда!
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
        "💡 Попробуйте бесплатно 7 дней, чтобы оценить все возможности!",
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

# Обработчики опроса из воронки
@dp.callback_query(F.data.in_(['survey_games', 'survey_creative', 'survey_learning']))
async def handle_survey(callback: types.CallbackQuery):
    """Обработка опроса предпочтений"""
    await callback.answer("Спасибо за ваш ответ! 💚", show_alert=True)

# Обработчики feedback
@dp.callback_query(F.data.in_(['feedback_expensive', 'feedback_content', 'feedback_time', 'feedback_other']))
async def handle_feedback(callback: types.CallbackQuery):
    """Обработка обратной связи"""
    await callback.answer("Спасибо за обратную связь! 🙏", show_alert=True)

@dp.callback_query(F.data == "how_it_works")
async def how_it_works(callback: types.CallbackQuery):
    """Инструкция как работает бот"""
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Активировать пробный период", callback_data="trial")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    
    await callback.message.edit_text(
        "📖 <b>КАК ЭТО РАБОТАЕТ?</b>\n\n"
        "<b>Шаг 1:</b> Активация пробного периода\n"
        "Нажмите кнопку \"Активировать пробный период\" и получите ссылку на закрытую группу.\n\n"
        "<b>Шаг 2:</b> Присоединитесь к группе\n"
        "Перейдите по ссылке и вступите в закрытую группу с материалами.\n\n"
        "<b>Шаг 3:</b> Начните заниматься\n"
        "В группе вы найдёте:\n"
        "• 📚 Развивающие игры и задания\n"
        "• 🎨 Творческие активности\n"
        "• 📖 Обучающие материалы\n"
        "• 🎯 Готовые занятия на каждый день\n\n"
        "<b>Шаг 4:</b> Оцените результат\n"
        "За 7 дней вы увидите прогресс ребенка и поймёте подходит ли вам наш клуб.\n\n"
        "💡 <b>Важно:</b>\n"
        "• Доступ бесплатный 7 дней\n"
        "• Никакой предоплаты\n"
        "• Можно отменить в любой момент\n\n"
        "🎁 <b>Попробуйте прямо сейчас!</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()
    
    # Уведомляем админа
    if ADMIN_ID:
        feedback_names = {
            'expensive': 'Слишком дорого',
            'content': 'Не понравился контент',
            'time': 'Нужно больше времени',
            'other': 'Другое'
        }
        feedback = callback.data.replace('feedback_', '')
        await bot.send_message(
            ADMIN_ID,
            f"📊 Новый отзыв!\n"
            f"👤 @{callback.from_user.username} (ID: {callback.from_user.id})\n"
            f"💭 {feedback_names.get(feedback, feedback)}"
        )

# КОМАНДА ДИАГНОСТИКИ БАЗЫ ДАННЫХ
# Добавить в bot.py после команды /stats

# ПРОСТАЯ КОМАНДА ДИАГНОСТИКИ (заменить admin_check_db в bot.py)

@dp.message(Command("checkdb"))
async def admin_check_db(message: types.Message):
    """Диагностика базы данных - простая версия"""
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer("🔍 Анализирую базу данных...")
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Основная статистика
        cur.execute('SELECT COUNT(*) as total FROM users')
        total = cur.fetchone()['total']
        
        cur.execute('SELECT COUNT(DISTINCT user_id) as unique_users FROM users')
        unique = cur.fetchone()['unique_users']
        
        # 2. Дубли
        cur.execute('''
            SELECT user_id, username, COUNT(*) as count
            FROM users
            GROUP BY user_id, username
            HAVING COUNT(*) > 1
            ORDER BY count DESC
            LIMIT 10
        ''')
        dupes = cur.fetchall()
        
        # 3. За последние часы
        cur.execute('''
            SELECT 
                DATE_TRUNC('hour', created_at) as hour,
                COUNT(*) as count
            FROM users
            WHERE created_at >= NOW() - INTERVAL '12 hours'
            GROUP BY hour
            ORDER BY hour DESC
            LIMIT 12
        ''')
        hourly = cur.fetchall()
        
        # 4. За последние 5 часов
        cur.execute('''
            SELECT COUNT(*) as count
            FROM users
            WHERE created_at >= NOW() - INTERVAL '5 hours'
        ''')
        last_5h = cur.fetchone()['count']
        
        # 5. Активных VS неактивных
        cur.execute('''
            SELECT 
                COUNT(*) FILTER (WHERE subscription_until > NOW()) as active,
                COUNT(*) FILTER (WHERE subscription_until <= NOW()) as expired,
                COUNT(*) FILTER (WHERE tariff = 'trial') as trial,
                COUNT(*) FILTER (WHERE tariff != 'trial') as paid
            FROM users
        ''')
        subs = cur.fetchone()
        
        # 6. Текущее время БД
        cur.execute('SELECT NOW() as db_time')
        db_time = cur.fetchone()['db_time']
        
        cur.close()
        conn.close()
        
        # Формируем отчет
        report = "🔍 **ДЕТАЛЬНАЯ ДИАГНОСТИКА**\n\n"
        
        # Блок 1: Основное
        report += "📊 **Записи в базе:**\n"
        report += f"• Всего записей: {total}\n"
        report += f"• Уникальных user_id: {unique}\n"
        
        if total != unique:
            report += f"• ⚠️ Дублей: {total - unique}\n\n"
        else:
            report += f"• ✅ Дублей нет\n\n"
        
        # Блок 2: Дубли если есть
        if dupes:
            report += "⚠️ **НАЙДЕНЫ ДУБЛИКАТЫ:**\n"
            for d in dupes[:5]:
                username = d['username'] or 'без username'
                report += f"• @{username} (ID: {d['user_id']}): {d['count']} записей\n"
            report += "\n"
        
        # Блок 3: Подписки
        report += "💎 **Статус подписок:**\n"
        report += f"• Активные: {subs['active']}\n"
        report += f"• Истёкшие: {subs['expired']}\n"
        report += f"• Trial: {subs['trial']}\n"
        report += f"• Платные: {subs['paid']}\n\n"
        
        # Блок 4: За последние часы
        report += f"⏰ **За последние 5 часов:** {last_5h} регистраций\n\n"
        
        report += "📈 **Регистрации по часам (UTC):**\n"
        for h in hourly[:8]:
            hour_str = h['hour'].strftime('%d.%m %H:00')
            report += f"• {hour_str}: {h['count']} чел\n"
        
        report += f"\n🕐 **Время БД:** {db_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        
        # Вывод
        report += "\n💡 **Вывод:**\n"
        
        if total != unique:
            report += "⚠️ В базе есть дубликаты записей!\n"
            report += "Причина: функция add_user() может создавать дубли\n"
        elif last_5h < 50:
            report += f"⚠️ За 5ч всего {last_5h} регистраций\n"
            report += "Это меньше ожидаемого (~100)\n"
        else:
            report += "✅ Всё отлично! База в норме!\n"
        
        # Активация
        activation_rate = round(100 * subs['active'] / total, 1) if total > 0 else 0
        report += f"\n📊 **Активация:** {activation_rate}%\n"
        
        if activation_rate < 70:
            report += "💡 Можно улучшить с приветственным сообщением!"
        
        await message.answer(report)
        
    except Exception as e:
        await message.answer(f"❌ Ошибка:\n{str(e)}")
        import traceback
        logging.error(f"Checkdb error: {e}\n{traceback.format_exc()}")

# ИСПРАВЛЕННАЯ ФУНКЦИЯ send_welcome_messages()
# Заменить в bot.py старую версию на эту

async def send_welcome_messages():
    """Фоновая задача: отправка приветственных сообщений через 5-10 минут после регистрации"""
    logging.info("Welcome messages task started!")
    
    while True:
        try:
            await asyncio.sleep(60)  # Проверка каждую минуту
            
            # Получаем список пользователей
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("""
                SELECT u.user_id, u.username
                FROM users u
                LEFT JOIN welcome_messages wm ON u.user_id = wm.user_id
                WHERE u.created_at >= NOW() - INTERVAL '10 minutes'
                  AND u.created_at <= NOW() - INTERVAL '5 minutes'
                  AND wm.user_id IS NULL
                  AND u.tariff = 'trial'
                  AND u.subscription_until > NOW()
            """)
            
            users = cur.fetchall()
            cur.close()
            conn.close()
            
            # Обрабатываем каждого пользователя отдельно
            for user in users:
                user_id = user['user_id']
                
                try:
                    # ПРИВЕТСТВЕННОЕ СООБЩЕНИЕ
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎁 Активировать пробный период", callback_data="trial")],
                        [InlineKeyboardButton(text="🎥 Видео инструкция", url="https://t.me/instrukcii_baza/35")],
                        [InlineKeyboardButton(text="📋 Как это работает?", callback_data="how_it_works")],
                        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")]
                    ])
                    
                    await bot.send_message(
                        user_id,
                        "👋 Привет!\n\n"
                        "Я вижу вы только что присоединились к нам!\n\n"
                        "🎯 <b>Вот что вас ждёт:</b>\n\n"
                        "🎁 <b>7 дней БЕСПЛАТНОГО доступа</b> ко всем материалам\n"
                        "📚 <b>5000+ развивающих материалов</b> для детей\n"
                        "🎨 Игры, задания, поделки, обучение\n"
                        "⚡️ Новые материалы каждую неделю\n\n"
                        "💡 <b>Как начать?</b>\n\n"
                        "1️⃣ Нажмите кнопку \"Активировать пробный период\"\n"
                        "2️⃣ Получите ссылку на закрытую группу\n"
                        "3️⃣ Начните заниматься с ребенком прямо сегодня!\n\n"
                        "⏰ Это займет всего 30 секунд!\n\n"
                        "👇 Нажимайте кнопку ниже:",
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                    
                    # Отметить что отправили (в отдельном соединении)
                    conn2 = get_db_connection()
                    cur2 = conn2.cursor()
                    cur2.execute("""
                        INSERT INTO welcome_messages (user_id, sent_at)
                        VALUES (%s, NOW())
                        ON CONFLICT (user_id) DO NOTHING
                    """, (user_id,))
                    conn2.commit()
                    cur2.close()
                    conn2.close()
                    
                    logging.info(f"Welcome message sent to user {user_id}")
                    
                    # Небольшая пауза между сообщениями
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logging.error(f"Error sending welcome to {user_id}: {e}")
            
        except Exception as e:
            logging.error(f"Error in send_welcome_messages: {e}")
            await asyncio.sleep(60)

async def main():
    init_db()
    feedback_broadcast.init_feedback_system(dp, bot, ADMIN_ID, get_db_connection)
    logging.info("Bot started successfully!")
    
    # Запускаем ТРИ фоновые задачи
    asyncio.create_task(check_and_remove_expired())
    asyncio.create_task(sales_funnel())  # для активных trial пользователей
    asyncio.create_task(expired_users_funnel())  # 👈 НОВАЯ ЗАДАЧА для истекших
    asyncio.create_task(send_welcome_messages())
    
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
