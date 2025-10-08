import os, datetime as dt
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from .db import Session, execute, fetch_one
from .access import create_trial_invite, kick_and_unban
from .keyboards import pay_kb
from .payments import create_payment

BOT = Bot(token=os.getenv("BOT_TOKEN"))
DP  = Dispatcher()

TRIAL_HOURS = int(os.getenv("TRIAL_HOURS", "48"))

@DP.message(Command("start"))
async def start(m: Message):
    async with Session() as s:
        u = await fetch_one(s, "select * from users where user_id=:uid", uid=m.from_user.id)
        if not u:
            link = await create_trial_invite(BOT, TRIAL_HOURS)
            await execute(s, """
                insert into users(user_id, username, status, trial_until, last_invite_link)
                values (:uid, :un, 'trial', now() + (:h || ' hours')::interval, :link)
            """, uid=m.from_user.id, un=m.from_user.username, h=TRIAL_HOURS, link=link)
            await s.commit()
            await m.answer(
                "Привет! Ты получил 48 часов бесплатного доступа 🔓\n"
                "Сохраняй посты и PDF пока доступ открыт.\n\n"
                f"Вход в канал: {link}"
            )
        else:
            await m.answer("Ты уже у нас. Если нет доступа — оформляй подписку:", reply_markup=pay_kb())

@DP.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery):
    tariff = cb.data.split(":")[1]
    payment = create_payment(cb.from_user.id, tariff)
    url = payment.confirmation.confirmation_url
    await cb.message.answer(
        f"Оплата тарифа **{tariff}**. Нажми для перехода к оплате:\n{url}",
        parse_mode="Markdown"
    )
    async with Session() as s:
        await execute(s, """
            insert into payments(id, user_id, amount, tariff, raw)
            values (:id, :uid, :amount, :tariff, to_jsonb(:raw::text))
            on conflict (id) do nothing
        """, id=payment.id, uid=cb.from_user.id, amount=payment.amount.value, tariff=tariff, raw=str(payment))
        await execute(s, "update users set last_payment_id=:pid where user_id=:uid", pid=payment.id, uid=cb.from_user.id)
        await s.commit()
    await cb.answer()
