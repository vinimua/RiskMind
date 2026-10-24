"""Shared asynchronous SQLAlchemy engine and session factory."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


DATABASE_URL_ENV = "RISK_DATABASE_URL"

# Base(DeclarativeBase) 是所有 ORM 表模型的共同基类
# KnowledgeChunkRow 继承它后，SQLAlchemy 才能把这个类识别为 ORM 表模型，并把表结构登记到 Base.metadata。Alembic 读取这些登记的信息，用于检查和生成数据库迁移。
class Base(DeclarativeBase):
    """Declarative base shared by persisted RiskMind models."""


_ENGINE: AsyncEngine | None = None
_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None


def get_database_url() -> str:
    """Return the configured database URL or fail with an actionable error."""

    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        raise RuntimeError(f"{DATABASE_URL_ENV} is required")
    return database_url

# 第一次被调用时创建 AsyncEngine，保存在 _ENGINE；后续调用复用它。
# pool_pre_ping=True 用来在取出连接时检查失效连接。
def get_engine() -> AsyncEngine:
    """Return the process-wide asynchronous database engine."""

    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(get_database_url(), pool_pre_ping=True)
    return _ENGINE

# 它是“创建 Session 的工厂”，并不是一个始终打开的 Session。
# expire_on_commit=False 表示提交后 ORM 对象属性不会立刻失效。
# 调用方通过 async with sessions() as session 获取一次操作使用的 Session，并由调用方决定事务何时提交
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return sessions bound to the process-wide engine."""

    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _SESSION_FACTORY


async def dispose_engine() -> None:
    """Dispose the process engine, primarily for CLI shutdown."""

    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        await _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
