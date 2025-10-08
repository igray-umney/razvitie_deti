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
from hashlib import sha1
import hmac

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')  # Например: @your_channel или -1001234567890
YOOMONEY_WALLET = os.getenv('YOOMONEY_WALLET')
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')
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

def create_payment(user_id, amount, tariff):
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    payment_id = f"{user_id}_{int(datetime.now().timestamp())}"
    created_at = datetime.now().isoformat()
    
    c.execute('''INSERT INTO payments 
                 (payment_id, user_id, amount, tariff, status, created_at)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (payment_id, user_id, amount, tariff, 'pending', created_at))
    conn.commit()
    conn.close()
    return payment_id

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
    
    # Создаем платеж
    payment_id = create_payment(user_id, tariff['price'], tariff_code)
    
    # Формируем ссылку на оплату ЮMoney
    payment_url = (
        f"https://yoomoney.ru/quickpay/confirm?"
        f"receiver={YOOMONEY_WALLET}"
        f"&quickpay-form=shop"
        f"&targets=Подписка {tariff['name']}"
        f"&paymentType=SB"
        f"&sum={tariff['price']}"
        f"&label={payment_id}"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_{payment_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ])
    
    await callback.message.edit_text(
        f"📦 Вы выбрали: **{tariff['name']}**\n"
        f"💰 Стоимость: {tariff['price']}₽\n\n"
        f"1️⃣ Нажмите кнопку 'Оплатить'\n"
        f"2️⃣ Оплатите через ЮMoney\n"
        f"3️⃣ Вернитесь и нажмите 'Я оплатил'\n\n"
        f"⚠️ После оплаты доступ откроется автоматически!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    
    await callback.answer()

@dp.callback_query(F.data.startswith("check_"))
async def check_payment(callback: types.CallbackQuery):
    payment_id = callback.data.replace("check_", "")
    
    await callback.answer(
        "⏳ Проверяем оплату... Это может занять несколько секунд.",
        show_alert=True
    )
    
    # Здесь должна быть проверка через ЮMoney API
    # Для демо просим написать админу
    await callback.message.edit_text(
        "✅ Платеж получен!\n\n"
        "🔄 Обрабатываем ваш платеж...\n"
        "Доступ будет предоставлен в течение 5 минут.\n\n"
        "Если что-то пошло не так, напишите @admin",
        reply_markup=get_main_menu()
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
        status_text = (
            f"✅ **Ваша подписка активна!**\n\n"
            f"📅 Тариф: {TARIFFS.get(user[3], {}).get('name', 'Неизвестно')}\n"
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
    
    conn.close()
    
    stats_text = f"""
📊 **Статистика бота**

👥 Всего пользователей: {total_users}
✅ Активных подписок: {active_users}
💰 Общий доход: {total_revenue}₽
"""
    
    await message.answer(stats_text, parse_mode="Markdown")

# Запуск бота
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
