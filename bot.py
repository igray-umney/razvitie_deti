import os
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
DATABASE_URL = os.getenv('DATABASE_URL')

# 🆕 TELEGRAM PAYMENTS - Provider Token от BotFather
YOOKASSA_PROVIDER_TOKEN = os.getenv('YOOKASSA_PROVIDER_TOKEN', '390540012:LIVE:83850')

# 🆕 Ссылки на демо-контент
DEMO_VIDEO_URL = "https://t.me/instrukcii_baza"
DEMO_PHOTOS_URL = "https://t.me/instrukcii_baza"
REVIEWS_URL = "https://t.me/otzovik_klub"

# Тарифы
TARIFFS = {
    'trial': {'name': '7 дней бесплатно', 'days': 7, 'price': 0},
    '1month': {'name': '1 месяц', 'days': 30, 'price': 199, 'old_price': 499},
    'forever': {'name': 'Навсегда', 'days': 36500, 'price': 599, 'old_price': 2990}
}

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Импорт системы обратной связи
import feedback_broadcast

# ========================================
# ФУНКЦИИ БАЗЫ ДАННЫХ
# ========================================

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
    
    cur.execute('''CREATE TABLE IF NOT EXISTS funnel_analytics
                 (id SERIAL PRIMARY KEY,
                  user_id BIGINT,
                  action TEXT,
                  created_at TIMESTAMP DEFAULT NOW())''')
    
    conn.commit()
    cur.close()
    conn.close()

def track_user_action(user_id, action):
    """Сохраняем действия пользователя для аналитики"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''INSERT INTO funnel_analytics (user_id, action, created_at)
                       VALUES (%s, %s, NOW())''', (user_id, action))
        conn.commit()
        cur.close()
        conn.close()
        logging.info(f"Tracked action: {action} for user {user_id}")
    except Exception as e:
        logging.error(f"Error tracking action: {e}")

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
    return time_diff.total_seconds() < 86400

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

def get_active_subscribers():
    """Получение всех пользователей с активной подпиской"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''SELECT user_id, username, subscription_until, tariff 
                   FROM users 
                   WHERE subscription_until > %s
                   ORDER BY subscription_until DESC''',
                (datetime.now(),))
    
    active_users = cur.fetchall()
    cur.close()
    conn.close()
    return active_users

async def send_safe_funnel_message(user_id, text, reply_markup=None, parse_mode="Markdown"):
    """Безопасная отправка сообщений воронки с обработкой блокировки"""
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception as e:
        if "bot was blocked by the user" in str(e) or "Forbidden" in str(e):
            logging.info(f"User {user_id} blocked the bot, skipping")
            return False
        else:
            logging.error(f"Error sending message to {user_id}: {e}")
            return False

# ========================================
# 🆕 TELEGRAM PAYMENTS - ФУНКЦИИ
# ========================================

async def send_invoice(user_id, tariff_code):
    """Отправка счета на оплату через Telegram Payments с фискализацией"""
    import json
    
    tariff = TARIFFS[tariff_code]
    payload = f"{user_id}_{tariff_code}_{int(datetime.now().timestamp())}"
    
    price = types.LabeledPrice(
        label="К оплате",
        amount=int(tariff['price'] * 100)
    )
    
    # Данные для чека (самозанятый/УСН)
    provider_data = {
        "receipt": {
            "items": [
                {
                    "description": f"Подписка: {tariff['name']}",
                    "quantity": "1",
                    "amount": {
                        "value": str(tariff['price']),
                        "currency": "RUB"
                    },
                    "vat_code": 6,
                    "payment_mode": "full_payment",
                    "payment_subject": "service"
                }
            ],
            "tax_system_code": 1
        }
    }
    
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=f"Подписка: {tariff['name']}",
            description=f"Доступ к развивающим материалам для детей.\n"
                       f"Полная цена: {tariff['old_price']}₽\n"
                       f"Со скидкой: {tariff['price']}₽",
            payload=payload,
            provider_token=YOOKASSA_PROVIDER_TOKEN,
            currency="RUB",
            prices=[price],
            start_parameter="subscription",
            need_email=True,
            send_email_to_provider=True,
            need_name=False,
            need_phone_number=False,
            need_shipping_address=False,
            is_flexible=False,
            provider_data=json.dumps(provider_data)
        )
        
        # Сохраняем в БД
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''INSERT INTO payments 
                     (payment_id, user_id, amount, tariff, status, yookassa_id, created_at)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                  (payload, user_id, tariff['price'], tariff_code, 'pending', payload, datetime.now()))
        conn.commit()
        cur.close()
        conn.close()
        
        logging.info(f"Invoice sent to user {user_id} for tariff {tariff_code}")
        return True
        
    except Exception as e:
        logging.error(f"Error sending invoice: {e}")
        return False

# ========================================
# 🆕 TELEGRAM PAYMENTS - ОБРАБОТЧИКИ
# ========================================

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    """Обработка pre-checkout query - ОБЯЗАТЕЛЬНО ответить в течение 10 секунд!"""
    try:
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=True
        )
        logging.info(f"Pre-checkout approved for user {pre_checkout_query.from_user.id}")
        
    except Exception as e:
        logging.error(f"Error in pre-checkout: {e}")
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=False,
            error_message="Произошла ошибка. Попробуйте позже."
        )

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Обработка успешного платежа"""
    try:
        payment_info = message.successful_payment
        
        # Получаем данные
        user_id = message.from_user.id
        username = message.from_user.username or "unknown"
        payload = payment_info.invoice_payload
        provider_payment_charge_id = payment_info.provider_payment_charge_id  # ID в ЮKassa
        total_amount = payment_info.total_amount / 100  # Из копеек в рубли
        
        # Парсим payload чтобы получить tariff_code
        parts = payload.split('_')
        tariff_code = parts[1] if len(parts) > 1 else '1month'
        
        tariff = TARIFFS.get(tariff_code)
        
        if not tariff:
            logging.error(f"Unknown tariff: {tariff_code}")
            await message.answer("❌ Ошибка определения тарифа. Обратитесь к администратору.")
            return
        
        # Обновляем статус платежа в БД
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''UPDATE payments 
                       SET status = %s, yookassa_id = %s 
                       WHERE payment_id = %s''',
                   ('completed', provider_payment_charge_id, payload))
        conn.commit()
        cur.close()
        conn.close()
        
        # Активируем подписку
        add_user(user_id, username, tariff['days'], tariff_code)
        track_user_action(user_id, f'completed_payment_{tariff_code}')
        
        # Создаем инвайт-ссылку
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
            
            # Отправляем подтверждение
            await message.answer(
                f"✅ **Оплата прошла успешно!**\n\n"
                f"🎉 Поздравляем! Вы получили доступ.\n"
                f"📅 Тариф: {tariff['name']}\n"
                f"💰 Оплачено: {total_amount}₽\n\n"
                f"🔗 **Переходите в группу:**\n{invite_link.invite_link}\n\n"
                f"💡 Сохраните эту ссылку!",
                reply_markup=get_main_menu(),
                parse_mode="Markdown"
            )
            
            # Уведомляем админа
            if ADMIN_ID:
                await bot.send_message(
                    ADMIN_ID,
                    f"💰 **НОВАЯ ОПЛАТА!**\n\n"
                    f"👤 User: @{username} (ID: {user_id})\n"
                    f"📦 Тариф: {tariff['name']}\n"
                    f"💵 Сумма: {total_amount}₽\n"
                    f"🆔 ЮKassa ID: {provider_payment_charge_id}",
                    parse_mode="Markdown"
                )
            
            logging.info(f"Payment successful: user {user_id}, tariff {tariff_code}, amount {total_amount}")
            
        except Exception as e:
            logging.error(f"Error creating invite after payment: {e}")
            await message.answer(
                "✅ Оплата получена!\n"
                "❌ Ошибка создания приглашения.\n"
                "Обратитесь к администратору @razvitie_dety",
                reply_markup=get_main_menu()
            )
    
    except Exception as e:
        logging.error(f"Error processing successful payment: {e}")
        await message.answer(
            "⚠️ Платёж получен, но возникла ошибка.\n"
            "Обратитесь к администратору @razvitie_dety"
        )

# ========================================
# КЛАВИАТУРЫ
# ========================================

def get_main_menu():
    """Главное меню для СУЩЕСТВУЮЩИХ пользователей"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Выбрать подписку", callback_data="show_tariffs")],
        [InlineKeyboardButton(text="ℹ️ Мой статус", callback_data="status")],
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")]
    ])
    return keyboard

def get_new_user_menu():
    """🆕 Меню для НОВЫХ пользователей (с прогревом)"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 4.9/5 - Почему 87% продлевают?", callback_data="show_reviews")],
        [InlineKeyboardButton(text="🎥 Посмотреть примеры материалов", callback_data="show_demo")],
        [InlineKeyboardButton(text="💰 Что входит в подписку?", callback_data="faq_5")],
        [InlineKeyboardButton(text="🎁 Попробовать 7 дней БЕСПЛАТНО", callback_data="ready_for_trial")]
    ])
    return keyboard

def get_tariffs_menu():
    """Меню выбора тарифов"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💎 1 месяц - {TARIFFS['1month']['price']}₽",
            callback_data="1month"
        )],
        [InlineKeyboardButton(
            text=f"🔥 НАВСЕГДА - {TARIFFS['forever']['price']}₽ (Экономия 1789₽!)",
            callback_data="forever"
        )],
        [InlineKeyboardButton(text="❓ Вопросы", callback_data="faq")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    return keyboard

# ========================================
# ВОРОНКА ПРОДАЖ
# ========================================

async def sales_funnel():
    """Фоновая задача: автоматическая отправка сообщений воронки продаж"""
    logging.info("Sales funnel started!")
    
    while True:
        try:
            await asyncio.sleep(1800)  # Проверка каждые 30 минут
            
            trial_users = get_trial_users_for_funnel()
            
            for user in trial_users:
                user_id = user['user_id']
                created_at = user['created_at']
                subscription_until = user['subscription_until']
                
                hours_since_registration = (datetime.now() - created_at).total_seconds() / 3600
                hours_until_end = (subscription_until - datetime.now()).total_seconds() / 3600
                
                try:
                    # ДЕНЬ 1 (20-28 часов) - ПРОВЕРКА ОПЫТА
                    if 20 <= hours_since_registration < 28:
                        if not get_funnel_message_sent(user_id, 'day1'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="👍 Всё отлично!", callback_data="feedback_good")],
                                [InlineKeyboardButton(text="🤔 Есть вопросы", url="https://t.me/razvitie_dety")],
                                [InlineKeyboardButton(text="📚 Нужна помощь", callback_data="need_help")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "Привет! 👋\n\n"
                                "Прошел первый день с нашими материалами!\n\n"
                                "🤔 **Как тебе?**\n"
                                "• Удалось позаниматься с ребенком?\n"
                                "• Понятно как пользоваться группой?\n"
                                "• Всё нашел что искал?\n\n"
                                "💡 **Лайфхак дня:**\n"
                                "Занимайся утром 15-20 минут - в это время дети максимально внимательны!\n\n"
                                "🎯 Осталось **6 дней** trial - используй по максимуму!\n\n"
                                "💬 Если есть вопросы - пиши, помогу разобраться!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day1')
                                logging.info(f"Sent day1 message to user {user_id}")

                    # 🆕 ДЕНЬ 2 (44-52 часа) - ЛАЙФХАК
                    if 44 <= hours_since_registration < 52:
                        if not get_funnel_message_sent(user_id, 'day2'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📚 В группу", url=f"https://t.me/+{CHANNEL_ID}")],
                                [InlineKeyboardButton(text="💬 Вопросы", url="https://t.me/razvitie_dety")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "👋 Привет!\n\n"
                                "Прошло 2 дня - как впечатления?\n\n"
                                "💡 **Лайфхак:**\n"
                                "Родители которые занимаются УТРОМ (до садика/завтрака) "
                                "видят результат быстрее!\n\n"
                                "🎯 Ребёнок свежий, внимательный, усваивает лучше\n\n"
                                "Попробуй завтра утром 15 минут - и увидишь разницу!\n\n"
                                "P.S. Осталось 5 дней trial - успей протестировать "
                                "разные материалы! 📚",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day2')
                                logging.info(f"Sent day2 message to user {user_id}")
                    
                    # ДЕНЬ 3 (68-76 часов) - СОЦИАЛЬНОЕ ДОКАЗАТЕЛЬСТВО
                    if 68 <= hours_since_registration < 76:
                        if not get_funnel_message_sent(user_id, 'day3'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📋 Посмотреть тарифы", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Отзывы других родителей", callback_data="show_reviews")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "Уже 3 дня вместе! 🎉\n\n"
                                "Надеемся, материалы вам нравятся!\n\n"
                                "📊 **Интересный факт:**\n"
                                "Родители которые занимаются по нашим материалам всего 15-20 минут в день, "
                                "замечают видимые результаты уже через неделю!\n\n"
                                "✨ Осталось **4 дня** пробного периода\n\n"
                                "🎁 **Специальная цена только для пробников:**\n"
                                "• 1 месяц - 199₽ вместо 499₽ (-60%)\n"
                                "• Навсегда - 599₽ вместо 2990₽ (-80%)\n\n"
                                "💡 **Совет:** 87% родителей выбирают тариф \"Навсегда\" - "
                                "это как раз чтобы пройти полный курс развития без спешки!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day3')
                                logging.info(f"Sent day3 message to user {user_id}")

                    # 🆕 ДЕНЬ 4 (92-100 часов) - РЕЗУЛЬТАТЫ
                    if 92 <= hours_since_registration < 100:
                        if not get_funnel_message_sent(user_id, 'day4'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💰 Посмотреть тарифы", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="📚 Продолжить занятия", url=f"https://t.me/+{CHANNEL_ID}")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "🎉 **Половина пути пройдена!**\n\n"
                                "Ты с нами уже 4 дня - заметил изменения?\n\n"
                                "📊 **Обычно к 4му дню родители видят:**\n"
                                "• Ребёнок стал усидчивее (+30%)\n"
                                "• Выучил 3-5 новых букв/цифр\n"
                                "• САМ просит позаниматься!\n\n"
                                "У тебя так же? 😊\n\n"
                                "💡 **Осталось 3 дня - самое время:**\n"
                                "1. Попробовать сложные материалы\n"
                                "2. Найти любимые темы ребёнка\n"
                                "3. Составить план после trial\n\n"
                                "⚠️ **После trial цена вырастет:**\n"
                                "• Сейчас: 199₽/мес или 599₽ навсегда\n"
                                "• Потом: 499₽/мес или 2990₽ навсегда\n\n"
                                "Успей оформить со скидкой! 🔥",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day4')
                                logging.info(f"Sent day4 message to user {user_id}")
                    
                    # ДЕНЬ 5 (116-124 часа) - ОТЗЫВЫ + СРОЧНОСТЬ
                    if 116 <= hours_since_registration < 124:
                        if not get_funnel_message_sent(user_id, 'day5'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оформить со скидкой", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="📸 Больше отзывов", callback_data="show_reviews")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "💬 **Что говорят другие родители:**\n\n"
                                "**Анна (2 детей):**\n"
                                "\"Дочка за неделю выучила 10 букв! Занимаемся по утрам 20 минут. "
                                "Теперь сама просит позаниматься!\"\n\n"
                                "**Олег (сын 5 лет):**\n"
                                "\"Раньше тратил часы на поиск заданий. Теперь всё в одном месте. "
                                "Окупилось за первую неделю!\"\n\n"
                                "**Мария (дочка 3 года):**\n"
                                "\"Попробовали trial - не смогли остановиться. "
                                "Взяли Навсегда со скидкой!\"\n\n"
                                "📊 **Наши цифры:**\n"
                                "• 87% родителей продлевают подписку\n"
                                "• 1000+ активных семей\n"
                                "• 5000+ развивающих материалов\n\n"
                                "⏰ Осталось **2 дня** пробного периода!\n\n"
                                "🎁 Успей оформить со скидкой 60-80%!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day5')
                                logging.info(f"Sent day5 message to user {user_id}")
                    
                    # ДЕНЬ 7 - ЗА 8 ЧАСОВ ДО КОНЦА
                    if 8 <= hours_until_end < 12:
                        if not get_funnel_message_sent(user_id, 'day7_8hours'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🔥 Продолжить со скидкой", callback_data="show_tariffs")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "⏰ **Осталось меньше 8 часов!**\n\n"
                                "Завтра доступ к материалам закроется...\n\n"
                                "🎁 Но у вас еще есть время оформить подписку со **СКИДКОЙ**:\n\n"
                                "💰 **Специальные цены (только для пробников):**\n"
                                "• 1 месяц: 199₽ (экономия 300₽)\n"
                                "• Навсегда: 599₽ (экономия 2391₽!)\n\n"
                                "⚠️ После окончания пробного периода эти цены **исчезнут навсегда**!\n\n"
                                "💡 P.S. Не теряйте то, что уже начали строить вместе с ребенком 💚",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day7_8hours')
                                logging.info(f"Sent day7_8hours message to user {user_id}")
                    
                    # ДЕНЬ 7 - ЗА 2 ЧАСА ДО КОНЦА (ПОСЛЕДНИЙ ШАНС)
                    if 1 <= hours_until_end < 3:
                        if not get_funnel_message_sent(user_id, 'day7_2hours'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Продолжить СЕЙЧАС!", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Срочный вопрос", url="https://t.me/razvitie_dety")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "🚨 **ПОСЛЕДНИЕ 2 ЧАСА!**\n\n"
                                "Представьте: завтра ваш ребенок спросит:\n"
                                "\"Мама/Папа, а где наши игры?\"\n\n"
                                "😔 Или завтра вы продолжите вместе:\n"
                                "✅ Развивать речь через игры\n"
                                "✅ Создавать поделки\n"
                                "✅ Учиться через творчество\n\n"
                                "💰 **199₽ в месяц = всего 6₽ в день**\n"
                                "☕ Это меньше чем чашка кофе!\n\n"
                                "🔥 Скидка 60-80% действует **только до конца пробного периода**!\n\n"
                                "⏰ Не упустите момент - осталось меньше 2 часов!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'day7_2hours')
                                logging.info(f"Sent day7_2hours message to user {user_id}")
                
                except Exception as e:
                    logging.error(f"Error sending funnel message to {user_id}: {e}")
            
            # ОБРАБОТКА ИСТЕКШИХ ПОЛЬЗОВАТЕЛЕЙ
            expired_users = get_expired_trial_users()
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_until = user['subscription_until']
                hours_since_expired = (datetime.now() - subscription_until).total_seconds() / 3600
                
                try:
                    # СРАЗУ ПОСЛЕ ИСТЕЧЕНИЯ (0-2 часа)
                    if 0 <= hours_since_expired < 2:
                        if not get_funnel_message_sent(user_id, 'expired_immediate'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Вернуться в клуб", callback_data="show_tariffs")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "😔 Ваш пробный доступ истек\n\n"
                                "Надеемся, материалы понравились вам и вашему ребенку.\n\n"
                                "🎁 **Хорошая новость:**\n\n"
                                "Специально для вас мы **сохранили скидку еще на 7 дней**!\n\n"
                                "Вернуться можно прямо сейчас:\n"
                                "• 199₽ за месяц (вместо 499₽)\n"
                                "• Или выбрать тариф Навсегда за 599₽\n\n"
                                "📊 **Что вы потеряете без подписки:**\n"
                                "❌ 1000+ развивающих материалов\n"
                                "❌ Еженедельные новинки\n"
                                "❌ Поддержку и советы\n\n"
                                "💡 P.S. Скидка действует 7 дней, потом цены вернутся к обычным.",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'expired_immediate')
                                logging.info(f"Sent expired_immediate message to user {user_id}")
                    
                    # ДЕНЬ 2 ПОСЛЕ ИСТЕЧЕНИЯ (46-50 часов)
                    if 46 <= hours_since_expired < 50:
                        if not get_funnel_message_sent(user_id, 'expired_day2'):
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📋 Выбрать тариф", callback_data="show_tariffs")],
                                [InlineKeyboardButton(text="💬 Задать вопрос", url="https://t.me/razvitie_dety")]
                            ])
                            success = await send_safe_funnel_message(
                                user_id,
                                "💬 **Посмотрите, что говорят родители:**\n\n"
                                "\"Вернулись после пробного и не жалеем! Ребенок с нетерпением ждет новых заданий!\" - Елена\n\n"
                                "\"За месяц сын научился считать до 20 и выучил все буквы!\" - Мария\n\n"
                                "\"Пожалела что не продлила сразу, пришлось платить по полной цене 😔\" - Ольга\n\n"
                                "🤔 А вы все еще думаете?\n\n"
                                "⏰ Осталось **5 дней** специальной цены!\n\n"
                                "💡 **Знаете ли вы:**\n"
                                "• 87% родителей продлевают подписку\n"
                                "• Экономия 2-3 часа в неделю на поиске материалов\n"
                                "• Средний результат: +10 новых навыков за месяц\n\n"
                                "🎯 1 месяц = всего **6₽ в день**!\n\n"
                                "❓ Не уверены? Напишите нам - расскажем подробнее!",
                                reply_markup=keyboard
                            )
                            if success:
                                mark_funnel_message_sent(user_id, 'expired_day2')
                                logging.info(f"Sent expired_day2 message to user {user_id}")
                    
                    # ДЕНЬ 5 ПОСЛЕ ИСТЕЧЕНИЯ (118-122 часа) - ФИДБЕК
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
                                "🙏 Можем узнать ваше мнение?\n\n"
                                "Мы заметили, что вы не продлили подписку после пробного периода.\n\n"
                                "**Что вас остановило?**\n\n"
                                "💡 За честный ответ - **специальный бонус**:\n"
                                "Промокод на скидку **30%** на любой тариф!\n\n"
                                "💚 P.S. Нам действительно важно ваше мнение - это поможет нам стать лучше!",
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

async def check_and_remove_expired():
    """Фоновая задача: проверка и удаление пользователей с истекшей подпиской"""
    while True:
        try:
            logging.info("Checking for expired subscriptions...")
            expired_users = get_expired_users()
            
            for user in expired_users:
                user_id = user['user_id']
                username = user['username']
                
                if user_id == ADMIN_ID:
                    logging.info(f"Skipping admin {user_id}")
                    continue
                
                if was_notified_recently(user_id):
                    logging.info(f"User {user_id} was already notified recently, skipping...")
                    continue
                
                try:
                    try:
                        chat_member = await bot.get_chat_member(CHANNEL_ID, user_id)
                        if chat_member.status in ['creator', 'administrator']:
                            logging.info(f"User {user_id} is admin/owner, skipping removal")
                            continue
                    except Exception as e:
                        logging.warning(f"Could not get chat member info for {user_id}: {e}")
                    
                    await bot.ban_chat_member(CHANNEL_ID, user_id)
                    await bot.unban_chat_member(CHANNEL_ID, user_id)
                    
                    logging.info(f"Removed expired user: {username} (ID: {user_id})")
                    
                    try:
                        await bot.send_message(
                            user_id,
                            "⏰ Ваша подписка истекла!\n\n"
                            "Продлите доступ чтобы продолжить пользоваться материалами.",
                            reply_markup=get_main_menu()
                        )
                        mark_as_notified(user_id)
                        logging.info(f"Notified user {user_id} about expiration")
                    except Exception as e:
                        logging.error(f"Could not notify user {user_id}: {e}")
                    
                except Exception as e:
                    logging.error(f"Error removing user {user_id}: {e}")
            
            await asyncio.sleep(3600)
            
        except Exception as e:
            logging.error(f"Error in check_and_remove_expired: {e}")
            await asyncio.sleep(3600)

async def send_welcome_messages():
    """Фоновая задача: отправка приветственных сообщений через 5-10 минут после регистрации"""
    logging.info("Welcome messages task started!")
    
    while True:
        try:
            await asyncio.sleep(60)
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("""
                SELECT u.user_id, u.username
                FROM users u
                LEFT JOIN welcome_messages wm ON u.user_id = wm.user_id
                WHERE u.created_at >= NOW() - INTERVAL '10 minutes'
                  AND u.created_at <= NOW() - INTERVAL '5 minutes'
                  AND wm.user_id IS NULL
                  AND u.tariff IS NULL
            """)
            
            users = cur.fetchall()
            cur.close()
            conn.close()
            
            for user in users:
                user_id = user['user_id']
                
                try:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎥 Посмотреть примеры", callback_data="show_demo")],
                        [InlineKeyboardButton(text="⭐ 4.9/5 - Почему 87% продлевают?", callback_data="show_reviews")],
                        [InlineKeyboardButton(text="🎁 Начать пробный период", callback_data="ready_for_trial")]
                    ])
                    
                    await bot.send_message(
                        user_id,
                        "👋 Я вижу ты заинтересовался нашим клубом!\n\n"
                        "**Не торопись активировать trial** 😊\n\n"
                        "Сначала посмотри:\n"
                        "🎥 Видео с примерами материалов\n"
                        "💬 Отзывы других родителей\n"
                        "📚 Как это работает\n\n"
                        "А **потом решишь** - подходит тебе или нет!\n\n"
                        "💡 87% родителей после просмотра сразу начинают trial 🔥\n\n"
                        "Что хочешь посмотреть первым?",
                        reply_markup=keyboard,
                        parse_mode="Markdown"
                    )
                    
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
                    
                    track_user_action(user_id, 'received_welcome_message')
                    logging.info(f"Welcome message sent to user {user_id}")
                    
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logging.error(f"Error sending welcome to {user_id}: {e}")
            
        except Exception as e:
            logging.error(f"Error in send_welcome_messages: {e}")
            await asyncio.sleep(60)

async def remind_pending_payments():
    """🆕 Фоновая задача: напоминание о неоплаченных инвойсах"""
    logging.info("Pending payments reminder task started!")
    
    while True:
        try:
            await asyncio.sleep(300)  # Проверка каждые 5 минут
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Ищем платежи pending старше 1 часа
            cur.execute('''
                SELECT DISTINCT p.user_id, p.payment_id, p.tariff, p.amount
                FROM payments p
                WHERE p.status = 'pending'
                  AND p.created_at < NOW() - INTERVAL '1 hour'
                  AND p.created_at > NOW() - INTERVAL '2 hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM funnel_messages fm
                      WHERE fm.user_id = p.user_id
                        AND fm.message_type = 'pending_reminder'
                        AND fm.sent_at > NOW() - INTERVAL '24 hours'
                  )
            ''')
            
            pending_users = cur.fetchall()
            cur.close()
            conn.close()
            
            for payment in pending_users:
                user_id = payment['user_id']
                tariff = payment['tariff']
                amount = payment['amount']
                
                try:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💳 Попробовать снова", callback_data=tariff)],
                        [InlineKeyboardButton(text="❓ Проблемы с оплатой?", url="https://t.me/razvitie_dety")]
                    ])
                    
                    success = await send_safe_funnel_message(
                        user_id,
                        "👋 Заметил что оплата не прошла\n\n"
                        "Возможно возникли сложности?\n\n"
                        "💡 **Частые проблемы:**\n"
                        "• Не хватает денег на карте\n"
                        "• Карта заблокирована для онлайн-покупок\n"
                        "• Не пришёл SMS с кодом\n"
                        "• Ошибка банка\n\n"
                        "Могу помочь разобраться! 😊\n\n"
                        "Или попробуй оплатить снова - "
                        "иногда помогает:",
                        reply_markup=keyboard
                    )
                    
                    if success:
                        mark_funnel_message_sent(user_id, 'pending_reminder')
                        logging.info(f"Sent pending reminder to user {user_id}")
                    
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logging.error(f"Error sending pending reminder to {user_id}: {e}")
            
        except Exception as e:
            logging.error(f"Error in remind_pending_payments: {e}")
            await asyncio.sleep(300)

# ========================================
# КОМАНДЫ И ОБРАБОТЧИКИ
# ========================================

class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    confirm = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start с воронкой прогрева"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    user = get_user(user_id)
    
    if not user:
        # НОВЫЙ пользователь - показываем ВОРОНКУ ПРОГРЕВА
        track_user_action(user_id, 'started_bot')
        
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}!\n\n"
            "Добро пожаловать в клуб развивающих материалов для детей!\n\n"
            "🎯 **Что у нас есть:**\n"
            "• 1000+ готовых занятий и игр\n"
            "• Материалы обновляются каждую неделю\n"
            "• Всё разделено по возрастам и навыкам\n"
            "• 87% родителей продлевают подписку после пробного периода\n\n"
            "💡 **Сначала посмотри примеры - потом решишь попробовать!**\n\n"
            "👇 Что хочешь узнать первым?",
            reply_markup=get_new_user_menu(),
            parse_mode="Markdown"
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

@dp.callback_query(F.data == "show_demo")
async def show_demo_content(callback: types.CallbackQuery):
    """Показать примеры материалов ПЕРЕД активацией trial"""
    track_user_action(callback.from_user.id, 'viewed_demo')
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎥 Видео-обзор материалов", url=DEMO_VIDEO_URL)],
        [InlineKeyboardButton(text="🎨 Примеры заданий", url=DEMO_PHOTOS_URL)],
        [InlineKeyboardButton(text="📚 Как это работает?", callback_data="how_it_works")],
        [InlineKeyboardButton(text="🔥 Хочу попробовать!", callback_data="ready_for_trial")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    
    await callback.message.edit_text(
        "🎨 **ПРИМЕРЫ НАШИХ МАТЕРИАЛОВ:**\n\n"
        "Посмотри что получают родители внутри клуба:\n\n"
        "🎯 **Для детей 3-5 лет:**\n"
        "• Игры на развитие внимания и памяти\n"
        "• Подготовка руки к письму\n"
        "• Изучение цветов, форм, размеров\n"
        "• Развитие речи через игру\n\n"
        "🎯 **Для детей 5-7 лет:**\n"
        "• Обучение чтению по слогам\n"
        "• Математика в игровой форме\n"
        "• Логические задачки\n"
        "• Подготовка к школе\n\n"
        "📹 **Смотри видео** - там показаны реальные материалы!\n\n"
        "💡 Всё это доступно в закрытой группе 24/7",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "show_reviews")
async def show_reviews(callback: types.CallbackQuery):
    """Показать РЕАЛЬНЫЕ отзывы родителей"""
    track_user_action(callback.from_user.id, 'viewed_reviews')
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Больше отзывов в канале", url=REVIEWS_URL)],
        [InlineKeyboardButton(text="🔥 Убедили! Хочу попробовать", callback_data="ready_for_trial")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    
    await callback.message.edit_text(
        "💬 **ЧТО ГОВОРЯТ РОДИТЕЛИ:**\n\n"
        "**Анна, 2 ребенка (4 и 6 лет):**\n"
        "_\"За неделю дочка выучила 10 букв! Занимаемся по 15 минут утром. "
        "Материалы яркие, ребенок сам просит позаниматься!\"_\n\n"
        "**Олег, сын 5 лет:**\n"
        "_\"Раньше тратил 2-3 часа на поиск заданий в интернете. "
        "Теперь всё в одном месте. Окупилось за первую неделю!\"_\n\n"
        "**Мария, дочка 3 года:**\n"
        "_\"Попробовали trial - не смогли остановиться. "
        "Оформили Навсегда со скидкой. Лучшее вложение в ребенка!\"_\n\n"
        "📊 **Наши цифры:**\n"
        "• 87% родителей продлевают после trial\n"
        "• 1000+ активных семей\n"
        "• 5000+ материалов в базе\n"
        "• 4.9/5 средняя оценка\n\n"
        "🎁 Попробуй сам - первые 7 дней бесплатно!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "ready_for_trial")
async def ready_for_trial(callback: types.CallbackQuery):
    """Пользователь ГОТОВ активировать trial - объясняем процесс"""
    track_user_action(callback.from_user.id, 'clicked_ready_for_trial')
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активировать Trial", callback_data="trial")],
        [InlineKeyboardButton(text="❓ У меня вопросы", callback_data="faq")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    
    await callback.message.edit_text(
        "🎁 **КАК ПОЛУЧИТЬ БЕСПЛАТНЫЙ ДОСТУП:**\n\n"
        "**Шаг 1:** Нажми кнопку \"Активировать Trial\"\n"
        "🎫 Получишь МГНОВЕННЫЙ доступ на 7 дней\n\n"
        "**Шаг 2:** Перейди по ссылке в группу\n"
        "🔗 Начинай заниматься с ребёнком!\n\n"
        "⏰ **ВАЖНО:**\n"
        "• Первые 7 дней - **полностью БЕСПЛАТНО**\n"
        "• Никаких платежей и карт\n"
        "• Отменить можно в любой момент\n\n"
        "🎯 **После trial (если понравится):**\n"
        "Сможешь продлить со **скидкой 60-80%**:\n"
        "• 1 месяц: 199₽ (вместо 499₽)\n"
        "• Навсегда: 599₽ (вместо 2990₽)\n\n"
        "💡 **Попробуй без риска - тебе понравится!**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery):
    """Вернуться к начальному меню"""
    await callback.message.edit_text(
        f"👋 Привет, {callback.from_user.first_name}!\n\n"
        "Добро пожаловать в клуб развивающих материалов для детей!\n\n"
        "🎯 **Что у нас есть:**\n"
        "• 1000+ готовых занятий и игр\n"
        "• Материалы обновляются каждую неделю\n"
        "• Всё разделено по возрастам и навыкам\n"
        "• 87% родителей продлевают подписку\n\n"
        "💡 **Сначала посмотри примеры - потом решишь!**\n\n"
        "👇 Что хочешь узнать первым?",
        reply_markup=get_new_user_menu(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "trial")
async def process_trial(callback: types.CallbackQuery):
    """Обработчик кнопки 'Попробовать бесплатно'"""
    user_id = callback.from_user.id
    username = callback.from_user.username
    
    user = get_user(user_id)
    
    if user:
        await callback.answer(
            "Вы уже использовали пробный период! 😊",
            show_alert=True
        )
        return
    
    add_user(user_id, username, TARIFFS['trial']['days'], 'trial')
    track_user_action(user_id, 'activated_trial')
    
    try:
        invite_link = await bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1,
            expire_date=datetime.now() + timedelta(days=TARIFFS['trial']['days'])
        )
        
        await callback.message.edit_text(
            f"🎉 **Поздравляем!**\n\n"
            f"Вам активирован пробный период на {TARIFFS['trial']['days']} дней!\n\n"
            f"**ВАЖНО: Сохрани эту ссылку!**\n\n"
            f"Переходи по ссылке: {invite_link.invite_link}\n\n"
            f"⏰ Доступ истечет через {TARIFFS['trial']['days']} дней.\n"
            f"После этого выбери подходящий тариф!\n\n"
            f"💡 Это ссылка для присоединения к закрытой группе.",
            parse_mode="HTML"
        )
        
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error adding user to channel: {e}")
        await callback.message.edit_text(
            "❌ Произошла ошибка. Обратитесь к администратору.",
            reply_markup=get_main_menu()
        )
    
    await callback.answer()

@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs(callback: types.CallbackQuery):
    """Показать список тарифов"""
    track_user_action(callback.from_user.id, 'viewed_tariffs')
    
    await callback.message.edit_text(
        "📋 **Выберите подходящую подписку:**\n\n"
        "💎 **1 месяц - 199₽**\n"
        "~~499₽~~ → Скидка 60%!\n"
        "• Идеально чтобы протестировать\n"
        "• Самый популярный выбор\n\n"
        "🔥 **НАВСЕГДА - 599₽**\n"
        "~~2990₽~~ → Скидка 80%!\n"
        "• Разовый платеж - больше не платишь\n"
        "• Лучшая цена!\n"
        "• Доступ без ограничений\n\n"
        "⚡️ **Специальные цены только для вас!**",
        reply_markup=get_tariffs_menu(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == '1month')
async def process_1month_tariff(callback: types.CallbackQuery):
    """Обработка выбора тарифа 1 месяц"""
    user_id = callback.from_user.id
    tariff_code = '1month'
    tariff = TARIFFS[tariff_code]
    
    track_user_action(user_id, f'selected_tariff_{tariff_code}')
    
    await callback.answer("⏳ Отправляю счёт на оплату...", show_alert=False)
    
    success = await send_invoice(user_id, tariff_code)
    
    if success:
        await callback.message.answer(
            f"📋 **Счёт на оплату отправлен!**\n\n"
            f"📦 Тариф: {tariff['name']}\n"
            f"💰 К оплате: **{tariff['price']}₽**\n\n"
            f"👆 Нажмите на счёт выше для оплаты\n\n"
            f"💳 Принимаем все российские карты 🇷🇺\n\n"
            f"✅ После оплаты доступ откроется **АВТОМАТИЧЕСКИ**!",
            parse_mode="Markdown"
        )
    else:
        await callback.message.answer(
            "❌ Ошибка создания счёта. Попробуйте позже.",
            reply_markup=get_main_menu()
        )

@dp.callback_query(F.data == 'forever')
async def process_forever_tariff(callback: types.CallbackQuery):
    """🆕 Обработка выбора Forever - С КАЛЬКУЛЯТОРОМ"""
    user_id = callback.from_user.id
    track_user_action(user_id, 'selected_tariff_forever')
    
    # 🆕 СНАЧАЛА ПОКАЗЫВАЕМ КАЛЬКУЛЯТОР
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 599₽", callback_data="forever_confirmed")],
        [InlineKeyboardButton(text="📊 Сравнить с 1 месяцем", callback_data="compare_tariffs")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="show_tariffs")]
    ])
    
    await callback.message.edit_text(
        "🔥 **НАВСЕГДА - 599₽**\n\n"
        "💡 **МАТЕМАТИКА:**\n\n"
        
        "**Вариант А (по месяцам):**\n"
        "• Месяц 1: 199₽\n"
        "• Месяц 2: 199₽\n"
        "• Месяц 3: 199₽\n"
        "• Месяц 4: 199₽\n"
        "━━━━━━━━━━━\n"
        "**Итого за 4 месяца: 796₽**\n\n"
        
        "**Вариант Б (Forever):**\n"
        "• ОДИН платёж: 599₽\n"
        "• Больше НИКОГДА не платишь\n"
        "━━━━━━━━━━━\n"
        "**Экономия: 197₽ уже на 4й месяц!**\n\n"
        
        "📊 **Статистика:**\n"
        "• 92% используют >6 месяцев\n"
        "• Средняя экономия: 1500₽ в год\n\n"
        
        "🎯 **Окупаемость: 3 месяца**\n"
        "Всё что после - БЕСПЛАТНО!\n\n"
        
        "⚠️ **ВАЖНО:** Эта цена только для trial!\n"
        "После истечения: 2990₽\n\n"
        "Готов оформить навсегда?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == 'forever_confirmed')
async def forever_confirmed(callback: types.CallbackQuery):
    """Подтверждение Forever - отправка инвойса"""
    user_id = callback.from_user.id
    tariff_code = 'forever'
    tariff = TARIFFS[tariff_code]
    
    await callback.answer("⏳ Отправляю счёт на оплату...", show_alert=False)
    
    success = await send_invoice(user_id, tariff_code)
    
    if success:
        await callback.message.answer(
            f"📋 **Счёт на оплату отправлен!**\n\n"
            f"📦 Тариф: Навсегда\n"
            f"💰 К оплате: **599₽**\n\n"
            f"👆 Нажмите на счёт выше для оплаты\n\n"
            f"💳 Принимаем все российские карты 🇷🇺\n\n"
            f"✅ После оплаты доступ откроется **АВТОМАТИЧЕСКИ**!\n\n"
            f"🎯 Это ПОСЛЕДНИЙ раз когда платишь за доступ!",
            parse_mode="Markdown"
        )
    else:
        await callback.message.answer(
            "❌ Ошибка создания счёта. Попробуйте позже.",
            reply_markup=get_main_menu()
        )

@dp.callback_query(F.data == 'compare_tariffs')
async def compare_tariffs(callback: types.CallbackQuery):
    """🆕 Сравнение тарифов"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 1 месяц - 199₽", callback_data="1month")],
        [InlineKeyboardButton(text="🔥 НАВСЕГДА - 599₽", callback_data="forever_confirmed")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="show_tariffs")]
    ])
    
    await callback.message.edit_text(
        "📊 **СРАВНЕНИЕ ТАРИФОВ**\n\n"
        "```\n"
        "┌─────────┬────────┬──────────┐\n"
        "│ Период  │ 1 мес  │ Навсегда │\n"
        "├─────────┼────────┼──────────┤\n"
        "│ 1 мес   │  199₽  │   599₽   │\n"
        "│ 3 мес   │  597₽  │   599₽ ✅│\n"
        "│ 6 мес   │ 1194₽  │   599₽ ✅│\n"
        "│ 1 год   │ 2388₽  │   599₽ ✅│\n"
        "└─────────┴────────┴──────────┘\n"
        "```\n\n"
        
        "💡 **Вывод:**\n"
        "• Через 3 месяца Forever выгоднее!\n"
        "• Экономия за год: **1789₽**\n"
        "• Не нужно помнить о продлении\n\n"
        
        "🎯 **87% родителей выбирают Forever**\n"
        "Они понимают что это выгоднее!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

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

@dp.callback_query(F.data == "how_it_works")
async def how_it_works(callback: types.CallbackQuery):
    """Инструкция как работает бот"""
    track_user_action(callback.from_user.id, 'viewed_how_it_works')
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Попробовать сейчас", callback_data="ready_for_trial")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
    ])
    
    await callback.message.edit_text(
        "📖 **КАК ЭТО РАБОТАЕТ?**\n\n"
        "**Шаг 1:** Активация пробного периода\n"
        "Нажми кнопку и получи доступ мгновенно!\n\n"
        "**Шаг 2:** Получи ссылку на группу\n"
        "Перейди в закрытую группу с материалами\n\n"
        "**Шаг 3:** Начни заниматься!\n"
        "В группе найдешь:\n"
        "• 📚 Развивающие игры и задания\n"
        "• 🎨 Творческие активности\n"
        "• 📖 Обучающие материалы\n"
        "• 🎯 Готовые занятия на каждый день\n\n"
        "💡 **Важно:**\n"
        "• Доступ бесплатный 7 дней\n"
        "• Никакой предоплаты\n"
        "• Можно отменить в любой момент\n\n"
        "🎁 **Попробуй прямо сейчас!**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "need_help")
async def need_help(callback: types.CallbackQuery):
    """Пользователь просит помощи"""
    track_user_action(callback.from_user.id, 'requested_help')
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать в поддержку", url="https://t.me/razvitie_dety")],
        [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "💡 **Чем могу помочь?**\n\n"
        "Напиши нам в поддержку - ответим в течение 5 минут!\n\n"
        "Или посмотри частые вопросы - возможно, там уже есть ответ 👇",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.in_(['feedback_expensive', 'feedback_content', 'feedback_time', 'feedback_other', 'feedback_good']))
async def handle_feedback(callback: types.CallbackQuery):
    """Обработка обратной связи"""
    feedback_type = callback.data.replace('feedback_', '')
    track_user_action(callback.from_user.id, f'feedback_{feedback_type}')
    await callback.answer("Спасибо за обратную связь! 🙏", show_alert=True)

# ========================================
# FAQ
# ========================================

@dp.callback_query(F.data == "faq")
async def show_faq(callback: types.CallbackQuery):
    """Показать FAQ"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Как продлить подписку?", callback_data="faq_1")],
        [InlineKeyboardButton(text="2️⃣ Как узнать срок окончания подписки?", callback_data="faq_3")],
        [InlineKeyboardButton(text="3️⃣ Можно ли вернуть деньги?", callback_data="faq_4")],
        [InlineKeyboardButton(text="4️⃣ Что входит в подписку?", callback_data="faq_5")],
        [InlineKeyboardButton(text="5️⃣ Как изменить тариф?", callback_data="faq_6")],
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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**1. Как продлить подписку?**\n\n"
        "• Введите /start\n"
        "• Выберите нужный тариф\n"
        "• Оплатите удобным способом\n\n"
        "⚠️ **Важно:** Подписка продлевается вручную. "
        "Мы пришлём напоминание за 2 дня до окончания!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_3")
async def faq_answer_3(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**2. Как узнать срок окончания подписки?**\n\n"
        "Чтобы проверить свою подписку:\n\n"
        "1️⃣ Введите команду /start\n"
        "2️⃣ Нажмите кнопку \"ℹ️ Мой статус\"\n\n"
        "Вы увидите:\n"
        "• Текущий тариф\n"
        "• Дату окончания подписки\n"
        "• Количество оставшихся дней\n\n"
        "📱 Также бот отправит вам уведомление за 2 дня до окончания!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_4")
async def faq_answer_4(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**3. Можно ли вернуть деньги?**\n\n"
        "🎁 **Пробный период:**\n"
        "Воспользуйтесь бесплатным доступом на 7 дней, чтобы оценить качество материалов перед покупкой!\n\n"
        "💰 **Возврат средств:**\n"
        "Возврат возможен в течение 3 дней после оплаты, если:\n"
        "• Вы не получили доступ к материалам\n"
        "• Возникли технические проблемы\n"
        "• Контент не соответствует описанию\n\n"
        "Для оформления возврата свяжитесь с поддержкой\n\n"
        "⚠️ **Обратите внимание:**\n"
        "После использования материалов возврат не предусмотрен согласно законодательству об информационных услугах.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "faq_5")
async def faq_answer_5(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎥 Видео: Обзор материалов", url=DEMO_VIDEO_URL)],
        [InlineKeyboardButton(text="🎥 Примеры заданий", url=DEMO_PHOTOS_URL)],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**4. Что входит в подписку?**\n\n"
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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Посмотреть тарифы", callback_data="show_tariffs")],
        [InlineKeyboardButton(text="◀️ К вопросам", callback_data="faq")]
    ])
    
    await callback.message.edit_text(
        "**5. Как изменить тариф?**\n\n"
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

@dp.message(Command("faq"))
async def cmd_faq(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Как продлить подписку?", callback_data="faq_1")],
        [InlineKeyboardButton(text="2️⃣ Как узнать срок окончания подписки?", callback_data="faq_3")],
        [InlineKeyboardButton(text="3️⃣ Можно ли вернуть деньги?", callback_data="faq_4")],
        [InlineKeyboardButton(text="4️⃣ Что входит в подписку?", callback_data="faq_5")],
        [InlineKeyboardButton(text="5️⃣ Как изменить тариф?", callback_data="faq_6")],
        [InlineKeyboardButton(text="💬 Связаться с поддержкой", url="https://t.me/razvitie_dety")]
    ])
    
    await message.answer(
        "❓ **Часто задаваемые вопросы**\n\n"
        "Выберите интересующий вас вопрос:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ========================================
# АДМИНСКИЕ КОМАНДЫ
# ========================================

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    """Начать рассылку по активным подписчикам"""
    if message.from_user.id != ADMIN_ID:
        return
    
    active_users = get_active_subscribers()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всем активным", callback_data="broadcast_active")],
        [InlineKeyboardButton(text="🎁 Только Trial", callback_data="broadcast_trial")],
        [InlineKeyboardButton(text="💳 Только платным", callback_data="broadcast_paid")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")]
    ])
    
    await message.answer(
        f"📢 **СИСТЕМА РАССЫЛКИ**\n\n"
        f"👥 Активных подписчиков: {len(active_users)}\n\n"
        f"Выбери кому отправить:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    
    await state.set_state(BroadcastStates.waiting_for_message)

@dp.callback_query(F.data.startswith("broadcast_"))
async def select_broadcast_type(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа рассылки"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    action = callback.data.replace("broadcast_", "")
    
    if action == "cancel":
        await callback.message.edit_text("❌ Рассылка отменена")
        await state.clear()
        return
    
    await state.update_data(broadcast_type=action)
    
    await callback.message.edit_text(
        "✍️ **Напиши текст сообщения для рассылки:**\n\n"
        "Можешь использовать форматирование Markdown\n\n"
        "💡 Для отмены отправь /cancel",
        parse_mode="Markdown"
    )
    
    await callback.answer()

@dp.message(BroadcastStates.waiting_for_message)
async def receive_broadcast_message(message: types.Message, state: FSMContext):
    """Получение текста рассылки"""
    if message.from_user.id != ADMIN_ID:
        return
    
    if message.text == "/cancel":
        await message.answer("❌ Рассылка отменена")
        await state.clear()
        return
    
    await state.update_data(message_text=message.text)
    data = await state.get_data()
    broadcast_type = data.get('broadcast_type', 'active')
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if broadcast_type == "active":
        cur.execute('''SELECT COUNT(*) as count FROM users 
                       WHERE subscription_until > %s''', (datetime.now(),))
    elif broadcast_type == "trial":
        cur.execute('''SELECT COUNT(*) as count FROM users 
                       WHERE subscription_until > %s AND tariff = %s''', 
                    (datetime.now(), 'trial'))
    else:
        cur.execute('''SELECT COUNT(*) as count FROM users 
                       WHERE subscription_until > %s AND tariff != %s''', 
                    (datetime.now(), 'trial'))
    
    count = cur.fetchone()['count']
    cur.close()
    conn.close()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_broadcast")]
    ])
    
    type_names = {
        'active': 'Всем активным',
        'trial': 'Trial пользователям',
        'paid': 'Платным подписчикам'
    }
    
    await message.answer(
        f"📋 **ПРЕВЬЮ РАССЫЛКИ**\n\n"
        f"👥 Получателей: {count}\n"
        f"📢 Тип: {type_names.get(broadcast_type, 'Всем')}\n\n"
        f"📝 **Текст сообщения:**\n"
        f"{'─' * 30}\n"
        f"{message.text}\n"
        f"{'─' * 30}\n\n"
        f"⚠️ Отправить рассылку?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    
    await state.set_state(BroadcastStates.confirm)

@dp.callback_query(F.data == "confirm_broadcast", BroadcastStates.confirm)
async def execute_broadcast(callback: types.CallbackQuery, state: FSMContext):
    """Выполнение рассылки"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    data = await state.get_data()
    message_text = data.get('message_text')
    broadcast_type = data.get('broadcast_type', 'active')
    
    await callback.message.edit_text("⏳ Начинаю рассылку...")
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    if broadcast_type == "active":
        cur.execute('''SELECT user_id, username FROM users 
                       WHERE subscription_until > %s''', (datetime.now(),))
    elif broadcast_type == "trial":
        cur.execute('''SELECT user_id, username FROM users 
                       WHERE subscription_until > %s AND tariff = %s''', 
                    (datetime.now(), 'trial'))
    else:
        cur.execute('''SELECT user_id, username FROM users 
                       WHERE subscription_until > %s AND tariff != %s''', 
                    (datetime.now(), 'trial'))
    
    users = cur.fetchall()
    cur.close()
    conn.close()
    
    sent = 0
    blocked = 0
    errors = 0
    
    for user in users:
        try:
            await bot.send_message(user['user_id'], message_text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            if "bot was blocked" in str(e) or "Forbidden" in str(e):
                blocked += 1
            else:
                errors += 1
                logging.error(f"Broadcast error for {user['user_id']}: {e}")
    
    await callback.message.answer(
        f"✅ **РАССЫЛКА ЗАВЕРШЕНА**\n\n"
        f"📊 Статистика:\n"
        f"• Отправлено: {sent}\n"
        f"• Заблокировали бота: {blocked}\n"
        f"• Ошибки: {errors}\n"
        f"• Всего получателей: {len(users)}\n\n"
        f"📈 Успешность: {round(100 * sent / len(users), 1)}%",
        parse_mode="Markdown"
    )
    
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "cancel_broadcast", BroadcastStates.confirm)
async def cancel_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Рассылка отменена")
    await state.clear()
    await callback.answer()

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
    
    cur.execute('''SELECT action, COUNT(*) as count 
                   FROM funnel_analytics 
                   WHERE created_at >= NOW() - INTERVAL '7 days'
                   GROUP BY action''')
    funnel_stats = cur.fetchall()
    
    cur.close()
    conn.close()
    
    stats_text = f"""📊 **Статистика бота**

👥 Всего пользователей: {total_users}
✅ Активных подписок: {active_users}
💰 Общий доход: {total_revenue}₽
⏳ Ожидают оплаты: {pending_payments}

📈 **Воронка прогрева (7 дней):**
"""
    
    for stat in funnel_stats:
        stats_text += f"• {stat['action']}: {stat['count']}\n"
    
    await message.answer(stats_text, parse_mode="HTML")

@dp.message(Command("cleardb"))
async def admin_clear_db(message: types.Message):
    """Очистка базы данных (только для админа)"""
    if message.from_user.id != ADMIN_ID:
        return
    
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
        "• Все уведомления\n"
        "• Всю аналитику\n\n"
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
        
        tables_cleared = []
        
        for table in ['notifications', 'payments', 'users', 'funnel_analytics', 'welcome_messages', 'funnel_messages']:
            try:
                cur.execute(f'DELETE FROM {table}')
                tables_cleared.append(table)
            except Exception as e:
                logging.warning(f"Error clearing {table}: {e}")
        
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
    await callback.message.edit_text("✅ Очистка отменена. База данных не изменена.")
    await callback.answer()

@dp.message(Command("checkdb"))
async def admin_check_db(message: types.Message):
    """Диагностика базы данных"""
    if message.from_user.id != ADMIN_ID:
        return
    
    await message.answer("🔍 Анализирую базу данных...")
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute('SELECT COUNT(*) as total FROM users')
        total = cur.fetchone()['total']
        
        cur.execute('SELECT COUNT(DISTINCT user_id) as unique_users FROM users')
        unique = cur.fetchone()['unique_users']
        
        cur.execute('''
            SELECT 
                COUNT(*) FILTER (WHERE subscription_until > NOW()) as active,
                COUNT(*) FILTER (WHERE subscription_until <= NOW()) as expired,
                COUNT(*) FILTER (WHERE tariff = 'trial') as trial,
                COUNT(*) FILTER (WHERE tariff != 'trial') as paid
            FROM users
        ''')
        subs = cur.fetchone()
        
        cur.execute('SELECT NOW() as db_time')
        db_time = cur.fetchone()['db_time']
        
        cur.close()
        conn.close()
        
        report = "🔍 **ДЕТАЛЬНАЯ ДИАГНОСТИКА**\n\n"
        report += "📊 **Записи в базе:**\n"
        report += f"• Всего записей: {total}\n"
        report += f"• Уникальных user_id: {unique}\n\n"
        report += "💎 **Статус подписок:**\n"
        report += f"• Активные: {subs['active']}\n"
        report += f"• Истёкшие: {subs['expired']}\n"
        report += f"• Trial: {subs['trial']}\n"
        report += f"• Платные: {subs['paid']}\n\n"
        report += f"🕐 **Время БД:** {db_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        
        await message.answer(report, parse_mode="Markdown")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка:\n{str(e)}")

# ========================================
# ЗАПУСК БОТА
# ========================================

async def main():
    init_db()
    feedback_broadcast.init_feedback_system(dp, bot, ADMIN_ID, get_db_connection)
    logging.info("🚀 Bot started successfully with Telegram Payments!")
    
    asyncio.create_task(check_and_remove_expired())
    asyncio.create_task(sales_funnel())
    asyncio.create_task(send_welcome_messages())
    asyncio.create_task(remind_pending_payments())
    
    while True:
        try:
            logging.info("Starting polling...")
            await dp.start_polling(bot, timeout=30, request_timeout=20)
        except Exception as e:
            logging.error(f"Polling crashed: {e}")
            logging.info("Restarting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == '__main__':
    asyncio.run(main())
