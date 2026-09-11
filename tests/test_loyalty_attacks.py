"""
Hostile review — forged and stolen loyalty buttons, through the production bot.

Telegram does not check callback data against the buttons a message carries: a
modified client can send any data it likes from any of its own chats. So these
attacks copy callback data off a victim's screens, or invent it, and send it from
the attacker's own chat through the production dispatcher
(:mod:`tests.production_bot`). Identity always comes from the update's sender,
never from the data, so nothing an attacker sends can spend, claim, see or redeem
anything of another customer's — and a customer's own forged data is refused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from decimal import Decimal

import pytest
import pytest_asyncio
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.middlewares.database
from app import lifecycle
from app.handlers.user import roulette as roulette_screen
from app.models.enums import RewardStatus, RewardType
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.utils.cache import invalidate_categories_cache
from tests.factories import make_user
from tests.production_bot import RunningBot, tree_settings
from tests.test_loyalty_journeys import (
    balance,
    buy,
    count,
    latest_order,
    no_errors,
    open_shop,
    rewards,
)

EN = LocalizationService("en")
VERA, SAM = 7701, 7702  # a customer, and the stranger replaying her buttons


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


def own_screen(bot: RunningBot, chat: int) -> Message:
    """The attacker's latest screen — where his crafted callback claims to come from."""
    screens = bot.screens(chat)
    return screens[max(screens)]


def alerts(bot: RunningBot, telegram_id: int) -> list[str | None]:
    return [text for text, _ in bot.alerts(telegram_id)]


async def test_a_stranger_replaying_a_customers_buttons_gets_nothing_of_hers(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None]
) -> None:
    """Callback manipulation, cross-customer access and stale buttons, one after another."""
    bottle = await open_shop(sessions)
    async with sessions() as session:
        vera_id = (await make_user(session, telegram_id=VERA)).id
        sam_id = (await make_user(session, telegram_id=SAM)).id
        await session.commit()
    settings = tree_settings()
    await lifecycle.activate_loyalty(settings)  # a welcome spin each
    async with sessions() as session:
        await LoyaltyService(session).adjust(vera_id, amount=10, note="a full card")
        await session.commit()
    bot = RunningBot(sessions, settings)

    # Her spin, named from his roulette — before she plays it, and after.
    await bot.send(VERA, EN.t("menu.roulette"))
    her_spin = bot.button(VERA, "roulette:spin:")
    await bot.send(SAM, EN.t("menu.roulette"))
    await bot.press(SAM, her_spin, on=own_screen(bot, SAM))
    assert await count(sessions, RouletteSpin) == 0, "his tap spent a spin"
    lands("discount_10")
    await bot.press(VERA, her_spin)
    await bot.press(SAM, her_spin, on=own_screen(bot, SAM))
    assert await count(sessions, RouletteSpin) == 1, "only hers"
    # Never "already played", which would show him her result.
    assert alerts(bot, SAM) == [EN.t("roulette.stale")] * 2

    # Her claim, named from his card; then her own card, forged from the future.
    await bot.send(VERA, EN.t("menu.stamp_card"))
    her_claim = bot.button(VERA, "stamp:claim:")
    version = int(her_claim.removeprefix("stamp:claim:"))
    await bot.send(SAM, EN.t("menu.stamp_card"))
    await bot.press(SAM, her_claim, on=own_screen(bot, SAM))
    await bot.press(VERA, f"stamp:claim:{version + 1000}", on=bot.showing(VERA, her_claim))
    assert alerts(bot, SAM)[-1] == EN.t("stamp_card.changed")
    assert alerts(bot, VERA)[-1] == EN.t("stamp_card.changed")
    assert await balance(sessions, vera_id) == 10
    assert await balance(sessions, sam_id) == 0
    assert [row[1] for row in await rewards(sessions, vera_id)] == [RewardType.DISCOUNT_PERCENT]
    await bot.press(VERA, her_claim)  # the real button still works — for her
    assert await balance(sessions, vera_id) == 0

    # Her reward, named at his own checkout.
    lands("discount_5")
    await bot.press(SAM, bot.button(SAM, "roulette:spin:"))  # his own spin, his own reward
    her_reward = next(
        row[0] for row in await rewards(sessions, vera_id) if row[1] == RewardType.DISCOUNT_PERCENT
    )
    await buy(bot, SAM, bottle)
    await bot.send(SAM, EN.t("menu.cart"))
    await bot.press(SAM, "cart:checkout")
    await bot.send(SAM, "Sam")
    await bot.press(SAM, "checkout:delivery:pickup")
    await bot.send(SAM, "Street 1")
    await bot.send(SAM, "18:00")
    await bot.send(SAM, EN.t("checkout.use_telegram"))
    await bot.press(SAM, "checkout:pay:cash")
    await bot.press(
        SAM, f"checkout:reward:{her_reward}", on=bot.showing(SAM, "checkout:reward:none")
    )
    assert alerts(bot, SAM)[-1] == EN.t("checkout.reward_unavailable")
    await bot.press(SAM, "checkout:reward:none")
    await bot.press(SAM, "checkout:confirm")

    assert (await latest_order(sessions, sam_id)).total_price == Decimal("20.00")
    hers = {row[0]: row[3:] for row in await rewards(sessions, vera_id)}
    assert hers[her_reward] == (RewardStatus.AVAILABLE, None), "her reward was touched"
    assert [row[3] for row in await rewards(sessions, sam_id)] == [RewardStatus.AVAILABLE]
    assert no_errors(bot, VERA, SAM)


@pytest.mark.parametrize(
    "data",
    [
        "roulette:spin:",
        "roulette:spin:0",
        "roulette:spin:-1",
        "roulette:spin:1.0",
        "roulette:spin:99999999999",
        "roulette:spin:" + "9" * 50,
        "roulette:spin:1;DROP TABLE roulette_spins",
        "roulette:spin:٣",  # an Arabic-Indic three: a real digit, just not his spin
        "stamp:claim:",
        "stamp:claim:-1",
        "stamp:claim:abc",
        "stamp:claim:99999999999",
        "checkout:reward:0",  # outside checkout
        "checkout:reward:abc",
        "invite:close:extra",
    ],
)
async def test_forged_loyalty_callbacks_change_nothing(
    sessions: async_sessionmaker[AsyncSession], data: str
) -> None:
    """Malformed, out-of-range and misplaced payloads: answered, refused, nothing written."""
    async with sessions() as session:
        user_id = (await make_user(session, telegram_id=SAM)).id
        await session.commit()
    await lifecycle.activate_loyalty(tree_settings())
    bot = RunningBot(sessions, tree_settings())
    await bot.send(SAM, EN.t("menu.roulette"))

    await bot.press(SAM, data, on=own_screen(bot, SAM))

    assert await count(sessions, RouletteSpin) == 0
    assert await count(sessions, UserReward) == 0
    assert await balance(sessions, user_id) == 0
    assert bot.alerts(SAM), "answered — never a spinner left turning"
    assert no_errors(bot, SAM)
