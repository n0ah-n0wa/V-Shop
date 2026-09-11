"""
Loyalty activation for existing users — the backfill, and running it again.

Every customer ends up with exactly one loyalty account and exactly one welcome
roulette spin, whether they registered before the programme existed, were left
half-migrated, or joined yesterday — and running the backfill again, on a
restart or a redeploy, adds nothing. It never touches the catalog, the users'
own rows, their carts or any order, and orders placed before launch earn no
stamps. Concurrent starts are raced on PostgreSQL in
``tests/test_loyalty_postgres.py``, and a real ``alembic upgrade`` of a
pre-loyalty shop in ``tests/test_loyalty_activation_postgres.py``.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import lifecycle
from app.config import Settings
from app.handlers.user.start import cmd_start
from app.models.cart import Cart, CartItem
from app.models.category import Category, Subcategory
from app.models.enums import OrderStatus, RoulettePrizeType, SpinGrantReason
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.admin import AdminService
from app.services.cart import CartService
from app.services.loyalty import LoyaltyService
from app.services.loyalty_activation import ActivationReport, LoyaltyActivationService
from app.services.referral import ReferralService
from app.services.roulette import RoulettePrize, RouletteService
from app.services.spin_entitlement import SpinPolicy
from app.services.user import UserService
from tests.factories import add_order_item, make_category, make_order, make_product, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "3b9d6f2a8c14_loyalty_foundation.py"
INITIAL = SpinGrantReason.INITIAL_PROMO
STAMP_1 = RoulettePrize("stamp_1", RoulettePrizeType.STAMPS, 1)
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
# The shop's own tables: activation must leave every row of them exactly as it was.
SHOP_TABLES = (User, Category, Subcategory, Product, Cart, CartItem, Order, OrderItem)


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


async def activate(session: AsyncSession, **overrides: Any) -> ActivationReport:
    policy = SpinPolicy.from_settings(configured(**overrides))
    return await LoyaltyActivationService(session, policy).activate_everyone()


class Chatting(Message):
    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        return self


async def send_start(session: AsyncSession, telegram_id: int) -> None:
    """A customer's very first contact: /start."""
    await cmd_start(
        message=Chatting(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=telegram_id, type="private"),
            from_user=TgUser(id=telegram_id, is_bot=False, first_name="New"),
            text="/start",
        ),
        state=FSMContext(
            storage=MemoryStorage(),
            key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
        ),
        session=session,
        settings=configured(),
    )


async def user_by_telegram(session: AsyncSession, telegram_id: int) -> User:
    user = await UserRepository(session).get_by_telegram_id(telegram_id)
    assert user is not None
    return user


async def legacy_shop(
    session: AsyncSession, first_telegram_id: int, customers: int = 3
) -> list[User]:
    """Customers, a catalog, carts and orders from before the loyalty programme existed."""
    category = await make_category(session, name="Liquids")
    mango = await make_product(session, category, name_en="Mango", price="12.50")
    mint = await make_product(session, category, name_en="Mint", price="20.00")
    users = []
    for index in range(customers):
        user = await make_user(session, telegram_id=first_telegram_id + index)
        await CartService(session).add_product(user.id, mango, quantity=index + 1)
        for status in (OrderStatus.COMPLETED, OrderStatus.NEW):
            order = await make_order(session, user, status=status)
            # As migration 8e4c1a7b2d95 left every order placed before launch.
            order.loyalty_eligible = False
            await add_order_item(session, order, mint, quantity=2)
        users.append(user)
    await session.flush()
    return users


async def snapshot(session: AsyncSession) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of the shop's own tables, straight from the database."""
    await session.flush()
    rows: dict[str, list[tuple[Any, ...]]] = {}
    for model in SHOP_TABLES:
        table = model.__table__
        result = await session.execute(select(table).order_by(*table.primary_key.columns))
        rows[table.name] = [tuple(row) for row in result.all()]
    return rows


async def per_user(session: AsyncSession, column: Any, *where: Any) -> dict[int, int]:
    result = await session.execute(select(column, func.count()).where(*where).group_by(column))
    return {user_id: count for user_id, count in result.all()}


async def accounts_by_user(session: AsyncSession) -> dict[int, int]:
    return await per_user(session, LoyaltyAccount.user_id)


async def welcome_by_user(session: AsyncSession) -> dict[int, int]:
    return await per_user(session, RouletteSpinGrant.user_id, RouletteSpinGrant.reason == INITIAL)


async def account_rows(session: AsyncSession) -> dict[int, tuple[Any, ...]]:
    table = LoyaltyAccount.__table__
    result = await session.execute(select(table))
    return {row.user_id: tuple(row) for row in result.all()}


async def grant_rows(session: AsyncSession) -> list[tuple[Any, ...]]:
    table = RouletteSpinGrant.__table__
    result = await session.execute(select(table).order_by(table.c.id))
    return [tuple(row) for row in result.all()]


async def count(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def migration_backfill() -> list[str]:
    """The SQL ``alembic upgrade`` runs on an existing shop when the programme launches."""
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    return [
        call.args[0].value
        for call in ast.walk(upgrade)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "execute"
        and call.args
        and isinstance(call.args[0], ast.Constant)
    ]


@pytest.fixture
def bot_sessions(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """The bot's own session factory, pointed at the test database."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: factory)
    return factory


# ======================================================= the backfill


async def test_an_empty_database_activates_nothing(session: AsyncSession) -> None:
    assert await activate(session) == ActivationReport(accounts_opened=0, welcome_spins_granted=0)
    assert await count(session, LoyaltyAccount) == 0
    assert await count(session, RouletteSpinGrant) == 0


async def test_every_existing_customer_gets_one_account_and_one_welcome_spin(
    session: AsyncSession,
) -> None:
    users = await legacy_shop(session, 9601, customers=4)
    before = await snapshot(session)

    report = await activate(session)

    assert report == ActivationReport(accounts_opened=4, welcome_spins_granted=4)
    everyone = {user.id: 1 for user in users}
    assert await accounts_by_user(session) == everyone
    assert await welcome_by_user(session) == everyone
    accounts = await session.execute(
        select(
            LoyaltyAccount.stamp_balance,
            LoyaltyAccount.qualifying_purchase_count,
            LoyaltyAccount.referral_code,
        )
    )
    assert set(accounts.all()) == {(0, 0, None)}  # the orders they placed earned nothing
    assert (await session.scalars(select(RouletteSpinGrant.consumed_at))).all() == [None] * 4
    assert await count(session, LoyaltyTransaction) == 0
    assert await snapshot(session) == before  # catalog, users, carts and orders untouched


async def test_partially_migrated_customers_get_only_what_they_lack(
    session: AsyncSession,
) -> None:
    nothing, account_only, spin_only, spun, complete = await legacy_shop(session, 9611, customers=5)
    loyalty = LoyaltyService(session)
    # Account only, with history: a balance and a referral code.
    await loyalty.adjust(account_only.id, amount=3, note="migrated balance")
    await ReferralService(session).get_or_create_referral_code(account_only.id)
    # Welcome spin only — a row the migration left without its account.
    session.add(RouletteSpinGrant(user_id=spin_only.id, reason=INITIAL))
    # Welcome spin spent long ago (it won a stamp).
    await RouletteService(session).grant_initial_spin(spun.id)
    await RouletteService(session).spin(spun.id, STAMP_1)
    # Fully migrated already.
    session.add(LoyaltyAccount(user_id=complete.id))
    session.add(RouletteSpinGrant(user_id=complete.id, reason=INITIAL))
    await session.flush()
    accounts_before = await account_rows(session)
    grants_before = await grant_rows(session)
    shop_before = await snapshot(session)
    assert set(accounts_before) == {account_only.id, spun.id, complete.id}

    report = await activate(session)

    assert report == ActivationReport(accounts_opened=2, welcome_spins_granted=2)
    everyone = {user.id: 1 for user in (nothing, account_only, spin_only, spun, complete)}
    assert await accounts_by_user(session) == everyone
    assert await welcome_by_user(session) == everyone  # a spent welcome spin is never replaced
    accounts_after = await account_rows(session)
    assert {user_id: accounts_after[user_id] for user_id in accounts_before} == accounts_before
    assert (await grant_rows(session))[: len(grants_before)] == grants_before
    assert await loyalty.balance(account_only.id) == 3
    assert await loyalty.ledger_balance(account_only.id) == 3
    assert await loyalty.balance(spun.id) == 1
    assert await snapshot(session) == shop_before


async def test_running_it_again_adds_nothing(session: AsyncSession) -> None:
    await legacy_shop(session, 9621)

    reports = [await activate(session) for _ in range(3)]

    assert reports == [ActivationReport(3, 3), ActivationReport(0, 0), ActivationReport(0, 0)]
    assert await count(session, LoyaltyAccount) == 3
    assert await count(session, RouletteSpinGrant) == 3


async def test_accounts_open_even_with_the_welcome_spin_switched_off(
    session: AsyncSession,
) -> None:
    await legacy_shop(session, 9631)

    assert await activate(session, roulette_initial_free_spin=False) == ActivationReport(3, 0)
    assert await count(session, RouletteSpinGrant) == 0
    # Switched on later: the next start grants them — once.
    assert await activate(session) == ActivationReport(0, 3)
    assert await activate(session) == ActivationReport(0, 0)


# ======================================================= new customers


async def test_new_customers_are_in_the_programme_from_first_contact(
    session: AsyncSession,
) -> None:
    await send_start(session, 9641)  # the account at registration, the welcome spin at /start
    starter = await user_by_telegram(session, 9641)
    # Someone first seen by another handler: registered, but no /start yet.
    quiet = await UserService(session).ensure_user(
        TgUser(id=9642, is_bot=False, first_name="Quiet")
    )

    assert await accounts_by_user(session) == {starter.id: 1, quiet.id: 1}
    assert await welcome_by_user(session) == {starter.id: 1}
    assert await activate(session) == ActivationReport(0, 1)  # the next start catches up
    assert await welcome_by_user(session) == {starter.id: 1, quiet.id: 1}


async def test_registering_again_opens_no_second_account(session: AsyncSession) -> None:
    for _ in range(3):
        await UserService(session).ensure_user(TgUser(id=9643, is_bot=False, first_name="Again"))
        await send_start(session, 9643)

    assert await count(session, LoyaltyAccount) == 1
    assert await count(session, RouletteSpinGrant) == 1


# ======================================================= starts, restarts, redeploys


async def test_every_bot_start_activates_and_a_restart_adds_nothing(
    bot_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with bot_sessions() as session:
        users = await legacy_shop(session, 9651)
        await session.commit()
        before = await snapshot(session)

    reports = [await lifecycle.activate_loyalty(configured()) for _ in range(3)]

    assert reports == [ActivationReport(3, 3), ActivationReport(0, 0), ActivationReport(0, 0)]
    async with bot_sessions() as session:
        assert await accounts_by_user(session) == {user.id: 1 for user in users}
        assert await welcome_by_user(session) == {user.id: 1 for user in users}
        assert await snapshot(session) == before


async def test_launching_then_restarting_and_redeploying(
    bot_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The upgrade's backfill, the first start, a new customer, a restart, a redeploy."""
    async with bot_sessions() as session:
        legacy = await legacy_shop(session, 9661)
        await session.commit()
        legacy_rows = await snapshot(session)

    async def upgrade() -> None:
        """What ``alembic upgrade head`` writes on an existing shop at launch."""
        async with bot_sessions() as session:
            for sql in migration_backfill():
                await session.execute(text(sql))
            await session.commit()

    assert len(migration_backfill()) == 2
    await upgrade()
    first_start = await lifecycle.activate_loyalty(configured())
    async with bot_sessions() as session:
        await send_start(session, 9670)  # a customer joins after launch
        await session.commit()
    restart = await lifecycle.activate_loyalty(configured())
    await upgrade()  # a redeploy runs the upgrade's SQL again…
    redeploy = await lifecycle.activate_loyalty(configured())  # …and starts again

    # The upgrade already brought everyone in; /start brought in the newcomer.
    assert [first_start, restart, redeploy] == [ActivationReport(0, 0)] * 3
    async with bot_sessions() as session:
        newcomer = await user_by_telegram(session, 9670)
        everyone = {user.id: 1 for user in legacy} | {newcomer.id: 1}
        assert await accounts_by_user(session) == everyone
        assert await welcome_by_user(session) == everyone
        assert await count(session, LoyaltyTransaction) == 0
        now = await snapshot(session)
        for table, rows in legacy_rows.items():
            current = {row[0]: row for row in now[table]}
            assert {row[0]: current.get(row[0]) for row in rows} == {row[0]: row for row in rows}

        # Later, the admin completes an order placed before launch: no stamps.
        pending = await session.scalar(
            select(Order).where(Order.user_id == legacy[0].id, Order.status == OrderStatus.NEW)
        )
        assert pending is not None and pending.loyalty_eligible is False
        admin = AdminService(session, settings=configured())
        for status in TO_COMPLETED:
            pending = await admin.set_order_status(pending, status)
        assert await count(session, LoyaltyTransaction) == 0
        assert await LoyaltyService(session).purchase_count(legacy[0].id) == 0


# ======================================================= guards


def test_activation_never_reaches_orders_or_the_catalog() -> None:
    """The backfill writes loyalty rows only; the shop's own tables are not its business."""
    shop = {"Order", "OrderItem", "Product", "Category", "Subcategory", "Cart", "CartItem"}
    for relative in ("app/services/loyalty_activation.py", "app/repositories/loyalty_account.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        named |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not named & shop, f"{relative} names {sorted(named & shop)}"
