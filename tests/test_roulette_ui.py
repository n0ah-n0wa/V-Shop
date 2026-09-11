"""
🎰 Lucky Roulette — the customer-facing screen, through its real handlers.

Spy Telegram objects stand in for the chat; the roulette engine, the spin
grants, the ledger and the database are real. The server draws every prize: a
test chooses which by handing the engine a ticket source, as the engine's own
tests do. No tap — repeated, stale, crafted or concurrent — may win more than
the spins the customer holds, and nothing on screen may come from anywhere but
the backend.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.user import roulette as roulette_screen
from app.handlers.user.roulette import (
    close_roulette,
    open_roulette,
    refresh_roulette,
    spin_roulette,
)
from app.keyboards.catalog import CALLBACK_CATALOG_OPEN
from app.keyboards.reply import main_menu_keyboard
from app.keyboards.roulette import (
    CALLBACK_ROULETTE_CLOSE,
    CALLBACK_ROULETTE_OPEN,
    CALLBACK_ROULETTE_SPIN_PREFIX,
    roulette_keyboard,
    spin_result_keyboard,
)
from app.keyboards.stamp_card import CALLBACK_STAMP_OPEN
from app.models.enums import (
    LanguageCode,
    OrderStatus,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
)
from app.models.loyalty import LoyaltyAccount
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.repositories.roulette_spin import RouletteSpinRepository
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.roulette import PRIZE_CATALOGUE, RouletteService
from app.services.roulette_engine import RouletteEngine, RoulettePolicy
from app.services.stamp_card import StampCard
from app.utils.roulette_display import (
    ICONS,
    RouletteView,
    SpinResultView,
    format_roulette,
    format_spin_result,
    suspense_frames,
)
from app.utils.stamp_card_display import progress_bar
from app.utils.validators import MAX_DB_INT
from tests.factories import make_order, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
LANGS = ("ru", "en", "de", "uk")
EN = LocalizationService("en")
CEILING = Decimal("20.00")
SPIN = CALLBACK_ROULETTE_SPIN_PREFIX
TELEGRAM_CALLBACK_LIMIT = 64
TELEGRAM_ALERT_LIMIT = 200
PRIZE = {prize.code: prize for prize in PRIZE_CATALOGUE}
PRIZES = tuple((prize.kind, prize.value) for prize in PRIZE_CATALOGUE)
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suspense plays frame by frame — just without the pauses."""
    monkeypatch.setattr(roulette_screen, "FRAME_DELAY", 0)


def landing_on(code: str, policy: RoulettePolicy) -> Callable[[int], int]:
    """A ticket source whose draw lands on ``code``."""
    ticket = 0
    for entry in policy.table.entries:
        if entry.prize.code == code:
            break
        ticket += entry.weight
    return lambda total: ticket


@pytest.fixture
def draws(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make the server's draws land on a chosen prize."""

    def land(code: str) -> None:
        def engine(session: AsyncSession, settings: Settings) -> RouletteEngine:
            policy = RoulettePolicy.from_settings(settings)
            return RouletteEngine(session, policy, randbelow=landing_on(code, policy))

        monkeypatch.setattr(roulette_screen, "_engine", engine)

    return land


class Screen(Message):
    """A chat message the handler answers, edits or deletes; records what the customer sees."""

    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        self.shown.append(("answer", text, kwargs.get("reply_markup")))
        return self

    async def edit_text(self, text: str, **kwargs: Any) -> Any:
        markup = kwargs.get("reply_markup")
        if self.shown and self.shown[-1][1:] == (text, markup):
            # Telegram refuses an edit that would change nothing.
            raise TelegramBadRequest(
                method=EditMessageText(text=text),
                message="Bad Request: message is not modified",
            )
        self.shown.append(("edit", text, markup))
        return self

    async def delete(self, **kwargs: Any) -> Any:
        self.shown.append(("delete", "", None))
        return True

    async def edit_reply_markup(self, **kwargs: Any) -> Any:
        self.shown.append(("buttons", "", kwargs.get("reply_markup")))
        return self

    @property
    def shown(self) -> list[tuple[str, str, Any]]:
        return self.__dict__.setdefault("seen", [])


class OldScreen(Screen):
    """A message Telegram no longer lets the bot edit or delete."""

    async def edit_text(self, text: str, **kwargs: Any) -> Any:
        raise TelegramBadRequest(
            method=EditMessageText(text=text),
            message="Bad Request: message can't be edited",
        )

    async def delete(self, **kwargs: Any) -> Any:
        raise TelegramBadRequest(
            method=DeleteMessage(chat_id=1, message_id=7),
            message="Bad Request: message can't be deleted",
        )


class Tap(CallbackQuery):
    """A button tap; records every answer (toast or alert) the handler gives."""

    model_config = {"extra": "allow"}

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None, **kwargs: Any
    ) -> Any:
        self.alerts.append((text, bool(show_alert)))
        return True

    @property
    def alerts(self) -> list[tuple[str | None, bool]]:
        return self.__dict__.setdefault("seen", [])


def customer(user: User) -> TgUser:
    return TgUser(id=user.telegram_id, is_bot=False, first_name="Test", username=user.username)


def chat_message(
    user: User, *, text: str = "roulette", from_bot: bool = True, kind: type[Screen] = Screen
) -> Screen:
    # On a message the bot sent, Telegram reports the *bot* as from_user.
    sender = TgUser(id=1, is_bot=True, first_name="VShop") if from_bot else customer(user)
    return kind(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=user.telegram_id, type="private"),
        from_user=sender,
        text=text,
    )


def tap(user: User, data: str, *, on: Screen | None = None) -> Tap:
    """The customer taps a button on a screen the bot sent."""
    return Tap(
        id="t",
        from_user=customer(user),
        chat_instance="ci",
        data=data,
        message=on if on is not None else chat_message(user),
    )


def last(message: Message | None) -> tuple[str, str, InlineKeyboardMarkup]:
    assert isinstance(message, Screen)
    assert message.shown, "nothing was shown to the customer"
    return message.shown[-1]


def buttons(markup: InlineKeyboardMarkup) -> list[tuple[str, str | None]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def shown_nothing(callback: Tap) -> bool:
    return isinstance(callback.message, Screen) and callback.message.shown == []


def won(i18n: LocalizationService, code: str) -> str:
    """The prize line of a result: the prize's icon and its name."""
    prize = PRIZE[code]
    return i18n.t("roulette.prize_line", icon=ICONS[prize.kind], prize=i18n.t(prize.name_key))


def card_progress(i18n: LocalizationService, filled: int, required: int = 10) -> str:
    """The stamp card's own progress line, as the result draws it."""
    bar = progress_bar(filled, required)
    return i18n.t("stamp_card.progress", bar=bar, filled=filled, required=required)


async def open_screen(
    session: AsyncSession, user: User, *, settings: Settings | None = None
) -> Screen:
    """The customer taps 🎰 Lucky Roulette in the main menu."""
    i18n = LocalizationService.from_user(user)
    message = chat_message(user, text=i18n.t("menu.roulette"), from_bot=False)
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user.telegram_id, user_id=user.telegram_id),
    )
    await open_roulette(
        message=message, session=session, state=state, settings=settings or configured()
    )
    return message


async def play(
    session: AsyncSession, user: User, grant_id: int | str, *, on: Screen | None = None
) -> Tap:
    """The customer taps a Spin button carrying ``grant_id``."""
    callback = tap(user, f"{SPIN}{grant_id}", on=on)
    await spin_roulette(
        callback=callback,
        session=session,
        settings=configured(),
        i18n=LocalizationService.from_user(user),
    )
    return callback


async def refresh(session: AsyncSession, user: User, *, on: Screen | None = None) -> Tap:
    callback = tap(user, CALLBACK_ROULETTE_OPEN, on=on)
    await refresh_roulette(
        callback=callback,
        session=session,
        settings=configured(),
        i18n=LocalizationService.from_user(user),
    )
    return callback


async def with_spins(
    session: AsyncSession,
    telegram_id: int,
    count: int = 1,
    *,
    language: LanguageCode = LanguageCode.EN,
) -> User:
    """A customer holding ``count`` spins: the welcome spin, then milestone spins."""
    user = await make_user(session, telegram_id=telegram_id, language=language)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)
    for _ in range(count - 1):
        order = await make_order(session, user)
        await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)
    return user


async def offered(session: AsyncSession, user: User) -> int:
    """The spin the roulette screen offers: the customer's oldest unspent one."""
    grant_id = await RouletteEngine(session, RoulettePolicy.defaults()).next_grant_id(user.id)
    assert grant_id is not None
    return grant_id


async def complete(session: AsyncSession, user: User) -> None:
    """A €20 order the admin takes all the way to Completed."""
    order = await make_order(session, user)
    order.total_price = Decimal("20.00")
    admin = AdminService(session, settings=configured())
    for status in TO_COMPLETED:
        order = await admin.set_order_status(order, status)


async def count_rows(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def available(session: AsyncSession, user_id: int) -> int:
    return await RouletteService(session).available_spins(user_id)


# ================================================================ main menu


@pytest.mark.parametrize("language", LANGS)
def test_the_main_menu_offers_the_roulette(language: str) -> None:
    """On its own full-width row, right below the stamp card: the loyalty screens together."""
    i18n = LocalizationService(language)
    rows = [[button.text for button in row] for row in main_menu_keyboard(i18n).keyboard]

    assert [i18n.t("menu.roulette")] in rows
    assert rows.index([i18n.t("menu.roulette")]) == rows.index([i18n.t("menu.stamp_card")]) + 1
    assert i18n.t("menu.roulette").startswith("🎰 ")


def test_the_english_label_is_the_one_asked_for() -> None:
    assert EN.t("menu.roulette") == "🎰 Lucky Roulette"


@pytest.mark.parametrize("language", LANGS)
async def test_the_menu_button_is_recognised_in_every_language(language: str) -> None:
    """A customer who switched language mid-session still reaches the roulette."""
    label = LocalizationService(language).t("menu.roulette")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text=label
    )

    assert await LocalizedText("menu.roulette")(message)


# ================================================================== the screen


async def test_no_spins_says_so_and_shows_how_to_earn_one(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9701)

    kind, text, markup = last(await open_screen(session, user))

    assert kind == "answer"
    assert EN.t("roulette.spins", count=0) in text
    assert EN.t("roulette.no_spins") in text
    assert EN.t("roulette.next_spin", orders=5) in text
    assert EN.t("roulette.prizes") in text
    assert {
        "🟢 Stamps: +1 · +2",
        "🏷 Discount on one order: 5% · 10%",
        "🎁 A free bottle up to €20",
    } <= set(text.splitlines()), "every prize, one line per kind"
    assert buttons(markup) == [
        (EN.t("menu.catalog"), CALLBACK_CATALOG_OPEN),
        (EN.t("common.back"), CALLBACK_ROULETTE_CLOSE),
    ], "no Spin button without a spin — and a way on, not a dead end"


async def test_one_spin_offers_exactly_that_spin(session: AsyncSession) -> None:
    user = await with_spins(session, 9702)
    grant = await offered(session, user)

    _, text, markup = last(await open_screen(session, user))

    assert EN.t("roulette.spins", count=1) in text
    assert EN.t("roulette.intro", button=EN.t("roulette.spin")) in text
    assert EN.t("roulette.no_spins") not in text
    assert buttons(markup) == [
        (EN.t("roulette.spin"), f"{SPIN}{grant}"),
        (EN.t("common.back"), CALLBACK_ROULETTE_CLOSE),
    ]
    assert len(markup.inline_keyboard[0]) == 1, "the Spin button has the full width to itself"


async def test_several_spins_are_counted_and_the_oldest_is_offered(session: AsyncSession) -> None:
    user = await with_spins(session, 9703, count=3)
    oldest = (await RouletteService(session).grants.list_for_user(user.id))[0].id

    _, text, markup = last(await open_screen(session, user))

    assert EN.t("roulette.spins", count=3) in text
    assert buttons(markup)[0] == (EN.t("roulette.spin"), f"{SPIN}{oldest}")


async def test_the_prize_list_follows_the_configuration(session: AsyncSession) -> None:
    """A prize weighted 0 cannot be won, so it is not advertised."""
    user = await make_user(session, telegram_id=9704)
    settings = configured(roulette_prize_stamp_2_weight=0, roulette_prize_free_bottle_weight=0)

    _, text, _ = last(await open_screen(session, user, settings=settings))

    lines = text.splitlines()
    assert "🟢 Stamps: +1" in lines
    assert "🏷 Discount on one order: 5% · 10%" in lines
    assert not any(line.startswith(ICONS[RoulettePrizeType.FREE_BOTTLE]) for line in lines)


async def test_the_countdown_to_the_next_spin_follows_completed_orders(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9705)
    for _ in range(3):
        await complete(session, user)

    _, text, _ = last(await open_screen(session, user))

    assert EN.t("roulette.next_spin", orders=2) in text


async def test_no_countdown_when_orders_earn_no_spins(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9706)

    _, text, _ = last(
        await open_screen(session, user, settings=configured(roulette_spin_every_n_purchases=0))
    )

    assert EN.t("roulette.next_spin", orders=0).split("<b>")[0] not in text


async def test_opening_the_roulette_again_and_again_changes_nothing(
    session: AsyncSession,
) -> None:
    user = await with_spins(session, 9707)

    texts = [last(await open_screen(session, user))[1] for _ in range(3)]

    assert len(set(texts)) == 1
    assert await count_rows(session, RouletteSpin) == 0
    assert await count_rows(session, LoyaltyAccount) == 0, "looking creates no account"
    assert await count_rows(session, RouletteSpinGrant) == 1, "and no spin"
    assert await available(session, user.id) == 1


# ==================================================================== spinning


async def test_a_spin_builds_suspense_then_shows_the_saved_result(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    draws("discount_10")
    user = await with_spins(session, 9710, count=2)
    user_id = user.id
    grant = await offered(session, user)
    await session.commit()  # so the rollback below can only undo the spin itself

    callback = await play(session, user, grant)

    screen = callback.message
    assert isinstance(screen, Screen)
    frames = suspense_frames(EN)
    assert screen.shown[: len(frames)] == [("edit", frame, None) for frame in frames], (
        "the reels turn first, with no button to tap twice"
    )
    assert all(EN.t("roulette.prize.discount_10") not in frame for frame in frames)
    kind, text, markup = last(screen)
    assert kind == "edit" and len(screen.shown) == len(frames) + 1
    lines = text.splitlines()
    headline = lines.index(EN.t("roulette.won"))
    assert lines[headline + 1 : headline + 3] == [
        won(EN, "discount_10"),
        EN.t("roulette.reward_saved"),
    ], "the reveal, the prize, and what became of it"
    assert EN.t("roulette.remaining", count=1) in text
    assert callback.alerts == [(EN.t("roulette.good_luck"), False)]

    await session.rollback()  # the spin was committed before the customer saw anything
    assert await count_rows(session, RouletteSpin) == 1
    reward = (await session.scalars(select(UserReward))).one()
    assert (reward.user_id, reward.kind, reward.value, reward.status) == (
        user_id,
        RewardType.DISCOUNT_PERCENT,
        10,
        RewardStatus.AVAILABLE,
    )
    assert await available(session, user_id) == 1


@pytest.mark.parametrize("code", list(PRIZE))
async def test_every_prize_is_shown_as_the_backend_saved_it(
    session: AsyncSession, draws: Callable[[str], None], code: str
) -> None:
    draws(code)
    user = await with_spins(session, 9711)

    callback = await play(session, user, await offered(session, user))

    _, text, markup = last(callback.message)
    saved = (await session.scalars(select(RouletteSpin))).one()
    assert (saved.prize_code, saved.prize_value) == (code, PRIZE[code].value)
    kind = PRIZE[code].kind
    headline = "roulette.jackpot" if kind == RoulettePrizeType.FREE_BOTTLE else "roulette.won"
    assert EN.t(headline) in text
    assert won(EN, code) in text
    assert EN.t("roulette.remaining", count=0) in text
    assert EN.t("roulette.next_spin", orders=5) in text, "none left: when the next one comes"
    stamp_card = (EN.t("menu.stamp_card"), CALLBACK_STAMP_OPEN)
    if kind == RoulettePrizeType.STAMPS:
        assert EN.t("roulette.reward_stamps") in text
        assert card_progress(EN, PRIZE[code].value) in text, "the new stamps, on the card"
        assert await LoyaltyService(session).balance(user.id) == PRIZE[code].value
        assert stamp_card in buttons(markup), "one tap to the stamp card"
    else:
        reward = (await session.scalars(select(UserReward))).one()
        assert reward.spin_id == saved.id
        if kind == RoulettePrizeType.FREE_BOTTLE:
            assert EN.t("roulette.reward_free_bottle", amount="€20") in text
            assert reward.max_item_price == CEILING
        else:
            assert EN.t("roulette.reward_saved") in text
        assert stamp_card not in buttons(markup)
    assert buttons(markup)[-2:] == [
        (EN.t("menu.catalog"), CALLBACK_CATALOG_OPEN),
        (EN.t("roulette.back"), CALLBACK_ROULETTE_OPEN),
    ], "none left: the catalog, where more are earned, and the way back"
    assert not any(data and data.startswith(SPIN) for _, data in buttons(markup)), (
        "the last spin is played: none is offered"
    )


async def test_stamps_that_fill_the_card_point_to_the_free_bottle(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    draws("stamp_1")
    user = await with_spins(session, 9728)
    await LoyaltyService(session).adjust(user.id, amount=9, note="test setup")

    callback = await play(session, user, await offered(session, user))

    _, text, markup = last(callback.message)
    assert card_progress(EN, 10) in text
    assert EN.t("roulette.card_full", menu=EN.t("menu.stamp_card")) in text
    assert (EN.t("menu.stamp_card"), CALLBACK_STAMP_OPEN) in buttons(markup)


async def test_spin_again_offers_the_next_spin(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    draws("stamp_1")
    user = await with_spins(session, 9712, count=2)
    first = await offered(session, user)

    callback = await play(session, user, first)

    second = await offered(session, user)
    _, _, markup = last(callback.message)
    assert second != first
    assert buttons(markup)[0] == (EN.t("roulette.spin_again"), f"{SPIN}{second}")

    again = await play(session, user, second, on=callback.message)  # type: ignore[arg-type]

    assert again.alerts == [(EN.t("roulette.good_luck"), False)]
    assert await count_rows(session, RouletteSpin) == 2
    assert await available(session, user.id) == 0
    assert await LoyaltyService(session).balance(user.id) == 2


# ===================================================== repeated and stale taps


async def test_a_double_tap_plays_one_spin_and_shows_its_result_again(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    draws("free_bottle")
    user = await with_spins(session, 9713, count=2)
    grant = await offered(session, user)
    await play(session, user, grant)

    draws("stamp_2")  # were a second draw made, it would land elsewhere
    second = await play(session, user, grant)

    assert second.alerts == [(EN.t("roulette.already_played"), False)], "a toast, not a popup"
    _, text, _ = last(second.message)
    assert won(EN, "free_bottle") in text, "the first result, not a new draw"
    assert isinstance(second.message, Screen) and len(second.message.shown) == 1, (
        "a replay skips the suspense"
    )
    assert await count_rows(session, RouletteSpin) == 1
    assert await count_rows(session, UserReward) == 1
    assert await LoyaltyService(session).balance(user.id) == 0
    assert await available(session, user.id) == 1, "the second tap spent nothing"


async def test_concurrent_taps_play_one_spin(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    draws("discount_5")
    user = await with_spins(session, 9714, count=2)
    grant = await offered(session, user)
    await session.commit()

    taps = await asyncio.gather(*(play(session, user, grant) for _ in range(3)))

    answers = [callback.alerts for callback in taps]
    assert answers.count([(EN.t("roulette.good_luck"), False)]) == 1
    assert answers.count([(EN.t("roulette.already_played"), False)]) == 2
    assert await count_rows(session, RouletteSpin) == 1
    assert await count_rows(session, UserReward) == 1
    assert await available(session, user.id) == 1


async def test_a_button_naming_someone_elses_spin_plays_nothing(session: AsyncSession) -> None:
    alice = await with_spins(session, 9715)
    mallory = await with_spins(session, 9716)
    alices = await offered(session, alice)

    callback = await play(session, mallory, alices)

    assert callback.alerts == [(EN.t("roulette.stale"), True)]
    kind, text, markup = last(callback.message)
    assert kind == "edit" and EN.t("roulette.spins", count=1) in text, "Mallory's own roulette"
    assert buttons(markup)[0][1] == f"{SPIN}{await offered(session, mallory)}"
    assert await count_rows(session, RouletteSpin) == 0
    assert await count_rows(session, LoyaltyAccount) == 0, "a refused spin writes nothing"
    assert (await available(session, alice.id), await available(session, mallory.id)) == (1, 1)


async def test_a_spin_button_with_no_spin_left_says_so(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9717)

    callback = await play(session, user, 424242)

    assert callback.alerts == [(EN.t("roulette.no_spins_left"), True)]
    _, text, markup = last(callback.message)
    assert EN.t("roulette.no_spins") in text
    assert buttons(markup)[0] == (EN.t("menu.catalog"), CALLBACK_CATALOG_OPEN)
    assert await count_rows(session, RouletteSpin) == 0
    assert await count_rows(session, LoyaltyAccount) == 0


@pytest.mark.parametrize(
    "payload",
    ["", "abc", "0", "-1", "1.5", "1e3", "9" * 40, "1 OR 1=1", str(MAX_DB_INT + 1)],
)
async def test_a_malformed_spin_is_refused_untouched(session: AsyncSession, payload: str) -> None:
    user = await with_spins(session, 9718)

    callback = await play(session, user, payload)

    assert callback.alerts == [(EN.t("error.invalid_callback"), True)]
    assert shown_nothing(callback)
    assert await count_rows(session, RouletteSpin) == 0
    assert await available(session, user.id) == 1


# ================================================================== failures


def connection_lost(statement: str) -> OperationalError:
    return OperationalError(statement, None, ConnectionResetError("server closed the connection"))


async def test_a_failed_spin_loses_nothing_and_the_same_button_retries_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, draws: Callable[[str], None]
) -> None:
    draws("stamp_2")
    user = await with_spins(session, 9719)
    user_id = user.id
    grant = await offered(session, user)
    await session.commit()
    original = RouletteSpinRepository.create_and_add

    async def dropped(self: Any, **fields: Any) -> None:
        await self.session.flush()  # the spent spin reaches the server ...
        raise connection_lost("INSERT INTO roulette_spins …")  # ... then the connection drops

    monkeypatch.setattr(RouletteSpinRepository, "create_and_add", dropped)
    failed = await play(session, user, grant)
    monkeypatch.setattr(RouletteSpinRepository, "create_and_add", original)

    assert failed.alerts == [(EN.t("roulette.failed", button=EN.t("roulette.spin")), True)]
    assert shown_nothing(failed), "no suspense and no result for a spin that did not happen"
    assert await count_rows(session, RouletteSpin) == 0
    assert await available(session, user_id) == 1, "the spin is still there"

    await session.refresh(user)  # the rollback expired everything loaded
    retried = await play(session, user, grant)

    assert retried.alerts == [(EN.t("roulette.good_luck"), False)]
    assert await count_rows(session, RouletteSpin) == 1
    assert await LoyaltyService(session).balance(user_id) == 2


async def test_a_spin_whose_commit_fails_is_undone(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, draws: Callable[[str], None]
) -> None:
    draws("discount_10")
    user = await with_spins(session, 9720)
    user_id = user.id
    grant = await offered(session, user)
    await session.commit()
    real_commit = AsyncSession.commit

    async def commit_lost(self: AsyncSession) -> None:
        raise connection_lost("COMMIT")

    monkeypatch.setattr(AsyncSession, "commit", commit_lost)
    failed = await play(session, user, grant)
    monkeypatch.setattr(AsyncSession, "commit", real_commit)

    assert failed.alerts == [(EN.t("roulette.failed", button=EN.t("roulette.spin")), True)]
    assert shown_nothing(failed)
    assert (await count_rows(session, RouletteSpin), await count_rows(session, UserReward)) == (
        0,
        0,
    )
    assert await available(session, user_id) == 1


async def test_a_screen_telegram_will_not_edit_still_gets_the_result(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    """The suspense is skipped and the result arrives as a new message; the prize is kept."""
    draws("stamp_1")
    user = await with_spins(session, 9721)
    screen = chat_message(user, kind=OldScreen)

    callback = await play(session, user, await offered(session, user), on=screen)

    assert callback.alerts == [(EN.t("roulette.good_luck"), False)]
    assert [kind for kind, _, _ in screen.shown] == ["answer"]
    assert won(EN, "stamp_1") in screen.shown[0][1]
    assert await LoyaltyService(session).balance(user.id) == 1


async def test_a_customer_still_onboarding_can_neither_spin_nor_open(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9722, city=None)
    not_available = LocalizationService.from_user(user).t("common.not_available")

    spun = await play(session, user, 1)
    opened = await refresh(session, user)

    assert spun.alerts == [(not_available, True)]
    assert opened.alerts == [(not_available, True)]
    assert shown_nothing(spun) and shown_nothing(opened)
    assert await count_rows(session, RouletteSpinGrant) == 0


async def test_a_tap_on_a_message_telegram_no_longer_shows_does_nothing(
    session: AsyncSession,
) -> None:
    user = await with_spins(session, 9723)
    callback = Tap(
        id="t",
        from_user=customer(user),
        chat_instance="ci",
        data=f"{SPIN}{await offered(session, user)}",
    )

    await spin_roulette(callback=callback, session=session, settings=configured(), i18n=EN)

    assert callback.alerts == [(None, False)]
    assert await available(session, user.id) == 1


async def test_the_customer_is_answered_in_their_own_language(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    """The stored language wins, whatever the update was injected with."""
    draws("discount_5")
    user = await with_spins(session, 9724, language=LanguageCode.UK)
    callback = tap(user, f"{SPIN}{await offered(session, user)}")

    await spin_roulette(callback=callback, session=session, settings=configured(), i18n=EN)

    uk = LocalizationService("uk")
    assert callback.alerts == [(uk.t("roulette.good_luck"), False)]
    _, text, markup = last(callback.message)
    assert won(uk, "discount_5") in text
    assert buttons(markup)[-1][0] == uk.t("roulette.back")


# ================================================================ navigation


async def test_back_from_a_result_redraws_the_roulette_in_place(
    session: AsyncSession, draws: Callable[[str], None]
) -> None:
    draws("stamp_1")
    user = await with_spins(session, 9725, count=2)
    played = await play(session, user, await offered(session, user))

    back = await refresh(session, user, on=played.message)  # type: ignore[arg-type]

    kind, text, markup = last(back.message)
    assert kind == "edit"
    assert EN.t("roulette.spins", count=1) in text
    assert buttons(markup)[0] == (EN.t("roulette.spin"), f"{SPIN}{await offered(session, user)}")
    assert back.alerts == [(None, False)]


async def test_back_closes_the_roulette(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9726)
    screen = chat_message(user)
    callback = tap(user, CALLBACK_ROULETTE_CLOSE, on=screen)

    await close_roulette(callback=callback)

    assert screen.shown == [("delete", "", None)], "the main menu is what remains"
    assert callback.alerts == [(None, False)]


async def test_back_on_a_screen_too_old_to_delete_drops_its_buttons(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9727)
    screen = chat_message(user, kind=OldScreen)

    await close_roulette(callback=tap(user, CALLBACK_ROULETTE_CLOSE, on=screen))

    assert screen.shown == [("buttons", "", None)]


# ================================================================ rendering

SCREENS = {
    "no-spins": RouletteView(0, None, 5, PRIZES, CEILING),
    "no-spins-purchases-off": RouletteView(0, None, None, PRIZES, CEILING),
    "one-spin": RouletteView(1, 7, 1, PRIZES, CEILING),
    "many-spins": RouletteView(12, 7, 4, PRIZES, CEILING),
}
RESULTS = {
    f"{code}-{state}": SpinResultView(
        prize_key=prize.name_key,
        kind=prize.kind,
        free_bottle_max_price=CEILING if prize.kind == RoulettePrizeType.FREE_BOTTLE else None,
        remaining=remaining,
        next_grant_id=8 if remaining else None,
        orders_to_next_spin=3,
        stamp_card=StampCard(stamps, 10),
    )
    for code, prize in PRIZE.items()
    for state, remaining, stamps in (("more-left", 2, 7), ("last-spin-full-card", 0, 12))
}


def rendered(language: str) -> list[str]:
    i18n = LocalizationService(language)
    return [format_roulette(view, i18n, currency="€") for view in SCREENS.values()] + [
        format_spin_result(result, i18n, currency="€") for result in RESULTS.values()
    ]


@pytest.mark.parametrize("language", LANGS)
def test_every_state_renders_completely_in_every_language(language: str) -> None:
    for text in rendered(language) + suspense_frames(LocalizationService(language)):
        assert "{" not in text and "}" not in text, "an unfilled placeholder"
        assert "roulette." not in text, "a missing key renders as its raw name"


# A phone shows roughly 35-45 characters a line: the screen must stay scannable.
MAX_LINES = 10
MAX_CHARS = 520
MAX_LINE = 100


@pytest.mark.parametrize("language", LANGS)
def test_every_screen_fits_a_phone(language: str) -> None:
    for text in rendered(language):
        assert len(text.splitlines()) <= MAX_LINES
        assert len(text) <= MAX_CHARS
        assert max(len(line) for line in text.splitlines()) <= MAX_LINE


@pytest.mark.parametrize("language", LANGS)
def test_every_notice_fits_a_telegram_alert(language: str) -> None:
    i18n = LocalizationService(language)
    for key in ("failed", "stale", "no_spins_left", "already_played", "good_luck"):
        assert len(i18n.t(f"roulette.{key}")) <= TELEGRAM_ALERT_LIMIT


@pytest.mark.parametrize("language", LANGS)
def test_the_suspense_reveals_nothing_and_never_varies(language: str) -> None:
    i18n = LocalizationService(language)
    frames = suspense_frames(i18n)

    assert len(frames) >= 2
    assert frames == suspense_frames(i18n), "the same frames every spin: no chance on this side"
    for frame in frames:
        assert not any(i18n.t(prize.name_key) in frame for prize in PRIZE_CATALOGUE)


@pytest.mark.parametrize("language", LANGS)
def test_every_payload_fits_telegram_and_carries_only_a_spin_id(language: str) -> None:
    i18n = LocalizationService(language)
    markups = (
        roulette_keyboard(i18n, MAX_DB_INT),
        roulette_keyboard(i18n, None),
        spin_result_keyboard(i18n, MAX_DB_INT, stamps_won=True),
        spin_result_keyboard(i18n, None, stamps_won=False),
    )
    for markup in markups:
        for _, data in buttons(markup):
            assert data is not None and len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT
            if data.startswith(SPIN):
                assert re.fullmatch(rf"{SPIN}\d+", data), "no prize, value or balance in a tap"


# ================================================================= guards


def test_the_screen_never_draws_or_names_a_prize() -> None:
    """Its only way to a prize is RouletteEngine.spin, told nothing but the spin's id."""
    tree = ast.parse((ROOT / "app/handlers/user/roulette.py").read_text(encoding="utf-8"))
    modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    modules |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not modules & {"random", "secrets"}, "no chance on the client side of the engine"
    assert not names & {"RoulettePrize", "RouletteService", "PRIZE_CATALOGUE", "validate_prize"}
    assert "RouletteEngine" in names
    spins = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "spin"
    ]
    assert len(spins) == 1
    assert len(spins[0].args) == 1 and [kw.arg for kw in spins[0].keywords] == ["grant_id"]
