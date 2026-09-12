"""
End-to-end QA of the loyalty ecosystem — the acceptance plan, through the production bot.

Every scenario is fed as real Telegram updates through the production dispatcher
(:mod:`tests.production_bot`): every middleware, router, filter and FSM state,
one database session per update. The existing customer from before launch and
the new customer brought in by a friend each run in every language, with the
customer-facing results checked word for word in that language; then the
negative cases, one test each. Concurrent requests are raced on PostgreSQL in
``tests/test_loyalty_journeys_postgres.py``. Last, on every run: no answer — to a
customer or the admin, on a happy path, a refusal or a replay — waits while the
bot still holds a lock (``tests/test_loyalty_concurrency.py`` checks the same
against PostgreSQL's real locks).

Owner decisions the plan relies on: stamps are earned on the charged total when
an order completes; a free bottle covers a product up to €20; a referral is
attributed at ``/start`` for a customer who never ordered and pays both sides
at that customer's first completed, paid order; a reward used on an order that
is later cancelled stays used.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, GetMe, TelegramMethod
from aiogram.types import Message
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

import app.middlewares.database
from app import lifecycle
from app.handlers.user import roulette as roulette_screen
from app.models.enums import (
    CityChoice,
    LanguageCode,
    OrderStatus,
    PaymentMethod,
    ReferralStatus,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.order import Order
from app.models.product import Product
from app.models.referral import Referral
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.repositories.cart import CartRepository
from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.repositories.order import OrderRepository
from app.repositories.order_item import OrderItemRepository
from app.repositories.referral import ReferralRepository
from app.repositories.roulette_spin_grant import RouletteSpinGrantRepository
from app.repositories.user_reward import UserRewardRepository
from app.services.cart import CartService
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.loyalty_activation import ActivationReport
from app.services.order import OrderService
from app.utils.cache import invalidate_categories_cache
from app.utils.i18n import SUPPORTED_LANGUAGES
from app.utils.roulette_display import ICONS
from app.utils.stamp_card_display import progress_bar
from app.utils.statistics_display import format_amount
from app.verify_deployment import loyalty_health
from tests.factories import make_order, make_user
from tests.production_bot import ADMIN_ID, FakeTelegram, RunningBot, tree_settings
from tests.test_loyalty_journeys import (
    INITIAL,
    MILESTONE,
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
    loyalty_snapshot,
    open_shop,
    rewards,
    spins,
    status_tap,
    to_confirmation,
)
from tests.test_loyalty_languages import double_tap

EN = LocalizationService("en")
ERROR_KEYS = ("error.generic", "error.database", "error.unauthorized", "error.telegram")


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


# ======================================================= reading what the customer saw


def card_progress(i18n: LocalizationService, stamps: int, required: int = 10) -> str:
    filled = min(stamps, required)
    bar = progress_bar(filled, required)
    return i18n.t("stamp_card.progress", bar=bar, filled=filled, required=required)


def money(i18n: LocalizationService, amount: str) -> str:
    currency = tree_settings().currency_symbol
    return format_amount(Decimal(amount), i18n, currency, trim_zero_cents=True)


def latest(bot: RunningBot, chat: int) -> Message:
    screens = bot.screens(chat)
    return screens[max(screens)]


def link_on(screen: Message) -> str:
    assert screen.reply_markup is not None
    return next(
        b.copy_text.text for row in screen.reply_markup.inline_keyboard for b in row if b.copy_text
    )


def spin_buttons(screen: Message) -> list[str]:
    markup = screen.reply_markup
    rows = markup.inline_keyboard if markup is not None else []
    return [
        b.callback_data or ""
        for row in rows
        for b in row
        if (b.callback_data or "").startswith("roulette:spin:")
    ]


def times(bot: RunningBot, chat: int, text: str) -> int:
    """How many messages to ``chat`` contain ``text``."""
    return sum(text in sent for sent in bot.texts(chat))


def no_errors(bot: RunningBot, *chats: int) -> bool:
    errors = {
        LocalizationService(code).t(key) for code in SUPPORTED_LANGUAGES for key in ERROR_KEYS
    }
    return not any(text in errors for chat in chats for text in bot.texts(chat))


async def user_id(sessions: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    async with sessions() as session:
        found = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))
    assert found is not None, f"no user {telegram_id}"
    return found


async def give_stamps(sessions: async_sessionmaker[AsyncSession], user: int, stamps: int) -> None:
    async with sessions() as session:
        await LoyaltyService(session).adjust(user, amount=stamps, note="QA setup")
        await session.commit()


async def to_reward_step(bot: RunningBot, chat: int) -> None:
    """🛒 Cart → Checkout → name, pickup, address, time, contact, cash — up to the rewards."""
    await bot.send(chat, EN.t("menu.cart"))
    await bot.press(chat, "cart:checkout")
    await bot.send(chat, "Clara Schmidt")
    await bot.press(chat, "checkout:delivery:pickup")
    await bot.send(chat, "Alexanderplatz 1")
    await bot.send(chat, "18:00")
    await bot.send(chat, EN.t("checkout.use_telegram"))
    await bot.press(chat, "checkout:pay:cash")


async def books_are_exact(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        health = await loyalty_health(session)
    assert set(health["integrity"].values()) == {0}, health["integrity"]


# ======================================================= the existing customer


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_an_existing_customer_from_before_launch(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None], language: str
) -> None:
    i18n = LocalizationService(language)
    anna = 9101
    bottle = await open_shop(sessions)

    # 1. A customer from before the deployment, with a completed order the programme ignores.
    async with sessions() as session:
        customer = await make_user(session, telegram_id=anna, language=LanguageCode(language))
        old = await make_order(session, customer, status=OrderStatus.COMPLETED)
        old.loyalty_eligible = False
        anna_id = customer.id
        await session.commit()
    settings = tree_settings()
    assert await lifecycle.activate_loyalty(settings) == ActivationReport(1, 1)  # the deployment
    bot = RunningBot(sessions, settings)

    # 2. Exactly one welcome spin, whatever they do first.
    await bot.send(anna, "/start")
    await bot.send(anna, "/start")
    assert await spins(sessions, anna_id, reason=INITIAL) == 1
    assert await spins(sessions, anna_id) == 1

    # 3–4. The stamp card, in their language: 0 of 10 — the old order earned nothing.
    await bot.send(anna, i18n.t("menu.stamp_card"))
    card = bot.texts(anna)[-1]
    assert i18n.t("stamp_card.title") in card
    assert card_progress(i18n, 0) in card
    assert i18n.t("stamp_card.remaining", remaining=10) in card
    assert i18n.t("stamp_card.empty") in card

    # 5–6. A qualifying purchase — €20, completed — earns one stamp.
    await buy(bot, anna, bottle)
    await check_out(bot, anna)
    await admin_moves(bot, (await latest_order(sessions, anna_id)).id, *TO_COMPLETED)
    assert await ledger(sessions, anna_id, PURCHASE) == [1]
    await bot.send(anna, i18n.t("menu.stamp_card"))
    assert card_progress(i18n, 1) in bot.texts(anna)[-1]
    assert i18n.t("stamp_card.remaining", remaining=9) in bot.texts(anna)[-1]

    # 7–8. Purchases 2–5: the fifth qualifying purchase grants exactly one spin.
    for number in range(2, 6):
        await buy(bot, anna, bottle)
        await check_out(bot, anna)
        await admin_moves(bot, (await latest_order(sessions, anna_id)).id, *TO_COMPLETED)
        assert await spins(sessions, anna_id, reason=MILESTONE) == (1 if number == 5 else 0)
    assert await balance(sessions, anna_id) == 5

    # 9–12. The roulette offers both spins; each result is persisted and told in their language.
    await bot.send(anna, i18n.t("menu.roulette"))
    assert i18n.t("roulette.spins", count=2) in bot.texts(anna)[-1]
    lands("stamp_2")
    await bot.press(anna, bot.button(anna, "roulette:spin:"))
    won = bot.texts(anna)[-1]
    assert i18n.t("roulette.won") in won
    stamps_prize = i18n.t("roulette.prize.stamp_2")
    assert (
        i18n.t("roulette.prize_line", icon=ICONS[RoulettePrizeType.STAMPS], prize=stamps_prize)
        in won
    )
    assert card_progress(i18n, 7) in won, "the stamps land on the card at once"
    assert i18n.t("roulette.remaining", count=1) in won
    lands("free_bottle")
    await bot.press(anna, bot.button(anna, "roulette:spin:"))
    jackpot = bot.texts(anna)[-1]
    assert i18n.t("roulette.jackpot") in jackpot
    assert i18n.t("roulette.reward_free_bottle", amount=money(i18n, "20.00")) in jackpot
    assert i18n.t("roulette.remaining", count=0) in jackpot
    ((reward_id, kind, _, status, bound_to),) = await rewards(sessions, anna_id)
    assert (kind, status, bound_to) == (RewardType.FREE_BOTTLE, RewardStatus.AVAILABLE, None)
    assert await balance(sessions, anna_id) == 7

    # 13. The reward used: two bottles at checkout, one of them free; stamps on what was charged.
    await buy(bot, anna, bottle, quantity=2)
    await check_out(bot, anna, reward_id=reward_id)
    order = await latest_order(sessions, anna_id)
    assert order.total_price == Decimal("20.00")
    await admin_moves(bot, order.id, *TO_COMPLETED)
    assert (await rewards(sessions, anna_id))[0][3:] == (RewardStatus.USED, order.id)
    assert await balance(sessions, anna_id) == 8
    assert no_errors(bot, anna, ADMIN_ID)

    # 14–15. The application restarts: every reward and balance exactly as it was.
    before = await loyalty_snapshot(sessions)
    bot.restart()
    assert await lifecycle.activate_loyalty(settings) == ActivationReport(0, 0)
    assert await loyalty_snapshot(sessions) == before
    await bot.send(anna, i18n.t("menu.stamp_card"))
    assert card_progress(i18n, 8) in bot.texts(anna)[-1]
    await bot.send(anna, i18n.t("menu.roulette"))
    assert i18n.t("roulette.spins", count=0) in bot.texts(anna)[-1]
    await books_are_exact(sessions)


# ======================================================= the new customer


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_a_new_customer_invites_a_friend(
    sessions: async_sessionmaker[AsyncSession], language: str
) -> None:
    i18n = LocalizationService(language)
    zoe, max_ = 9201, 9202
    bottle = await open_shop(sessions)
    bot = RunningBot(sessions, tree_settings())  # the default: a referral earns a spin too

    # 1–2. Zoe starts the bot and completes onboarding.
    await bot.send(zoe, "/start", first_name="Zoe")
    await bot.press(zoe, f"lang:{language}")
    await bot.press(zoe, "city:berlin")
    assert i18n.t("onboarding.welcome") in bot.texts(zoe)[-1]
    zoe_id = await user_id(sessions, zoe)
    assert await spins(sessions, zoe_id, reason=INITIAL) == 1

    # 3–4. 👥 Invite a Friend: her personal link — a random code, never an id.
    await bot.send(zoe, i18n.t("menu.invite"))
    screen = latest(bot, zoe)
    assert i18n.t("invite.title") in (screen.text or "")
    link = link_on(screen)
    assert re.fullmatch(r"https://t\.me/VShopTestBot\?start=ref_[A-Za-z0-9_-]{12}", link)
    assert str(zoe) not in link
    payload = link.split("?start=")[1]

    # 5–6. Max opens the link and onboards: attributed to Zoe, who is told.
    first_contact = bot.message(max_, f"/start {payload}", first_name="Max")
    await bot.feed(first_contact)
    await bot.press(max_, f"lang:{language}")
    await bot.press(max_, "city:berlin")
    max_id = await user_id(sessions, max_)
    async with sessions() as session:
        referral = await session.scalar(select(Referral).where(Referral.referred_user_id == max_id))
    assert referral is not None and referral.referrer_user_id == zoe_id
    assert referral.status == ReferralStatus.PENDING
    stamps_text = i18n.plural("invite.stamps", 2)
    assert times(bot, zoe, i18n.t("invite.news.joined")) == 1
    assert times(bot, zoe, i18n.t("invite.news.joined_stamps_spin", stamps=stamps_text)) == 1
    assert await ledger(sessions, max_id, REFERRAL) == []  # nothing is paid at sign-up

    # 10. Repeated /start before the payout — the same update again, the link, a plain one.
    await bot.feed(first_contact)
    await bot.send(max_, f"/start {payload}")
    await bot.send(max_, "/start")
    assert await count(sessions, Referral) == 1
    assert await spins(sessions, max_id, reason=INITIAL) == 1
    assert times(bot, zoe, i18n.t("invite.news.joined")) == 1

    # 7–9. Max's first order completes: +2 stamps each, and Zoe's spin.
    await buy(bot, max_, bottle)
    await check_out(bot, max_)
    await admin_moves(bot, (await latest_order(sessions, max_id)).id, *TO_COMPLETED)
    assert await ledger(sessions, max_id, REFERRAL) == [2]
    assert await ledger(sessions, zoe_id, REFERRAL) == [2]
    assert await spins(sessions, zoe_id, reason=SpinGrantReason.REFERRAL) == 1
    added = i18n.t("invite.news.stamps_added", stamps=stamps_text)
    assert times(bot, zoe, i18n.t("invite.news.paid")) == 1
    assert times(bot, zoe, added) == 1
    assert times(bot, zoe, i18n.t("invite.news.spin_added")) == 1
    assert times(bot, max_, i18n.t("invite.news.welcome_bonus")) == 1
    assert times(bot, max_, added) == 1

    # 10. And after the payout: more /starts and another order pay nothing more.
    await bot.feed(first_contact)
    await bot.send(max_, f"/start {payload}")
    await buy(bot, max_, bottle)
    await check_out(bot, max_)
    await admin_moves(bot, (await latest_order(sessions, max_id)).id, *TO_COMPLETED)
    assert await ledger(sessions, max_id, REFERRAL) == [2]
    assert await ledger(sessions, zoe_id, REFERRAL) == [2]
    assert await spins(sessions, zoe_id, reason=SpinGrantReason.REFERRAL) == 1
    assert times(bot, zoe, i18n.t("invite.news.paid")) == 1

    # Zoe's screen now counts the friend as rewarded.
    await bot.send(zoe, i18n.t("menu.invite"))
    assert i18n.t("invite.stats", invited=1, rewarded=1) in bot.texts(zoe)[-1]
    assert no_errors(bot, zoe, max_, ADMIN_ID)
    await books_are_exact(sessions)


# ======================================================= negative cases


async def referrer_with_link(
    sessions: async_sessionmaker[AsyncSession], bot: RunningBot, telegram_id: int
) -> tuple[int, str]:
    """An onboarded customer who has opened 👥 Invite a Friend: (their id, their /start payload)."""
    async with sessions() as session:
        referrer = (await make_user(session, telegram_id=telegram_id)).id
        await session.commit()
    await bot.send(telegram_id, EN.t("menu.invite"))
    return referrer, link_on(latest(bot, telegram_id)).split("?start=")[1]


@pytest.mark.parametrize(
    "payload",
    [
        "ref_",
        "ref_short",
        "ref_Unknown-code",
        "promo_Abcdefgh1234",
        "ref_" + "x" * 60,
        "ref_abc def",
    ],
)
async def test_an_invalid_referral_link_is_a_plain_start(
    sessions: async_sessionmaker[AsyncSession], payload: str
) -> None:
    bot = RunningBot(sessions, tree_settings())
    alex, _ = await referrer_with_link(sessions, bot, 9301)
    told = len(bot.texts(9301))

    await bot.send(9302, "/start")
    await bot.send(9303, f"/start {payload}")

    assert bot.texts(9303) == bot.texts(9302), "nothing tells a guesser anything"
    assert await count(sessions, Referral) == 0
    assert len(bot.texts(9301)) == told, "the referrer hears nothing"
    assert await ledger(sessions, alex, REFERRAL) == []
    assert no_errors(bot, 9302, 9303)


async def test_opening_your_own_link_is_not_a_referral(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bot = RunningBot(sessions, tree_settings())
    alex, payload = await referrer_with_link(sessions, bot, 9311)

    await bot.send(9311, f"/start {payload}")

    assert await count(sessions, Referral) == 0
    assert times(bot, 9311, EN.t("invite.own_link", menu=EN.t("menu.invite"))) == 1
    assert times(bot, 9311, EN.t("invite.news.joined")) == 0
    assert await ledger(sessions, alex, REFERRAL) == []


async def test_a_second_friends_link_never_changes_the_referrer(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    bottle = await open_shop(sessions)
    bot = RunningBot(sessions, tree_settings())
    alex, alex_link = await referrer_with_link(sessions, bot, 9321)
    cara, cara_link = await referrer_with_link(sessions, bot, 9322)

    await bot.send(9323, f"/start {alex_link}", first_name="Bea")
    await bot.press(9323, "lang:en")
    await bot.press(9323, "city:berlin")
    await bot.send(9323, f"/start {cara_link}")
    await bot.send(9323, f"/start {alex_link}")
    bea = await user_id(sessions, 9323)
    await buy(bot, 9323, bottle)
    await check_out(bot, 9323)
    await admin_moves(bot, (await latest_order(sessions, bea)).id, *TO_COMPLETED)

    async with sessions() as session:
        referrers = list(await session.scalars(select(Referral.referrer_user_id)))
    assert referrers == [alex], "the first referrer, for good"
    assert times(bot, 9321, EN.t("invite.news.joined")) == 1
    assert times(bot, 9322, EN.t("invite.news.joined")) == 0
    assert await ledger(sessions, alex, REFERRAL) == [2]
    assert await ledger(sessions, cara, REFERRAL) == []
    assert await ledger(sessions, bea, REFERRAL) == [2]


async def test_a_duplicated_completion_of_the_fifth_purchase_books_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The Completed tap, delivered twice, and again from a card left open elsewhere."""
    bottle = await open_shop(sessions)
    async with sessions() as session:
        cleo = (await make_user(session, telegram_id=9331)).id
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    for _ in range(4):
        await buy(bot, 9331, bottle)
        await check_out(bot, 9331)
        await admin_moves(bot, (await latest_order(sessions, cleo)).id, *TO_COMPLETED)
    await buy(bot, 9331, bottle)
    await check_out(bot, 9331)
    fifth = await latest_order(sessions, cleo)
    await admin_moves(bot, fifth.id, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)
    open_card = bot.showing(ADMIN_ID, status_tap(fifth.id, OrderStatus.COMPLETED))
    complete = bot.tap(ADMIN_ID, status_tap(fifth.id, OrderStatus.COMPLETED))

    await bot.feed(complete)
    await bot.feed(complete)
    await bot.press(ADMIN_ID, status_tap(fifth.id, OrderStatus.COMPLETED), on=open_card)

    assert await ledger(sessions, cleo, PURCHASE) == [1, 1, 1, 1, 1]
    assert await spins(sessions, cleo, reason=MILESTONE) == 1
    completed = EN.t("notification.status_completed", order_id=fifth.id)
    assert times(bot, 9331, completed) == 1
    assert no_errors(bot, 9331, ADMIN_ID)


async def test_a_cancelled_order_earns_nothing_and_its_reward_stays_used(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    bottle = await open_shop(sessions)
    settings = tree_settings()
    bot = RunningBot(sessions, settings)
    alex, payload = await referrer_with_link(sessions, bot, 9341)
    await bot.send(9342, f"/start {payload}", first_name="Bea")
    await bot.press(9342, "lang:en")
    await bot.press(9342, "city:berlin")
    bea = await user_id(sessions, 9342)
    lands("discount_10")
    await bot.send(9342, EN.t("menu.roulette"))
    await bot.press(9342, bot.button(9342, "roulette:spin:"))
    discount = (await rewards(sessions, bea))[0][0]

    # Her first order, with the discount — and the admin cancels it.
    await buy(bot, 9342, bottle, quantity=2)
    await check_out(bot, 9342, reward_id=discount)
    order = await latest_order(sessions, bea)
    assert order.total_price == Decimal("36.00")
    await admin_moves(bot, order.id, OrderStatus.CANCELLED)

    assert await ledger(sessions, bea, PURCHASE) == []
    assert await ledger(sessions, bea, REFERRAL) == []
    assert await ledger(sessions, alex, REFERRAL) == []
    assert (await rewards(sessions, bea))[0][3:] == (RewardStatus.USED, order.id)  # owner decision
    assert times(bot, 9341, EN.t("invite.news.paid")) == 0

    # Reopened and completed after all: stamps once, and the referral pays now — once.
    for status in (OrderStatus.NEW, *TO_COMPLETED):
        await bot.press(ADMIN_ID, status_tap(order.id, status))
    assert await ledger(sessions, bea, PURCHASE) == [1]  # €36 charged
    assert await ledger(sessions, bea, REFERRAL) == [2]
    assert await ledger(sessions, alex, REFERRAL) == [2]
    assert (await rewards(sessions, bea))[0][3:] == (RewardStatus.USED, order.id)
    assert times(bot, 9341, EN.t("invite.news.paid")) == 1
    assert no_errors(bot, 9341, 9342, ADMIN_ID)
    await books_are_exact(sessions)


async def test_no_spins_means_no_spin(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await make_user(session, telegram_id=9351)
        await session.commit()
    settings = tree_settings(roulette_initial_free_spin=False)
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)

    await bot.send(9351, EN.t("menu.roulette"))
    screen = latest(bot, 9351)
    assert EN.t("roulette.no_spins") in (screen.text or "")
    assert spin_buttons(screen) == [], "no Spin button without a spin"

    await bot.press(9351, "roulette:spin:1", on=screen)

    assert bot.alerts(9351)[-1] == (EN.t("roulette.no_spins_left"), True)
    assert await count(sessions, RouletteSpin) == 0
    assert await count(sessions, RouletteSpinGrant) == 0
    assert no_errors(bot, 9351)


async def test_a_duplicated_spin_tap_spends_one_spin(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    """With a second spin waiting, the repeat must not spend it."""
    bottle = await open_shop(sessions)
    async with sessions() as session:
        dan = (await make_user(session, telegram_id=9361)).id
        await session.commit()
    settings = tree_settings(roulette_spin_every_n_purchases=1)
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)
    await buy(bot, 9361, bottle)
    await check_out(bot, 9361)
    await admin_moves(bot, (await latest_order(sessions, dan)).id, *TO_COMPLETED)
    assert await spins(sessions, dan) == 2

    lands("stamp_1")
    await bot.send(9361, EN.t("menu.roulette"))
    spin = bot.tap(9361, bot.button(9361, "roulette:spin:"))
    await bot.feed(spin)
    await bot.feed(spin)

    assert await count(sessions, RouletteSpin) == 1
    assert (EN.t("roulette.already_played"), False) in bot.alerts(9361)
    assert EN.t("roulette.remaining", count=1) in bot.texts(9361)[-1]
    await bot.press(9361, bot.button(9361, "roulette:spin:"))
    await bot.feed(spin)
    assert await count(sessions, RouletteSpin) == 2
    assert await balance(sessions, dan) == 1 + 1 + 1  # the purchase, then two +1 prizes
    assert no_errors(bot, 9361)


async def test_a_free_bottle_is_claimed_once_and_redeemed_once(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    bottle = await open_shop(sessions)
    async with sessions() as session:
        eve = (await make_user(session, telegram_id=9371)).id
        await session.commit()
    await lifecycle.activate_loyalty(tree_settings())
    bot = RunningBot(sessions, tree_settings())
    await give_stamps(sessions, eve, 10)

    # The claim, tapped twice: one free bottle, ten stamps.
    await bot.send(9371, EN.t("menu.stamp_card"))
    claim = bot.tap(9371, bot.button(9371, "stamp:claim:"))
    await bot.feed(claim)
    await bot.feed(claim)
    ((free_bottle, kind, _, _, _),) = await rewards(sessions, eve)
    assert kind == RewardType.FREE_BOTTLE
    assert await balance(sessions, eve) == 0

    # Redeemed on an order: the bottle charged €0.
    await buy(bot, 9371, bottle)
    await check_out(bot, 9371, reward_id=free_bottle)
    first = await latest_order(sessions, eve)
    assert first.total_price == Decimal("0.00")

    # Never again: not offered at the next checkout, and a crafted tap is refused.
    lands("discount_5")
    await bot.send(9371, EN.t("menu.roulette"))
    await bot.press(9371, bot.button(9371, "roulette:spin:"))
    discount = next(
        r[0] for r in await rewards(sessions, eve) if r[1] == RewardType.DISCOUNT_PERCENT
    )
    await buy(bot, 9371, bottle)
    await to_reward_step(bot, 9371)
    assert bot.shows(9371, f"checkout:reward:{discount}")
    assert not bot.shows(9371, f"checkout:reward:{free_bottle}")
    step = bot.showing(9371, "checkout:reward:none")
    await bot.press(9371, f"checkout:reward:{free_bottle}", on=step)
    assert bot.alerts(9371)[-1] == (EN.t("checkout.reward_unavailable"), True)
    await bot.press(9371, "checkout:reward:none")
    await bot.press(9371, "checkout:confirm")

    second = await latest_order(sessions, eve)
    assert second.id != first.id and second.total_price == Decimal("20.00")
    by_id = {r[0]: r[3:] for r in await rewards(sessions, eve)}
    assert by_id[free_bottle] == (RewardStatus.USED, first.id)
    assert by_id[discount] == (RewardStatus.AVAILABLE, None)
    assert no_errors(bot, 9371)
    await books_are_exact(sessions)


# ======================================================= nothing locked while Telegram waits

# Every read that takes a row lock, and the attribution advisory lock.
LOCKING_READS = (
    (LoyaltyAccountRepository, "get_by_user_id", "loyalty account"),
    (CartRepository, "get_by_user_id_with_items", "cart"),
    (OrderRepository, "get_for_update", "order"),
    (ReferralRepository, "get_for_update", "referral"),
    (ReferralRepository, "lock_attributions", "attribution lock"),
    (RouletteSpinGrantRepository, "get_for_user", "spin grant"),
    (RouletteSpinGrantRepository, "first_available", "spin grant"),
    (UserRewardRepository, "get_for_user", "reward"),
)
ALWAYS_LOCK = {"get_for_update", "lock_attributions"}  # the others lock on for_update=True


class LockWatch(FakeTelegram):
    """
    A Telegram that notes each call the bot makes while a transaction still holds a lock.

    SQLite ignores ``FOR UPDATE``, so a lock counts from the moment the code asks
    for it until that session's transaction ends — when PostgreSQL would grant
    and release it. ``getMe`` is left out: the bot answers it from its cache.
    """

    def __init__(self) -> None:
        super().__init__()
        self.held: dict[int, set[str]] = {}
        self.findings: list[str] = []

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        locks = sorted(set().union(*self.held.values()))
        if locks and not isinstance(method, GetMe):
            chat = getattr(method, "chat_id", None)
            if isinstance(method, AnswerCallbackQuery):
                chat = int(method.callback_query_id.split(":")[0])
            text = getattr(method, "text", None) or ""
            self.findings.append(f"{type(method).__name__} to {chat} {text[:40]!r}: {locks}")
        return await super().make_request(bot, method, timeout)


def noting_locks(telegram: LockWatch, repository: Any, name: str, lock: str) -> Any:
    """``repository.name``, noting ``lock`` as held by its session once it takes it."""
    original = getattr(repository, name)
    always = name in ALWAYS_LOCK

    async def locking(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        if always or kwargs.get("for_update"):
            telegram.held.setdefault(id(self.session.sync_session), set()).add(lock)
        return result

    return locking


@contextmanager
def watching_locks(telegram: LockWatch, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for repository, name, lock in LOCKING_READS:
        monkeypatch.setattr(repository, name, noting_locks(telegram, repository, name, lock))

    def ended(session: Session, transaction: Any) -> None:
        if transaction.parent is None:
            telegram.held.pop(id(session), None)

    event.listen(Session, "after_transaction_end", ended)
    try:
        yield
    finally:
        event.remove(Session, "after_transaction_end", ended)


async def on_sale(sessions: async_sessionmaker[AsyncSession], product: int, active: bool) -> None:
    async with sessions() as session:
        await session.execute(update(Product).where(Product.id == product).values(is_active=active))
        await session.commit()


async def card_version(sessions: async_sessionmaker[AsyncSession], user: int) -> int:
    async with sessions() as session:
        return await LoyaltyService(session).ledger_version(user)


async def test_no_answer_waits_while_a_lock_is_held(
    sessions: async_sessionmaker[AsyncSession],
    lands: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy paths, refusals and replays all end their transaction before anyone is told."""
    ann, ben, cal, dee, rex, fay = 9401, 9402, 9403, 9404, 9405, 9406
    bottle = await open_shop(sessions)
    async with sessions() as session:
        for telegram_id in (ann, ben, cal, dee, rex, ADMIN_ID):
            await make_user(session, telegram_id=telegram_id)
        await session.commit()
    settings = tree_settings()
    await lifecycle.activate_loyalty(settings)  # a welcome spin each
    telegram = LockWatch()
    bot = RunningBot(sessions, settings, telegram=telegram)
    dee_id = await user_id(sessions, dee)

    with watching_locks(telegram, monkeypatch):
        # Checkout refused at the last tap: sold out, emptied, no longer deliverable.
        await buy(bot, ann, bottle)
        await to_confirmation(bot, ann)
        await on_sale(sessions, bottle, False)
        await bot.press(ann, "checkout:confirm")
        await on_sale(sessions, bottle, True)

        await buy(bot, ben, bottle)
        await bot.send(ben, EN.t("menu.cart"))
        cart, item = latest(bot, ben), bot.button(ben, "cart:rm:")
        await to_confirmation(bot, ben)
        confirm = bot.showing(ben, "checkout:confirm")
        await bot.press(ben, item, on=cart)
        await bot.press(ben, "checkout:confirm", on=confirm)

        await buy(bot, cal, bottle)
        await to_confirmation(bot, cal)
        async with sessions() as session:
            await session.execute(
                update(User)
                .where(User.telegram_id == cal)
                .values(selected_city=CityChoice.DELIVERY)
            )
            await session.commit()
        await bot.press(cal, "checkout:confirm")

        # The stamp card: a claim, the same card again, a card that moved on, too few stamps.
        await give_stamps(sessions, dee_id, 10)
        await bot.send(dee, EN.t("menu.stamp_card"))
        claim = bot.tap(dee, bot.button(dee, "stamp:claim:"))
        await bot.feed(claim)
        await bot.feed(claim)
        await give_stamps(sessions, dee_id, 1)
        shown = await card_version(sessions, dee_id)
        await give_stamps(sessions, dee_id, 1)
        await bot.press(dee, f"stamp:claim:{shown}", on=latest(bot, dee))
        await bot.press(
            dee, f"stamp:claim:{await card_version(sessions, dee_id)}", on=latest(bot, dee)
        )

        # The roulette: a spin, the same tap again, a spin that is not hers.
        lands("discount_10")
        await bot.send(dee, EN.t("menu.roulette"))
        spin = bot.tap(dee, bot.button(dee, "roulette:spin:"))
        await bot.feed(spin)
        await bot.feed(spin)
        await bot.press(dee, "roulette:spin:999999", on=latest(bot, dee))

        # Rewards at checkout: one that is not hers refused, then her free bottle used.
        (free_bottle,) = [
            r[0] for r in await rewards(sessions, dee_id) if r[1] == RewardType.FREE_BOTTLE
        ]
        await buy(bot, dee, bottle)
        await to_confirmation(bot, dee)
        step = bot.showing(dee, "checkout:reward:none")
        await bot.press(dee, "checkout:reward:999999", on=step)
        await bot.press(dee, f"checkout:reward:{free_bottle}", on=step)
        await bot.press(dee, "checkout:confirm")

        # A friend through the link; her first order completed, tapped again, then cancelled.
        await bot.send(rex, EN.t("menu.invite"))
        payload = link_on(latest(bot, rex)).split("?start=")[1]
        await bot.send(fay, f"/start {payload}", first_name="Fay")
        await bot.press(fay, "lang:en")
        await bot.press(fay, "city:berlin")
        await buy(bot, fay, bottle)
        await check_out(bot, fay)
        order = await latest_order(sessions, await user_id(sessions, fay))
        await admin_moves(bot, order.id, *TO_COMPLETED)
        card = latest(bot, ADMIN_ID)
        for status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
            await bot.press(ADMIN_ID, status_tap(order.id, status), on=card)

    # Each step was answered as it should be ...
    assert bot.alerts(ann)[-1] == (EN.t("checkout.inactive_product"), True)
    assert (EN.t("checkout.empty_cart"), True) in bot.alerts(ben)
    assert bot.alerts(cal)[-1] == (EN.t("checkout.invalid_delivery"), True)
    told = [text for text, _ in bot.alerts(dee)]
    for key in (
        "stamp_card.claimed",
        "stamp_card.already_claimed",
        "stamp_card.changed",
        "stamp_card.not_enough",
        "roulette.already_played",
        "checkout.reward_unavailable",
    ):
        assert EN.t(key) in told, key
    assert await count(sessions, Referral, Referral.status == ReferralStatus.QUALIFIED) == 1
    # ... and none of those answers waited while a lock was held.
    assert telegram.findings == []
    await books_are_exact(sessions)


# ======================================================= failures on the way


async def test_an_unexpected_failure_while_placing_an_order_is_answered(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is kept, and the customer is told and given the menu back — no crash."""
    bottle = await open_shop(sessions)
    async with sessions() as session:
        await make_user(session, telegram_id=9421)
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    await buy(bot, 9421, bottle)
    await to_confirmation(bot, 9421)

    async def lines_fail(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("an order line could not be written")

    monkeypatch.setattr(OrderItemRepository, "add_items", lines_fail)
    await bot.press(9421, "checkout:confirm")

    assert bot.alerts(9421)[-1] == (EN.t("error.generic"), True)
    assert bot.texts(9421)[-1] == EN.t("error.generic")
    assert await count(sessions, Order) == 0


class AdminTapsExpire(FakeTelegram):
    """Telegram refusing the answer to the admin's taps once ``expired`` — too late to answer."""

    expired = False

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if (
            self.expired
            and isinstance(method, AnswerCallbackQuery)
            and method.callback_query_id.startswith(f"{ADMIN_ID}:")
        ):
            raise TelegramBadRequest(method, "Bad Request: query is too old")
        return await super().make_request(bot, method, timeout)


async def test_a_failed_admin_screen_never_holds_back_the_referral_news(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    rex, fay = 9431, 9432
    bottle = await open_shop(sessions)
    async with sessions() as session:
        for telegram_id in (rex, ADMIN_ID):
            await make_user(session, telegram_id=telegram_id)
        await session.commit()
    telegram = AdminTapsExpire()
    bot = RunningBot(sessions, tree_settings(), telegram=telegram)
    await bot.send(rex, EN.t("menu.invite"))
    payload = link_on(latest(bot, rex)).split("?start=")[1]
    await bot.send(fay, f"/start {payload}", first_name="Fay")
    await bot.press(fay, "lang:en")
    await bot.press(fay, "city:berlin")
    await buy(bot, fay, bottle)
    await check_out(bot, fay)
    order = await latest_order(sessions, await user_id(sessions, fay))
    await admin_moves(bot, order.id, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)

    telegram.expired = True  # the admin's Completed tap can no longer be answered
    await bot.press(ADMIN_ID, status_tap(order.id, OrderStatus.COMPLETED), on=latest(bot, ADMIN_ID))

    assert (await latest_order(sessions, await user_id(sessions, fay))).status == (
        OrderStatus.COMPLETED
    )
    assert times(bot, rex, EN.t("invite.news.paid")) == 1, "the referrer was never told"
    assert times(bot, fay, EN.t("invite.news.welcome_bonus")) == 1, "the friend was never told"


async def test_a_malformed_order_list_page_is_answered_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Telegram takes one answer per tap; a second one fails into the error path."""
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    await bot.send(ADMIN_ID, EN.t("admin.menu_orders"))

    for data in ("admin:ord:new:x", "admin:ord:done:-1"):
        before = len(bot.alerts(ADMIN_ID))
        await bot.press(ADMIN_ID, data, on=latest(bot, ADMIN_ID))
        assert bot.alerts(ADMIN_ID)[before:] == [(EN.t("error.invalid_callback"), True)], data


# ======================================================= the guards, through the real handlers


async def test_a_double_tapped_confirm_places_one_order(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two taps, the second while the first is at work: one order, and the second is told."""
    bottle = await open_shop(sessions)
    async with sessions() as session:
        await make_user(session, telegram_id=9441)
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    await buy(bot, 9441, bottle)
    await to_confirmation(bot, 9441)

    await double_tap(bot, 9441, "checkout:confirm", monkeypatch)

    assert await count(sessions, Order) == 1
    assert (EN.t("checkout.already_submitted"), True) in bot.alerts(9441)


async def test_a_repeated_status_tap_tells_the_customer_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A second tap, or the same update delivered again, moves nothing and tells no one."""
    bottle = await open_shop(sessions)
    async with sessions() as session:
        for telegram_id in (9451, ADMIN_ID):
            await make_user(session, telegram_id=telegram_id)
        await session.commit()
    bot = RunningBot(sessions, tree_settings())
    await buy(bot, 9451, bottle)
    await check_out(bot, 9451)
    order = await latest_order(sessions, await user_id(sessions, 9451))
    await admin_moves(bot, order.id, OrderStatus.ACCEPTED)

    again = bot.tap(ADMIN_ID, status_tap(order.id, OrderStatus.ACCEPTED), on=latest(bot, ADMIN_ID))
    await bot.feed(again)
    await bot.feed(again)

    assert times(bot, 9451, EN.t("notification.status_accepted", order_id=order.id)) == 1


async def test_a_reward_used_meanwhile_is_refused_at_confirm_with_nothing_locked(
    sessions: async_sessionmaker[AsyncSession],
    lands: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    At Confirm, the reward chosen on the summary has meanwhile paid for another order.

    The transaction that found it spent is rolled back — its cart and account
    locks with it — before the customer hears, and the reward stays spent once.
    """
    eve = 9481
    bottle = await open_shop(sessions)
    async with sessions() as session:
        await make_user(session, telegram_id=eve)
        await session.commit()
    settings = tree_settings()
    await lifecycle.activate_loyalty(settings)
    telegram = LockWatch()
    bot = RunningBot(sessions, settings, telegram=telegram)
    eve_id = await user_id(sessions, eve)

    lands("discount_10")
    await bot.send(eve, EN.t("menu.roulette"))
    await bot.press(eve, bot.button(eve, "roulette:spin:"))
    ((reward_id, *_),) = await rewards(sessions, eve_id)
    await buy(bot, eve, bottle, quantity=2)
    await to_confirmation(bot, eve)
    await bot.press(eve, f"checkout:reward:{reward_id}")

    # Meanwhile the same reward pays for an order placed elsewhere; the cart is refilled.
    async with sessions() as session:
        user = await session.get(User, eve_id)
        assert user is not None
        await OrderService(session).place_order_from_cart(
            user,
            customer_name="Eve",
            delivery_type="pickup",
            address="Street 1",
            preferred_time="now",
            phone=None,
            payment_method=PaymentMethod.CASH,
            reward_id=reward_id,
        )
    async with sessions() as session:
        product = await session.get(Product, bottle)
        assert product is not None
        await CartService(session).add_product(eve_id, product, quantity=2)
        await session.commit()

    with watching_locks(telegram, monkeypatch):
        await bot.press(eve, "checkout:confirm")

    assert bot.alerts(eve)[-1] == (EN.t("checkout.reward_unavailable"), True)
    assert [row[3] for row in await rewards(sessions, eve_id)] == [RewardStatus.USED]
    assert telegram.findings == []
