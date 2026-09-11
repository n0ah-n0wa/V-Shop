"""
Every loyalty feature, in every language — end to end, as customers and the admin see it.

One sweep per language through the production dispatcher (:mod:`tests.production_bot`):
the stamp card and a claim, the roulette and each kind of prize, a replayed and a
stale spin, the invite screen and both kinds of referral news, the reward step
at checkout, the admin's order card with the reward on it, a stale button, and a
customer opening their own link. Then everything the bot sent — messages,
edits, alerts, button labels — is checked: each feature appeared in the reader's
language, and nothing shows a raw key, an unfilled placeholder, markup Telegram
would reject, or English to a reader of another language.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.middlewares.database
from app import lifecycle
from app.handlers.user import roulette as roulette_screen
from app.models.category import Category, Subcategory
from app.models.enums import LanguageCode
from app.models.product import Product
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.utils.cache import invalidate_categories_cache
from app.utils.i18n import SUPPORTED_LANGUAGES, load_locale, locale_keys
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
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
# The manager's new-order alert is English on purpose (see docs/architecture.md).
MANAGER_ALERT = "🆕 <b>New order"
TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span",
    "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji",
}  # fmt: skip
PLACEHOLDER = re.compile(r"\{\w+\}")
KEY_LIKE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\b")
TAG = re.compile(r"<(/?)([a-zA-Z-]+)[^>]*>")
URL = re.compile(r"https?://\S+")
# Content that is not the bot's to translate: names people typed, the brand, Telegram.
NOT_OURS = ("Clara Schmidt", "Alexanderplatz", "Cheshire Vape", "Telegram", "VShopTestBot")

# What each feature says first — its own text must appear, in the reader's language.
FEATURES = (
    "menu.stamp_card",
    "menu.roulette",
    "menu.invite",
    "stamp_card.title",
    "stamp_card.promo",
    "stamp_card.ready",
    "stamp_card.claim",
    "stamp_card.claimed",
    "stamp_card.how",
    "roulette.title",
    "roulette.spins",
    "roulette.prizes",
    "roulette.spin",
    "roulette.good_luck",
    "roulette.spinning",
    "roulette.drumroll",
    "roulette.won",
    "roulette.jackpot",
    "roulette.reward_stamps",
    "roulette.reward_saved",
    "roulette.reward_free_bottle",
    "roulette.remaining",
    "roulette.already_played",
    "roulette.no_spins_left",
    "invite.title",
    "invite.how_title",
    "invite.link",
    "invite.no_friends",
    "invite.share",
    "invite.copy",
    "invite.news.joined",
    "invite.news.paid",
    "invite.news.stamps_added",
    "invite.news.welcome_bonus",
    "invite.news.your_turn",
    "invite.own_link",
    "checkout.ask_reward",
    "checkout.reward_option_discount",
    "checkout.reward_skip",
    "checkout.summary_subtotal",
    "checkout.summary_reward_discount",
    "admin.order_reward_discount",
    "error.invalid_callback",
)


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


def plain(text: str) -> str:
    return html.unescape(TAG.sub("", text))


def literal(template: str) -> str:
    """The longest fixed part of a template — what any rendering of it contains."""
    return max((part.strip() for part in PLACEHOLDER.split(plain(template))), key=len)


def latest(bot: RunningBot, chat: int) -> Message:
    screens = bot.screens(chat)
    return screens[max(screens)]


def sent(bot: RunningBot, *, since: dict[int, int]) -> list[tuple[str, int, str]]:
    """``(kind, chat, text)`` for every message, alert and button label the bot sent."""
    found: list[tuple[str, int, str]] = []
    for index, (method, _) in enumerate(bot.telegram.calls):
        if isinstance(method, AnswerCallbackQuery):
            chat = int(method.callback_query_id.split(":")[0])
            if method.text and index >= since.get(chat, 0):
                found.append(("alert", chat, method.text))
            continue
        chat = getattr(method, "chat_id", None)
        if not isinstance(chat, int) or chat == MANAGER_CHAT_ID or index < since.get(chat, 0):
            continue
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if isinstance(text, str) and not text.startswith(MANAGER_ALERT):
            found.append(("message", chat, text))
        markup = getattr(method, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
        for row in rows or []:
            found.extend(("button", chat, button.text) for button in row)
    return found


def markup_problem(kind: str, text: str) -> str | None:
    """Messages carry Telegram HTML, balanced; alerts and labels carry none."""
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


def english_phrases(language: str) -> set[str]:
    """Three or more English words in a row, from every English string that is translated."""
    english, catalog = load_locale("en"), load_locale(language)
    phrases: set[str] = set()
    for key, value in english.items():
        if catalog.get(key) == value:
            continue
        for chunk in re.split(r"\{\w+\}|\n| — |: |[()·•“”]", plain(value)):
            if len(re.findall(r"[A-Za-z]{2,}", chunk)) >= 3:
                phrases.add(chunk.strip())
    return phrases


# Words German never uses: two of them in one German message means English got in.
ENGLISH_WORDS = re.compile(
    r"\b(the|your|you|and|with|for|is|to|of|this|that|it|on|at)\b", re.IGNORECASE
)


def the_bots_words(text: str, not_ours: tuple[str, ...]) -> str:
    """The text without links, @usernames, or content that is not the bot's to translate."""
    body = URL.sub("", plain(text))
    for token in not_ours:
        # Whole words only: removing letters out of the bot's own words would hide them.
        body = re.sub(rf"(?<!\w){re.escape(token)}(?!\w)", "", body)
    return re.sub(r"@\w+", "", body)


def mostly_latin(body: str) -> bool:
    letters = [ch for ch in body if ch.isalpha()]
    cyrillic = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    return len(letters) >= 4 and cyrillic * 2 <= len(letters)


async def shop_names(sessions: async_sessionmaker[AsyncSession]) -> tuple[str, ...]:
    """
    Everything the shop itself stores about categories, brands and products —
    names, descriptions, attributes — in every language: its content, not the bot's words.
    """
    values: set[str] = set()
    async with sessions() as session:
        for model in (Category, Subcategory, Product):
            for row in (await session.scalars(select(model))).all():
                for column in model.__table__.columns:
                    value = getattr(row, column.key, None)
                    if isinstance(value, str) and len(value.strip()) >= 3:
                        values.add(value.strip())
    return tuple(sorted(values, key=len, reverse=True))


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_every_loyalty_feature_speaks_the_readers_language(
    sessions: async_sessionmaker[AsyncSession], lands: Callable[[str], None], language: str
) -> None:
    i18n = LocalizationService(language)
    code = LanguageCode(language)
    alex, bea, dora, fred = 7811, 7812, 7813, 7814
    settings = tree_settings()
    bottle = await open_shop(sessions)
    async with sessions() as session:
        for telegram_id in (alex, dora, fred, ADMIN_ID):
            await make_user(session, telegram_id=telegram_id, language=code)
        await session.commit()
    await lifecycle.activate_loyalty(settings)  # a welcome spin each
    async with sessions() as session:
        alex_id = await session.scalar(select(User.id).where(User.telegram_id == alex))
        dora_id = await session.scalar(select(User.id).where(User.telegram_id == dora))
        assert alex_id is not None and dora_id is not None
        await LoyaltyService(session).adjust(alex_id, amount=10, note="a full card")
        await session.commit()
    bot = RunningBot(sessions, settings)

    # 👥 Invite a Friend, and a friend who opens the link and orders.
    await bot.send(alex, EN.t("menu.invite"))
    screen = latest(bot, alex)
    assert screen.reply_markup is not None
    link = next(
        b.copy_text.text for r in screen.reply_markup.inline_keyboard for b in r if b.copy_text
    )
    payload = link.split("?start=")[1]
    await bot.send(bea, f"/start {payload}", first_name="Bea")
    bea_from = len(bot.telegram.calls)  # until she picks a language, the bot cannot know it
    await bot.press(bea, f"lang:{language}")
    await bot.press(bea, "city:berlin")

    # 🪪 My Stamp Card: a full card, claimed.
    await bot.send(alex, EN.t("menu.stamp_card"))
    await bot.press(alex, bot.button(alex, "stamp:claim:"))

    # 🎰 Lucky Roulette: stamps, then the same button again, then one naming nobody's spin.
    lands("stamp_2")
    await bot.send(alex, EN.t("menu.roulette"))
    spin = bot.button(alex, "roulette:spin:")
    await bot.press(alex, spin)
    await bot.press(alex, spin, on=latest(bot, alex))
    await bot.press(alex, "roulette:spin:999999", on=latest(bot, alex))
    lands("discount_10")
    await bot.send(dora, EN.t("menu.roulette"))
    await bot.press(dora, bot.button(dora, "roulette:spin:"))
    lands("free_bottle")
    await bot.send(fred, EN.t("menu.roulette"))
    await bot.press(fred, bot.button(fred, "roulette:spin:"))

    # The discount at checkout; the friend's first order; the admin completes both.
    await buy(bot, dora, bottle)
    await check_out(bot, dora, reward_id=(await rewards(sessions, dora_id))[0][0])
    await admin_moves(bot, (await latest_order(sessions, dora_id)).id, *TO_COMPLETED)
    await buy(bot, bea, bottle)
    await check_out(bot, bea)
    async with sessions() as session:
        bea_id = await session.scalar(select(User.id).where(User.telegram_id == bea))
    assert bea_id is not None
    await admin_moves(bot, (await latest_order(sessions, bea_id)).id, *TO_COMPLETED)

    # A customer opening their own link, and a button that outlived its screen.
    await bot.send(alex, f"/start {payload}")
    await bot.press(alex, "checkout:confirm", on=latest(bot, alex))

    everything = sent(bot, since={bea: bea_from})
    shown = "\n".join(plain(text) for _, _, text in everything)
    keys = locale_keys(language)
    # A key missing from the catalogs renders as itself, so it must exist as well as show.
    missing = [key for key in FEATURES if key not in keys or literal(i18n.t(key)) not in shown]
    assert missing == [], f"{language}: features not shown in the reader's language: {missing}"

    # A dotted token in a catalog namespace is a key shown raw, whether it exists or not.
    namespaces = {key.split(".")[0] for key in keys}
    problems: list[str] = []
    phrases = english_phrases(language) if language != "en" else set()
    endonyms = tuple(i18n.t(f"language.{other}") for other in SUPPORTED_LANGUAGES)
    not_ours = (*NOT_OURS, *await shop_names(sessions), *endonyms)
    for kind, chat, text in everything:
        where = f"{kind} to {chat}: {text[:60]!r}"
        body = the_bots_words(text, not_ours)
        if PLACEHOLDER.search(text):
            problems.append(f"unfilled placeholder in {where}")
        if raw := [t for t in KEY_LIKE.findall(text) if t.split(".")[0] in namespaces]:
            problems.append(f"raw key {raw} in {where}")
        if issue := markup_problem(kind, text):
            problems.append(f"{issue} in {where}")
        if leaked := [phrase for phrase in phrases if phrase in plain(text)]:
            problems.append(f"English {leaked[:2]} in {where}")
        if language == "de" and len({w.lower() for w in ENGLISH_WORDS.findall(body)}) >= 2:
            problems.append(f"English words in {where}")
        # Line by line: one English line in a mostly Cyrillic screen is still English.
        if language in ("ru", "uk") and any(mostly_latin(line) for line in body.splitlines()):
            problems.append(f"mostly Latin script in {where}")
    assert problems == [], f"{language}:\n" + "\n".join(problems)
