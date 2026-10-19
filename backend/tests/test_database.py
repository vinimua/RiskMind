import asyncio

import pytest

from risk_platform.db import database


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(database.DATABASE_URL_ENV, raising=False)

    with pytest.raises(RuntimeError, match="RISK_DATABASE_URL is required"):
        database.get_database_url()


def test_database_url_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+psycopg://risk:test@localhost/risk_platform"
    monkeypatch.setenv(database.DATABASE_URL_ENV, url)

    assert database.get_database_url() == url


def test_engine_and_session_factory_are_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        database.DATABASE_URL_ENV,
        "postgresql+psycopg://risk:test@localhost/risk_platform",
    )
    monkeypatch.setattr(database, "_ENGINE", None)
    monkeypatch.setattr(database, "_SESSION_FACTORY", None)

    engine = database.get_engine()
    factory = database.get_session_factory()

    assert database.get_engine() is engine
    assert database.get_session_factory() is factory
    asyncio.run(database.dispose_engine())
