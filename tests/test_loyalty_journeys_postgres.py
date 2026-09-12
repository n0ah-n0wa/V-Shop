"""
The customer journeys raced on PostgreSQL — opt-in.

The same production dispatcher and fake Telegram as
``tests/test_loyalty_journeys.py``, on a PostgreSQL where transactions really
overlap: one update delivered several times at once, an admin's tap racing
itself, a customer hammering Spin or Confirm. Every race must end with one
effect, and nobody told twice.

Runs only when ``VSHOP_TEST_POSTGRES_URL`` names an empty database whose name
ends in ``_test`` — the schema is created here and dropped afterwards::

    VSHOP_TEST_POSTGRES_URL=postgresql+asyncpg://vshop:pw@127.0.0.1:55432/loyalty_test \\
        python -m pytest tests/test_loyalty_journeys_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.middlewares.database
import app.models  # noqa: F401  (populates the metadata)
from app import lifecycle
from app.database.base import Base
from app.handlers.user import roulette as roulette_screen
from app.models.enums import OrderStatus, RewardStatus, RewardType, SpinGrantReason
from app.models.loyalty import LoyaltyAccount
from app.models.order import Order
from app.models.referral import Referral
from app.models.roulette import RouletteSpin
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.utils.cache import invalidate_categories_cache
from app.verify_deployment import loyalty_health
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import (
    PURCHASE,
    REFERRAL,
    TO_COMPLETED,
    admin_moves,
    balance,
    buy,
    check_out,
    count,
    latest_order,
    ledger,
    no_errors,
    open_shop,
    rewards,
    said,
    spins,
    status_tap,
)

URL = os.environ.get("VSHOP_TEST_POSTGRES_URL", "")
EN = LocalizationService("en")
RACERS = 6


def _refusal() -> str | None:
    if not URL:
        return "set VSHOP_TEST_POSTGRES_URL to run the PostgreSQL journey races"
    url = make_url(URL)
    if url.get_backend_name() != "postgresql":
        return "VSHOP_TEST_POSTGRES_URL must be a PostgreSQL URL"
    if not (url.database or "").endswith("_test"):
        return "refusing: the test database name must end in _test"
    return None


pytestmark = pytest.mark.skipif(_refusal() is not None, reason=_refusal() or "")


@pytest_asyncio.fixture
async def sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def at_once(bot: RunningBot, *updates: object) -> None:
    """Deliver updates concurrently, as polling hands them to handlers."""
    await asyncio.gather(*(bot.feed(update) for update in updates))  # type: ignore[arg-type]


async def test_a_link_opened_by_one_update_delivered_many_times_at_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent referral processing: one friend, one referral, one reward, one news each."""
    alex, bea = 8601, 8602
    bottle = await open_shop(sessions)
    async with sessions() as session:
        alex_id = (await make_user(session, telegram_id=alex)).id
        await session.commit()
    settings = tree_settings()
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)
    await bot.send(alex, EN.t("menu.invite"))
    invite = bot.screens(alex)[max(bot.screens(alex))]
    assert invite.reply_markup is not None
    link = next(
        b.copy_text.text for row in invite.reply_markup.inline_keyboard for b in row if b.copy_text
    )

    first_contact = bot.message(bea, f"/start {link.split('?start=')[1]}", first_name="Bea")
    await at_once(bot, *[first_contact] * RACERS)

    async with sessions() as session:
        bea_id = int(await session.scalar(select(User.id).where(User.telegram_id == bea)) or 0)
    assert await count(sessions, User, User.telegram_id == bea) == 1
    assert await count(sessions, LoyaltyAccount, LoyaltyAccount.user_id == bea_id) == 1
    assert await count(sessions, Referral) == 1
    assert await spins(sessions, bea_id, reason=SpinGrantReason.INITIAL_PROMO) == 1
    assert sum(t.startswith(EN.t("invite.news.joined")) for t in bot.texts(alex)) == 1

    # Her first order: the admin's Completed tap races itself; one payout, one news.
    await bot.press(bea, "lang:en")
    await bot.press(bea, "city:berlin")
    await buy(bot, bea, bottle)
    await check_out(bot, bea)
    order = await latest_order(sessions, bea_id)
    await admin_moves(bot, order.id, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)
    complete = bot.tap(ADMIN_ID, status_tap(order.id, OrderStatus.COMPLETED))
    await at_once(bot, *[complete] * RACERS)

    assert await ledger(sessions, bea_id, REFERRAL) == [2]
    assert await ledger(sessions, alex_id, REFERRAL) == [2]
    assert await ledger(sessions, bea_id, PURCHASE) == [1]
    assert await spins(sessions, alex_id, reason=SpinGrantReason.REFERRAL) == 1
    assert said(bot, bea, EN.t("notification.status_completed", order_id=order.id)) == 1
    assert sum(t.startswith(EN.t("invite.news.paid")) for t in bot.texts(alex)) == 1
    assert sum(t.startswith(EN.t("invite.news.welcome_bonus")) for t in bot.texts(bea)) == 1
    assert no_errors(bot, alex, bea, ADMIN_ID)


async def test_spin_taps_racing_spend_one_spin(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    """Concurrent roulette spinning: one prize for one spin, however many taps land."""
    cleo = 8611
    async with sessions() as session:
        cleo_id = (await make_user(session, telegram_id=cleo)).id
        await session.commit()
    await lifecycle.activate_loyalty(tree_settings())
    bot = RunningBot(sessions, tree_settings())
    lands("discount_10")
    await bot.send(cleo, EN.t("menu.roulette"))
    spin = bot.tap(cleo, bot.button(cleo, "roulette:spin:"))

    await at_once(bot, *[spin] * RACERS)

    assert await count(sessions, RouletteSpin) == 1
    assert [r[3] for r in await rewards(sessions, cleo_id)] == [RewardStatus.AVAILABLE]
    assert no_errors(bot, cleo)


async def test_confirm_taps_racing_place_one_order(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    """A Confirm tapped over and over: one order, the reward spent once."""
    dan = 8621
    bottle = await open_shop(sessions)
    async with sessions() as session:
        dan_id = (await make_user(session, telegram_id=dan)).id
        await session.commit()
    await lifecycle.activate_loyalty(tree_settings())
    bot = RunningBot(sessions, tree_settings())
    lands("discount_5")
    await bot.send(dan, EN.t("menu.roulette"))
    await bot.press(dan, bot.button(dan, "roulette:spin:"))
    reward_id = (await rewards(sessions, dan_id))[0][0]
    await buy(bot, dan, bottle, quantity=2)
    await bot.send(dan, EN.t("menu.cart"))
    await bot.press(dan, "cart:checkout")
    await bot.send(dan, "Dan")
    await bot.press(dan, "checkout:delivery:pickup")
    await bot.send(dan, "Street 1")
    await bot.send(dan, "now")
    await bot.send(dan, EN.t("checkout.use_telegram"))
    await bot.press(dan, "checkout:pay:cash")
    await bot.press(dan, f"checkout:reward:{reward_id}")
    confirm = bot.tap(dan, "checkout:confirm")

    await at_once(bot, *[confirm] * RACERS)

    assert await count(sessions, Order, Order.user_id == dan_id) == 1
    assert [r[3:] for r in await rewards(sessions, dan_id)] == [
        (RewardStatus.USED, (await latest_order(sessions, dan_id)).id)
    ]
    assert no_errors(bot, dan)


async def test_completions_racing_across_customers_book_each_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent purchase processing: many completions at once, each booked exactly once."""
    bottle = await open_shop(sessions)
    customers = [8631 + i for i in range(3)]
    async with sessions() as session:
        ids = [(await make_user(session, telegram_id=t)).id for t in customers]
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    orders = []
    for telegram_id, user_id in zip(customers, ids, strict=True):
        await buy(bot, telegram_id, bottle, quantity=2)
        await check_out(bot, telegram_id)
        order = await latest_order(sessions, user_id)
        await admin_moves(bot, order.id, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)
        orders.append(order.id)

    taps = [bot.tap(ADMIN_ID, status_tap(order_id, OrderStatus.COMPLETED)) for order_id in orders]
    await at_once(bot, *(taps * 2))

    for telegram_id, user_id, order_id in zip(customers, ids, orders, strict=True):
        assert await ledger(sessions, user_id, PURCHASE) == [2]
        assert said(bot, telegram_id, EN.t("notification.status_completed", order_id=order_id)) == 1
    assert no_errors(bot, ADMIN_ID, *customers)


async def test_claim_taps_racing_claim_one_free_bottle(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent reward requests: one card, many claim taps — one bottle, ten stamps."""
    eve = 8641
    async with sessions() as session:
        eve_id = (await make_user(session, telegram_id=eve)).id
        await session.commit()
    await lifecycle.activate_loyalty(tree_settings())
    async with sessions() as session:
        await LoyaltyService(session).adjust(eve_id, amount=20, note="two full cards")
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    await bot.send(eve, EN.t("menu.stamp_card"))
    claim = bot.tap(eve, bot.button(eve, "stamp:claim:"))

    await at_once(bot, *[claim] * RACERS)

    free = [r for r in await rewards(sessions, eve_id) if r[1] == RewardType.FREE_BOTTLE]
    assert len(free) == 1, "one card claims one bottle, however many taps land"
    assert await balance(sessions, eve_id) == 10
    assert [text for text, _ in bot.alerts(eve)].count(EN.t("stamp_card.claimed")) == 1
    assert no_errors(bot, eve)


async def test_one_customer_pressing_every_reward_button_at_once(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    """A claim, a spin and a checkout confirm — each tapped over and over, all at once."""
    fay = 8651
    bottle = await open_shop(sessions)
    async with sessions() as session:
        fay_id = (await make_user(session, telegram_id=fay)).id
        await session.commit()
    settings = tree_settings(roulette_spin_every_n_purchases=1)
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)
    # A 5% discount from the welcome spin, and a purchase that earns a stamp and a spin.
    lands("discount_5")
    await bot.send(fay, EN.t("menu.roulette"))
    await bot.press(fay, bot.button(fay, "roulette:spin:"))
    discount = (await rewards(sessions, fay_id))[0][0]
    await buy(bot, fay, bottle)
    await check_out(bot, fay)
    await admin_moves(bot, (await latest_order(sessions, fay_id)).id, *TO_COMPLETED)
    async with sessions() as session:
        await LoyaltyService(session).adjust(fay_id, amount=10, note="a full card")
        await session.commit()
    # Three screens ready: the full card, the roulette, and checkout at its last step.
    await bot.send(fay, EN.t("menu.stamp_card"))
    claim = bot.tap(fay, bot.button(fay, "stamp:claim:"))
    await bot.send(fay, EN.t("menu.roulette"))
    spin = bot.tap(fay, bot.button(fay, "roulette:spin:"))
    await buy(bot, fay, bottle)
    await bot.send(fay, EN.t("menu.cart"))
    await bot.press(fay, "cart:checkout")
    await bot.send(fay, "Fay")
    await bot.press(fay, "checkout:delivery:pickup")
    await bot.send(fay, "Street 1")
    await bot.send(fay, "18:00")
    await bot.send(fay, EN.t("checkout.use_telegram"))
    await bot.press(fay, "checkout:pay:cash")
    await bot.press(fay, f"checkout:reward:{discount}")
    confirm = bot.tap(fay, "checkout:confirm")
    lands("discount_10")  # a prize that books no stamps: the card on screen stays current

    await at_once(bot, *([claim] * 3 + [spin] * 3 + [confirm] * 3))

    kinds = [(r[1], r[2], r[3]) for r in await rewards(sessions, fay_id)]
    assert kinds.count((RewardType.FREE_BOTTLE, 1, RewardStatus.AVAILABLE)) == 1
    assert kinds.count((RewardType.DISCOUNT_PERCENT, 10, RewardStatus.AVAILABLE)) == 1
    assert await count(sessions, RouletteSpin) == 2
    assert await count(sessions, Order, Order.user_id == fay_id) == 2
    placed = await latest_order(sessions, fay_id)
    assert str(placed.total_price) == "19.00"
    by_id = {r[0]: r[3:] for r in await rewards(sessions, fay_id)}
    assert by_id[discount] == (RewardStatus.USED, placed.id)
    assert await balance(sessions, fay_id) == 1  # 1 from the purchase + 10, then 10 claimed
    async with sessions() as session:
        health = await loyalty_health(session)
    assert set(health["integrity"].values()) == {0}
    assert no_errors(bot, fay)
