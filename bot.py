async def sales_funnel():
    """Воронка продаж - автоматические сообщения (УСКОРЕННАЯ ДЛЯ ТЕСТИРОВАНИЯ)"""
    while True:
        try:
            logging.info("Running sales funnel check...")
            
            # Получаем пользователей с пробным периодом
            trial_users = get_trial_users_for_funnel()
            
            for user in trial_users:
                user_id = user['user_id']
                created_at = user['created_at']
                subscription_until = user['subscription_until']
                
                # ТЕСТОВЫЙ РЕЖИМ: минуты вместо часов
                minutes_since_start = (datetime.now() - created_at).total_seconds() / 60
                minutes_until_end = (subscription_until - datetime.now()).total_seconds() / 60
                
                try:
                    # Приветствие через 1 минуту (вместо 5 минут)
                    if 1 <= minutes_since_start < 2:
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
                            logging.info(f"✅ Sent 'welcome' to user {user_id}")
                    
                    # ДЕНЬ 1: Утро (через 3 минуты вместо 18 часов)
                    if 3 <= minutes_since_start < 4:
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
                            logging.info(f"✅ Sent 'day1_morning' to user {user_id}")
                    
                    # ДЕНЬ 1: Вечер (через 5 минут вместо 28 часов)
                    if 5 <= minutes_since_start < 6:
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
                            logging.info(f"✅ Sent 'day1_evening' to user {user_id}")
                    
                    # ДЕНЬ 2: За 8 часов до конца (через 7 минут вместо за 8 часов до конца)
                    if 7 <= minutes_since_start < 8:
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
                            logging.info(f"✅ Sent 'day2_8hours' to user {user_id}")
                    
                    # ДЕНЬ 2: За 2 часа до конца (через 9 минут вместо за 2 часа до конца)
                    if 9 <= minutes_since_start < 10:
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
                            logging.info(f"✅ Sent 'day2_2hours' to user {user_id}")
                
                except Exception as e:
                    logging.error(f"Error sending funnel message to {user_id}: {e}")
            
            # Обработка пользователей с истекшим пробным периодом
            expired_users = get_expired_trial_users()
            
            for user in expired_users:
                user_id = user['user_id']
                subscription_until = user['subscription_until']
                minutes_since_expired = (datetime.now() - subscription_until).total_seconds() / 60
                
                try:
                    # Сразу после истечения (через 1 минуту вместо 0-2 часа)
                    if 1 <= minutes_since_expired < 2:
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
                            logging.info(f"✅ Sent 'expired_immediate' to user {user_id}")
                    
                    # ДЕНЬ 3 (через 5 минут вместо 24 часа после истечения)
                    if 5 <= minutes_since_expired < 6:
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
                            logging.info(f"✅ Sent 'expired_day3' to user {user_id}")
                    
                    # ДЕНЬ 5 (через 10 минут вместо 72 часа)
                    if 10 <= minutes_since_expired < 11:
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
                            logging.info(f"✅ Sent 'expired_day5' to user {user_id}")
                
                except Exception as e:
                    logging.error(f"Error sending expired funnel message to {user_id}: {e}")
            
            # Проверяем каждые 30 секунд (вместо 30 минут)
            await asyncio.sleep(30)
            
        except Exception as e:
            logging.error(f"Error in sales funnel: {e}")
            await asyncio.sleep(30)
