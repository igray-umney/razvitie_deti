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
        [InlineKeyboardButton(text="📅 1 месяц - 380₽ / 190₽ (скидка 50%)", callback_data="1month")],
        [InlineKeyboardButton(text="📅 3 месяца - 1140₽ / 450₽ (скидка ~61%)", callback_data="3months")],
        [InlineKeyboardButton(text="📅 6 месяцев - 2280₽ / 690₽ (скидка ~70%)", callback_data="6months")],
        [InlineKeyboardButton(text="♾️ Навсегда - 4560₽ / 900₽ (скидка ~80%)", callback_data="forever")],
        [InlineKeyboardButton(text="ℹ️ Мой статус", callback_data="status")]
    ])
    return keyboard

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = f"""
👋 Привет, {message.from_user.first_name}!

Добро пожаловать в бот закрытой группы с развивающими материалами для детей! 

🎁 **Попробуй бесплатно 2 дня!**

После пробного периода выбери удобный тариф:
• 1 месяц - ~~380₽~~ **190₽** (скидка 50%!)
• 3 месяца - ~~1140₽~~ **450₽** (скидка ~61%!)
• 6 месяцев - ~~2280₽~~ **690₽** (скидка ~70%!)
• Навсегда - ~~4560₽~~ **900₽** (скидка ~80%!)

Выбери вариант ниже 👇
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
        f"💰 Полная цена: ~~{tariff['old_price']}₽~~\n"
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
        "Выберите подходящий тариф:",
        reply_markup=get_main_menu()
    )
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

async def main():
    init_db()
    logging.info("Bot started successfully!")
    
    # Запускаем фоновую задачу проверки подписок
    asyncio.create_task(check_and_remove_expired())
    
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
