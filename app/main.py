import os, json, datetime as dt, asyncio
from fastapi import FastAPI, Request, Response
from aiogram.types import Update
from .bot import DP, BOT
from .db import Session, execute, fetch_one
from .payments import tariff_to_delta
from .access import kick_and_unban

app = FastAPI()

# --- Telegram webhook endpoint ---
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
    data = await req.json()
    update = Update.model_validate(data)
    await DP.feed_update(BOT, update)
    return Response(status_code=200)

# --- YooKassa webhook endpoint ---
@app.post("/yk/webhook")
async def yk_webhook(req: Request):
    data = await req.json()
    event = data.get("event")
    obj   = data.get("object", {})
    if event == "payment.succeeded":
        payment_id = obj.get("id")
        meta = obj.get("metadata") or {}
        user_id = int(meta.get("user_id"))
        tariff  = meta.get("tariff")
        delta = tariff_to_delta(tariff)
        async with Session() as s:
            # помечаем платёж
            await execute(s, "update payments set succeeded=true, raw=:raw where id=:id",
                          raw=json.dumps(data), id=payment_id)
            if tariff == "lifetime":
                await execute(s, """
                  update users set status='active', lifetime=true, subscription_until=null
                  where user_id=:uid
                """, uid=user_id)
            else:
                await execute(s, """
                  update users set status='active',
                         subscription_until=coalesce(subscription_until, now()) + (:sec || ' seconds')::interval,
                         lifetime=false
                  where user_id=:uid
                """, uid=user_id, sec=int(delta.total_seconds()))
            await s.commit()
        # Приветствие и инструкции
        await BOT.send_message(user_id,
            "Оплата прошла ✅ Доступ к каналу открыт. Если выпало из канала — просто нажми на последнюю ссылку-приглашение или /start.")
    return Response(status_code=200)

# --- Scheduler: trials & subscriptions ---
async def scheduler():
    while True:
        try:
            async with Session() as s:
                # истёк триал -> удалить и статус expired
                await execute(s, """
                  update users set status='expired'
                  where status='trial' and trial_until is not null and trial_until < now()
                """)
                # удалить из канала всех у кого expired без активной подписки
                rows = await s.execute(
                    "select user_id from users where status='expired'"
                )
                for r in rows:
                    uid = r[0]
                    try:
                        await kick_and_unban(BOT, uid)
                        await BOT.send_message(uid,
                            "Пробный доступ завершён. Хочешь остаться? Выбери тариф 👇")
                    except Exception:
                        pass
                # истёк платный период (не lifetime)
                await execute(s, """
                  update users set status='expired'
                  where status='active' and lifetime=false and subscription_until < now()
                """)
                rows2 = await s.execute(
                    "select user_id from users where status='expired' and lifetime=false"
                )
                for r in rows2:
                    uid = r[0]
                    try:
                        await kick_and_unban(BOT, uid)
                        await BOT.send_message(uid,
                            "Подписка закончилась. Продлить доступ?")
                    except Exception:
                        pass
                await s.commit()
        except Exception:
            pass
        await asyncio.sleep(3600)  # раз в час

@app.on_event("startup")
async def on_start():
    asyncio.create_task(scheduler())
