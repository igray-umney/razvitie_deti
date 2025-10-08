import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text

engine = create_async_engine(os.getenv("POSTGRES_URL"), echo=False, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def fetch_one(session: AsyncSession, q: str, **params):
    return (await session.execute(text(q), params)).mappings().first()

async def execute(session: AsyncSession, q: str, **params):
    await session.execute(text(q), params)
