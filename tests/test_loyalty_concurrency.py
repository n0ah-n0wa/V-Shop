"""
The loyalty ecosystem under concurrency — a transaction audit, on PostgreSQL.

Every operation that moves loyalty state is raced here in real, overlapping
transactions, and the books are checked afterwards: no duplicate stamps, spins
or rewards, no negative balance, no reward lost, nothing half done. Two rules
the audit added are pinned as well:

* a customer's first order and their referral attribution are serialised on the
  customer's loyalty account, so a referral never lands on top of an order that
  was already being placed;
* no transaction holds a loyalty lock — a customer's account row, or the global
  attribution lock — while the bot waits on Telegram, and nothing reaches a
  customer before it is durable.

Runs only when ``VSHOP_TEST_POSTGRES_URL`` names an empty database whose name
ends in ``_test`` — the schema is created here and dropped afterwards::

    VSHOP_TEST_POSTGRES_URL=postgresql+asyncpg://vshop:pw@127.0.0.1:55432/loyalty_test \\
        python -m pytest tests/test_loyalty_concurrency.py
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, GetMe, TelegramMethod
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.middlewares.database
import app.models  # noqa: F401  (populates the metadata)
from app import lifecycle
from app.database.base import Base
from app.handlers.user import roulette as roulette_screen
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    ReferralStatus,
    RewardSource,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.repositories.referral import ATTRIBUTION_LOCK_KEY
from app.services.admin import AdminService
from app.services.cart import CartService
from app.services.localization import LocalizationService
from app.services.loyalty import InsufficientStampsError, LoyaltyService
from app.services.order import OrderService
from app.services.referral import ReferralService, referral_payload
from app.services.referral_program import ReferralOutcome, ReferralProgramService
from app.services.roulette import RoulettePrize, RouletteService
from app.services.stamp_card import StampCardService
from app.utils.cache import invalidate_categories_cache
from app.verify_deployment import loyalty_health
from tests.factories import make_category, make_order, make_product, make_user
from tests.production_bot import FakeTelegram, RunningBot, tree_settings
from tests.test_loyalty_journeys import TO_COMPLETED, admin_moves, buy, check_out, open_shop

URL = os.environ.get("VSHOP_TEST_POSTGRES_URL", "")
EN = LocalizationService("en")
WAIT = 0.5  # long enough for an unblocked transaction to finish; a blocked one never does
STAMP_1 = RoulettePrize("stamp_1", RoulettePrizeType.STAMPS, 1)
PURCHASE = LoyaltyTransactionType.PURCHASE
REFERRAL = LoyaltyTransactionType.REFERRAL

Factory = async_sessionmaker[AsyncSession]
Operation = Callable[[AsyncSession], Awaitable[Any]]


def _refusal() -> str | None:
    if not URL:
        return "set VSHOP_TEST_POSTGRES_URL to run the loyalty concurrency audit"
    url = make_url(URL)
    if url.get_backend_name() != "postgresql":
        return "VSHOP_TEST_POSTGRES_URL must be a PostgreSQL URL"
    if not (url.database or "").endswith("_test"):
        return "refusing: the test database name must end in _test"
    return None


pytestmark = pytest.mark.skipif(_refusal() is not None, reason=_refusal() or "")


@pytest_asyncio.fixture
async def pg(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Factory]:
    engine = create_async_engine(URL)
    async with engine.connect() as connection:
        existing = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    if existing:
        await engine.dispose()
        pytest.skip(f"refusing: {make_url(URL).database} is not empty: {sorted(existing)[:5]}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: factory)
    monkeypatch.setattr(roulette_screen, "FRAME_DELAY", 0)
    invalidate_categories_cache()
    try:
        yield factory
    finally:
        invalidate_categories_cache()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


# ======================================================= helpers


async def seed(pg: Factory, build: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    async with pg() as session:
        value = await build(session)
        await session.commit()
        return value


async def at_once(pg: Factory, operations: list[Operation]) -> list[Any]:
    """Each operation in its own transaction, all started together; errors are results too."""

    async def one(operation: Operation) -> Any:
        async with pg() as session:
            try:
                result = await operation(session)
                await session.commit()
                return result
            except Exception as exc:  # a loser's refusal is part of what is asserted
                await session.rollback()
                return exc

    return list(await asyncio.gather(*(one(operation) for operation in operations)))


def unexpected(results: list[Any], *allowed: type[Exception]) -> list[Exception]:
    return [r for r in results if isinstance(r, Exception) and not isinstance(r, allowed)]


async def shipped_order(session: AsyncSession, user: User, total: str = "20.00") -> int:
    """A post-launch order the admin has taken as far as Shipped."""
    order = await make_order(session, user)
    order.total_price = Decimal(total)
    admin = AdminService(session, settings=tree_settings())
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED):
        order = await admin.set_order_status(order, status)
    return order.id


def completing(order_id: int) -> Operation:
    async def complete(session: AsyncSession) -> Order:
        order = await session.get(Order, order_id)
        assert order is not None
        admin = AdminService(session, settings=tree_settings())
        return await admin.set_order_status(order, OrderStatus.COMPLETED)

    return complete


async def refer(session: AsyncSession, referrer: User, referred: User) -> None:
    await ReferralService(session).attribute(
        referrer_user_id=referrer.id, referred_user_id=referred.id
    )


async def amounts(pg: Factory, user_id: int, kind: LoyaltyTransactionType) -> list[int]:
    async with pg() as session:
        rows = await session.scalars(
            select(LoyaltyTransaction.amount)
            .where(LoyaltyTransaction.user_id == user_id, LoyaltyTransaction.kind == kind)
            .order_by(LoyaltyTransaction.id)
        )
        return list(rows)


async def count(pg: Factory, model: type[Any], *where: Any) -> int:
    async with pg() as session:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        return int(await session.scalar(statement) or 0)


async def books_are_exact(pg: Factory) -> None:
    """Every invariant the deployment check knows, plus the no-negative-balance rules."""
    async with pg() as session:
        health = await loyalty_health(session)
        assert set(health["integrity"].values()) == {0}, health["integrity"]
        negative = await session.scalar(
            select(func.count()).select_from(LoyaltyAccount).where(LoyaltyAccount.stamp_balance < 0)
        )
        assert negative == 0
        spent = await session.scalar(
            select(func.count())
            .select_from(RouletteSpinGrant)
            .where(RouletteSpinGrant.consumed_at.is_not(None))
        )
        assert spent == await session.scalar(select(func.count()).select_from(RouletteSpin))


# ======================================================= a first order vs attribution


async def attribute(pg: Factory, user_id: int, payload: str) -> ReferralOutcome:
    """What /start ref_<code> does, in its own transaction."""
    async with pg() as session:
        attempt = await ReferralProgramService(session).attribute_from_start(user_id, payload)
        await session.commit()
        return attempt.outcome


async def place_order(pg: Factory, user_id: int) -> int:
    """What confirming checkout does, in its own transaction (it commits itself)."""
    async with pg() as session:
        user = await session.get(User, user_id)
        assert user is not None
        order = await OrderService(session).place_order_from_cart(
            user,
            customer_name="Nia",
            delivery_type="pickup",
            address="Street 1",
            preferred_time="18:00",
            phone=None,
            payment_method=PaymentMethod.CASH,
        )
        return order.id


async def referrer_and_newcomer(session: AsyncSession, first_id: int) -> tuple[int, str]:
    """A referrer with a link, and a brand-new customer with a bottle in their cart."""
    referrer = await make_user(session, telegram_id=first_id)
    newcomer = await make_user(session, telegram_id=first_id + 1)
    category = await make_category(session, name="Liquids")
    bottle = await make_product(session, category, name_en="Mango", price="20.00")
    await CartService(session).add_product(newcomer.id, bottle, quantity=1)
    code = await ReferralProgramService(session).referral_code(referrer.id)
    return newcomer.id, referral_payload(code)


async def test_an_attribution_waits_for_a_first_order_being_placed(pg: Factory) -> None:
    newcomer_id, payload = await seed(pg, lambda s: referrer_and_newcomer(s, 9401))

    async with pg() as checkout:
        # What placing an order does right after locking the cart: take the
        # customer's loyalty lock. The order is written but not yet committed.
        await LoyaltyService(checkout).lock_account(newcomer_id)
        newcomer = await checkout.get(User, newcomer_id)
        assert newcomer is not None
        await make_order(checkout, newcomer)
        attribution = asyncio.create_task(attribute(pg, newcomer_id, payload))
        await asyncio.sleep(WAIT)
        assert not attribution.done(), "the referral was decided while an order was being placed"
        await checkout.commit()

    # They had ordered by the time the attribution could look: not a new customer.
    assert await attribution == ReferralOutcome.NOT_NEW_CUSTOMER
    assert await count(pg, Referral) == 0


async def test_a_first_order_waits_for_an_attribution_in_flight(pg: Factory) -> None:
    newcomer_id, payload = await seed(pg, lambda s: referrer_and_newcomer(s, 9411))

    async with pg() as start:
        attempt = await ReferralProgramService(start).attribute_from_start(newcomer_id, payload)
        assert attempt.outcome == ReferralOutcome.ATTRIBUTED
        checkout = asyncio.create_task(place_order(pg, newcomer_id))
        await asyncio.sleep(WAIT)
        assert not checkout.done(), "an order was placed while its customer was being attributed"
        await start.commit()

    order_id = await checkout
    # The order came after the referral, so it is the one that qualifies it.
    async with pg() as session:
        referral = await session.scalar(
            select(Referral).where(Referral.referred_user_id == newcomer_id)
        )
        assert referral is not None and referral.status == ReferralStatus.PENDING
        order = await session.get(Order, order_id)
        assert order is not None
        admin = AdminService(session, settings=tree_settings())
        for status in TO_COMPLETED:
            order = await admin.set_order_status(order, status)
        await session.commit()
    assert await amounts(pg, newcomer_id, REFERRAL) == [2]


# ======================================================= referral payouts racing


async def test_a_referral_chain_completing_together_pays_every_link_once(pg: Factory) -> None:
    """A ← B ← C ← D: every order completes at the same moment, each delivered twice."""

    async def build(session: AsyncSession) -> tuple[list[int], list[int]]:
        a, b, c, d = [await make_user(session, telegram_id=9420 + i) for i in range(4)]
        await refer(session, a, b)
        await refer(session, b, c)
        await refer(session, c, d)
        orders = [await shipped_order(session, user) for user in (a, b, c, d)]
        return [user.id for user in (a, b, c, d)], orders

    (a, b, c, d), orders = await seed(pg, build)

    results = await at_once(pg, [completing(order_id) for order_id in orders * 2])

    assert unexpected(results) == [], "a deadlock or a failure in the chain"
    assert await amounts(pg, a, REFERRAL) == [2]  # B's referrer
    assert await amounts(pg, b, REFERRAL) == [2, 2]  # referred by A, C's referrer
    assert await amounts(pg, c, REFERRAL) == [2, 2]
    assert await amounts(pg, d, REFERRAL) == [2]
    for user_id in (a, b, c, d):
        assert await amounts(pg, user_id, PURCHASE) == [1]
    for user_id, spins in ((a, 1), (b, 1), (c, 1), (d, 0)):
        assert (
            await count(
                pg,
                RouletteSpinGrant,
                RouletteSpinGrant.user_id == user_id,
                RouletteSpinGrant.reason == SpinGrantReason.REFERRAL,
            )
            == spins
        )
    await books_are_exact(pg)


async def test_many_friends_completing_together_credit_their_referrer_once_each(
    pg: Factory,
) -> None:
    friends = 8

    async def build(session: AsyncSession) -> tuple[int, list[int], list[int]]:
        referrer = await make_user(session, telegram_id=9440)
        invited = [await make_user(session, telegram_id=9441 + i) for i in range(friends)]
        for friend in invited:
            await refer(session, referrer, friend)
        orders = [await shipped_order(session, friend) for friend in invited]
        return referrer.id, [friend.id for friend in invited], orders

    referrer, invited, orders = await seed(pg, build)

    results = await at_once(pg, [completing(order_id) for order_id in orders * 2])

    assert unexpected(results) == []
    assert await amounts(pg, referrer, REFERRAL) == [2] * friends
    for friend in invited:
        assert await amounts(pg, friend, REFERRAL) == [2]
    assert (
        await count(
            pg,
            RouletteSpinGrant,
            RouletteSpinGrant.user_id == referrer,
            RouletteSpinGrant.reason == SpinGrantReason.REFERRAL,
        )
        == friends
    )
    await books_are_exact(pg)


# ======================================================= one customer, every kind of load


async def test_one_customer_under_every_kind_of_load_keeps_exact_books(pg: Factory) -> None:
    """
    At the same moment: their 5th purchase completes (twice), their friend's
    first order pays them a referral bonus (twice), they spin their welcome
    spin (three taps) and try to claim a free bottle (three taps).
    """

    async def build(session: AsyncSession) -> dict[str, int]:
        customer = await make_user(session, telegram_id=9460)
        friend = await make_user(session, telegram_id=9461)
        await refer(session, customer, friend)
        welcome = (await RouletteService(session).grant_initial_spin(customer.id)).grant.id
        admin = AdminService(session, settings=tree_settings())
        for _ in range(4):  # four €50 purchases: 8 stamps
            order = await make_order(session, customer)
            order.total_price = Decimal("50.00")
            for status in TO_COMPLETED:
                order = await admin.set_order_status(order, status)
        return {
            "customer": customer.id,
            "friend": friend.id,
            "welcome": welcome,
            "fifth": await shipped_order(session, customer, "40.00"),
            "friend_order": await shipped_order(session, friend),
        }

    ids = await seed(pg, build)
    customer = ids["customer"]

    def spinning(session: AsyncSession) -> Awaitable[Any]:
        return RouletteService(session).spin(customer, STAMP_1, grant_id=ids["welcome"])

    def claiming(session: AsyncSession) -> Awaitable[Any]:
        return StampCardService(session).claim_free_bottle(customer)

    results = await at_once(
        pg,
        [
            completing(ids["fifth"]),
            completing(ids["friend_order"]),
            spinning,
            claiming,
            completing(ids["fifth"]),
            spinning,
            claiming,
            completing(ids["friend_order"]),
            spinning,
            claiming,
        ],
    )

    assert unexpected(results, InsufficientStampsError) == []
    # Each source counted exactly once.
    assert await amounts(pg, customer, PURCHASE) == [2, 2, 2, 2, 2]
    assert await count(pg, LoyaltyTransaction, LoyaltyTransaction.order_id == ids["fifth"]) == 1
    assert await count(pg, RouletteSpinGrant, RouletteSpinGrant.order_id == ids["fifth"]) == 1
    assert await amounts(pg, customer, REFERRAL) == [2]
    assert await amounts(pg, ids["friend"], REFERRAL) == [2]
    assert await count(pg, RouletteSpin, RouletteSpin.grant_id == ids["welcome"]) == 1
    # Earned 10 (purchases) + 2 (referral) + 1 (spin) = 13: at most one bottle, paid for exactly.
    claims = await count(
        pg, UserReward, UserReward.user_id == customer, UserReward.source == RewardSource.STAMP_CARD
    )
    assert claims <= 1
    async with pg() as session:
        assert await LoyaltyService(session).balance(customer) == 13 - 10 * claims
    await books_are_exact(pg)


# ======================================================= no lock held across Telegram


def _shown(method: TelegramMethod[Any]) -> str:
    parts = [getattr(method, "text", None) or "", getattr(method, "caption", None) or ""]
    markup = getattr(method, "reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        for row in markup.inline_keyboard:
            for button in row:
                parts += [button.url or "", button.copy_text.text if button.copy_text else ""]
    return " ".join(parts)


class Watchful(FakeTelegram):
    """
    A Telegram that, on every call the bot makes, looks at what the bot still holds.

    From a separate connection, without ever waiting or holding anything another
    probe could trip over: is the attribution lock taken, is the customer's
    loyalty account row locked, and does any link shown point at a code that is
    not saved yet?
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        self._probe = create_async_engine(url, poolclass=NullPool)
        self.findings: list[str] = []

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if not isinstance(method, GetMe):
            await self._look(method)
        return await super().make_request(bot, method, timeout)

    async def _look(self, method: TelegramMethod[Any]) -> None:
        chat = getattr(method, "chat_id", None)
        if isinstance(method, AnswerCallbackQuery):
            chat = int(method.callback_query_id.split(":")[0])
        call = f"{type(method).__name__} to {chat} {_shown(method)[:40]!r}"
        async with self._probe.connect() as connection:
            transaction = await connection.begin()
            try:
                # Read from pg_locks, never taken: a probe that took the lock
                # would be what a concurrent probe (a fan-out) finds held. An
                # advisory lock belongs to one database — another database's, such
                # as a parallel test run on the same server, never blocks this bot.
                held = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                        " AND database = (SELECT oid FROM pg_database"
                        " WHERE datname = current_database())"
                        " AND objsubid = 1 AND ((classid::bigint << 32) | objid::bigint) = :key"
                    ),
                    {"key": ATTRIBUTION_LOCK_KEY},
                )
                if held:
                    self.findings.append(f"{call}: sent while the attribution lock was held")
                for code in re.findall(r"start=ref_([A-Za-z0-9_-]{12})", _shown(method)):
                    saved = await connection.scalar(
                        text("SELECT count(*) FROM loyalty_accounts WHERE referral_code = :code"),
                        {"code": code},
                    )
                    if not saved:
                        self.findings.append(f"{call}: shows a link whose code is not saved yet")
                user_id = (
                    await connection.scalar(
                        text("SELECT id FROM users WHERE telegram_id = :chat"), {"chat": chat}
                    )
                    if isinstance(chat, int)
                    else None
                )
                if user_id is not None:
                    try:
                        # FOR SHARE conflicts with FOR UPDATE and with any UPDATE of
                        # the row, but not with another probe's FOR SHARE.
                        await connection.execute(
                            text(
                                "SELECT 1 FROM loyalty_accounts WHERE user_id = :user "
                                "FOR SHARE NOWAIT"
                            ),
                            {"user": user_id},
                        )
                    except DBAPIError:
                        self.findings.append(f"{call}: their loyalty account was locked")
            finally:
                await transaction.rollback()

    async def close(self) -> None:
        await self._probe.dispose()


async def test_no_telegram_call_waits_while_a_loyalty_lock_is_held(
    pg: Factory, lands: Callable[[str], None]
) -> None:
    alex, bea = 9501, 9502
    bottle = await open_shop(pg)
    async with pg() as session:
        await make_user(session, telegram_id=alex)
        await session.commit()
    telegram = Watchful(URL)
    settings = tree_settings()
    bot = RunningBot(pg, settings, telegram=telegram)
    try:
        await lifecycle.activate_loyalty(settings)

        def link(chat: int) -> str:
            screen = bot.screens(chat)[max(bot.screens(chat))]
            assert screen.reply_markup is not None
            return next(
                b.copy_text.text
                for row in screen.reply_markup.inline_keyboard
                for b in row
                if b.copy_text
            )

        await bot.send(alex, EN.t("menu.invite"))  # Alex's code is created here
        alex_link = link(alex)
        await bot.send(bea, f"/start {alex_link.split('?start=')[1]}", first_name="Bea")
        await bot.press(bea, "lang:en")
        await bot.press(bea, "city:berlin")
        await bot.send(bea, EN.t("menu.invite"))
        bea_link = link(bea)
        # A loop: Alex opening the link of the friend Alex invited, refused under the lock.
        await bot.send(alex, f"/start {bea_link.split('?start=')[1]}")
        lands("stamp_2")
        await bot.send(alex, EN.t("menu.roulette"))
        await bot.press(alex, bot.button(alex, "roulette:spin:"))
        await buy(bot, bea, bottle)
        await check_out(bot, bea)
        async with pg() as session:
            order_id = await session.scalar(select(func.max(Order.id)))
        assert order_id is not None
        await admin_moves(bot, order_id, *TO_COMPLETED)  # the payout; both are told
    finally:
        await telegram.close()

    assert telegram.findings == []
    assert await count(pg, Referral) == 1  # the loop was refused
    await books_are_exact(pg)
