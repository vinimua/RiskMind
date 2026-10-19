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


def get_engine() -> AsyncEngine:
    """Return the process-wide asynchronous database engine."""

    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(get_database_url(), pool_pre_ping=True)
    return _ENGINE


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
