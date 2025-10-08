from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1 месяц — 390₽", callback_data="buy:month"),
    ],[
        InlineKeyboardButton(text="3 месяца — 990₽", callback_data="buy:3month"),
    ],[
        InlineKeyboardButton(text="6 месяцев — 1690₽", callback_data="buy:6month"),
    ],[
        InlineKeyboardButton(text="Навсегда — 2990₽", callback_data="buy:lifetime"),
    ]])
