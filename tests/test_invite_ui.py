"""
👥 Invite a Friend — the referral experience, through its real handlers.

Spy Telegram objects stand in for the chat and the bot; the referral programme,
the ledger and the database are real. Everything shown — the link, what each
side earns, the friends it brought in, the news when a friend joins and when
the rewards land — must come from the backend. The link carries a random code,
never an id, and works when a friend opens it; and nothing a newcomer is shown
may tell them whether the code they used was real.
"""

from __future__ import annotations

import json
import pathlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.user import invite as invite_screen
from app.handlers.user.invite import close_invite, open_invite
from app.handlers.user.start import cmd_start
from app.keyboards.invite import CALLBACK_INVITE_CLOSE, TELEGRAM_SHARE_URL
from app.keyboards.reply import main_menu_keyboard
from app.models.enums import LanguageCode, LoyaltyTransactionType, OrderStatus, ReferralStatus
from app.models.loyalty import LoyaltyTransaction
from app.models.order import Order
from app.models.referral import Referral
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.services.referral import parse_referral_payload
from app.services.referral_notification import ReferralNotificationService
from app.services.referral_program import Invitation, ReferralProgramService
from app.utils.i18n import PLURAL_FORMS, plural_category
from app.utils.invite_display import (
    format_friend_joined,
    format_invite,
    own_link_note,
    share_text,
)
from tests.factories import make_order, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
LANGS = ("ru", "en", "de", "uk")
EN = LocalizationService("en")
BOT = "VShopTestBot"
LINK = re.compile(rf"^https://t\.me/{BOT}\?start=ref_[A-Za-z0-9_-]{{12}}$")
TAGS = re.compile(r"<[^>]+>")
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
TELEGRAM_CALLBACK_LIMIT = 64

# The owner's sentence — "Share your link with a friend: your friend gets +2
# stamps to start, and you get +2 stamps on your card!" — in each language.
OFFER = {
    "en": (
        "🎁 Share your link with a friend: your friend gets +2 stamps to start,"
        " and you get +2 stamps on your card!"
    ),
    "ru": (
        "🎁 Поделитесь ссылкой с другом: друг получит +2 штампа для старта,"
        " а вы — +2 штампа на свою карту!"
    ),
    "de": (
        "🎁 Teilen Sie Ihren Link mit einem Freund: Ihr Freund bekommt +2 Stempel zum Start,"
        " und Sie bekommen +2 Stempel auf Ihre Karte!"
    ),
    "uk": (
        "🎁 Поділіться посиланням з другом: друг отримає +2 штампи для старту,"
        " а ви — +2 штампи на свою картку!"
    ),
}
# The friend's bonus, inflected, as the message shared with the link names it.
FRIEND_BONUS = {"en": "+2 stamps", "ru": "+2 штампа", "de": "+2 Stempel", "uk": "+2 штампи"}


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


class FakeBot:
    """
    Stands in for the bot: ``getMe`` answers with its username, and the messages
    it sends are recorded — or refused, for customers who blocked it.
    """

    def __init__(
        self,
        username: str = BOT,
        *,
        blocked: tuple[int, ...] = (),
        events: list[str] | None = None,
    ) -> None:
        self.username = username
        self.blocked = blocked
        self.events = events if events is not None else []
        self.sent: list[tuple[int, str, InlineKeyboardMarkup | None]] = []

    async def me(self) -> TgUser:
        return TgUser(id=42, is_bot=True, first_name="V-Shop", username=self.username)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.events.append("send")
        if chat_id in self.blocked:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Forbidden: bot was blocked by the user",
            )
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))
        return True

    def to(self, telegram_id: int) -> list[tuple[str, InlineKeyboardMarkup | None]]:
        return [(text, markup) for chat, text, markup in self.sent if chat == telegram_id]


class Screen(Message):
    """A chat message the handler answers or deletes; records what the customer sees."""

    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        self.shown.append(("answer", text, kwargs))
        return self

    async def delete(self, **kwargs: Any) -> Any:
        self.shown.append(("delete", "", kwargs))
        return True

    async def edit_reply_markup(self, **kwargs: Any) -> Any:
        self.shown.append(("buttons", "", kwargs))
        return self

    @property
    def shown(self) -> list[tuple[str, str, dict[str, Any]]]:
        return self.__dict__.setdefault("seen", [])


class OldScreen(Screen):
    """A message Telegram no longer lets the bot delete."""

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


def customer(telegram_id: int, first_name: str = "Test") -> TgUser:
    return TgUser(id=telegram_id, is_bot=False, first_name=first_name)


def from_customer(
    telegram_id: int, text: str, *, kind: type[Screen] = Screen, first_name: str = "Test"
) -> Screen:
    return kind(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=customer(telegram_id, first_name),
        text=text,
    )


def fresh_state(telegram_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
    )


async def open_screen(
    session: AsyncSession,
    user: User,
    *,
    settings: Settings | None = None,
    bot: FakeBot | None = None,
) -> Screen:
    """The customer taps 👥 Invite a Friend in the main menu."""
    label = LocalizationService.from_user(user).t("menu.invite")
    message = from_customer(user.telegram_id, label)
    await open_invite(
        message=message,
        session=session,
        state=fresh_state(user.telegram_id),
        settings=settings or configured(),
        bot=bot or FakeBot(),  # type: ignore[arg-type]
    )
    return message


async def send_start(
    session: AsyncSession,
    telegram_id: int,
    payload: str | None,
    *,
    bot: FakeBot | None = None,
    first_name: str = "Test",
) -> Screen:
    """Someone opens the bot — through a link when ``payload`` is given."""
    text = "/start" if payload is None else f"/start {payload}"
    message = from_customer(telegram_id, text, first_name=first_name)
    await cmd_start(
        message=message,
        state=fresh_state(telegram_id),
        session=session,
        settings=configured(),
        command=CommandObject(prefix="/", command="start", args=payload),
        bot=bot,  # type: ignore[arg-type]
    )
    return message


def answer_of(message: Screen) -> tuple[str, dict[str, Any]]:
    assert message.shown, "nothing was shown to the customer"
    kind, text, kwargs = message.shown[-1]
    assert kind == "answer"
    return text, kwargs


def replies(message: Screen) -> list[tuple[str, Any]]:
    """Everything the customer was answered, with its buttons."""
    return [(text, kwargs.get("reply_markup")) for _, text, kwargs in message.shown]


def keyboard(message: Screen) -> InlineKeyboardMarkup:
    markup = answer_of(message)[1].get("reply_markup")
    assert isinstance(markup, InlineKeyboardMarkup)
    return markup


def all_buttons(message: Screen) -> list[InlineKeyboardButton]:
    return [button for row in keyboard(message).inline_keyboard for button in row]


def link_on(message: Screen) -> str:
    """The link the 📋 Copy button copies."""
    copy = keyboard(message).inline_keyboard[1][0].copy_text
    assert copy is not None
    return copy.text


def share_params(button: InlineKeyboardButton) -> dict[str, list[str]]:
    """What a 📤 button hands Telegram's share sheet."""
    assert button.url is not None and button.url.startswith(f"{TELEGRAM_SHARE_URL}?")
    return parse_qs(urlsplit(button.url).query)


def shared(message: Screen) -> dict[str, list[str]]:
    return share_params(keyboard(message).inline_keyboard[0][0])


def payload_of(link: str) -> str:
    (payload,) = parse_qs(urlsplit(link).query)["start"]
    return payload


def plain(text: str) -> str:
    return TAGS.sub("", text)


def invitation(**overrides: Any) -> Invitation:
    fields: dict[str, Any] = {
        "link": f"https://t.me/{BOT}?start=ref_Abcdefgh1234",
        "referrer_stamps": 2,
        "referred_stamps": 2,
        "referrer_spins": 1,
        "invited": 0,
        "rewarded": 0,
    }
    return Invitation(**(fields | overrides))


async def user_by_telegram(session: AsyncSession, telegram_id: int) -> User:
    user = await UserRepository(session).get_by_telegram_id(telegram_id)
    assert user is not None
    return user


async def complete_first_order(
    session: AsyncSession, user: User, *, settings: Settings | None = None
) -> Order:
    """A €20 post-launch order the admin takes all the way to Completed."""
    order = await make_order(session, user)
    order.total_price = Decimal("20.00")
    order.loyalty_eligible = True
    admin = AdminService(session, settings=settings or configured())
    for status in TO_COMPLETED:
        order = await admin.set_order_status(order, status)
    return order


async def invited_pair(
    session: AsyncSession,
    telegram_id: int,
    *,
    referrer_language: LanguageCode = LanguageCode.EN,
    friend_language: LanguageCode = LanguageCode.EN,
) -> tuple[User, User]:
    """A referrer, and a friend who arrived through their link and chose a language."""
    referrer = await make_user(session, telegram_id=telegram_id, language=referrer_language)
    link = link_on(await open_screen(session, referrer))
    await send_start(session, telegram_id + 1, payload_of(link))
    friend = await user_by_telegram(session, telegram_id + 1)
    friend.language = friend_language
    return referrer, friend


async def announce(session: AsyncSession, order: Order, bot: FakeBot) -> int:
    """What the admin's status change sends once the completion is committed."""
    news = ReferralNotificationService(session, bot, settings=configured())  # type: ignore[arg-type]
    return await news.rewards_paid(order.id)


async def referral_bonuses(session: AsyncSession, user_id: int) -> list[int]:
    rows = await session.scalars(
        select(LoyaltyTransaction.amount)
        .where(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.kind == LoyaltyTransactionType.REFERRAL,
        )
        .order_by(LoyaltyTransaction.id)
    )
    return list(rows)


async def count_rows(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def callers_of(call: str) -> list[str]:
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if call in path.read_text(encoding="utf-8")
    )


# ================================================================ main menu


@pytest.mark.parametrize("lang", LANGS)
def test_the_main_menu_offers_invite_a_friend_after_the_roulette(lang: str) -> None:
    i18n = LocalizationService(lang)
    rows = [[b.text for b in row] for row in main_menu_keyboard(i18n).keyboard]

    at = rows.index([i18n.t("menu.roulette")])
    assert rows[at + 1] == [i18n.t("menu.invite")]
    assert i18n.t("menu.invite").startswith("👥 ")


@pytest.mark.parametrize("lang", LANGS)
async def test_the_menu_button_is_recognised_in_every_language(lang: str) -> None:
    label = LocalizationService(lang).t("menu.invite")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text=label
    )

    assert await LocalizedText("menu.invite")(message)


# ================================================================ the link


async def test_the_link_is_a_telegram_deep_link_to_the_customers_own_code(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9801)

    link = link_on(await open_screen(session, user))

    assert LINK.match(link), link
    code = parse_referral_payload(payload_of(link))
    assert code == await ReferralProgramService(session).referral_code(user.id)


async def test_the_link_names_the_bot_telegram_reports(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9802)

    link = link_on(await open_screen(session, user, bot=FakeBot("OtherShopBot")))

    assert link.startswith("https://t.me/OtherShopBot?start=ref_")


async def test_no_internal_id_appears_anywhere_on_the_screen(session: AsyncSession) -> None:
    """The code is random: nothing on the screen or in its buttons names the customer."""
    user = await make_user(session, telegram_id=918273645)
    screen = await open_screen(session, user)
    text, _ = answer_of(screen)
    link = link_on(screen)

    carried = [text, *(b.text for b in all_buttons(screen))]
    carried += [b.url or "" for b in all_buttons(screen)]
    carried += [b.copy_text.text for b in all_buttons(screen) if b.copy_text is not None]
    for value in carried:
        assert str(user.telegram_id) not in value
        assert "user_id" not in value and "uid" not in value
    # The link carries exactly one parameter — the payload — and nothing else.
    assert list(parse_qs(urlsplit(link).query)) == ["start"]
    assert payload_of(link) == f"ref_{parse_referral_payload(payload_of(link))}"
    assert {b.callback_data for b in all_buttons(screen)} - {None} == {CALLBACK_INVITE_CLOSE}


# ================================================================ rendering


async def test_the_offer_reads_as_the_owner_worded_it(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9803)

    text, _ = answer_of(await open_screen(session, user))

    assert OFFER["en"] in plain(text)


async def test_the_screen_reads_offer_steps_link_progress_then_action(
    session: AsyncSession,
) -> None:
    """What you get, how it works, your link, how it's going, what to tap — top down."""
    user = await make_user(session, telegram_id=9804)
    screen = await open_screen(session, user)
    text, _ = answer_of(screen)
    link = link_on(screen)

    parts = [
        EN.t("invite.title"),
        EN.t("invite.offer", friend="+2 stamps", you="+2 stamps"),
        EN.t("invite.spin_bonus"),
        EN.t("invite.how_title"),
        EN.t("invite.step_send"),
        EN.t("invite.step_order"),
        EN.t("invite.step_both"),
        EN.t("invite.link", link=link),
        EN.t("invite.no_friends"),
        EN.t("invite.cta"),
    ]
    positions = [text.index(part) for part in parts]
    assert positions == sorted(positions)
    assert text.startswith(EN.t("invite.title"))
    assert text.endswith(EN.t("invite.cta"))  # the 👇 sits right above the buttons
    assert f"<code>{link}</code>" in text  # a tap copies it


async def test_send_copy_and_back_sit_under_the_screen(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9805)

    screen = await open_screen(session, user)
    link = link_on(screen)

    assert [[b.text for b in row] for row in keyboard(screen).inline_keyboard] == [
        [EN.t("invite.share")],
        [EN.t("invite.copy")],
        [EN.t("common.back")],
    ]
    assert shared(screen) == {
        "url": [link],
        "text": [EN.t("invite.share_text", stamps="+2 stamps")],
    }
    assert keyboard(screen).inline_keyboard[2][0].callback_data == CALLBACK_INVITE_CLOSE
    preview = answer_of(screen)[1].get("link_preview_options")
    assert isinstance(preview, LinkPreviewOptions) and preview.is_disabled


def test_the_link_is_escaped_for_telegram_html() -> None:
    text = format_invite(invitation(link="https://t.me/b?start=ref_<x>&y"), EN)

    assert "<code>https://t.me/b?start=ref_&lt;x&gt;&amp;y</code>" in text


@pytest.mark.parametrize("lang", LANGS)
def test_friends_brought_in_are_counted(lang: str) -> None:
    i18n = LocalizationService(lang)

    text = format_invite(invitation(invited=3, rewarded=1), i18n)

    assert i18n.t("invite.stats", invited=3, rewarded=1) in text
    assert i18n.t("invite.waiting", waiting=2) in text
    assert i18n.t("invite.no_friends") not in text


def test_nobody_waiting_means_no_waiting_line() -> None:
    text = format_invite(invitation(invited=2, rewarded=2), EN)

    assert EN.t("invite.stats", invited=2, rewarded=2) in text
    assert "Waiting" not in text


# ================================================================ localization


@pytest.mark.parametrize("lang", LANGS)
async def test_the_screen_speaks_the_customers_language(session: AsyncSession, lang: str) -> None:
    user = await make_user(
        session, telegram_id=9810 + LANGS.index(lang), language=LanguageCode(lang)
    )
    i18n = LocalizationService(lang)

    screen = await open_screen(session, user)
    text, _ = answer_of(screen)

    assert text.startswith(i18n.t("invite.title"))
    assert OFFER[lang] in plain(text)
    for key in (
        "invite.how_title",
        "invite.step_send",
        "invite.step_order",
        "invite.step_both",
        "invite.no_friends",
        "invite.cta",
    ):
        assert i18n.t(key) in text
    assert [b.text for b in all_buttons(screen)] == [
        i18n.t("invite.share"),
        i18n.t("invite.copy"),
        i18n.t("common.back"),
    ]
    (message,) = shared(screen)["text"]
    assert message == i18n.t("invite.share_text", stamps=FRIEND_BONUS[lang])
    assert FRIEND_BONUS[lang] in message
    if lang != "en":
        for key in ("invite.step_send", "invite.no_friends", "invite.cta"):
            assert EN.t(key) not in text


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_carries_every_plural_form(lang: str) -> None:
    catalog = json.loads((ROOT / "app" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    forms = catalog["invite"]["stamps"]

    assert set(forms) == set(PLURAL_FORMS)
    assert all("{count}" in form for form in forms.values())


@pytest.mark.parametrize(
    ("lang", "count", "category"),
    [
        ("ru", 0, "many"),
        ("ru", 1, "one"),
        ("ru", 2, "few"),
        ("ru", 4, "few"),
        ("ru", 5, "many"),
        ("ru", 11, "many"),
        ("ru", 12, "many"),
        ("ru", 14, "many"),
        ("ru", 21, "one"),
        ("ru", 22, "few"),
        ("ru", 25, "many"),
        ("ru", 101, "one"),
        ("ru", 111, "many"),
        ("uk", 1, "one"),
        ("uk", 3, "few"),
        ("uk", 13, "many"),
        ("uk", 100, "many"),
        ("en", 0, "other"),
        ("en", 1, "one"),
        ("en", 2, "other"),
        ("de", 1, "one"),
        ("de", 7, "other"),
    ],
)
def test_plural_categories_follow_cldr(lang: str, count: int, category: str) -> None:
    assert plural_category(lang, count) == category


@pytest.mark.parametrize(
    ("lang", "count", "expected"),
    [
        ("en", 1, "+1 stamp"),
        ("en", 5, "+5 stamps"),
        ("ru", 1, "+1 штамп"),
        ("ru", 3, "+3 штампа"),
        ("ru", 5, "+5 штампов"),
        ("ru", 21, "+21 штамп"),
        ("uk", 1, "+1 штамп"),
        ("uk", 2, "+2 штампи"),
        ("uk", 11, "+11 штампів"),
        ("de", 1, "+1 Stempel"),
        ("de", 3, "+3 Stempel"),
    ],
)
async def test_configured_amounts_are_shown_and_inflected(
    session: AsyncSession, lang: str, count: int, expected: str
) -> None:
    user = await make_user(session, telegram_id=9820, language=LanguageCode(lang))
    settings = configured(referral_reward_stamps=count, referred_user_start_stamps=count)

    screen = await open_screen(session, user, settings=settings)
    text, _ = answer_of(screen)

    assert text.count(f"<b>{expected}</b>") == 2  # the friend's, and the customer's
    assert f"{expected} " in shared(screen)["text"][0]  # and what the friend is told


# ================================================================ configuration


async def test_a_friend_bonus_of_zero_is_not_promised(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9830)
    settings = configured(referred_user_start_stamps=0, referral_reward_stamps=3)

    screen = await open_screen(session, user, settings=settings)
    text, _ = answer_of(screen)

    assert "your friend gets" not in text
    assert EN.t("invite.offer_you", you="+3 stamps") in text
    assert EN.t("invite.step_you") in text
    assert shared(screen)["text"] == [EN.t("invite.share_text_plain")]


async def test_a_spin_alone_is_still_a_reward_for_you(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9833)
    settings = configured(referral_reward_stamps=0, referral_spins=1)

    text, _ = answer_of(await open_screen(session, user, settings=settings))

    assert EN.t("invite.offer_friend", friend="+2 stamps") in text
    assert EN.t("invite.spin") in text
    assert EN.t("invite.step_both") in text


async def test_the_spin_is_promised_only_when_configured(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9831)

    with_spin, _ = answer_of(
        await open_screen(session, user, settings=configured(referral_spins=1))
    )
    without, _ = answer_of(await open_screen(session, user, settings=configured(referral_spins=0)))

    assert EN.t("invite.spin_bonus") in with_spin
    assert EN.t("invite.spin_bonus") not in without
    assert EN.t("invite.spin") not in without


async def test_with_nothing_configured_the_link_is_still_offered(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9832)
    settings = configured(referral_reward_stamps=0, referred_user_start_stamps=0, referral_spins=0)

    screen = await open_screen(session, user, settings=settings)
    text, _ = answer_of(screen)

    assert EN.t("invite.offer_plain") in text
    assert "stamp" not in plain(text).lower()
    for key in ("invite.step_both", "invite.step_friend", "invite.step_you"):
        assert EN.t(key) not in text  # no reward, so no promise of one
    assert LINK.match(link_on(screen))
    assert shared(screen)["text"] == [EN.t("invite.share_text_plain")]


def test_the_shared_message_names_the_friends_bonus() -> None:
    """A friend who has never seen the shop learns what they get, and what it is worth."""
    assert share_text(invitation(), EN) == EN.t("invite.share_text", stamps="+2 stamps")
    assert "free bottle" in share_text(invitation(), EN)
    assert "+1 stamp towards" in share_text(invitation(referred_stamps=1), EN)
    assert share_text(invitation(referred_stamps=0), EN) == EN.t("invite.share_text_plain")


# ================================================================ repeated opening


async def test_opening_again_shows_the_same_link_and_writes_nothing_new(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9840)
    first = await open_screen(session, user)
    before = [await count_rows(session, m) for m in (User, Referral, LoyaltyTransaction)]

    again = [await open_screen(session, user) for _ in range(3)]

    assert {link_on(screen) for screen in again} == {link_on(first)}
    assert answer_of(again[-1])[0] == answer_of(first)[0]
    assert all(len(screen.shown) == 1 for screen in again)  # a fresh screen each tap
    after = [await count_rows(session, m) for m in (User, Referral, LoyaltyTransaction)]
    assert after == before


# ================================================================ no referral history


async def test_a_customer_with_no_referrals_sees_an_invitation_to_start(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9850)
    tables = (Referral, LoyaltyTransaction, RouletteSpinGrant)
    before = [await count_rows(session, m) for m in tables]

    text, _ = answer_of(await open_screen(session, user))

    assert EN.t("invite.no_friends") in text
    assert "Friends joined" not in text
    after = [await count_rows(session, m) for m in tables]
    assert after == before  # opening the screen grants and attributes nothing


async def test_a_referred_customer_gets_a_link_of_their_own(session: AsyncSession) -> None:
    referrer = await make_user(session, telegram_id=9851)
    referrer_link = link_on(await open_screen(session, referrer))
    await send_start(session, 9852, payload_of(referrer_link))
    friend = await user_by_telegram(session, 9852)
    friend.language, friend.selected_city = referrer.language, referrer.selected_city

    friend_screen = await open_screen(session, friend)

    assert LINK.match(link_on(friend_screen))
    assert link_on(friend_screen) != referrer_link
    assert EN.t("invite.no_friends") in answer_of(friend_screen)[0]


# ================================================================ the deep link


async def test_a_friend_opening_the_link_is_attributed_and_both_are_paid_once(
    session: AsyncSession,
) -> None:
    referrer = await make_user(session, telegram_id=9860)
    link = link_on(await open_screen(session, referrer))

    await send_start(session, 9861, payload_of(link))
    friend = await user_by_telegram(session, 9861)

    referral = await session.scalar(select(Referral).where(Referral.referred_user_id == friend.id))
    assert referral is not None
    assert referral.referrer_user_id == referrer.id
    assert referral.status == ReferralStatus.PENDING
    assert await referral_bonuses(session, referrer.id) == []  # nothing is paid at sign-up
    joined, _ = answer_of(await open_screen(session, referrer))
    assert EN.t("invite.stats", invited=1, rewarded=0) in joined
    assert EN.t("invite.waiting", waiting=1) in joined

    await complete_first_order(session, friend)
    await complete_first_order(session, friend)  # a second order pays nothing more

    assert await referral_bonuses(session, referrer.id) == [2]
    assert await referral_bonuses(session, friend.id) == [2]
    rewarded, _ = answer_of(await open_screen(session, referrer))
    assert EN.t("invite.stats", invited=1, rewarded=1) in rewarded
    assert "Waiting" not in rewarded


async def test_the_shared_link_is_the_one_that_attributes(session: AsyncSession) -> None:
    """What the share sheet sends is the customer's link — and it works."""
    referrer = await make_user(session, telegram_id=9863)
    (sent,) = shared(await open_screen(session, referrer))["url"]

    await send_start(session, 9864, payload_of(sent))

    friend = await user_by_telegram(session, 9864)
    referral = await session.scalar(select(Referral).where(Referral.referred_user_id == friend.id))
    assert referral is not None and referral.referrer_user_id == referrer.id


async def test_counts_belong_to_each_referrer(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9870)
    bob = await make_user(session, telegram_id=9871)
    alice_link = link_on(await open_screen(session, alice))
    bob_link = link_on(await open_screen(session, bob))
    for telegram_id, link in ((9872, alice_link), (9873, alice_link), (9874, bob_link)):
        await send_start(session, telegram_id, payload_of(link))
    await complete_first_order(session, await user_by_telegram(session, 9872))

    programme = ReferralProgramService(session)
    alice_view = await programme.invitation(alice.id, bot_username=BOT)
    bob_view = await programme.invitation(bob.id, bot_username=BOT)

    assert (alice_view.invited, alice_view.rewarded, alice_view.waiting) == (2, 1, 1)
    assert (bob_view.invited, bob_view.rewarded, bob_view.waiting) == (1, 0, 1)


# ================================================================ your own link


@pytest.mark.parametrize("lang", LANGS)
async def test_opening_your_own_link_says_it_works(session: AsyncSession, lang: str) -> None:
    """Customers tap their own link to check it: say so, rather than nothing."""
    user = await make_user(
        session, telegram_id=9950 + LANGS.index(lang), language=LanguageCode(lang)
    )
    link = link_on(await open_screen(session, user))
    bot = FakeBot()

    screen = await send_start(session, user.telegram_id, payload_of(link), bot=bot)

    i18n = LocalizationService(lang)
    assert answer_of(screen)[0] == own_link_note(i18n)
    assert i18n.t("menu.invite") in own_link_note(i18n)  # where to share it from
    assert await count_rows(session, Referral) == 0
    assert bot.sent == []


# ================================================================ news: a friend joined


async def test_the_referrer_hears_when_a_friend_joins(session: AsyncSession) -> None:
    referrer = await make_user(session, telegram_id=9900, language=LanguageCode.RU)
    link = link_on(await open_screen(session, referrer))
    bot = FakeBot()

    await send_start(session, 9901, payload_of(link), bot=bot)

    ru = LocalizationService("ru")
    ((text, markup),) = bot.to(referrer.telegram_id)
    assert text == "\n".join(
        [ru.t("invite.news.joined"), ru.t("invite.news.joined_stamps_spin", stamps="+2 штампа")]
    )
    # One tap to invite the next friend, with the referrer's own link.
    assert markup is not None
    ((button,),) = markup.inline_keyboard
    assert button.text == ru.t("invite.share")
    assert share_params(button)["url"] == [link]


async def test_the_newcomer_sees_the_same_whatever_the_code(session: AsyncSession) -> None:
    """No oracle: the news goes to the referrer, and the newcomer's replies never differ."""
    referrer = await make_user(session, telegram_id=9905)
    link = link_on(await open_screen(session, referrer))
    bot = FakeBot()

    real = await send_start(session, 9906, payload_of(link), bot=bot)
    fake = await send_start(session, 9907, "ref_Fake-code-12", bot=bot)
    none = await send_start(session, 9908, None, bot=bot)

    assert replies(real) == replies(fake) == replies(none)
    assert [chat for chat, _, _ in bot.sent] == [referrer.telegram_id]


async def test_a_friend_who_starts_again_is_announced_once(session: AsyncSession) -> None:
    referrer = await make_user(session, telegram_id=9910)
    link = link_on(await open_screen(session, referrer))
    bot = FakeBot()

    for _ in range(3):
        await send_start(session, 9911, payload_of(link), bot=bot)

    assert len(bot.to(referrer.telegram_id)) == 1


async def test_the_attribution_is_committed_before_the_referrer_hears(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    referrer = await make_user(session, telegram_id=9915)
    link = link_on(await open_screen(session, referrer))
    events: list[str] = []
    commit = session.commit

    async def recording_commit() -> None:
        events.append("commit")
        await commit()

    monkeypatch.setattr(session, "commit", recording_commit)

    await send_start(session, 9916, payload_of(link), bot=FakeBot(events=events))

    assert "send" in events
    assert events.index("commit") < events.index("send")


async def test_a_referrer_who_blocked_the_bot_does_not_hold_up_the_friend(
    session: AsyncSession,
) -> None:
    referrer = await make_user(session, telegram_id=9920)
    link = link_on(await open_screen(session, referrer))
    bot = FakeBot(blocked=(referrer.telegram_id,))

    screen = await send_start(session, 9921, payload_of(link), bot=bot)

    assert screen.shown, "onboarding carries on as usual"
    friend = await user_by_telegram(session, 9921)
    referral = await session.scalar(select(Referral).where(Referral.referred_user_id == friend.id))
    assert referral is not None and referral.referrer_user_id == referrer.id
    assert bot.sent == []


def test_the_joined_news_promises_what_the_referrer_will_get() -> None:
    joined = EN.t("invite.news.joined")

    assert format_friend_joined(invitation(), EN) == "\n".join(
        [joined, EN.t("invite.news.joined_stamps_spin", stamps="+2 stamps")]
    )
    assert format_friend_joined(invitation(referrer_spins=0), EN) == "\n".join(
        [joined, EN.t("invite.news.joined_stamps", stamps="+2 stamps")]
    )
    assert format_friend_joined(invitation(referrer_stamps=0), EN) == "\n".join(
        [joined, EN.t("invite.news.joined_spin")]
    )
    assert format_friend_joined(invitation(referrer_stamps=0, referrer_spins=0), EN) == joined


# ================================================================ news: the rewards landed


async def test_both_sides_hear_when_the_rewards_land(session: AsyncSession) -> None:
    referrer, friend = await invited_pair(
        session, 9930, referrer_language=LanguageCode.RU, friend_language=LanguageCode.DE
    )
    order = await complete_first_order(session, friend)
    bot = FakeBot()

    assert await announce(session, order, bot) == 2

    ru, de = LocalizationService("ru"), LocalizationService("de")
    ((to_referrer, markup),) = bot.to(referrer.telegram_id)
    assert to_referrer.startswith(ru.t("invite.news.paid"))
    assert ru.t("invite.news.stamps_added", stamps="+2 штампа") in to_referrer
    assert ru.t("invite.news.spin_added") in to_referrer
    assert to_referrer.endswith(ru.t("invite.news.more"))
    assert markup is not None
    ((button,),) = markup.inline_keyboard
    link = await ReferralProgramService(session).referral_link(referrer.id, bot_username=BOT)
    assert button.text == ru.t("invite.share") and share_params(button)["url"] == [link]
    ((to_friend, friend_markup),) = bot.to(friend.telegram_id)
    assert to_friend.startswith(de.t("invite.news.welcome_bonus"))
    assert de.t("invite.news.stamps_added", stamps="+2 Stempel") in to_friend
    assert to_friend.endswith(de.t("invite.news.your_turn", menu=de.t("menu.invite")))
    assert friend_markup is None


async def test_the_news_reports_what_was_booked_not_todays_settings(
    session: AsyncSession,
) -> None:
    _, friend = await invited_pair(session, 9935)
    booked = configured(referral_reward_stamps=3, referred_user_start_stamps=1, referral_spins=0)
    order = await complete_first_order(session, friend, settings=booked)
    bot = FakeBot()

    await announce(session, order, bot)  # under the defaults: 2, 2 and a spin

    ((to_friend, _), (to_referrer, _)) = [(text, markup) for _, text, markup in bot.sent]
    assert "<b>+3 stamps</b>" in to_referrer
    assert EN.t("invite.news.spin_added") not in to_referrer
    assert "<b>+1 stamp</b>" in to_friend


async def test_orders_that_qualify_nothing_send_no_news(session: AsyncSession) -> None:
    _, friend = await invited_pair(session, 9940)
    first = await complete_first_order(session, friend)
    second = await complete_first_order(session, friend)
    loner = await complete_first_order(session, await make_user(session, telegram_id=9942))
    bot = FakeBot()

    assert await announce(session, second, bot) == 0
    assert await announce(session, loner, bot) == 0
    assert bot.sent == []
    assert await announce(session, first, bot) == 2


async def test_a_side_that_earned_nothing_is_not_messaged(session: AsyncSession) -> None:
    referrer, friend = await invited_pair(session, 9945)
    nothing_for_referrer = configured(referral_reward_stamps=0, referral_spins=0)
    order = await complete_first_order(session, friend, settings=nothing_for_referrer)
    bot = FakeBot()

    assert await announce(session, order, bot) == 1

    assert bot.to(referrer.telegram_id) == []
    assert len(bot.to(friend.telegram_id)) == 1


async def test_a_blocked_side_does_not_stop_the_other(session: AsyncSession) -> None:
    referrer, friend = await invited_pair(session, 9950)
    order = await complete_first_order(session, friend)
    bot = FakeBot(blocked=(friend.telegram_id,))

    assert await announce(session, order, bot) == 1

    assert len(bot.to(referrer.telegram_id)) == 1


async def test_the_news_names_nobody(session: AsyncSession) -> None:
    """Neither side learns who the other is, or what they ordered."""
    referrer = await make_user(session, telegram_id=9955)
    link = link_on(await open_screen(session, referrer))
    bot = FakeBot()
    await send_start(session, 9956, payload_of(link), bot=bot, first_name="Zelda")
    friend = await user_by_telegram(session, 9956)
    order = await complete_first_order(session, friend)

    await announce(session, order, bot)

    assert len(bot.sent) == 3  # joined, then one each when the rewards land
    for _, text, _ in bot.sent:
        for secret in ("Zelda", "Test", "user9955", "9955", "9956", f"#{order.id}"):
            assert secret not in text


async def test_news_that_cannot_be_read_is_skipped_quietly(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    referrer, friend = await invited_pair(session, 9960)
    order = await complete_first_order(session, friend)
    await session.commit()  # the admin handler commits before any news

    async def broken(self: Any, order_id: int) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(ReferralProgramService, "payout_for_order", broken)
    bot = FakeBot()

    assert await announce(session, order, bot) == 0

    assert bot.sent == []
    assert await referral_bonuses(session, referrer.id) == [2]  # what was committed stays


def test_each_piece_of_news_has_one_sender() -> None:
    assert callers_of(".friend_joined(") == ["app/handlers/user/start.py"]
    assert callers_of(".rewards_paid(") == ["app/handlers/admin/orders.py"]


def test_the_payout_news_follows_the_completion_commit() -> None:
    source = (ROOT / "app" / "handlers" / "admin" / "orders.py").read_text(encoding="utf-8")

    assert source.index("await session.commit()") < source.index(".rewards_paid(")


# ================================================================ navigation


async def test_back_closes_the_screen(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9880)
    screen = from_customer(user.telegram_id, "invite")
    callback = Tap(
        id="t",
        from_user=customer(user.telegram_id),
        chat_instance="ci",
        data=CALLBACK_INVITE_CLOSE,
        message=screen,
    )

    await close_invite(callback=callback)

    assert callback.alerts == [(None, False)]
    assert [kind for kind, _, _ in screen.shown] == ["delete"]


async def test_back_on_a_screen_too_old_to_delete_removes_its_buttons(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9881)
    screen = from_customer(user.telegram_id, "invite", kind=OldScreen)
    callback = Tap(
        id="t",
        from_user=customer(user.telegram_id),
        chat_instance="ci",
        data=CALLBACK_INVITE_CLOSE,
        message=screen,
    )

    await close_invite(callback=callback)

    assert callback.alerts == [(None, False)]
    assert screen.shown == [("buttons", "", {"reply_markup": None})]


async def test_back_on_a_message_telegram_no_longer_shows_just_answers() -> None:
    callback = Tap(id="t", from_user=customer(9882), chat_instance="ci", data=CALLBACK_INVITE_CLOSE)

    await close_invite(callback=callback)

    assert callback.alerts == [(None, False)]


async def test_a_customer_still_onboarding_is_sent_back_to_onboarding(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9883, city=None)

    screen = await open_screen(session, user)

    assert answer_of(screen)[0] == EN.t("onboarding.choose_city")
    assert all("start=ref_" not in text for _, text, _ in screen.shown)


def test_the_screens_only_callback_closes_it() -> None:
    """Nothing on the screen can attribute or pay a referral: its one callback closes it."""
    assert [h.callback for h in invite_screen.router.callback_query.handlers] == [close_invite]
    assert [h.callback for h in invite_screen.router.message.handlers] == [open_invite]
    assert len(CALLBACK_INVITE_CLOSE.encode()) <= TELEGRAM_CALLBACK_LIMIT
    assert CALLBACK_INVITE_CLOSE.startswith("invite:")
