"""Shared pytest fixtures — in-memory async SQLite for repository/service tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Import models so metadata is complete.
import app.models  # noqa: F401
from app.config import Settings
from app.database.base import Base
from app.models.category import Category
from app.models.enums import CityChoice, LanguageCode
from app.models.product import Product
from app.models.user import User

# Shared seeded shop, used by the statistics service and dashboard suites.
from tests.shop_dataset import shop, stats  # noqa: F401

# --------------------------------------------------------------------------
# Settings must never inherit the developer's machine.
#
# `Settings` declares `env_file=".env"`, so any test that builds one picks up
# whatever the local `.env` happens to contain for fields it did not set. Two
# reviews tests asserting "no reviews group is configured" started failing the
# moment a real `REVIEW_GROUP_CHAT_ID` was added to `.env` — the code was fine,
# the suite was reading the operator's configuration.
# --------------------------------------------------------------------------
_APP_ENV_VARS = tuple(name.upper() for name in Settings.model_fields)


@pytest.fixture(autouse=True, scope="session")
def _isolate_settings_from_the_environment() -> Iterator[None]:
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    saved = {name: os.environ.pop(name) for name in _APP_ENV_VARS if name in os.environ}
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original
        os.environ.update(saved)


@pytest.fixture
def lands(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """
    The roulette screen's next draws land on the prize named — by prize code.

    Only the ticket source is replaced: the engine still validates, spends the
    spin and books the prize, as the roulette engine's own tests do.
    """
    from app.handlers.user import roulette as roulette_screen
    from app.services.roulette_engine import RouletteEngine, RoulettePolicy

    def land(code: str) -> None:
        def engine(session: AsyncSession, settings: Settings) -> RouletteEngine:
            policy = RoulettePolicy.from_settings(settings)
            ticket = 0
            for entry in policy.table.entries:
                if entry.prize.code == code:
                    break
                ticket += entry.weight
            return RouletteEngine(session, policy, randbelow=lambda total: ticket)

        monkeypatch.setattr(roulette_screen, "_engine", engine)

    return land


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    entity = User(
        telegram_id=1001,
        username="buyer",
        first_name="Buyer",
        language=LanguageCode.EN,
        selected_city=CityChoice.BERLIN,
    )
    session.add(entity)
    await session.flush()
    return entity


@pytest_asyncio.fixture
async def category(session: AsyncSession) -> Category:
    entity = Category(
        name="Liquids",
        name_ru="Жидкости",
        name_en="Liquids",
        name_de="Liquids",
        name_uk="Рідини",
        sort_order=0,
    )
    session.add(entity)
    await session.flush()
    return entity


@pytest_asyncio.fixture
async def product(session: AsyncSession, category: Category) -> Product:
    entity = Product(
        category_id=category.id,
        name_ru="Тест",
        name_en="Test Juice",
        name_de="Test Saft",
        name_uk="Тестовий сік",
        description_ru="Описание",
        description_en="Description",
        description_de="Beschreibung",
        description_uk="Опис",
        flavor="Mango",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("12.50"),
        is_active=True,
    )
    session.add(entity)
    await session.flush()
    return entity
