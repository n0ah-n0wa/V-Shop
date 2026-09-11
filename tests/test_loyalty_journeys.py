"""
Loyalty, roulette and referrals, end to end — as customers and the admin use them.

Every step is a real Telegram update fed through the production dispatcher
(:mod:`tests.production_bot`): its middlewares, routers, filters and FSM, one
database session per update. Customers tap only buttons the bot showed them. A
restart is a new dispatcher — empty FSM storage — on the same database, after
the start-up activation runs again. Concurrent deliveries are raced on
PostgreSQL in ``tests/test_loyalty_journeys_postgres.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.middlewares.database
from app import lifecycle
from app.handlers.user import roulette as roulette_screen
from app.models.category import Subcategory
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    ReferralStatus,
    RewardStatus,
    RewardType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty_activation import ActivationReport
from app.utils.cache import invalidate_categories_cache
from app.utils.order_status import status_code
from app.utils.stamp_card_display import progress_bar
from app.verify_deployment import loyalty_health
from tests.factories import make_category, make_order, make_product, make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings

EN = LocalizationService("en")
PURCHASE = LoyaltyTransactionType.PURCHASE
REFERRAL = LoyaltyTransactionType.REFERRAL
INITIAL = SpinGrantReason.INITIAL_PROMO
MILESTONE = SpinGrantReason.PURCHASE_MILESTONE
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
LOYALTY_TABLES = (
    LoyaltyAccount,
    LoyaltyTransaction,
    RouletteSpinGrant,
    RouletteSpin,
    UserReward,
    Referral,
)
ERRORS = tuple(EN.t(key) for key in ("error.generic", "error.database", "error.unauthorized"))


# ======================================================= the shop


@pytest_asyncio.fixture
async def sessions(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The bot's database: every update and every start opens its sessions here."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: factory)
    monkeypatch.setattr(roulette_screen, "FRAME_DELAY", 0)
    invalidate_categories_cache()
    yield factory
    invalidate_categories_cache()


async def open_shop(sessions: async_sessionmaker[AsyncSession]) -> int:
    """A category, a brand and one €20.00 bottle. Returns the bottle's id."""
    async with sessions() as session:
        category = await make_category(session, name="Liquids")
        brand = Subcategory(
            category_id=category.id,
            name_ru="Бренд",
            name_en="Brand",
            name_de="Marke",
            name_uk="Бренд",
        )
        session.add(brand)
        await session.flush()
        bottle = await make_product(session, category, name_en="Mango Ice", price="20.00")
        bottle.subcategory_id = brand.id
        await session.commit()
        return bottle.id


async def customer_from_before_launch(
    sessions: async_sessionmaker[AsyncSession], telegram_id: int
) -> int:
    """An onboarded customer whose completed order predates the loyalty programme."""
    async with sessions() as session:
        user = await make_user(session, telegram_id=telegram_id)
        old = await make_order(session, user, status=OrderStatus.COMPLETED)
        old.loyalty_eligible = False
        await session.commit()
        return user.id


async def buy(bot: RunningBot, telegram_id: int, bottle_id: int, quantity: int = 1) -> None:
    """🛍 Catalog → Liquids → Brand → Mango Ice → Add to cart (``quantity`` taps)."""
    await bot.send(telegram_id, EN.t("menu.catalog"))
    await bot.press(telegram_id, bot.button(telegram_id, "category:"))
    await bot.press(telegram_id, bot.button(telegram_id, "subcat:"))
    await bot.press(telegram_id, f"prod:{bottle_id}")
    for _ in range(quantity):
        await bot.press(telegram_id, f"cart:add:{bottle_id}")


async def check_out(bot: RunningBot, telegram_id: int, *, reward_id: int | None = None) -> None:
    """🛒 Cart → Checkout → name, pickup, address, time, contact, cash → (reward) → confirm."""
    await bot.send(telegram_id, EN.t("menu.cart"))
    await bot.press(telegram_id, "cart:checkout")
    await bot.send(telegram_id, "Clara Schmidt")
    await bot.press(telegram_id, "checkout:delivery:pickup")
    await bot.send(telegram_id, "Alexanderplatz 1")
    await bot.send(telegram_id, "18:00")
    await bot.send(telegram_id, EN.t("checkout.use_telegram"))
    await bot.press(telegram_id, "checkout:pay:cash")
    if bot.shows(telegram_id, "checkout:reward:none"):
        await bot.press(telegram_id, f"checkout:reward:{reward_id if reward_id else 'none'}")
    else:
        assert reward_id is None, "a reward was to be used, but checkout offered none"
    await bot.press(telegram_id, "checkout:confirm")


async def admin_moves(bot: RunningBot, order_id: int, *statuses: OrderStatus) -> None:
    """The admin: 📋 Orders → New orders → the order → a status button, per status."""
    await bot.send(ADMIN_ID, EN.t("admin.menu_orders"))
    await bot.press(ADMIN_ID, "admin:ord:new")
    await bot.press(ADMIN_ID, f"admin:ord:view:{order_id}:new:0")
    for status in statuses:
        await bot.press(ADMIN_ID, status_tap(order_id, status))


def status_tap(order_id: int, status: OrderStatus) -> str:
    return f"admin:ord:st:{order_id}:{status_code(status)}:new:0"


# ======================================================= reading the database


async def latest_order(sessions: async_sessionmaker[AsyncSession], user_id: int) -> Order:
    async with sessions() as session:
        order = await session.scalar(
            select(Order).where(Order.user_id == user_id).order_by(Order.id.desc()).limit(1)
        )
        assert order is not None
        return order


async def ledger(sessions: async_sessionmaker[AsyncSession], user_id: int, kind: Any) -> list[int]:
    async with sessions() as session:
        rows = await session.scalars(
            select(LoyaltyTransaction.amount)
            .where(LoyaltyTransaction.user_id == user_id, LoyaltyTransaction.kind == kind)
            .order_by(LoyaltyTransaction.id)
        )
        return list(rows)


async def balance(sessions: async_sessionmaker[AsyncSession], user_id: int) -> int:
    async with sessions() as session:
        value = await session.scalar(
            select(func.coalesce(func.sum(LoyaltyTransaction.amount), 0)).where(
                LoyaltyTransaction.user_id == user_id
            )
        )
        cached = await session.scalar(
            select(LoyaltyAccount.stamp_balance).where(LoyaltyAccount.user_id == user_id)
        )
        assert cached == value, "the cached balance must match the ledger"
        return int(value)


async def spins(
    sessions: async_sessionmaker[AsyncSession], user_id: int, *, reason: Any = None
) -> int:
    async with sessions() as session:
        statement = (
            select(func.count())
            .select_from(RouletteSpinGrant)
            .where(RouletteSpinGrant.user_id == user_id)
        )
        if reason is not None:
            statement = statement.where(RouletteSpinGrant.reason == reason)
        return int(await session.scalar(statement) or 0)


async def rewards(
    sessions: async_sessionmaker[AsyncSession], user_id: int
) -> list[tuple[Any, ...]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                UserReward.id,
                UserReward.kind,
                UserReward.value,
                UserReward.status,
                UserReward.order_id,
            )
            .where(UserReward.user_id == user_id)
            .order_by(UserReward.id)
        )
        return [tuple(row) for row in rows.all()]


async def count(sessions: async_sessionmaker[AsyncSession], model: type[Any], *where: Any) -> int:
    async with sessions() as session:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        return int(await session.scalar(statement) or 0)


async def loyalty_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> dict[str, list[tuple[Any, ...]]]:
    """Every loyalty row, straight from the database."""
    async with sessions() as session:
        snapshot = {}
        for model in LOYALTY_TABLES:
            table = model.__table__
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            snapshot[table.name] = [tuple(row) for row in rows.all()]
        return snapshot


def progress(stamps: int, required: int = 10) -> str:
    filled = min(stamps, required)
    return EN.t(
        "stamp_card.progress", bar=progress_bar(filled, required), filled=filled, required=required
    )


def said(bot: RunningBot, chat_id: int, text: str) -> int:
    """How many times ``text`` was sent or edited into ``chat_id``."""
    return sum(1 for sent in bot.texts(chat_id) if sent == text)


def no_errors(bot: RunningBot, *chat_ids: int) -> bool:
    return not any(text in ERRORS for chat in chat_ids for text in bot.texts(chat))


# ======================================================= journey 1: an existing customer


async def test_an_existing_customer_through_stamps_spins_rewards_and_a_restart(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    anna = 8101
    bottle = await open_shop(sessions)
    anna_id = await customer_from_before_launch(sessions, anna)
    settings = tree_settings()
    assert await lifecycle.activate_loyalty(settings) == ActivationReport(1, 1)  # the deploy
    bot = RunningBot(sessions, settings)

    # 1–2. The existing customer opens the bot: exactly one welcome spin, whatever they send.
    await bot.send(anna, "/start")
    await bot.send(anna, "/start")
    assert await spins(sessions, anna_id, reason=INITIAL) == 1

    # 3–4. 🪪 My Stamp Card shows where they stand: 0/10 — the order from before launch earned none.
    await bot.send(anna, EN.t("menu.stamp_card"))
    assert progress(0) in bot.texts(anna)[-1]

    # 5–6. A €40 purchase (2 × €20), completed by the admin: +2 stamps.
    await buy(bot, anna, bottle, quantity=2)
    await check_out(bot, anna)
    first = await latest_order(sessions, anna_id)
    assert first.total_price == Decimal("40.00")
    await admin_moves(bot, first.id, *TO_COMPLETED)
    assert await ledger(sessions, anna_id, PURCHASE) == [2]
    assert said(bot, anna, EN.t("notification.status_completed", order_id=first.id)) == 1
    await bot.send(anna, EN.t("menu.stamp_card"))
    assert progress(2) in bot.texts(anna)[-1]

    # 7. Purchases 2–5 (€20 each): the 5th qualifying purchase grants exactly one spin.
    for number in range(2, 6):
        await buy(bot, anna, bottle)
        await check_out(bot, anna)
        await admin_moves(bot, (await latest_order(sessions, anna_id)).id, *TO_COMPLETED)
        assert await spins(sessions, anna_id, reason=MILESTONE) == (1 if number == 5 else 0)
    assert await ledger(sessions, anna_id, PURCHASE) == [2, 1, 1, 1, 1]
    assert await balance(sessions, anna_id) == 6

    # 8–9. Two spins (the welcome one and the milestone one): +2 stamps, then 10% off.
    await bot.send(anna, EN.t("menu.roulette"))
    lands("stamp_2")
    await bot.press(anna, bot.button(anna, "roulette:spin:"))
    lands("discount_10")
    await bot.press(anna, bot.button(anna, "roulette:spin:"))
    assert await balance(sessions, anna_id) == 8
    ((discount_id, kind, value, status, _),) = await rewards(sessions, anna_id)
    assert (kind, value, status) == (RewardType.DISCOUNT_PERCENT, 10, RewardStatus.AVAILABLE)
    assert EN.t("roulette.reward_saved") in bot.texts(anna)[-1]  # …and where to use it

    # 10a. The discount, chosen at checkout: €40 charged €36, +1 stamp on what was charged.
    await buy(bot, anna, bottle, quantity=2)
    await check_out(bot, anna, reward_id=discount_id)
    discounted = await latest_order(sessions, anna_id)
    assert discounted.total_price == Decimal("36.00")
    await admin_moves(bot, discounted.id, *TO_COMPLETED)
    assert (await rewards(sessions, anna_id))[0][3:] == (RewardStatus.USED, discounted.id)
    assert await balance(sessions, anna_id) == 9

    # 10b. One more purchase fills the card; 🎁 Claim Free Bottle; then the bottle at checkout.
    await buy(bot, anna, bottle)
    await check_out(bot, anna)
    await admin_moves(bot, (await latest_order(sessions, anna_id)).id, *TO_COMPLETED)
    await bot.send(anna, EN.t("menu.stamp_card"))
    assert progress(10) in bot.texts(anna)[-1]
    claim = bot.button(anna, "stamp:claim:")
    await bot.press(anna, claim)
    assert (EN.t("stamp_card.claimed"), True) in bot.alerts(anna)
    bottle_reward = [r for r in await rewards(sessions, anna_id) if r[1] == RewardType.FREE_BOTTLE]
    assert [r[3] for r in bottle_reward] == [RewardStatus.AVAILABLE]
    assert await balance(sessions, anna_id) == 0
    await buy(bot, anna, bottle)
    await check_out(bot, anna, reward_id=bottle_reward[0][0])
    free = await latest_order(sessions, anna_id)
    assert free.total_price == Decimal("0.00")
    await admin_moves(bot, free.id, *TO_COMPLETED)
    assert await ledger(sessions, anna_id, PURCHASE) == [2, 1, 1, 1, 1, 1, 1]  # €0 is no purchase
    assert await spins(sessions, anna_id, reason=MILESTONE) == 1
    assert no_errors(bot, anna, ADMIN_ID)

    # 11–12. The application restarts: every loyalty row survives exactly as it was.
    before = await loyalty_snapshot(sessions)
    old_spin_screen = bot.showing(anna, bot.button(anna, "roulette:"))
    bot.restart()
    assert await lifecycle.activate_loyalty(settings) == ActivationReport(0, 0)
    assert await loyalty_snapshot(sessions) == before
    async with sessions() as session:
        health = await loyalty_health(session)
    assert set(health["integrity"].values()) == {0}
    assert health["coverage"] == {"users_without_account": 0, "users_without_welcome_spin": 0}
    await bot.send(anna, EN.t("menu.stamp_card"))
    assert progress(0) in bot.texts(anna)[-1]
    # A button from before the restart changes nothing.
    await bot.press(
        anna,
        "stamp:claim:" + claim.removeprefix("stamp:claim:"),
        on=bot.showing(anna, "stamp:open"),
    )
    await bot.feed(bot.tap(anna, "roulette:open", on=old_spin_screen))
    assert await loyalty_snapshot(sessions) == before


# ======================================================= journey 2: a friend through a link


@pytest.mark.parametrize("referral_spins", [1, 0])
async def test_a_friend_invited_through_the_link(
    sessions: async_sessionmaker[AsyncSession], referral_spins: int
) -> None:
    alex, bea = 8200 + referral_spins * 10, 8201 + referral_spins * 10
    bottle = await open_shop(sessions)
    async with sessions() as session:
        alex_id = (await make_user(session, telegram_id=alex)).id
        await session.commit()
    settings = tree_settings(referral_spins=referral_spins)
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)

    # 1–2. Alex opens 👥 Invite a Friend and gets their personal link.
    await bot.send(alex, EN.t("menu.invite"))
    invite = bot.screens(alex)[max(bot.screens(alex))]
    assert invite.reply_markup is not None
    link = next(
        b.copy_text.text for row in invite.reply_markup.inline_keyboard for b in row if b.copy_text
    )
    assert link.startswith("https://t.me/VShopTestBot?start=ref_")
    payload = link.split("?start=")[1]

    # 3–4. Bea opens the link and onboards: attributed to Alex; Alex hears a friend joined.
    first_contact = bot.message(bea, f"/start {payload}", first_name="Bea")
    await bot.feed(first_contact)
    await bot.press(bea, "lang:en")
    await bot.press(bea, "city:berlin")
    async with sessions() as session:
        bea_id = int(await session.scalar(select(User.id).where(User.telegram_id == bea)) or 0)
        referral = await session.scalar(select(Referral).where(Referral.referred_user_id == bea_id))
    assert referral is not None and referral.referrer_user_id == alex_id
    assert referral.status == ReferralStatus.PENDING
    joined = sum(t.startswith(EN.t("invite.news.joined")) for t in bot.texts(alex))
    assert joined == 1

    # 8–9. Bea repeats /start — the same update redelivered, the link again, a plain /start.
    await bot.feed(first_contact)
    await bot.send(bea, f"/start {payload}")
    await bot.send(bea, "/start")
    assert await count(sessions, Referral) == 1
    assert await spins(sessions, bea_id, reason=INITIAL) == 1
    assert sum(t.startswith(EN.t("invite.news.joined")) for t in bot.texts(alex)) == 1
    assert await ledger(sessions, bea_id, REFERRAL) == []  # nothing is paid at sign-up

    # 5–7. Bea's first order completes: +2 stamps each, Alex's spin as configured.
    await buy(bot, bea, bottle)
    await check_out(bot, bea)
    order = await latest_order(sessions, bea_id)
    await admin_moves(bot, order.id, *TO_COMPLETED)
    assert await ledger(sessions, bea_id, REFERRAL) == [2]
    assert await ledger(sessions, alex_id, REFERRAL) == [2]
    assert await spins(sessions, alex_id, reason=SpinGrantReason.REFERRAL) == referral_spins
    assert sum(t.startswith(EN.t("invite.news.paid")) for t in bot.texts(alex)) == 1
    assert sum(t.startswith(EN.t("invite.news.welcome_bonus")) for t in bot.texts(bea)) == 1

    # And again after the payout: another order, more /starts — no second reward, no news.
    await bot.feed(first_contact)
    await bot.send(bea, f"/start {payload}")
    await buy(bot, bea, bottle)
    await check_out(bot, bea)
    await admin_moves(bot, (await latest_order(sessions, bea_id)).id, *TO_COMPLETED)
    assert await ledger(sessions, bea_id, REFERRAL) == [2]
    assert await ledger(sessions, alex_id, REFERRAL) == [2]
    assert await spins(sessions, alex_id, reason=SpinGrantReason.REFERRAL) == referral_spins
    assert sum(t.startswith(EN.t("invite.news.paid")) for t in bot.texts(alex)) == 1
    assert no_errors(bot, alex, bea, ADMIN_ID)


# ======================================================= duplicate Telegram updates


async def test_a_redelivered_update_changes_nothing_twice(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    """Telegram redelivers an update the bot did not acknowledge: each is safe to repeat."""
    cleo = 8301
    bottle = await open_shop(sessions)
    settings = tree_settings()
    bot = RunningBot(sessions, settings)

    start = bot.message(cleo, "/start")
    await bot.feed(start)
    await bot.feed(start)
    await bot.press(cleo, "lang:en")
    await bot.press(cleo, "city:berlin")
    async with sessions() as session:
        cleo_id = int(await session.scalar(select(User.id).where(User.telegram_id == cleo)) or 0)
    assert await count(sessions, User, User.telegram_id == cleo) == 1
    assert await count(sessions, LoyaltyAccount, LoyaltyAccount.user_id == cleo_id) == 1
    assert await spins(sessions, cleo_id, reason=INITIAL) == 1

    # A spin tap, delivered twice: one prize, and the repeat is told so.
    lands("discount_5")
    await bot.send(cleo, EN.t("menu.roulette"))
    spin = bot.tap(cleo, bot.button(cleo, "roulette:spin:"))
    await bot.feed(spin)
    await bot.feed(spin)
    assert await count(sessions, RouletteSpin) == 1
    assert len(await rewards(sessions, cleo_id)) == 1
    assert (EN.t("roulette.already_played"), False) in bot.alerts(cleo)

    # A checkout confirmation, delivered twice: one order, the reward spent once.
    reward_id = (await rewards(sessions, cleo_id))[0][0]
    await buy(bot, cleo, bottle)
    await check_out(bot, cleo, reward_id=reward_id)
    confirm = bot.tap(cleo, "checkout:confirm", on=bot.screens(cleo)[max(bot.screens(cleo))])
    await bot.feed(confirm)
    assert await count(sessions, Order, Order.user_id == cleo_id) == 1
    assert (await rewards(sessions, cleo_id))[0][3] == RewardStatus.USED

    # The admin's "Completed" tap, delivered twice: stamps once, the customer told once.
    order = await latest_order(sessions, cleo_id)
    await admin_moves(bot, order.id, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)
    complete = bot.tap(ADMIN_ID, status_tap(order.id, OrderStatus.COMPLETED))
    await bot.feed(complete)
    await bot.feed(complete)
    assert await ledger(sessions, cleo_id, PURCHASE) == [0]  # €19.00 charged: a purchase, 0 stamps
    assert said(bot, cleo, EN.t("notification.status_completed", order_id=order.id)) == 1

    # A claim tap, delivered twice: one free bottle.
    async with sessions() as session:
        from app.services.loyalty import LoyaltyService

        await LoyaltyService(session).adjust(cleo_id, amount=10, note="test setup")
        await session.commit()
    await bot.send(cleo, EN.t("menu.stamp_card"))
    claim = bot.tap(cleo, bot.button(cleo, "stamp:claim:"))
    await bot.feed(claim)
    await bot.feed(claim)
    free_bottles = [r for r in await rewards(sessions, cleo_id) if r[1] == RewardType.FREE_BOTTLE]
    assert len(free_bottles) == 1
    assert await balance(sessions, cleo_id) == 0
    assert no_errors(bot, cleo, ADMIN_ID)


# ======================================================= stale callbacks


async def test_stale_buttons_are_answered_and_change_nothing(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    dan = 8401
    bottle = await open_shop(sessions)
    async with sessions() as session:
        dan_id = (await make_user(session, telegram_id=dan)).id
        await session.commit()
    settings = tree_settings()
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)

    # An old roulette screen: its Spin button, after the spin was played elsewhere.
    await bot.send(dan, EN.t("menu.roulette"))
    old_roulette = bot.showing(dan, bot.button(dan, "roulette:spin:"))
    spin_data = bot.button(dan, "roulette:spin:")
    await bot.send(dan, EN.t("menu.roulette"))
    lands("stamp_1")
    await bot.press(dan, spin_data)
    await bot.press(dan, spin_data, on=old_roulette)
    assert await count(sessions, RouletteSpin) == 1
    assert await balance(sessions, dan_id) == 1

    # An old stamp card: its claim button, after the card changed underneath it.
    async with sessions() as session:
        from app.services.loyalty import LoyaltyService

        await LoyaltyService(session).adjust(dan_id, amount=9, note="test setup")
        await session.commit()
    await bot.send(dan, EN.t("menu.stamp_card"))
    old_card = bot.showing(dan, bot.button(dan, "stamp:claim:"))
    stale_claim = bot.button(dan, "stamp:claim:")
    async with sessions() as session:
        await LoyaltyService(session).adjust(dan_id, amount=1, note="a stamp from elsewhere")
        await session.commit()
    await bot.press(dan, stale_claim, on=old_card)
    assert not [r for r in await rewards(sessions, dan_id) if r[1] == RewardType.FREE_BOTTLE]
    assert (EN.t("stamp_card.changed"), True) in bot.alerts(dan)

    # Checkout buttons after a restart wiped the conversation: answered, keyboard removed.
    await buy(bot, dan, bottle)
    await bot.send(dan, EN.t("menu.cart"))
    await bot.press(dan, "cart:checkout")
    await bot.send(dan, "Dan")
    await bot.press(dan, "checkout:delivery:pickup")
    await bot.send(dan, "Street 1")
    await bot.send(dan, "now")
    await bot.send(dan, EN.t("checkout.use_telegram"))
    payment = bot.showing(dan, "checkout:pay:cash")
    bot.restart()
    answered = await bot.press(dan, "checkout:pay:cash", on=payment)
    assert (EN.t("error.invalid_callback"), True) in bot.alerts(dan)
    assert any(type(m).__name__ == "EditMessageReplyMarkup" for m, _ in answered)
    assert await count(sessions, Order, Order.user_id == dan_id) == 0

    # An old admin order card: "Shipped" after the order was completed from another card.
    await check_out(bot, dan)
    order = await latest_order(sessions, dan_id)
    await admin_moves(bot, order.id, OrderStatus.ACCEPTED)
    old_card_for_admin = bot.showing(ADMIN_ID, status_tap(order.id, OrderStatus.SHIPPED))
    await bot.press(ADMIN_ID, status_tap(order.id, OrderStatus.SHIPPED))
    await bot.press(ADMIN_ID, status_tap(order.id, OrderStatus.COMPLETED))
    stamps = await ledger(sessions, dan_id, PURCHASE)
    await bot.press(ADMIN_ID, status_tap(order.id, OrderStatus.SHIPPED), on=old_card_for_admin)
    async with sessions() as session:
        assert await session.scalar(select(Order.status).where(Order.id == order.id)) == (
            OrderStatus.COMPLETED
        )
    assert await ledger(sessions, dan_id, PURCHASE) == stamps == [1]

    # A button no screen offers any more at all.
    await bot.press(dan, "catalog:discontinued", on=payment)
    assert bot.alerts(dan)[-1] == (EN.t("error.invalid_callback"), True)
    assert no_errors(bot, dan, ADMIN_ID)


async def test_a_stranger_tapping_an_admin_button_is_still_ignored(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The stale-button answer never speaks for the admin router, which drops strangers silently."""
    bot = RunningBot(sessions, tree_settings())
    await bot.send(8501, "/start")
    card = bot.screens(8501)[max(bot.screens(8501))]

    calls = await bot.feed(bot.tap(8501, "admin:ord:st:1:completed:new:0", on=card))

    assert calls == []
    assert MANAGER_CHAT_ID not in {getattr(m, "chat_id", None) for m, _ in bot.telegram.calls}
