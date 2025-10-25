"""
Модуль рассылки обратной связи для пользователей с истекшей подпиской
Правильная интеграция с bot.py
"""

from aiogram import types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from datetime import datetime
import asyncio
import logging

# ============================================
# ТАБЛИЦА ОБРАТНОЙ СВЯЗИ
# ============================================

def create_feedback_table(get_db_connection):
    """Создаём таблицу для хранения обратной связи"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                feedback_type TEXT NOT NULL,
                additional_text TEXT,
                promo_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
        logging.info("✅ Таблица feedback создана")
    except Exception as e:
        logging.error(f"❌ Ошибка создания таблицы feedback: {e}")

# ============================================
# ПОЛУЧЕНИЕ ПОЛЬЗОВАТЕЛЕЙ
# ============================================

def get_users_with_expired_subscription(get_db_connection):
    """Получаем пользователей у которых истекла подписка"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT user_id, username 
            FROM users 
            WHERE subscription_until < %s 
            AND subscription_until IS NOT NULL
        ''', (datetime.now(),))
        
        users = cur.fetchall()
        cur.close()
        conn.close()
        
        return users
    except Exception as e:
        logging.error(f"Ошибка получения пользователей: {e}")
        return []

# ============================================
# КЛАВИАТУРА И СООБЩЕНИЯ
# ============================================

def get_feedback_keyboard():
    """Клавиатура с вариантами обратной связи"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Слишком дорого", callback_data="fb_price")],
        [InlineKeyboardButton(text="📚 Мало материалов", callback_data="fb_content")],
        [InlineKeyboardButton(text="🗺️ Неудобная навигация", callback_data="fb_navigation")],
        [InlineKeyboardButton(text="⏰ Нужно время подумать", callback_data="fb_time")],
        [InlineKeyboardButton(text="❓ Не понял как пользоваться", callback_data="fb_unclear")],
        [InlineKeyboardButton(text="🔧 Технические проблемы", callback_data="fb_tech")],
        [InlineKeyboardButton(text="💬 Другая причина", callback_data="fb_other")]
    ])
    return keyboard

FEEDBACK_MESSAGE = """
👋 <b>Привет!</b>

Мы заметили, что ты попробовал наш клуб <b>"Развитие для детей"</b>, но пока не продлил подписку.

🙏 <b>Нам очень важно твоё мнение!</b>

Можешь рассказать, что помешало оформить подписку? Это поможет нам стать лучше!

Выбери основную причину ниже 👇

🎁 <b>За честный ответ - промокод на скидку 30%!</b>
"""

FEEDBACK_NAMES = {
    'price': '💰 Слишком дорого',
    'content': '📚 Мало материалов',
    'navigation': '🗺️ Неудобная навигация',
    'time': '⏰ Нужно время подумать',
    'unclear': '❓ Не понял как пользоваться',
    'tech': '🔧 Технические проблемы',
    'other': '💬 Другая причина'
}

# ============================================
# РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# ============================================

def register_handlers(dp, bot, ADMIN_ID, get_db_connection):
    """Регистрация всех хендлеров для обратной связи"""
    
    # Команда запуска рассылки
    @dp.message(Command("send_feedback"))
    async def cmd_send_feedback_request(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            await message.answer("❌ Эта команда доступна только администратору")
            return
        
        users = get_users_with_expired_subscription(get_db_connection)
        
        if not users:
            await message.answer("✅ Нет пользователей с истекшей подпиской")
            return
        
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, отправить", callback_data="confirm_fb_broadcast"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_fb_broadcast")
            ]
        ])
        
        await message.answer(
            f"📊 <b>Найдено пользователей с истекшей подпиской: {len(users)}</b>\n\n"
            f"Отправить им запрос обратной связи с промокодом на скидку 30%?",
            reply_markup=confirm_keyboard,
            parse_mode="HTML"
        )
    
    # Подтверждение рассылки
    @dp.callback_query(F.data == "confirm_fb_broadcast")
    async def confirm_feedback_broadcast(callback: types.CallbackQuery):
        await callback.message.edit_text("⏳ Начинаю рассылку...")
        
        users = get_users_with_expired_subscription(get_db_connection)
        success_count = 0
        error_count = 0
        
        keyboard = get_feedback_keyboard()
        
        for user in users:
            try:
                await bot.send_message(
                    chat_id=user['user_id'],
                    text=FEEDBACK_MESSAGE,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                success_count += 1
                await asyncio.sleep(0.1)
                
            except Exception as e:
                error_count += 1
                logging.error(f"Ошибка отправки пользователю {user['user_id']}: {e}")
        
        await callback.message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📤 Успешно отправлено: {success_count}\n"
            f"❌ Ошибок: {error_count}",
            parse_mode="HTML"
        )
        
        await callback.answer()
    
    # Отмена рассылки
    @dp.callback_query(F.data == "cancel_fb_broadcast")
    async def cancel_feedback_broadcast(callback: types.CallbackQuery):
        await callback.message.edit_text("❌ Рассылка отменена")
        await callback.answer()
    
    # Обработка выбора причины
    @dp.callback_query(F.data.startswith("fb_"))
    async def handle_feedback_choice(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        username = callback.from_user.username or "без username"
        feedback_type = callback.data.replace('fb_', '')
        
        promo_code = f"SAVE30_{user_id}"
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO feedback (user_id, username, feedback_type, promo_code, created_at)
                VALUES (%s, %s, %s, %s, %s)
            ''', (user_id, username, feedback_type, promo_code, datetime.now()))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Ошибка сохранения feedback: {e}")
        
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"📊 <b>Новый отзыв!</b>\n"
                    f"👤 @{username} (ID: {user_id})\n"
                    f"💭 {FEEDBACK_NAMES.get(feedback_type, feedback_type)}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Ошибка уведомления админа: {e}")
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Посмотреть тарифы", callback_data="back")],
            [InlineKeyboardButton(text="💬 Написать подробнее", callback_data="fb_write_more")]
        ])
        
        await callback.message.edit_text(
            f"🙏 <b>Спасибо за ваш ответ!</b>\n\n"
            f"Вы выбрали: {FEEDBACK_NAMES.get(feedback_type)}\n\n"
            f"🎁 Ваш промокод на скидку 30%:\n"
            f"<code>{promo_code}</code>\n\n"
            f"💡 Промокод действителен 7 дней!\n"
            f"Просто введите его при оплате.\n\n"
            f"P.S. Мы обязательно учтём ваше мнение и станем лучше! 💚",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
        await callback.answer("✅ Промокод отправлен!", show_alert=True)
    
    # Написать подробнее
    @dp.callback_query(F.data == "fb_write_more")
    async def write_more_feedback(callback: types.CallbackQuery, state: FSMContext):
        await callback.message.answer(
            "✍️ Напишите, пожалуйста, подробнее о причине.\n\n"
            "Что именно вам не понравилось или что можно улучшить?"
        )
        
        await state.set_state("waiting_detailed_feedback")
        await callback.answer()
    
    # Сохранение подробного отзыва
    @dp.message(StateFilter("waiting_detailed_feedback"))
    async def save_detailed_feedback(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        detailed_text = message.text
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute('''
                UPDATE feedback 
                SET additional_text = %s 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT 1
            ''', (detailed_text, user_id))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Ошибка сохранения подробного отзыва: {e}")
        
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"💬 <b>Подробный отзыв от @{message.from_user.username}:</b>\n\n"
                    f"{detailed_text}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Ошибка уведомления админа: {e}")
        
        await message.answer(
            "✅ Спасибо! Ваш отзыв сохранён.\n\n"
            "Мы обязательно всё учтём! 💚"
        )
        
        await state.clear()
    
    # Статистика
    @dp.message(Command("feedback_stats"))
    async def cmd_feedback_stats(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            return
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute('''
                SELECT feedback_type, COUNT(*) as count 
                FROM feedback 
                GROUP BY feedback_type 
                ORDER BY count DESC
            ''')
            
            stats = cur.fetchall()
            
            cur.execute('SELECT COUNT(*) as total FROM feedback')
            total = cur.fetchone()['total']
            
            cur.close()
            conn.close()
            
            if total == 0:
                await message.answer("📊 Пока нет ответов на опрос обратной связи")
                return
            
            text = "📊 <b>Статистика обратной связи</b>\n\n"
            
            for stat in stats:
                fb_type = stat['feedback_type']
                count = stat['count']
                percent = (count / total * 100) if total > 0 else 0
                name = FEEDBACK_NAMES.get(fb_type, fb_type)
                
                bar_length = int(percent / 5)
                bar = "█" * bar_length + "░" * (20 - bar_length)
                
                text += f"{name}\n{bar} {count} ({percent:.1f}%)\n\n"
            
            text += f"<b>Всего ответов: {total}</b>"
            
            await message.answer(text, parse_mode="HTML")
            
        except Exception as e:
            logging.error(f"Ошибка получения статистики: {e}")
            await message.answer(f"❌ Ошибка: {e}")
    
    # Экспорт в CSV
    @dp.message(Command("export_feedback"))
    async def cmd_export_feedback(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            return
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute('''
                SELECT user_id, username, feedback_type, additional_text, promo_code, created_at 
                FROM feedback 
                ORDER BY created_at DESC
            ''')
            
            feedbacks = cur.fetchall()
            cur.close()
            conn.close()
            
            if not feedbacks:
                await message.answer("📊 Нет данных для экспорта")
                return
            
            import csv
            from io import StringIO
            
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(['User ID', 'Username', 'Тип отзыва', 'Подробности', 'Промокод', 'Дата'])
            
            for fb in feedbacks:
                writer.writerow([
                    fb['user_id'],
                    fb['username'],
                    FEEDBACK_NAMES.get(fb['feedback_type'], fb['feedback_type']),
                    fb['additional_text'] or '-',
                    fb['promo_code'],
                    fb['created_at'].strftime('%Y-%m-%d %H:%M')
                ])
            
            output.seek(0)
            await message.answer_document(
                types.BufferedInputFile(
                    output.getvalue().encode('utf-8-sig'),
                    filename=f'feedback_{datetime.now().strftime("%Y%m%d")}.csv'
                ),
                caption="📊 Экспорт обратной связи"
            )
            
        except Exception as e:
            logging.error(f"Ошибка экспорта: {e}")
            await message.answer(f"❌ Ошибка экспорта: {e}")

# ============================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================

def init_feedback_system(dp, bot, ADMIN_ID, get_db_connection):
    """Инициализация системы обратной связи"""
    try:
        create_feedback_table(get_db_connection)
        register_handlers(dp, bot, ADMIN_ID, get_db_connection)
        logging.info("✅ Система обратной связи инициализирована")
    except Exception as e:
        logging.error(f"❌ Ошибка инициализации системы обратной связи: {e}")
