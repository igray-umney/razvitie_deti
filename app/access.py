import os, datetime as dt
from aiogram import Bot

CHANNEL_ID = os.getenv("CHANNEL_ID")

async def create_trial_invite(bot: Bot, hours: int) -> str:
    # уникальная ссылка на 48ч, 1 юзер
    expire = dt.datetime.utcnow() + dt.timedelta(hours=hours)
    link = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expire,
        member_limit=1,
        creates_join_request=False
    )
    return link.invite_link

async def kick_and_unban(bot: Bot, user_id: int):
    # удаление из канала: короткий бан и сразу разбан
    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id, revoke_messages=False)
    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id, only_if_banned=True)
