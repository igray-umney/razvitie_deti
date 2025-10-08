import os
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import sqlite3
import aiohttp
import uuid
import base64

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')  # Например: @your_channel или -1001234567890
YOOKASSA_SHOP_ID = os.getenv('YOOKASSA_SHOP_ID')  # 1119525
YOOKASSA_SECRET_KEY = os.getenv('YOOKASSA_SECRET_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))  # Твой Telegram ID

# Тарифы
TARIFFS = {
    'trial': {'name': 'Пробный период', 'days': 2, 'price': 0},
    '1month': {'name': '1 месяц (скидка 50%)', 'days': 30, 'price': 95},
    '3months': {'name': '3 месяца', 'days': 90, 'price': 490},
    '6months': {'name': '6 месяцев', 'days': 180, 'price': 890},
    'forever': {'name': 'Навсегда', 'days': 36500, 'price': 1990}
}

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# База данных
def init_db():
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  subscription_until TEXT,
                  tariff TEXT,
                  created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments
                 (payment_id TEXT PRIMARY KEY,
                  user_id INTEGER,
                  amount REAL,
                  tariff TEXT,
                  status TEXT,
                  yookassa_id TEXT,
                  created_at TEXT)''')
    conn.commit()
    conn.close()

def add_user(user_id, username, days, tariff):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    subscription_until = (datetime.now() + timedelta(days=days)).isoformat()
    created_at = datetime.now().isoformat()
    
    c.execute('''INSERT OR REPLACE INTO users 
                 (user_id, username, subscription_until, tariff, created_at)
                 VALUES (?, ?, ?, ?, ?)''',
              (user_id, username, subscription_until, tariff, created_at))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def is_subscription_active(user_id):
    user = get_user(user_id)
    if not user:
        return False
    subscription_until = datetime.fromisoformat(user[2])
    return datetime.now() < subscription_until

def create_payment(user_id, amount, tariff, yookassa_id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    payment_id = f"{user_id}_{int(datetime.now().timestamp())}"
    created_at = datetime.now().isoformat()
    
    c.execute('''INSERT INTO payments 
                 (payment_id, user_id, amount, tariff, status, yookassa_id, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (payment_id, user_id, amount, tariff, 'pending', yookassa_id, created_at))
    conn.commit()
    conn.close()
    return payment_id

def update_payment_status(yookassa_id, status):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('UPDATE payments SET status = ? WHERE yookassa_id = ?', (status, yookassa_id))
    conn.commit()
    conn.close()

def get_payment_by_yookassa_id(yookassa_id):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    c.execute('SELECT * FROM payments WHERE yookassa_id = ?', (yookassa_id,))
    payment = c.fetchone()
    conn.close()
    return payment

# ЮKassa API
async def create_yookassa_payment(amount, description, user_id):
    """Создание платежа в ЮKassa"""
    url = "https://api.yookassa.ru/v3/payments"
    
    # Создаем idempotence key для безопасности
    idempotence_key = str(uuid.uuid4())
    
    # Базовая авторизация
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
        [InlineKeyboardButton(text="📅 1 месяц - 95₽ (скидка 50%)", callback_data="1month")],
        [InlineKeyboardButton(text="📅 3 месяца - 490₽", callback_data="3months")],
        [InlineKeyboardButton(text="📅 6 месяцев - 890₽", callback_data="6months")],
        [InlineKeyboardButton(text="♾️ Навсегда - 1990₽", callback_data="forever")],
        [InlineKeyboardButton(text="ℹ️ Мой статус", callback_data="status")]
    ])
    return keyboard

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "unknown"
    
    welcome_text = f"""
👋 Привет, {message.from_user.first_name}!

Добро пожаловать в бот закрытого канала с развивающими материалами для детей! 

🎁 **Попробуй бесплатно 2 дня!**

После пробного периода выбери удобный тариф:
• 1 месяц - 95₽ (скидка 50%!)
• 3 месяца - 490₽
• 6 месяцев - 890₽
• Навсегда - 1990₽

Выбери вариант ниже 👇
"""
    
    await message.answer(welcome_text, reply_markup=get_main_menu())

@dp.callback_query(F.data == "trial")
async def process_trial(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or "unknown"
    
    # Проверяем, не использовал ли уже пробный период
    user = get_user(user_id)
    if user:
        await callback.answer("❌ Вы уже использовали пробный период!", show_alert=True)
        return
    
    # Добавляем пользователя с пробным доступом
    add_user(user_id, username, TARIFFS['trial']['days'], 'trial')
    
    try:
        # Добавляем в канал
        invite_link = await bot.create_chat_invite_link(
            CHANNEL_ID,
            member_limit=1,
            expire_date=datetime.now() + timedelta(days=2)
        )
        
        await callback.message.edit_text(
            f"🎉 Отлично! Ты получил пробный доступ на 2 дня!\n\n"
            f"Переходи по ссылке: {invite_link.invite_link}\n\n"
            f"⏰ Доступ истечет через 2 дня.\n"
            f"После этого выбери подходящий тариф!",
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
    
    # Создаем платеж в ЮKassa
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
    
    # Сохраняем платеж в БД
    payment_id = create_payment(user_id, tariff['price'], tariff_code, payment['id'])
    
    # Получаем ссылку на оплату
    confirmation_url = payment['confirmation']['confirmation_url']
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=confirmation_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_{payment['id']}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    
    await callback.message.edit_text(
        f"📦 Вы выбрали: **{tariff['name']}**\n"
        f"💰 Стоимость: {tariff['price']}₽\n\n"
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
    
    # Проверяем статус в ЮKassa
    payment_info = await check_yookassa_payment(yookassa_payment_id)
    
    if not payment_info:
        await callback.answer("❌ Ошибка проверки платежа", show_alert=True)
        return
    
    status = payment_info.get('status')
    
    if status == 'succeeded':
        # Платеж успешен!
        payment = get_payment_by_yookassa_id(yookassa_payment_id)
        if payment:
            user_id = payment[1]
            tariff_code = payment[3]
            tariff = TARIFFS[tariff_code]
            username = callback.from_user.username or "unknown"
            
            # Обновляем статус платежа
            update_payment_status(yookassa_payment_id, 'completed')
            
            # Добавляем пользователя с подпиской
            add_user(user_id, username, tariff['days'], tariff_code)
            
            try:
                # Создаем инвайт в канал
                if tariff_code == 'forever':
                    # Для навсегда - без ограничения времени
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
                    f"Переходите в канал: {invite_link.invite_link}",
                    reply_markup=get_main_menu(),
                    parse_mode="Markdown"
                )
                
                # Уведомление админу
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
    elif status == 'waiting_for_capture':
        await callback.answer(
            "⏳ Ожидаем подтверждения оплаты...",
            show_alert=True
        )
    else:
        await callback.answer(
            f"❌ Статус платежа: {status}. Попробуйте снова или обратитесь к поддержке.",
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
    
    subscription_until = datetime.fromisoformat(user[2])
    is_active = datetime.now() < subscription_until
    
    if is_active:
        days_left = (subscription_until - datetime.now()).days
        tariff_info = TARIFFS.get(user[3], {})
        
        if user[3] == 'forever':
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

# Админ команды
@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) FROM users')
    total_users = c.fetchone()[0]
    
    c.execute('SELECT COUNT(*) FROM users WHERE subscription_until > ?', 
              (datetime.now().isoformat(),))
    active_users = c.fetchone()[0]
    
    c.execute('SELECT SUM(amount) FROM payments WHERE status = "completed"')
    total_revenue = c.fetchone()[0] or 0
    
    c.execute('SELECT COUNT(*) FROM payments WHERE status = "pending"')
    pending_payments = c.fetchone()[0]
    
    conn.close()
    
    stats_text = f"""
📊 **Статистика бота**

👥 Всего пользователей: {total_users}
✅ Активных подписок: {active_users}
💰 Общий доход: {total_revenue}₽
⏳ Ожидают оплаты: {pending_payments}
"""
    
    await message.answer(stats_text, parse_mode="Markdown")

# Запуск бота
async def main():
    init_db()
    logging.info("Bot started successfully!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
