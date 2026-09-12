"""
The whole customer journey, in every language — every screen, button, alert and message.

One journey per language through the production dispatcher (:mod:`tests.production_bot`),
reaching every state a customer can see: onboarding; the catalog, empty and not
found; the cart and each change to it; checkout with every prompt, refusal and
reward; the order notifications; the info pages for both kinds of city and the
reviews link; the stamp card, the roulette and every prize; the invite screen and
the referral news; a customer opening their own link; a stale button; each kind
of error. What the referral settings change is rendered for every combination.

Everything the bot sent — and every text a customer shares on — is then checked:

* every text a code path can show a customer appeared, in the reader's language;
* nothing is a raw key, an unfilled placeholder or markup Telegram would reject;
* nothing is in another language: no text or phrase of another catalog, no
  English words in German, no Latin line in Russian or Ukrainian, no letter that
  only Russian or only Ukrainian has in the other;
* everything reads on a phone: every button label fits its share of the row,
  alerts stay within Telegram's 200 characters and toasts within two lines, no
  paragraph wraps onto more than four lines, and every message can be seen
  whole on one screen.
"""

from __future__ import annotations

import asyncio
import html
import math
import pathlib
import re
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    TelegramMethod,
)
from aiogram.types import Contact, InlineKeyboardMarkup, Message, Update
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.middlewares.database
from app import lifecycle
from app.handlers.user import checkout as checkout_screen
from app.handlers.user import roulette as roulette_screen
from app.models.category import Category, Subcategory
from app.models.enums import CityChoice, LanguageCode, OrderStatus
from app.models.product import Product
from app.models.referral import Referral
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.referral_program import Invitation, ReferralPayout
from app.services.review import invalidate_review_link_cache
from app.services.roulette_engine import RouletteEngine
from app.services.user import UserService
from app.utils import concurrency
from app.utils.cache import invalidate_categories_cache
from app.utils.i18n import PLURAL_FORMS, SUPPORTED_LANGUAGES, load_locale, plural_category
from app.utils.invite_display import (
    format_friend_joined,
    format_invite,
    format_payout_for_friend,
    format_payout_for_referrer,
    share_text,
)
from tests.factories import make_category, make_user
from tests.production_bot import (
    ADMIN_ID,
    MANAGER_CHAT_ID,
    FakeTelegram,
    RunningBot,
    tree_settings,
)
from tests.test_loyalty_journeys import (
    TO_COMPLETED,
    admin_moves,
    buy,
    check_out,
    latest_order,
    open_shop,
    rewards,
)

EN = LocalizationService("en")
APP = pathlib.Path(__file__).resolve().parent.parent / "app"
REVIEWS_LINK = "https://t.me/+VShopReviews"
# The manager's new-order alert is English on purpose (see docs/architecture.md).
MANAGER_ALERT = "🆕 <b>New order"
# Content that is not the bot's to translate: what people typed, the brand, Telegram.
NOT_OURS = ("Clara Schmidt", "Alexanderplatz", "Ivy Ivanova", "Cheshire Vape", "Telegram")

UNUSED = "no code path shows it"
ADMIN_ONLY = "only the admin panel shows it"
# Customer-namespace keys a customer can never see — each checked against the code below.
UNREACHABLE = {
    "catalog.title": UNUSED,
    "catalog.back_to_products": UNUSED,
    "catalog.category_empty": UNUSED,
    "city.changed": UNUSED,
    "common.continue": UNUSED,
    "common.error": UNUSED,
    "common.no": UNUSED,
    "common.try_again": UNUSED,
    "common.yes": UNUSED,
    "error.product_missing": UNUSED,
    "error.unauthorized": UNUSED,
    "product.inactive": UNUSED,
    "common.cancel": ADMIN_ONLY,
    "common.confirm": ADMIN_ONLY,
    "common.skip": ADMIN_ONLY,
    # Only an order from before payment methods existed has none: the admin's order card.
    "checkout.payment_not_set": "orders placed before payment methods existed",
}
# Admin-namespace text the journey shows too: a customer typing /admin, and the
# reward a customer used, on the admin's order card.
ALSO_SHOWN = (
    "admin.access_denied",
    "admin.order_reward_free_bottle",
    "admin.order_reward_discount",
)

TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span",
    "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji",
}  # fmt: skip
PLACEHOLDER = re.compile(r"\{\w+\}")
KEY_LIKE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\b")
TAG = re.compile(r"<(/?)([a-zA-Z-]+)[^>]*>")
URL = re.compile(r"https?://\S+")
CHUNK_BREAKS = re.compile(r"\{\w+\}|\n| — |: |[()·•“”„«»]")
# Words German never uses: two of them in one German message means English got in.
ENGLISH_WORDS = re.compile(
    r"\b(the|your|you|and|with|for|is|to|of|this|that|it|on|at)\b", re.IGNORECASE
)
ONLY_RUSSIAN = set("ыэъёЫЭЪЁ")
ONLY_UKRAINIAN = set("іїєґІЇЄҐ")

# A typical phone: 390 pt wide (iPhone 12 to 16; Android's common widths are
# narrower still), at Telegram's default sizes — message text 17 pt, buttons
# 16 pt, toasts 15 pt. A message bubble, and the inline keyboard under it, is
# about 330 pt wide, 300 pt of it text. Glyph widths are averages in em: enough
# to tell a comfortable fit from a label cut short or a wall of text.
KEYBOARD = 330
TEXT_WIDTH = 300
MESSAGE_PT, BUTTON_PT, TOAST_PT = 17, 16, 15
MAX_PARAGRAPH = 4  # phone lines one paragraph may wrap onto
SCREEN_LINES = 30  # lines a phone shows at once: a taller message is never seen whole
MAX_ALERT = 200  # Telegram refuses a longer callback answer
MAX_TOAST = 2  # a toast is gone in a moment: two lines are read at a glance


def em(ch: str) -> float:
    category = unicodedata.category(ch)
    if category in ("Mn", "Cf"):  # variation selectors, joiners
        return 0
    if category == "So":  # an emoji
        return 1.25
    if ch.isspace():
        return 0.27
    if "Ѐ" <= ch <= "ӿ":
        return 0.57
    if ch.isdigit():
        return 0.55
    if ch.isalpha():
        return 0.53
    return 0.33


def width(text: str, size: float) -> float:
    """Points ``text`` takes on one line at ``size`` pt."""
    return sum(em(ch) for ch in text) * size


def button_room(per_row: int) -> float:
    """Text width inside one of ``per_row`` buttons: 6 pt apart, 8 pt padding a side."""
    return (KEYBOARD - 6 * (per_row - 1)) / per_row - 16


def phone_lines(line: str, size: float = MESSAGE_PT) -> float:
    """Lines of a phone screen one line of text wraps onto."""
    return width(line, size) / TEXT_WIDTH


def screen_lines(text: str) -> int:
    return sum(max(1, math.ceil(phone_lines(line))) for line in text.split("\n"))


@pytest_asyncio.fixture
async def sessions(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The bot's database: every update and every start opens its sessions here."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: factory)
    monkeypatch.setattr(roulette_screen, "FRAME_DELAY", 0)
    # An asyncio lock belongs to the loop that first waited on it, and each test has
    # its own loop: a double tap must not meet a lock another language's test made.
    monkeypatch.setattr(concurrency, "_locks", {})
    invalidate_categories_cache()
    invalidate_review_link_cache()
    yield factory
    invalidate_categories_cache()
    invalidate_review_link_cache()


class FaithfulTelegram(FakeTelegram):
    """Refuses an edit that changes nothing, as Telegram does — "up to date" notices need it."""

    def __init__(self) -> None:
        super().__init__()
        self.current: dict[tuple[int, int], tuple[str | None, InlineKeyboardMarkup | None]] = {}

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, EditMessageText | EditMessageReplyMarkup):
            where = (int(method.chat_id or 0), method.message_id or 0)
            text, markup = self.current.get(where, (None, None))
            edited = method.text if isinstance(method, EditMessageText) else text
            keyboard = method.reply_markup
            new = (edited, keyboard if isinstance(keyboard, InlineKeyboardMarkup) else None)
            if where in self.current and new == (text, markup):
                raise TelegramBadRequest(method, "Bad Request: message is not modified")
            self.current[where] = new
        result = await super().make_request(bot, method, timeout)
        if isinstance(result, Message):
            self.current[(result.chat.id, result.message_id)] = (result.text, result.reply_markup)
        return result


# ======================================================= what was shown


@dataclass(frozen=True, slots=True)
class Shown:
    kind: str  # message, alert, toast, button, or shared (what the share sheet sends on)
    chat: int
    text: str
    per_row: int = 1  # a button's share of its row


def plain(text: str) -> str:
    return html.unescape(TAG.sub("", text))


def literal(template: str) -> str:
    """The longest fixed part of a template — what any rendering of it contains."""
    return max((part.strip() for part in PLACEHOLDER.split(plain(template))), key=len)


def sent(bot: RunningBot, *, since: dict[int, int]) -> list[Shown]:
    """Every message, alert, button label and shared text the bot sent to customers."""
    found: list[Shown] = []
    for index, (method, _) in enumerate(bot.telegram.calls):
        if isinstance(method, AnswerCallbackQuery):
            chat = int(method.callback_query_id.split(":")[0])
            if method.text and index >= since.get(chat, 0):
                kind = "alert" if method.show_alert else "toast"
                found.append(Shown(kind, chat, method.text))
            continue
        chat = getattr(method, "chat_id", None)
        if not isinstance(chat, int) or chat == MANAGER_CHAT_ID or index < since.get(chat, 0):
            continue
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if isinstance(text, str) and not text.startswith(MANAGER_ALERT):
            found.append(Shown("message", chat, text))
        markup = getattr(method, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
        for row in rows or []:
            for button in row:
                found.append(Shown("button", chat, button.text, len(row)))
                url = getattr(button, "url", None) or ""
                if url.startswith("https://t.me/share/url?"):
                    for shared in parse_qs(urlsplit(url).query).get("text", []):
                        found.append(Shown("shared", chat, shared))
    return found


def referral_variants(i18n: LocalizationService) -> list[Shown]:
    """What every combination of the referral settings makes the invite texts say."""
    shown: list[Shown] = []
    for referrer, referred, spin in (
        (2, 2, 1), (2, 2, 0), (0, 2, 1), (0, 2, 0), (2, 0, 1),
        (2, 0, 0), (0, 0, 1), (0, 0, 0), (1, 1, 1), (5, 5, 0), (21, 22, 1),
    ):  # fmt: skip
        invitation = Invitation(
            link="https://t.me/VShopTestBot?start=ref_Abcdefgh1234",
            referrer_stamps=referrer,
            referred_stamps=referred,
            referrer_spins=spin,
            invited=3,
            rewarded=1,
        )
        payout = ReferralPayout(
            referral=Referral(referrer_user_id=1, referred_user_id=2),
            referred_stamps=referred,
            referrer_stamps=referrer,
            spin_granted=bool(spin),
        )
        shown += [
            Shown("message", 0, format_invite(invitation, i18n)),
            Shown("shared", 0, share_text(invitation, i18n)),
            Shown("message", 0, format_friend_joined(invitation, i18n)),
        ]
        if referrer or spin:
            shown.append(Shown("message", 0, format_payout_for_referrer(payout, i18n)))
        if referred:
            shown.append(Shown("message", 0, format_payout_for_friend(payout, i18n)))
    return shown


# ======================================================= what a customer can see


def required(language: str) -> set[str]:
    """Every key a code path can show a customer, in the forms this language uses."""
    catalog = load_locale(language)
    forms = {plural_category(language, count) for count in range(200)}
    keys: set[str] = set(ALSO_SHOWN)
    for key in catalog:
        if key.startswith(("admin.", "language.", "format.")) or key in UNREACHABLE:
            continue
        stem, _, form = key.rpartition(".")
        if form in PLURAL_FORMS and f"{stem}.one" in catalog and form not in forms:
            continue
        keys.add(key)
    return keys


def other_languages_phrases(language: str) -> set[str]:
    """Three words or more in a row from any other catalog, where it says something else."""
    own = load_locale(language)
    phrases: set[str] = set()
    for other in SUPPORTED_LANGUAGES:
        if other == language:
            continue
        for key, value in load_locale(other).items():
            if own.get(key) == value:
                continue
            for chunk in CHUNK_BREAKS.split(plain(value)):
                if len(re.findall(r"[^\W\d_]{2,}", chunk)) >= 3:
                    phrases.add(chunk.strip())
    return phrases


def other_languages_texts(language: str) -> set[str]:
    """Whole texts of the other catalogs that this one never says: a label or line fallen back."""
    own = {plain(value).strip() for value in load_locale(language).values()}
    texts: set[str] = set()
    for other in SUPPORTED_LANGUAGES:
        if other == language:
            continue
        for value in load_locale(other).values():
            text = plain(value).strip()
            if text not in own and not PLACEHOLDER.search(text) and re.search(r"[^\W\d_]{2}", text):
                texts.add(text)
    return texts


def markup_problem(kind: str, text: str) -> str | None:
    """Messages carry Telegram HTML, balanced; alerts, labels and shared text carry none."""
    stack: list[str] = []
    for match in TAG.finditer(text):
        closing, tag = match.group(1), match.group(2).lower()
        if kind != "message":
            return f"markup in a {kind}"
        if tag not in TELEGRAM_TAGS:
            return f"<{tag}> is not Telegram HTML"
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return f"unbalanced </{tag}>"
    return f"unclosed <{stack[-1]}>" if stack else None


def the_bots_words(text: str, not_ours: tuple[str, ...]) -> str:
    """The text without links, @usernames, or content that is not the bot's to translate."""
    body = URL.sub("", plain(text))
    for token in not_ours:
        # Whole words only: removing letters out of the bot's own words would hide them.
        body = re.sub(rf"(?<!\w){re.escape(token)}(?!\w)", "", body)
    return re.sub(r"@\w+", "", body)


def mostly_latin(line: str) -> bool:
    letters = [ch for ch in line if ch.isalpha()]
    cyrillic = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    return len(letters) >= 4 and cyrillic * 2 <= len(letters)


def language_problems(
    shown: Shown, language: str, body: str, phrases: set[str], texts: set[str]
) -> list[str]:
    problems = []
    if leaked := [phrase for phrase in phrases if phrase in plain(shown.text)]:
        problems.append(f"another language's words {leaked[:2]}")
    if fallen := [line.strip() for line in body.split("\n") if line.strip() in texts]:
        problems.append(f"another language's text {fallen[:2]}")
    if language == "de" and len({w.lower() for w in ENGLISH_WORDS.findall(body)}) >= 2:
        problems.append("English words")
    if language in ("en", "de") and re.search(r"[Ѐ-ӿ]", body):
        problems.append("Cyrillic letters")
    if language in ("ru", "uk") and any(mostly_latin(line) for line in body.splitlines()):
        problems.append("a mostly Latin line")
    if language == "ru" and ONLY_UKRAINIAN & set(body):
        problems.append("Ukrainian letters")
    if language == "uk" and ONLY_RUSSIAN & set(body):
        problems.append("Russian letters")
    return problems


def phone_problems(shown: Shown, is_shop_content: bool) -> list[str]:
    text = plain(shown.text)
    if shown.kind in ("alert", "toast") and len(text) > MAX_ALERT:
        return [f"{len(text)} characters: Telegram refuses more than {MAX_ALERT}"]
    if shown.kind == "toast" and phone_lines(text, TOAST_PT) > MAX_TOAST:
        return [f"a toast {phone_lines(text, TOAST_PT):.1f} lines long, gone before it is read"]
    if shown.kind == "button" and not is_shop_content:
        needed, room = width(text, BUTTON_PT), button_room(shown.per_row)
        if needed > room:
            return [f"a label {needed:.0f} pt wide in {room:.0f} pt, cut short on a phone"]
    if shown.kind in ("message", "shared"):
        longest = max(phone_lines(line) for line in text.split("\n"))
        if longest > MAX_PARAGRAPH:
            return [f"a paragraph {longest:.1f} phone lines long"]
        if screen_lines(text) > SCREEN_LINES:
            return [f"{screen_lines(text)} lines, never seen whole on a phone"]
    return []


async def shop_content(sessions: async_sessionmaker[AsyncSession]) -> tuple[str, ...]:
    """Everything the shop stores about categories, brands and products, in every language."""
    values: set[str] = set()
    async with sessions() as session:
        for model in (Category, Subcategory, Product):
            for row in (await session.scalars(select(model))).all():
                for column in model.__table__.columns:
                    value = getattr(row, column.key, None)
                    if isinstance(value, str) and len(value.strip()) >= 3:
                        values.add(value.strip())
    return tuple(sorted(values, key=len, reverse=True))


# ======================================================= steps of the journey


def latest(bot: RunningBot, chat: int) -> Message:
    screens = bot.screens(chat)
    return screens[max(screens)]


def link_on(screen: Message) -> str:
    markup = screen.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return next(b.copy_text.text for row in markup.inline_keyboard for b in row if b.copy_text)


def without_text(bot: RunningBot, chat: int) -> Update:
    """A message with nothing typed in it — a sticker, say."""
    update = bot.message(chat, "-")
    assert update.message is not None
    return update.model_copy(update={"message": update.message.model_copy(update={"text": None})})


def contact_from(bot: RunningBot, chat: int, *, owner: int) -> Update:
    """A shared contact card — someone else's when ``owner`` is not ``chat``."""
    update = bot.message(chat, "-")
    assert update.message is not None
    card = Contact(phone_number="+491511234567", first_name="Friend", user_id=owner)
    message = update.message.model_copy(update={"text": None, "contact": card})
    return update.model_copy(update={"message": message})


async def user_id(sessions: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    async with sessions() as session:
        found = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))
    assert found is not None
    return found


async def give_stamps(
    sessions: async_sessionmaker[AsyncSession], telegram_id: int, stamps: int
) -> None:
    owner = await user_id(sessions, telegram_id)
    async with sessions() as session:
        await LoyaltyService(session).adjust(owner, amount=stamps, note="journey setup")
        await session.commit()


async def ledger_version(sessions: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    owner = await user_id(sessions, telegram_id)
    async with sessions() as session:
        return await LoyaltyService(session).ledger_version(owner)


async def empty_corners(
    sessions: async_sessionmaker[AsyncSession], bottle: int
) -> tuple[int, int, int, int]:
    """(the shop's category, its brand, an empty category, an empty brand)."""
    async with sessions() as session:
        product = await session.get(Product, bottle)
        assert product is not None and product.subcategory_id is not None
        shelf = await make_category(session, name="Zz Empty Shelf", sort_order=99)
        empty = Subcategory(
            category_id=product.category_id,
            name_ru="Zz Empty Brand",
            name_en="Zz Empty Brand",
            name_de="Zz Empty Brand",
            name_uk="Zz Empty Brand",
            sort_order=99,
        )
        session.add(empty)
        await session.commit()
        return product.category_id, product.subcategory_id, shelf.id, empty.id


async def set_on_sale(
    sessions: async_sessionmaker[AsyncSession], bottle: int, on_sale: bool
) -> None:
    async with sessions() as session:
        product = await session.get(Product, bottle)
        assert product is not None
        product.is_active = on_sale
        await session.commit()


async def to_payment(bot: RunningBot, chat: int, *, name: str = "Clara Schmidt") -> None:
    await bot.send(chat, EN.t("menu.cart"))
    await bot.press(chat, "cart:checkout")
    await bot.send(chat, name)
    await bot.press(chat, "checkout:delivery:pickup")
    await bot.send(chat, "Alexanderplatz 1")
    await bot.send(chat, "18:00")
    await bot.send(chat, EN.t("checkout.use_telegram"))


async def connection_lost(*args: object, **kwargs: object) -> None:
    raise OperationalError("INSERT INTO roulette_spins", {}, ConnectionResetError("closed"))


async def double_tap(
    bot: RunningBot, chat: int, data: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two taps on one checkout button, the second arriving while the first is at work."""
    screen = bot.showing(chat, data)
    real = checkout_screen.keyed_lock
    arrived = 0

    @asynccontextmanager
    async def counted(key: str) -> AsyncIterator[None]:
        nonlocal arrived
        arrived += 1
        async with real(key):
            yield

    with monkeypatch.context() as patched:
        patched.setattr(checkout_screen, "keyed_lock", counted)
        async with real(f"checkout:{chat}"):  # the first tap, still at work
            taps = [asyncio.create_task(bot.press(chat, data, on=screen)) for _ in range(2)]
            for _ in range(500):
                if arrived == 2:
                    break
                await asyncio.sleep(0.01)
        await asyncio.gather(*taps)
    assert arrived == 2, "both taps reached checkout while it was busy"


# ======================================================= the journey


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_the_whole_customer_journey_speaks_the_readers_language(
    sessions: async_sessionmaker[AsyncSession],
    lands: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    i18n = LocalizationService(language)
    code = LanguageCode(language)
    gus, nia, ivy = 7801, 7817, 7818  # the catalog and errors; half onboarded; another city
    alex, bea, dora, fred, hal, kim = 7811, 7812, 7813, 7814, 7815, 7816
    settings = tree_settings(review_invite_link=REVIEWS_LINK)
    async with sessions() as session:
        for telegram_id in (gus, alex, dora, fred, hal, kim, ADMIN_ID):
            await make_user(session, telegram_id=telegram_id, language=code)
        await make_user(session, telegram_id=nia, language=code, city=None)
        await session.commit()
    bot = RunningBot(sessions, settings, telegram=FaithfulTelegram())

    # The catalog before the shop has anything in it.
    await bot.send(gus, EN.t("menu.catalog"))

    bottle = await open_shop(sessions)
    liquids, brand, shelf, empty_brand = await empty_corners(sessions, bottle)
    invalidate_categories_cache()  # as the admin's catalog edits do
    await lifecycle.activate_loyalty(settings)  # the deployment: a welcome spin each
    async with sessions() as session:  # a customer from after it, in the other kind of city
        await make_user(session, telegram_id=ivy, language=code, city=CityChoice.DELIVERY)
        await session.commit()
    await give_stamps(sessions, alex, 13)
    await give_stamps(sessions, hal, 9)

    # 👥 Invite a Friend, and a friend who opens the link and onboards.
    await bot.send(alex, EN.t("menu.invite"))
    payload = link_on(latest(bot, alex)).split("?start=")[1]
    await bot.send(bea, f"/start {payload}", first_name="Bea")
    bea_from = len(bot.telegram.calls)  # until she picks a language, the bot cannot know it
    await bot.press(bea, f"lang:{language}")
    await bot.press(bea, "city:berlin")
    await bot.send(alex, EN.t("menu.invite"))  # one friend joined, waiting for a first order

    # The catalog: every level, a missing category, brand and product, empty ones.
    await bot.send(bea, EN.t("menu.catalog"))
    await bot.press(bea, f"category:{liquids}")
    await bot.press(bea, f"subcat:{brand}")
    await bot.press(bea, f"prod:{bottle}")
    await bot.press(bea, f"cart:add:{bottle}")
    for missing in ("category:999999", "subcat:999999", "prod:999999"):
        await bot.press(bea, missing, on=latest(bot, bea))
    await bot.press(bea, f"category:{shelf}", on=latest(bot, bea))
    await bot.press(bea, f"subcat:{empty_brand}", on=latest(bot, bea))

    # The cart: more, fewer, removed, then checkout from an empty cart.
    await bot.send(bea, EN.t("menu.cart"))
    item = bot.button(bea, "cart:inc:").removeprefix("cart:inc:")
    for change in ("inc", "dec", "rm"):
        await bot.press(bea, f"cart:{change}:{item}")
    await bot.press(bea, "cart:checkout", on=latest(bot, bea))

    # Checkout, with every refusal a customer can run into on the way.
    await buy(bot, bea, bottle)
    await bot.send(bea, EN.t("menu.cart"))
    await bot.press(bea, "cart:checkout")
    await bot.feed(without_text(bot, bea))
    await bot.send(bea, "B")
    await bot.send(bea, "Clara Schmidt")
    await bot.send(bea, "courier, please")
    await bot.press(bea, "checkout:delivery:pickup")
    await bot.send(bea, "Alexanderplatz 1")
    await bot.send(bea, "18:00")
    await bot.send(bea, "not a phone")
    await bot.feed(contact_from(bot, bea, owner=alex))
    await bot.send(bea, EN.t("checkout.use_telegram"))
    await bot.send(bea, "cash")
    await bot.press(bea, "checkout:pay:card")
    await bot.send(bea, "ok")
    await double_tap(bot, bea, "checkout:confirm", monkeypatch)  # two taps, one order
    bea_id = await user_id(sessions, bea)
    await admin_moves(bot, (await latest_order(sessions, bea_id)).id, *TO_COMPLETED)

    # A checkout she cancels, an order the shop cancels, a product gone before she confirms.
    await buy(bot, bea, bottle)
    await bot.send(bea, EN.t("menu.cart"))
    await bot.press(bea, "cart:checkout")
    await bot.send(bea, EN.t("checkout.cancel"))
    await check_out(bot, bea)
    await admin_moves(bot, (await latest_order(sessions, bea_id)).id, OrderStatus.CANCELLED)
    await buy(bot, bea, bottle)
    await to_payment(bot, bea)
    await bot.press(bea, "checkout:pay:cash")
    await set_on_sale(sessions, bottle, False)
    await bot.press(bea, "checkout:confirm")
    await set_on_sale(sessions, bottle, True)

    # ℹ️ Info in Berlin, the reviews link with and without a reviews group, a new language.
    await bot.send(bea, EN.t("menu.info"))
    for topic in ("delivery", "payment", "contacts"):
        await bot.press(bea, f"info:{topic}")
        await bot.press(bea, "info:open")
    await bot.press(bea, "info:contacts")
    await bot.press(bea, "info:reviews")
    bot.dispatcher["settings"] = tree_settings()  # a shop without a reviews group
    invalidate_review_link_cache()
    await bot.press(bea, "info:reviews", on=latest(bot, bea))
    bot.dispatcher["settings"] = settings
    await bot.press(bea, "info:open", on=latest(bot, bea))
    await bot.press(bea, "info:change_language")
    await bot.press(bea, f"lang:{language}")
    # …and moved out of Berlin: the other cities' pages.
    await bot.send(bea, EN.t("menu.info"))
    await bot.press(bea, "info:change_city")
    await bot.press(bea, "city:delivery")
    await bot.send(bea, EN.t("menu.info"))
    for topic in ("delivery", "payment", "contacts"):
        await bot.press(bea, f"info:{topic}")
        await bot.press(bea, "info:open")

    # 🪪 The stamp card: full with extra, refreshed, claimed twice, a bottle waiting, too few.
    await bot.send(dora, EN.t("menu.stamp_card"))  # an empty card
    await bot.send(alex, EN.t("menu.stamp_card"))
    await bot.press(alex, "stamp:open")
    claim = bot.tap(alex, bot.button(alex, "stamp:claim:"))
    await bot.feed(claim)
    await bot.feed(claim)
    await bot.send(alex, EN.t("menu.stamp_card"))
    after_claim = await ledger_version(sessions, alex)
    await bot.press(alex, f"stamp:claim:{after_claim}", on=latest(bot, alex))

    # 🎰 The roulette: none, every prize, a full card, a replay, a stale spin, a failure.
    await bot.send(ivy, EN.t("menu.roulette"))
    await bot.press(ivy, "roulette:spin:1", on=latest(bot, ivy))
    lands("stamp_1")
    await bot.send(hal, EN.t("menu.roulette"))
    hals_spin = bot.button(hal, "roulette:spin:")
    await bot.press(hal, hals_spin)
    lands("stamp_2")
    await bot.send(alex, EN.t("menu.roulette"))  # the welcome spin, and the referral one
    alexs_spin = bot.button(alex, "roulette:spin:")
    await bot.press(alex, alexs_spin)
    await bot.press(alex, alexs_spin, on=latest(bot, alex))
    await bot.press(alex, hals_spin, on=latest(bot, alex))
    # The card changed since the claim (the spin's stamps): that old card is stale now.
    await bot.press(alex, f"stamp:claim:{after_claim}", on=latest(bot, alex))
    lands("discount_10")
    await bot.send(dora, EN.t("menu.roulette"))
    await bot.press(dora, bot.button(dora, "roulette:spin:"))
    with monkeypatch.context() as broken:
        broken.setattr(RouletteEngine, "spin", connection_lost)
        await bot.send(kim, EN.t("menu.roulette"))
        await bot.press(kim, bot.button(kim, "roulette:spin:"))
    lands("discount_5")
    await bot.press(kim, bot.button(kim, "roulette:spin:"))
    lands("free_bottle")
    await bot.send(fred, EN.t("menu.roulette"))
    await bot.press(fred, bot.button(fred, "roulette:spin:"))

    # 🎁 Rewards at checkout: a free bottle, a discount, one that is not hers to use.
    fred_id, dora_id = await user_id(sessions, fred), await user_id(sessions, dora)
    await buy(bot, fred, bottle, quantity=2)
    await check_out(bot, fred, reward_id=(await rewards(sessions, fred_id))[0][0])
    await admin_moves(bot, (await latest_order(sessions, fred_id)).id, *TO_COMPLETED)
    doras_discount = (await rewards(sessions, dora_id))[0][0]
    await buy(bot, dora, bottle)
    await check_out(bot, dora, reward_id=doras_discount)
    await admin_moves(bot, (await latest_order(sessions, dora_id)).id, *TO_COMPLETED)
    await buy(bot, kim, bottle)
    await to_payment(bot, kim)
    await bot.press(kim, "checkout:pay:cash")
    step = bot.showing(kim, "checkout:reward:none")
    await bot.press(kim, f"checkout:reward:{doras_discount}", on=step)
    await bot.press(kim, "checkout:reward:none")
    await bot.press(kim, "checkout:confirm")

    # Checkout outside Berlin: a delivery her city does not have, then she cancels.
    await buy(bot, ivy, bottle)
    await bot.send(ivy, EN.t("menu.cart"))
    await bot.press(ivy, "cart:checkout")
    await bot.send(ivy, "Ivy Ivanova")
    await bot.press(ivy, "checkout:delivery:pickup", on=latest(bot, ivy))
    await bot.press(ivy, "checkout:delivery:postal")
    await bot.send(ivy, EN.t("checkout.cancel"))

    # Their own link, /admin, a stale button, half an onboarding, and each kind of failure.
    await bot.send(alex, f"/start {payload}")
    await bot.send(gus, "/admin")
    await bot.press(gus, "catalog:discontinued", on=latest(bot, gus))
    await bot.send(nia, "/start")
    await bot.press(nia, "stamp:open", on=latest(bot, nia))
    for failure in (
        RuntimeError("boom"),
        OperationalError("SELECT 1", {}, ConnectionResetError("closed")),
        TelegramNetworkError(method=GetMe(), message="timed out"),
        TelegramBadRequest(method=GetMe(), message="Bad Request: chat not found"),
    ):
        with monkeypatch.context() as broken:

            async def fail(*args: object, failure: Exception = failure, **kwargs: object) -> None:
                raise failure

            broken.setattr(UserService, "ensure_user", fail)
            await bot.send(gus, EN.t("menu.cart"))

    # ======================================================= what the journey showed
    everything = sent(bot, since={bea: bea_from}) + referral_variants(i18n)
    shown_text = "\n".join(plain(item.text) for item in everything)
    missing = sorted(
        key
        for key in required(language)
        if (fixed := literal(i18n.t(key))) and fixed not in shown_text
    )

    namespaces = {key.split(".")[0] for key in load_locale(language)}
    phrases = other_languages_phrases(language)
    texts = other_languages_texts(language)
    endonyms = tuple(i18n.t(f"language.{other}") for other in SUPPORTED_LANGUAGES)
    shop = await shop_content(sessions)
    not_ours = (*NOT_OURS, *shop, *endonyms)
    problems: list[str] = []
    for item in everything:
        where = f"{item.kind} to {item.chat}: {item.text[:70]!r}"
        body = the_bots_words(item.text, not_ours)
        found: list[str] = []
        if PLACEHOLDER.search(item.text):
            found.append("an unfilled placeholder")
        if raw := [t for t in KEY_LIKE.findall(item.text) if t.split(".")[0] in namespaces]:
            found.append(f"raw key {raw}")
        if issue := markup_problem(item.kind, item.text):
            found.append(issue)
        found += language_problems(item, language, body, phrases, texts)
        is_shop_content = any(token in item.text for token in shop)
        found += phone_problems(item, is_shop_content)
        problems += [f"{problem} in {where}" for problem in found]
    assert missing == [], f"{language}: never shown to a customer in this language: {missing}"
    assert problems == [], f"{language}:\n" + "\n".join(sorted(set(problems)))


def test_what_customers_never_see_is_really_out_of_their_reach() -> None:
    """The UNREACHABLE list must not hide a text that some code path does show."""
    key = re.compile(r"""["']((?:common|catalog|city|checkout|error|product)\.[a-z0-9_.]+)["']""")
    named: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        for found in key.findall(path.read_text(encoding="utf-8")):
            named.setdefault(found, set()).add(rel)
    admin_modules = ("handlers/admin", "keyboards/admin", "services/admin", "utils/admin")
    for unreachable, reason in UNREACHABLE.items():
        places = named.get(unreachable, set())
        if reason == UNUSED:
            assert places == set(), f"{unreachable} is shown by {places} — take it off the list"
        elif reason == ADMIN_ONLY:
            assert places and all(p.startswith(admin_modules) for p in places), unreachable
