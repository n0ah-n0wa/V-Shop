"""
Launching the loyalty programme on an existing shop, on PostgreSQL — opt-in.

The real Alembic migrations run in-process against a scratch database: first the
shop as it was before the programme (revision ``f6b1d4e8a207``) with customers,
a catalog, carts and orders; then ``alembic upgrade head`` — twice, as a
redeploy does — then bot starts, a rolling deploy and restarts. Every customer
ends up with exactly one loyalty account and one welcome spin, the shop's own
rows are exactly what they were, and orders placed before launch stay
ineligible for stamps.

Runs only when ``VSHOP_TEST_POSTGRES_URL`` names an empty database whose name
ends in ``_test`` — the schema is built here and dropped afterwards::

    VSHOP_TEST_POSTGRES_URL=postgresql+asyncpg://vshop:pw@127.0.0.1:55432/loyalty_test \\
        python -m pytest tests/test_loyalty_activation_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from argparse import Namespace
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from aiogram.types import User as TgUser
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import column, inspect, select, table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.config
from alembic import command
from app import lifecycle
from app.config import Settings
from app.services.loyalty_activation import ActivationReport
from app.services.user import UserService
from app.verify_deployment import loyalty_health

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = os.environ.get("VSHOP_TEST_POSTGRES_URL", "")
BEFORE_LOYALTY = "f6b1d4e8a207"  # the revision just before 3b9d6f2a8c14_loyalty_foundation
SHOP_TABLES = (
    "users",
    "categories",
    "subcategories",
    "products",
    "carts",
    "cart_items",
    "orders",
    "order_items",
)
# The shop before the programme: written against the pre-loyalty schema.
LEGACY_SHOP = (
    "INSERT INTO users (telegram_id, username, first_name, language, selected_city) VALUES "
    "(7100001, 'anna', 'Anna', 'ru', 'berlin'), (7100002, 'ben', 'Ben', 'de', 'delivery'), "
    "(7100003, NULL, 'Cleo', NULL, NULL)",
    "INSERT INTO categories (name, name_ru, name_en, name_de, name_uk) "
    "VALUES ('Liquids', 'Жидкости', 'Liquids', 'Liquids', 'Рідини')",
    "INSERT INTO subcategories (category_id, name_ru, name_en, name_de, name_uk) "
    "SELECT id, 'Бренд', 'Brand', 'Marke', 'Бренд' FROM categories",
    "INSERT INTO products (category_id, subcategory_id, name_ru, name_en, name_de, name_uk, "
    "description_ru, description_en, description_de, description_uk, flavor, volume, "
    "nicotine_strength, price) "
    "SELECT s.category_id, s.id, 'Манго', 'Mango', 'Mango', 'Манго', 'd', 'd', 'd', 'd', "
    "'mango', '10 ml', '20 mg', 12.50 FROM subcategories s",
    "INSERT INTO carts (user_id) SELECT id FROM users",
    "INSERT INTO cart_items (cart_id, product_id, quantity) "
    "SELECT c.id, p.id, 2 FROM carts c CROSS JOIN products p",
    "INSERT INTO orders (user_id, customer_name, city, delivery_type, address, preferred_time, "
    "total_price, status, payment_method) "
    "SELECT id, 'Legacy', 'berlin', 'pickup', 'Street 1', '18:00', 25.00, 'Completed', 'cash' "
    "FROM users",
    "INSERT INTO orders (user_id, customer_name, city, delivery_type, address, total_price, "
    "status) SELECT id, 'Pending', 'berlin', 'pickup', 'Street 1', 25.00, 'New' FROM users "
    "WHERE telegram_id = 7100001",
    "INSERT INTO order_items (order_id, product_id, quantity, price) "
    "SELECT o.id, p.id, 2, 12.50 FROM orders o CROSS JOIN products p",
)


def _refusal() -> str | None:
    if not URL:
        return "set VSHOP_TEST_POSTGRES_URL to run the PostgreSQL deployment test"
    url = make_url(URL)
    if url.get_backend_name() != "postgresql":
        return "VSHOP_TEST_POSTGRES_URL must be a PostgreSQL URL"
    if not (url.database or "").endswith("_test"):
        return "refusing: the test database name must end in _test"
    return None


pytestmark = pytest.mark.skipif(_refusal() is not None, reason=_refusal() or "")

Sessions = async_sessionmaker[AsyncSession]


def _alembic_config(**x: str) -> Config:
    # No ini file: its logging section would reconfigure the test run's loggers.
    config = Config(cmd_opts=Namespace(x=[f"{key}={value}" for key, value in x.items()]))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


async def alembic(action: str, revision: str) -> None:
    """``alembic <action> <revision>``, as a deploy runs it (its own event loop, so a thread)."""
    await asyncio.to_thread(getattr(command, action), _alembic_config(), revision)


def head() -> str:
    revision = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert revision is not None
    return revision


@pytest_asyncio.fixture
async def scratch(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sessions]:
    engine = create_async_engine(URL, poolclass=NullPool)
    async with engine.connect() as connection:
        existing = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    if existing:
        await engine.dispose()
        pytest.skip(f"refusing: {make_url(URL).database} is not empty: {sorted(existing)[:5]}")

    settings = Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url=URL,
        manager_chat_id=-1001234567890,
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # Alembic's env.py and the bot's start-up both read these.
    monkeypatch.setattr(app.config, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: sessions)
    try:
        yield sessions
    finally:
        # Only reached when the database was empty to begin with: leave it empty.
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        await engine.dispose()


async def start() -> ActivationReport:
    """A bot start: the activation ``on_startup`` runs, in its own transaction."""
    report = await lifecycle.activate_loyalty(app.config.get_settings())
    assert report is not None, "the start-up activation failed"
    return report


async def legacy_columns(session: AsyncSession) -> dict[str, list[str]]:
    """The shop's tables as they were before the programme — the columns to compare."""
    columns: dict[str, list[str]] = {}
    for name in SHOP_TABLES:
        result = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :name ORDER BY ordinal_position"
            ),
            {"name": name},
        )
        columns[name] = [row[0] for row in result.all()]
    return columns


async def snapshot(session: AsyncSession, columns: dict[str, list[str]]) -> dict[str, Any]:
    """Every row of the shop's tables, keyed by id, over their pre-programme columns."""
    rows: dict[str, Any] = {}
    for name, names in columns.items():
        statement = select(*(column(n) for n in names)).select_from(table(name))
        result = await session.execute(statement.order_by(column("id")))
        rows[name] = {row[0]: tuple(row) for row in result.all()}
    return rows


async def scalar(session: AsyncSession, sql: str) -> Any:
    return await session.scalar(text(sql))


async def per_user(session: AsyncSession, sql: str) -> dict[int, int]:
    return {user_id: count for user_id, count in (await session.execute(text(sql))).all()}


ACCOUNTS = "SELECT user_id, count(*) FROM loyalty_accounts GROUP BY user_id"
WELCOME = (
    "SELECT user_id, count(*) FROM roulette_spin_grants "
    "WHERE reason = 'initial_promo' GROUP BY user_id"
)


async def test_launching_on_an_existing_shop_then_redeploying_and_restarting(
    scratch: Sessions,
) -> None:
    await alembic("upgrade", BEFORE_LOYALTY)
    async with scratch() as session:
        for sql in LEGACY_SHOP:
            await session.execute(text(sql))
        await session.commit()
        columns = await legacy_columns(session)
        before = await snapshot(session, columns)
        users = list(before["users"])
    assert len(users) == 3 and len(before["orders"]) == 4 and len(before["products"]) == 1

    await alembic("upgrade", "head")  # the deploy that launches the programme
    async with scratch() as session:
        launched = (await per_user(session, ACCOUNTS), await per_user(session, WELCOME))
    await alembic("upgrade", "head")  # a redeploy: nothing left to apply
    first_start = await start()
    restart = await start()

    # The upgrade's own backfill brought everyone in; the starts found nothing to do.
    assert launched == (dict.fromkeys(users, 1), dict.fromkeys(users, 1))
    assert (first_start, restart) == (ActivationReport(0, 0), ActivationReport(0, 0))
    async with scratch() as session:
        assert await scalar(session, "SELECT version_num FROM alembic_version") == head()
        assert (await per_user(session, ACCOUNTS), await per_user(session, WELCOME)) == launched
        assert await scalar(session, "SELECT count(*) FROM loyalty_transactions") == 0
        balances = "SELECT count(*) FROM loyalty_accounts WHERE stamp_balance <> 0"
        assert await scalar(session, balances) == 0
        # Orders placed before launch never earn stamps, and were not rewritten.
        assert await scalar(session, "SELECT count(*) FROM orders WHERE loyalty_eligible") == 0
        assert await snapshot(session, columns) == before

    # A rolling deploy: the old instance registers someone the new code never saw…
    async with scratch() as session:
        await session.execute(
            text("INSERT INTO users (telegram_id, first_name) VALUES (7100009, 'Late')")
        )
        # …while a newcomer's first update reaches the new instance.
        await UserService(session).ensure_user(TgUser(id=7100010, is_bot=False, first_name="New"))
        await session.commit()
    caught_up = await start()
    again = await start()

    # Late: account and spin; the newcomer: account at registration, spin now.
    assert caught_up == ActivationReport(accounts_opened=1, welcome_spins_granted=2)
    assert again == ActivationReport(0, 0)
    async with scratch() as session:
        everyone = [row[0] for row in (await session.execute(text("SELECT id FROM users"))).all()]
        assert await per_user(session, ACCOUNTS) == dict.fromkeys(everyone, 1)
        assert await per_user(session, WELCOME) == dict.fromkeys(everyone, 1)
        health = await loyalty_health(session)
        assert set(health["integrity"].values()) == {0}
        assert health["coverage"] == {"users_without_account": 0, "users_without_welcome_spin": 0}
        now = await snapshot(session, columns)
        for name, rows in before.items():
            assert {row_id: now[name].get(row_id) for row_id in rows} == rows, name
