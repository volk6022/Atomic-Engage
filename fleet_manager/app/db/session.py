import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def _get_database_url() -> str:
    settings = get_settings()
    return settings.DATABASE_URL


_engine = None
_async_session_maker = None


def get_engine():
    global _engine
    if _engine is None:
        url = _get_database_url()
        _engine = create_async_engine(
            url,
            poolclass=NullPool,
            echo=False,
        )
    return _engine


def get_session_maker():
    global _async_session_maker
    if _async_session_maker is None:
        engine = get_engine()
        _async_session_maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_maker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
    test_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://fleet_user:fleet_password@localhost:5433/fleet_test",
    )
    engine = create_async_engine(test_url, poolclass=NullPool, echo=False)
    session_maker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
            await engine.dispose()
