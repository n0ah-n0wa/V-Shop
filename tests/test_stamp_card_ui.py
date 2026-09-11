"""
🪪 My Stamp Card — the customer-facing screen, through its real handlers.

Spy Telegram objects stand in for the chat; the stamp-card service, the ledger
and the database are real. Every figure on screen must come from the backend,
and no tap — repeated, stale or crafted — may claim more than the stamps allow.

The card must answer, at a glance: how many stamps the customer has, how many
they need, how they earn them, what a full card gives, and how to claim it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.user.stamp_card import (
    claim_free_bottle,
    open_stamp_card,
    refresh_stamp_card,
)
from app.keyboards.catalog import CALLBACK_CATALOG_OPEN
from app.keyboards.reply import main_menu_keyboard
from app.keyboards.stamp_card import (
    CALLBACK_STAMP_CLAIM_PREFIX,
    CALLBACK_STAMP_OPEN,
    stamp_card_keyboard,
)
from app.models.enums import LanguageCode, RewardStatus, RewardType
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.reward import UserReward
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.stamp_card import StampCard, StampCardService
from app.utils.stamp_card_display import EMPTY_SLOT, STAMP, format_stamp_card, progress_bar
from app.utils.statistics_display import format_amount
from app.utils.validators import MAX_DB_INT
from tests.factories import make_user

LANGS = ("ru", "en", "de", "uk")
EN = LocalizationService("en")
THRESHOLD = Decimal("20.00")
TELEGRAM_CALLBACK_LIMIT = 64


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


class Screen(Message):
    """A chat message the handler answers or edits; records what the customer sees."""

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

    @property
    def shown(self) -> list[tuple[str, str, Any]]:
        return self.__dict__.setdefault("seen", [])


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


def chat_message(user: User, *, text: str = "card", from_bot: bool = True) -> Screen:
    # On a message the bot sent, Telegram reports the *bot* as from_user.
    sender = TgUser(id=1, is_bot=True, first_name="VShop") if from_bot else customer(user)
    return Screen(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=user.telegram_id, type="private"),
        from_user=sender,
        text=text,
    )


def tap(user: User, data: str, *, on: Screen | None = None) -> Tap:
    """The customer taps a button on a card the bot sent."""
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


def navigation(i18n: LocalizationService) -> list[tuple[str, str | None]]:
    """What every card offers below it: the next step, and a refresh."""
    return [
        (i18n.t("menu.catalog"), CALLBACK_CATALOG_OPEN),
        (i18n.t("stamp_card.refresh"), CALLBACK_STAMP_OPEN),
    ]


def expected_progress(i18n: LocalizationService, filled: int, required: int = 10) -> str:
    bar = STAMP * filled + EMPTY_SLOT * (required - filled)
    return i18n.t("stamp_card.progress", bar=bar, filled=filled, required=required)


def prose_amount(i18n: LocalizationService, amount: Decimal = THRESHOLD) -> str:
    return format_amount(amount, i18n, "€", trim_zero_cents=True)


def render(card: StampCard, i18n: LocalizationService) -> str:
    return format_stamp_card(card, i18n, purchase_threshold=THRESHOLD, currency="€")


async def open_card(
    session: AsyncSession, user: User, *, settings: Settings | None = None
) -> Screen:
    """The customer taps 🪪 My Stamp Card in the main menu."""
    i18n = LocalizationService.from_user(user)
    message = chat_message(user, text=i18n.t("menu.stamp_card"), from_bot=False)
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user.telegram_id, user_id=user.telegram_id),
    )
    await open_stamp_card(
        message=message, session=session, state=state, settings=settings or configured()
    )
    return message


async def claim(session: AsyncSession, user: User, version: int | str) -> Tap:
    callback = tap(user, f"{CALLBACK_STAMP_CLAIM_PREFIX}{version}")
    await claim_free_bottle(
        callback=callback,
        session=session,
        settings=configured(),
        i18n=LocalizationService.from_user(user),
    )
    return callback


async def refresh(session: AsyncSession, user: User, *, on: Screen | None = None) -> Tap:
    callback = tap(user, CALLBACK_STAMP_OPEN, on=on)
    await refresh_stamp_card(
        callback=callback,
        session=session,
        settings=configured(),
        i18n=LocalizationService.from_user(user),
    )
    return callback


async def give(session: AsyncSession, user: User, stamps: int) -> None:
    await LoyaltyService(session).adjust(user.id, amount=stamps, note="test setup")


async def version_of(session: AsyncSession, user: User) -> int:
    return (await StampCardService(session).card(user.id)).version


async def balance(session: AsyncSession, user_id: int) -> int:
    """Straight from the database, never from the identity map."""
    value = await session.scalar(
        select(LoyaltyAccount.stamp_balance).where(LoyaltyAccount.user_id == user_id)
    )
    return int(value or 0)


async def count_rows(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ================================================================ main menu


@pytest.mark.parametrize("language", LANGS)
def test_the_main_menu_offers_the_stamp_card(language: str) -> None:
    """On its own full-width row: the ru/de/uk labels would truncate at half width."""
    i18n = LocalizationService(language)
    layout = [[button.text for button in row] for row in main_menu_keyboard(i18n).keyboard]

    assert layout == [
        [i18n.t("menu.catalog"), i18n.t("menu.cart")],
        [i18n.t("menu.stamp_card")],
        [i18n.t("menu.roulette")],
        [i18n.t("menu.info")],
    ]
    assert i18n.t("menu.stamp_card").startswith("🪪 ")


def test_the_english_labels_are_the_ones_asked_for() -> None:
    assert EN.t("menu.stamp_card") == "🪪 My Stamp Card"
    assert EN.t("stamp_card.claim") == "🎁 Claim Free Bottle"


@pytest.mark.parametrize("language", LANGS)
async def test_the_menu_button_is_recognised_in_every_language(language: str) -> None:
    """A customer who switched language mid-session still reaches the card."""
    label = LocalizationService(language).t("menu.stamp_card")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text=label
    )

    assert await LocalizedText("menu.stamp_card")(message)


# ================================================================== states


async def test_a_customer_without_stamps_sees_an_empty_card(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9401)

    kind, text, markup = last(await open_card(session, user))

    assert kind == "answer"
    assert expected_progress(EN, 0) in text
    assert EN.t("stamp_card.remaining", remaining=10) in text
    assert EN.t("stamp_card.empty") in text
    assert buttons(markup) == navigation(EN), "a way on, not a dead end"


async def test_partial_progress_shows_what_is_left(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9402)
    await give(session, user, 2)

    _, text, markup = last(await open_card(session, user))

    assert "[🟢🟢⚪️⚪️⚪️⚪️⚪️⚪️⚪️⚪️] <b>2/10</b>" in text, "the layout the owner asked for"
    assert EN.t("stamp_card.remaining", remaining=8) in text
    assert EN.t("stamp_card.empty") not in text
    assert EN.t("stamp_card.ready") not in text
    assert buttons(markup) == navigation(EN)


async def test_exactly_ten_stamps_offer_the_claim_and_say_how(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9403)
    await give(session, user, 10)

    _, text, markup = last(await open_card(session, user))

    assert expected_progress(EN, 10) in text
    assert EN.t("stamp_card.ready") in text
    assert EN.t("stamp_card.ready_hint", button=EN.t("stamp_card.claim")) in text
    assert EN.t("stamp_card.remaining", remaining=0) not in text
    version = await version_of(session, user)
    assert buttons(markup) == [
        (EN.t("stamp_card.claim"), f"{CALLBACK_STAMP_CLAIM_PREFIX}{version}"),
        *navigation(EN),
    ]
    assert len(markup.inline_keyboard[0]) == 1, "the claim has the full width to itself"


async def test_more_than_ten_stamps_keep_the_card_full_and_count_the_extra(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9404)
    await give(session, user, 13)

    _, text, markup = last(await open_card(session, user))

    assert expected_progress(EN, 10) in text, "a full card stays full until it is claimed"
    assert EN.t("stamp_card.ready") in text
    assert EN.t("stamp_card.extra", extra=3) in text
    assert buttons(markup)[0][0] == EN.t("stamp_card.claim")


# The owner's line — "buy e-liquid for €20, collect stamps and get the 11th
# bottle free" — as each language says it.
PROMISE = {
    "en": "Buy e-liquid for €20, collect stamps and get your 11th bottle free!",
    "de": "sammeln Sie Stempel und erhalten Sie die 11. Flasche gratis!",
    "ru": "собирайте штампы и получите 11-ю бутылку в подарок!",
    "uk": "збирайте штампи й отримайте 11-ту пляшку в подарунок!",
}


@pytest.mark.parametrize("language", LANGS)
async def test_the_card_says_what_a_full_card_gives_and_how_to_earn_it(
    session: AsyncSession, language: str
) -> None:
    user = await make_user(session, telegram_id=9405, language=LanguageCode(language))
    i18n = LocalizationService(language)

    _, text, _ = last(await open_card(session, user))

    assert i18n.t("stamp_card.title") in text
    assert PROMISE[language] in text
    assert prose_amount(i18n) in text, "the configured amount, in the reader's format"
    assert i18n.t("stamp_card.how", amount=prose_amount(i18n)) in text


async def test_the_card_follows_the_configuration(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9406)
    settings = configured(
        loyalty_stamp_purchase_threshold=Decimal("25.00"), loyalty_stamps_required=8
    )

    _, text, _ = last(await open_card(session, user, settings=settings))

    assert "Buy e-liquid for €25," in text
    assert expected_progress(EN, 0, required=8) in text
    assert "get your 9th bottle free" in text


async def test_opening_the_card_again_and_again_changes_nothing(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9407)

    texts = [last(await open_card(session, user))[1] for _ in range(3)]

    assert len(set(texts)) == 1
    assert await count_rows(session, LoyaltyAccount) == 0, "looking creates no account"
    assert await count_rows(session, LoyaltyTransaction) == 0
    assert await count_rows(session, UserReward) == 0


async def test_refresh_redraws_the_card_in_place(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9408)
    await give(session, user, 2)

    callback = await refresh(session, user)

    kind, text, _ = last(callback.message)
    assert kind == "edit"
    assert expected_progress(EN, 2) in text
    assert callback.alerts == [(None, False)]


async def test_a_refresh_that_changes_nothing_says_the_card_is_up_to_date(
    session: AsyncSession,
) -> None:
    """Telegram refuses a no-op edit; the customer must not be left wondering."""
    user = await make_user(session, telegram_id=9418)
    await give(session, user, 2)
    screen = chat_message(user)
    await refresh(session, user, on=screen)

    second = await refresh(session, user, on=screen)

    assert second.alerts == [(EN.t("stamp_card.up_to_date"), False)], "a toast, not a popup"
    assert len(screen.shown) == 1, "no duplicate card is sent"


# ================================================================== claiming


async def test_claiming_a_full_card(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9409)
    user_id = user.id
    await give(session, user, 10)
    await session.commit()  # so the rollback below can only undo the claim itself

    callback = await claim(session, user, await version_of(session, user))

    assert callback.alerts == [(EN.t("stamp_card.claimed"), True)]
    kind, text, markup = last(callback.message)
    assert kind == "edit", "the card is redrawn in place"
    assert expected_progress(EN, 0) in text, "exactly 10 stamps were spent"
    assert EN.t("stamp_card.waiting", count=1) in text
    assert buttons(markup) == navigation(EN)

    await session.rollback()  # the claim was committed before the customer was told
    assert await balance(session, user_id) == 0
    assert await count_rows(session, UserReward) == 1
    reward = (await session.scalars(select(UserReward))).one()
    assert (reward.user_id, reward.kind, reward.status) == (
        user_id,
        RewardType.FREE_BOTTLE,
        RewardStatus.AVAILABLE,
    )


async def test_a_double_tap_claims_once(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9410)
    await give(session, user, 10)
    version = await version_of(session, user)

    first = await claim(session, user, version)
    second = await claim(session, user, version)

    assert first.alerts == [(EN.t("stamp_card.claimed"), True)]
    assert second.alerts == [(EN.t("stamp_card.already_claimed"), True)]
    assert await count_rows(session, UserReward) == 1
    assert await balance(session, user.id) == 0
    _, text, _ = last(second.message)
    assert expected_progress(EN, 0) in text, "the second tap is shown the current card"


async def test_an_old_card_cannot_claim_again_but_the_fresh_one_can(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9411)
    await give(session, user, 20)
    old = await version_of(session, user)

    await claim(session, user, old)
    again = await claim(session, user, old)

    assert again.alerts == [(EN.t("stamp_card.already_claimed"), True)]
    _, text, markup = last(again.message)
    assert expected_progress(EN, 10) in text
    fresh = await version_of(session, user)
    assert buttons(markup)[0][1] == f"{CALLBACK_STAMP_CLAIM_PREFIX}{fresh}"

    second = await claim(session, user, fresh)

    assert second.alerts == [(EN.t("stamp_card.claimed"), True)]
    assert await count_rows(session, UserReward) == 2
    assert await balance(session, user.id) == 0


async def test_a_card_that_changed_meanwhile_is_redrawn_not_claimed(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9412)
    await give(session, user, 10)
    version = await version_of(session, user)
    await give(session, user, 1)  # e.g. an order completed while the card was open

    callback = await claim(session, user, version)

    assert callback.alerts == [(EN.t("stamp_card.changed"), True)]
    assert await count_rows(session, UserReward) == 0
    assert await balance(session, user.id) == 11
    _, text, markup = last(callback.message)
    assert EN.t("stamp_card.extra", extra=1) in text
    current = await version_of(session, user)
    assert buttons(markup)[0][1] == f"{CALLBACK_STAMP_CLAIM_PREFIX}{current}"


async def test_a_claim_below_ten_stamps_is_refused(session: AsyncSession) -> None:
    """A hand-made claim for the current card still needs the stamps."""
    user = await make_user(session, telegram_id=9413)
    await give(session, user, 5)

    callback = await claim(session, user, await version_of(session, user))

    assert callback.alerts == [(EN.t("stamp_card.not_enough"), True)]
    assert await count_rows(session, UserReward) == 0
    assert await balance(session, user.id) == 5
    _, text, markup = last(callback.message)
    assert expected_progress(EN, 5) in text
    assert buttons(markup) == navigation(EN)


@pytest.mark.parametrize(
    "payload",
    ["", "abc", "-1", "1.5", "1e3", "9" * 40, "1 OR 1=1", str(MAX_DB_INT + 1)],
)
async def test_a_malformed_claim_is_refused_untouched(session: AsyncSession, payload: str) -> None:
    user = await make_user(session, telegram_id=9414)
    await give(session, user, 10)

    callback = await claim(session, user, payload)

    assert callback.alerts == [(EN.t("error.invalid_callback"), True)]
    assert last_shown_nothing(callback)
    assert await count_rows(session, UserReward) == 0
    assert await balance(session, user.id) == 10


def last_shown_nothing(callback: Tap) -> bool:
    return isinstance(callback.message, Screen) and callback.message.shown == []


async def test_a_customer_still_onboarding_cannot_claim_or_refresh(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9415, city=None)
    not_available = LocalizationService.from_user(user).t("common.not_available")

    claimed = await claim(session, user, 0)
    refreshed = await refresh(session, user)

    assert claimed.alerts == [(not_available, True)]
    assert refreshed.alerts == [(not_available, True)]
    assert last_shown_nothing(claimed) and last_shown_nothing(refreshed)
    assert await count_rows(session, LoyaltyAccount) == 0
    assert await count_rows(session, UserReward) == 0


async def test_a_tap_on_a_message_telegram_no_longer_shows_does_nothing(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9416)
    await give(session, user, 10)
    callback = Tap(
        id="t",
        from_user=customer(user),
        chat_instance="ci",
        data=f"{CALLBACK_STAMP_CLAIM_PREFIX}{await version_of(session, user)}",
    )

    await claim_free_bottle(callback=callback, session=session, settings=configured(), i18n=EN)

    assert callback.alerts == [(None, False)]
    assert await count_rows(session, UserReward) == 0
    assert await balance(session, user.id) == 10


async def test_the_customer_is_answered_in_their_own_language(session: AsyncSession) -> None:
    """The stored language wins, whatever the update was injected with."""
    user = await make_user(session, telegram_id=9417, language=LanguageCode.RU)
    await give(session, user, 10)
    callback = tap(user, f"{CALLBACK_STAMP_CLAIM_PREFIX}{await version_of(session, user)}")

    await claim_free_bottle(callback=callback, session=session, settings=configured(), i18n=EN)

    ru = LocalizationService("ru")
    assert callback.alerts == [(ru.t("stamp_card.claimed"), True)]
    _, text, markup = last(callback.message)
    assert ru.t("stamp_card.title") in text
    assert ru.t("stamp_card.waiting", count=1) in text
    assert buttons(markup) == navigation(ru)


# ================================================================ rendering


@pytest.mark.parametrize(
    ("filled", "total", "expected"),
    [
        (0, 10, EMPTY_SLOT * 10),
        (3, 10, STAMP * 3 + EMPTY_SLOT * 7),
        (10, 10, STAMP * 10),
        (12, 10, STAMP * 10),
        (4, 8, STAMP * 4 + EMPTY_SLOT * 4),
        (15, 30, STAMP * 5 + EMPTY_SLOT * 5),
        (29, 30, STAMP * 9 + EMPTY_SLOT),
        (30, 30, STAMP * 10),
    ],
    ids=["empty", "partial", "full", "never-overfull", "short-card", "to-scale", "not-yet", "long"],
)
def test_the_progress_bar(filled: int, total: int, expected: str) -> None:
    assert progress_bar(filled, total) == expected


CARDS = {
    "empty": StampCard(0, 10),
    "partial": StampCard(2, 10, 3),
    "full": StampCard(10, 10, 5),
    "overfull-with-bottles-waiting": StampCard(13, 10, 7, free_bottles_waiting=2),
}


@pytest.mark.parametrize("language", LANGS)
@pytest.mark.parametrize("state", CARDS)
def test_every_state_renders_completely_in_every_language(language: str, state: str) -> None:
    text = render(CARDS[state], LocalizationService(language))

    assert "{" not in text and "}" not in text, "an unfilled placeholder"
    assert "stamp_card." not in text, "a missing key renders as its raw name"


@pytest.mark.parametrize("language", LANGS)
@pytest.mark.parametrize("state", CARDS)
def test_the_card_reads_top_down(language: str, state: str) -> None:
    """Where you are, what you get, what to do next — then, quietly, the rules."""
    i18n = LocalizationService(language)
    card = CARDS[state]
    lines = render(card, i18n).splitlines()

    bar = lines.index(expected_progress(i18n, card.filled))
    assert lines[bar + 1].startswith(i18n.t("stamp_card.promo").split("{")[0]), (
        "the promo sits directly beneath the progress bar"
    )
    status = (
        i18n.t("stamp_card.ready")
        if card.can_claim
        else i18n.t("stamp_card.remaining", remaining=card.remaining)
    )
    assert lines[bar + 3] == status, "the next step is the first thing after the promo"
    assert lines[-1] == i18n.t("stamp_card.how", amount=prose_amount(i18n))


# A phone shows roughly 35-45 characters a line: the card must stay scannable.
MAX_LINES = 12
MAX_CHARS = 420
MAX_LINE = 100


@pytest.mark.parametrize("language", LANGS)
@pytest.mark.parametrize("state", CARDS)
def test_the_card_fits_a_phone_screen(language: str, state: str) -> None:
    text = render(CARDS[state], LocalizationService(language))

    assert len(text.splitlines()) <= MAX_LINES
    assert len(text) <= MAX_CHARS
    assert max(len(line) for line in text.splitlines()) <= MAX_LINE, "no wall of text"


@pytest.mark.parametrize(
    ("amount", "language", "in_prose", "by_default"),
    [
        (Decimal("20.00"), "en", "€20", "€20.00"),
        (Decimal("20.00"), "de", "20 €", "20,00 €"),
        (Decimal("20.50"), "en", "€20.50", "€20.50"),
        (Decimal("1250.00"), "en", "€1,250", "€1,250.00"),
    ],
)
def test_prose_money_drops_zero_cents_only(
    amount: Decimal, language: str, in_prose: str, by_default: str
) -> None:
    i18n = LocalizationService(language)
    assert format_amount(amount, i18n, "€", trim_zero_cents=True) == in_prose
    assert format_amount(amount, i18n, "€") == by_default, "every other screen is unchanged"


@pytest.mark.parametrize(("stamps", "claimable"), [(0, False), (9, False), (10, True), (13, True)])
def test_the_claim_button_appears_only_when_the_backend_allows_it(
    stamps: int, claimable: bool
) -> None:
    card = StampCard(stamps=stamps, stamps_required=10, version=42)
    payloads = [data for _, data in buttons(stamp_card_keyboard(EN, card))]

    assert (f"{CALLBACK_STAMP_CLAIM_PREFIX}42" in payloads) is claimable
    assert payloads[-2:] == [CALLBACK_CATALOG_OPEN, CALLBACK_STAMP_OPEN]


@pytest.mark.parametrize("language", LANGS)
def test_every_payload_fits_telegram(language: str) -> None:
    card = StampCard(stamps=10, stamps_required=10, version=MAX_DB_INT)
    for _, data in buttons(stamp_card_keyboard(LocalizationService(language), card)):
        assert data is not None and len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT
