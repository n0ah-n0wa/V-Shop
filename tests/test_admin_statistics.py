"""
The statistics dashboard: who can reach it, and what it renders.

The seeded shop comes from :mod:`tests.test_statistics` — importing its fixture
rather than restating the dataset keeps the numbers on screen pinned to the same
enumerated orders the service tests verify. If a figure here disagrees with one
there, one of them is wrong and both will say so.
"""

from __future__ import annotations

import asyncio
import functools
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.admin import IsAdmin
from app.filters.localized_text import LocalizedText
from app.handlers.admin.statistics import _render, refresh_statistics
from app.keyboards.admin import admin_menu_keyboard
from app.keyboards.admin_statistics import CALLBACK_STATS_REFRESH, statistics_keyboard
from app.keyboards.reply import main_menu_keyboard
from app.middlewares.admin import AdminOnlyMiddleware
from app.middlewares.private_chat import PrivateChatMiddleware
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.services.statistics import StatisticsService
from app.states.admin import AddProductStates
from app.utils.statistics_display import (
    MAX_PRODUCT_NAME,
    format_amount,
    format_statistics,
    shorten,
)
from tests.production_bot import ADMIN_ID, mount, production_router
from tests.shop_dataset import NOW, Shop

LANGUAGES = ("en", "ru", "de", "uk")
CUSTOMER_ID = 7000001
TELEGRAM_MESSAGE_LIMIT = 4096


def settings_for(
    *admin_ids: int,
    timezone: str = "Europe/Berlin",
    currency: str = "€",
) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        admin_ids=list(admin_ids),
        manager_chat_id=-1001234567890,
        app_timezone=timezone,
        currency_symbol=currency,
    )


def _chat(kind: str = "private", chat_id: int = ADMIN_ID) -> Chat:
    return Chat(id=chat_id, type=kind)


def _message(text: str, *, kind: str = "private", user_id: int = ADMIN_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=_chat(kind, user_id),
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        text=text,
    )


def _callback(
    data: str = CALLBACK_STATS_REFRESH,
    *,
    kind: str = "private",
    user_id: int = ADMIN_ID,
) -> CallbackQuery:
    return CallbackQuery(
        id="c",
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        chat_instance="i",
        data=data,
        message=_message("dashboard", kind=kind, user_id=user_id),
    )


def button_text(language: str) -> str:
    return LocalizationService(language).t("admin.menu_statistics")


async def dashboard(
    session: AsyncSession, language: str = "en", *, timezone: str = "Europe/Berlin"
) -> str:
    stats = await StatisticsService(session, timezone).collect(NOW)
    return format_statistics(stats, LocalizationService(language))


class _RecordingBot(Bot):
    """A Bot that records outgoing API calls instead of making them."""

    def __init__(self) -> None:
        super().__init__(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.calls: list[TelegramMethod[Any]] = []

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        self.calls.append(method)
        return None


class _StubSession:
    """Stands in for Database + Localization, injecting what handlers expect."""

    def __init__(self) -> None:
        self.session: AsyncSession | None = None

    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        data["session"] = self.session
        data["i18n"] = LocalizationService("en")
        data["language"] = "en"
        data["db_user"] = None
        return await handler(event, data)


def routers_under(name: str) -> set[str]:
    """Every router name in the subtree rooted at ``name``."""

    def find(router: Router) -> Router | None:
        if router.name == name:
            return router
        for child in router.sub_routers:
            hit = find(child)
            if hit is not None:
                return hit
        return None

    subtree = find(production_router())
    assert subtree is not None, f"no router named {name!r} in the tree"
    return _routers(subtree)


@functools.cache
def _harness() -> tuple[Dispatcher, _StubSession]:
    """
    One dispatcher for the module — a Router attaches to exactly one parent.

    The stub middleware is mutable so each test can point it at its own session.
    """
    stub = _StubSession()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings_for(ADMIN_ID)
    dispatcher.update.outer_middleware(PrivateChatMiddleware())
    dispatcher.update.outer_middleware(stub)
    mount(dispatcher)
    return dispatcher, stub


async def _feed(
    session: AsyncSession, message: Message, *, state: str | None = None
) -> list[dict[str, Any]]:
    """
    Push an update through the real router tree and return what was sent.

    Only the database and localization middlewares are stubbed; the private-chat
    gate, the admin filter, the admin middleware and every router filter are the
    production objects. ``state`` puts the admin mid-wizard first.
    """
    dispatcher, stub = _harness()
    mount(dispatcher)  # another module's dispatcher may hold the shared tree
    stub.session = session

    bot = _RecordingBot()
    key = StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=message.chat.id)
    try:
        await dispatcher.storage.set_state(key, state)
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    finally:
        await dispatcher.storage.set_state(key, None)
        await bot.session.close()

    return [
        {"chat_id": call.chat_id, "text": call.text, "reply_markup": call.reply_markup}
        for call in bot.calls
        if isinstance(call, SendMessage)
    ]


# ======================================================== who can reach it


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_button_is_on_the_admin_menu(language: str) -> None:
    i18n = LocalizationService(language)
    labels = {button.text for row in admin_menu_keyboard(i18n).keyboard for button in row}

    assert i18n.t("admin.menu_statistics") in labels
    # the existing sections must still be there
    for key in ("menu_products", "menu_categories", "menu_orders", "menu_settings"):
        assert i18n.t(f"admin.{key}") in labels


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_button_is_on_no_customer_keyboard(language: str) -> None:
    """A customer must never be offered the dashboard."""
    i18n = LocalizationService(language)
    labels = {button.text for row in main_menu_keyboard(i18n).keyboard for button in row}

    assert i18n.t("admin.menu_statistics") not in labels


@pytest.mark.asyncio
async def test_the_router_filter_admits_admins_only() -> None:
    cfg = settings_for(ADMIN_ID)
    is_admin = IsAdmin(cfg)

    assert await is_admin(_message(button_text("en"), user_id=ADMIN_ID))
    assert await is_admin(_callback(user_id=ADMIN_ID))
    assert not await is_admin(_message(button_text("en"), user_id=CUSTOMER_ID))
    assert not await is_admin(_callback(user_id=CUSTOMER_ID))


@pytest.mark.asyncio
async def test_a_customer_is_dropped_by_the_admin_middleware() -> None:
    """Second gate: even past the filter, a non-admin reaches no handler."""
    reached: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        reached.append(event)
        return "reached"

    middleware = AdminOnlyMiddleware(settings_for(ADMIN_ID))

    def data_for(user_id: int) -> dict[str, Any]:
        # the dispatcher resolves the sender before outer middlewares run
        return {"event_from_user": User(id=user_id, is_bot=False, first_name="T")}

    customer = data_for(CUSTOMER_ID)
    assert (
        await middleware(handler, _message(button_text("en"), user_id=CUSTOMER_ID), customer)
        is None
    )
    assert await middleware(handler, _callback(user_id=CUSTOMER_ID), customer) is None
    assert reached == []

    admin = data_for(ADMIN_ID)
    assert (
        await middleware(handler, _message(button_text("en"), user_id=ADMIN_ID), admin) == "reached"
    )
    assert reached


@pytest.mark.parametrize("kind", ["group", "supergroup", "channel"])
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_no_group_chat_can_open_the_dashboard(kind: str, language: str) -> None:
    """Even from an admin's own account, in a group it must go nowhere."""
    reached: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        reached.append(event)
        return "reached"

    gate = PrivateChatMiddleware()
    for update in (
        Update(update_id=1, message=_message(button_text(language), kind=kind)),
        Update(update_id=2, callback_query=_callback(kind=kind)),
    ):
        assert await gate(handler, update, {}) is None

    assert reached == [], f"the dashboard was reachable from a {kind}"


@pytest.mark.asyncio
async def test_the_same_updates_work_in_a_private_chat() -> None:
    """The gate must not be so tight that it breaks the feature."""
    reached: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        reached.append(event)
        return "reached"

    gate = PrivateChatMiddleware()
    for update in (
        Update(update_id=1, message=_message(button_text("en"))),
        Update(update_id=2, callback_query=_callback()),
    ):
        assert await gate(handler, update, {}) == "reached"

    assert len(reached) == 2


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_button_matches_in_every_language(language: str) -> None:
    """
    An admin who switched language mid-session still opens the dashboard.

    ``LocalizedText`` compares against every language's translation of the key,
    so the button typed in Ukrainian matches while the session says English.
    """
    matcher = LocalizedText("admin.menu_statistics")

    assert asyncio.run(matcher(_message(button_text(language))))
    assert not asyncio.run(matcher(_message("not the button")))


def test_the_refresh_callback_fits_telegram() -> None:
    """Telegram truncates callback data over 64 bytes."""
    assert len(CALLBACK_STATS_REFRESH.encode()) <= 64


# ============================================================ what it renders


@pytest.mark.asyncio
async def test_the_dashboard_reports_every_required_figure(
    session: AsyncSession, shop: Shop
) -> None:
    text = await dashboard(session)

    # GENERAL — users, categories, subcategories, products, total orders
    for label, value in (
        ("Users", 3),
        ("Categories", 2),
        ("Subcategories", 3),
        ("Products", 8),
        ("Orders", 18),
    ):
        assert f"{label}: <b>{value}</b>" in text, f"missing {label}"

    # ORDERS — three periods, each total / completed / cancelled
    assert "All time: 18 · ✅ 11 · ❌ 3" in text
    assert "This month: 9 · ✅ 5 · ❌ 1" in text
    assert "Last month: 6 · ✅ 4 · ❌ 1" in text

    # REVENUE — three periods, completed only
    assert "All time: €193.00" in text
    assert "This month: €82.00" in text
    assert "Last month: €83.00" in text

    # PRODUCTS — top 3 and bottom 3
    assert "1. mango — 5" in text
    assert "2. berry — 4" in text
    assert "3. ice — 1" in text
    assert "1. mint — 0" in text
    assert "2. void — 0" in text


@pytest.mark.asyncio
async def test_every_section_is_present_and_ordered(session: AsyncSession, shop: Shop) -> None:
    text = await dashboard(session)
    i18n = LocalizationService("en")

    positions = [
        text.index(i18n.t(key))
        for key in (
            "admin.stats_title",
            "admin.stats_general",
            "admin.stats_orders",
            "admin.stats_revenue",
            "admin.stats_top",
            "admin.stats_bottom",
        )
    ]
    assert positions == sorted(positions), "sections are out of order"


@pytest.mark.asyncio
async def test_the_dashboard_fits_one_telegram_message(session: AsyncSession, shop: Shop) -> None:
    """A dashboard split across two messages is not a dashboard."""
    assert len(await dashboard(session)) < TELEGRAM_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_the_layout_is_narrow_enough_for_a_phone(session: AsyncSession, shop: Shop) -> None:
    """Long lines wrap mid-figure on a phone and the columns stop lining up."""
    plain = (
        (await dashboard(session))
        .replace("<b>", "")
        .replace("</b>", "")
        .replace("<i>", "")
        .replace("</i>", "")
    )
    longest = max(plain.splitlines(), key=len)

    assert len(longest) <= 60, f"{len(longest)} chars: {longest!r}"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_the_dashboard_renders_in_every_language(
    session: AsyncSession, shop: Shop, language: str
) -> None:
    i18n = LocalizationService(language)
    text = await dashboard(session, language)

    for key in (
        "admin.stats_title",
        "admin.stats_general",
        "admin.stats_orders",
        "admin.stats_revenue",
        "admin.stats_top",
        "admin.stats_bottom",
        "admin.stats_users",
        "admin.stats_period_current",
    ):
        assert i18n.t(key) in text, f"{language}: {key} not rendered"
    assert "{" not in text, f"{language}: an unfilled placeholder leaked through"


@pytest.mark.asyncio
async def test_the_languages_actually_differ(session: AsyncSession, shop: Shop) -> None:
    rendered = {lang: await dashboard(session, lang) for lang in LANGUAGES}

    assert len(set(rendered.values())) == len(LANGUAGES), "two languages render alike"
    # the numbers are the same in all of them
    for text in rendered.values():
        assert "193" in text


@pytest.mark.asyncio
async def test_hidden_products_never_reach_the_dashboard(session: AsyncSession, shop: Shop) -> None:
    """``ghost``/``shelved``/``archived`` are off sale; two of them have sales."""
    text = await dashboard(session)

    for hidden in ("ghost", "shelved", "archived"):
        assert hidden not in text


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_product_names_are_localized(session: AsyncSession, language: str) -> None:
    admin = AdminService(session)
    category = await admin.create_category("C")
    await session.flush()
    await admin.create_product(
        category_id=category.id,
        name_ru="Манго",
        name_en="Mango",
        name_de="Mango-Eis",
        name_uk="Манго Айс",
        description_ru="d",
        description_en="d",
        description_de="d",
        description_uk="d",
        flavor="f",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("10.00"),
    )
    await session.flush()

    text = await dashboard(session, language)
    expected = {"ru": "Манго", "en": "Mango", "de": "Mango-Eis", "uk": "Манго Айс"}

    assert f"1. {expected[language]} — 0" in text


@pytest.mark.asyncio
async def test_product_names_are_html_escaped(session: AsyncSession) -> None:
    """A product name is admin-supplied text going into an HTML message."""
    admin = AdminService(session)
    category = await admin.create_category("C")
    await session.flush()
    hostile = "<script>x</script> & co"  # short enough to survive truncation
    await admin.create_product(
        category_id=category.id,
        name_ru=hostile,
        name_en=hostile,
        name_de=hostile,
        name_uk=hostile,
        description_ru="d",
        description_en="d",
        description_de="d",
        description_uk="d",
        flavor="f",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("10.00"),
    )
    await session.flush()

    text = await dashboard(session)

    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


# ============================================================== zero data


@pytest.mark.asyncio
async def test_an_empty_shop_renders_placeholders(session: AsyncSession) -> None:
    i18n = LocalizationService("en")
    text = await dashboard(session)

    assert "Users: <b>0</b>" in text
    assert "All time: 0 · ✅ 0 · ❌ 0" in text
    assert "All time: €0.00" in text
    assert text.count(i18n.t("admin.stats_empty_products")) == 2, (
        "both product lists should say the catalog is empty"
    )
    assert len(text) < TELEGRAM_MESSAGE_LIMIT


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_an_empty_shop_renders_in_every_language(
    session: AsyncSession, language: str
) -> None:
    text = await dashboard(session, language)

    assert LocalizationService(language).t("admin.stats_empty_products") in text
    assert "{" not in text


@pytest.mark.asyncio
async def test_products_with_no_sales_say_so_differently(
    session: AsyncSession,
) -> None:
    """
    "No products yet" and "nothing has sold yet" are different problems.

    The first tells the admin to build a catalog; the second tells them the
    catalog is fine and no order has completed.
    """
    admin = AdminService(session)
    category = await admin.create_category("C")
    await session.flush()
    await admin.create_product(
        category_id=category.id,
        name_ru="p",
        name_en="p",
        name_de="p",
        name_uk="p",
        description_ru="d",
        description_en="d",
        description_de="d",
        description_uk="d",
        flavor="f",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("10.00"),
    )
    await session.flush()
    i18n = LocalizationService("en")

    text = await dashboard(session)

    assert i18n.t("admin.stats_empty_sales") in text, "top list: nothing has sold"
    assert "1. p — 0" in text, "bottom list still ranks the product"


# ============================================================== formatting


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("0", "€0.00"),
        ("0.5", "€0.50"),
        ("9.99", "€9.99"),
        ("999.99", "€999.99"),
        ("1000", "€1,000.00"),
        ("18721", "€18,721.00"),
        ("1234567.89", "€1,234,567.89"),
        ("-42.5", "€-42.50"),
    ],
)
def test_english_amounts_are_grouped_and_two_decimal(amount: str, expected: str) -> None:
    assert format_amount(Decimal(amount), LocalizationService("en")) == expected


@pytest.mark.parametrize(
    "language,expected",
    [
        ("en", "€1,234,567.89"),
        ("de", "1.234.567,89 €"),
        ("ru", "1 234 567,89 €"),
        ("uk", "1 234 567,89 €"),
    ],
)
def test_money_follows_the_reader_s_own_convention(language: str, expected: str) -> None:
    """
    "1,234.56" reads as one-point-two-three to a German shop owner.

    Separators and the position of the symbol both come from the catalog, so
    each language sees the number the way it writes numbers.
    """
    assert format_amount(Decimal("1234567.89"), LocalizationService(language)) == expected


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_language_places_the_currency(language: str) -> None:
    rendered = format_amount(Decimal("12.30"), LocalizationService(language))

    assert "€" in rendered, f"{language} lost the currency symbol"
    assert rendered.strip("€ ").replace(" ", "") in {"12.30", "12,30"}


def test_the_currency_symbol_is_configurable(session: AsyncSession) -> None:
    """A shop that does not price in euros must not be stuck with one."""
    assert format_amount(Decimal("5"), LocalizationService("en"), currency="£") == "£5.00"
    assert Settings.model_fields["currency_symbol"].default == "€"


@pytest.mark.asyncio
async def test_the_configured_currency_reaches_the_dashboard(
    session: AsyncSession, shop: Shop
) -> None:
    text = await _render(session, LocalizationService("en"), settings_for(ADMIN_ID, currency="zł"))

    assert "zł193.00" in text
    assert "€" not in text


def test_a_group_separator_never_doubles_as_the_decimal_mark() -> None:
    """Within one language the two marks must differ, or the number is unreadable."""
    for language in LANGUAGES:
        i18n = LocalizationService(language)
        assert i18n.t("format.group_separator") != i18n.t("format.decimal_separator"), language


# =============================================================== timezone


@pytest.mark.asyncio
async def test_the_dashboard_uses_the_configured_timezone(
    session: AsyncSession, shop: Shop
) -> None:
    """``_render`` must pass ``settings.app_timezone`` through, not a default."""
    berlin = await _render(session, LocalizationService("en"), settings_for(ADMIN_ID))
    honolulu = await _render(
        session,
        LocalizationService("en"),
        settings_for(ADMIN_ID, timezone="Pacific/Honolulu"),
    )

    assert "Europe/Berlin" in berlin
    assert "Pacific/Honolulu" in honolulu


@pytest.mark.asyncio
async def test_a_broken_timezone_still_renders(session: AsyncSession, shop: Shop) -> None:
    """A typo in APP_TIMEZONE must not take the dashboard down."""
    text = await _render(
        session, LocalizationService("en"), settings_for(ADMIN_ID, timezone="Not/AZone")
    )

    assert "Europe/Berlin" in text, "falls back to the default zone"


# ================================================================ refresh


class _SpyMessage:
    """Stands in for the message being edited in place."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.edits.append(text)


class _SpyCallback:
    def __init__(self, message: Any) -> None:
        self.message = message
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append(text)


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=message)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_refresh_redraws_the_dashboard(session: AsyncSession, shop: Shop) -> None:
    spy = _SpyMessage()
    callback = _SpyCallback(spy)

    await refresh_statistics(
        callback,  # type: ignore[arg-type]
        session,
        LocalizationService("en"),
        settings_for(ADMIN_ID),
    )

    assert callback.answers == [LocalizationService("en").t("admin.stats_refreshed")]
    assert len(spy.edits) == 1
    assert "All time: €193.00" in spy.edits[0]


@pytest.mark.asyncio
async def test_refresh_tolerates_an_unchanged_dashboard(session: AsyncSession, shop: Shop) -> None:
    """
    Nothing sold since the last tap is the common case, not an error.

    Telegram rejects an edit that would not change the message; swallowing only
    that one rejection keeps a genuine API failure loud.
    """
    spy = _SpyMessage(_bad_request("Bad Request: message is not modified"))
    callback = _SpyCallback(spy)

    await refresh_statistics(
        callback,  # type: ignore[arg-type]
        session,
        LocalizationService("en"),
        settings_for(ADMIN_ID),
    )

    assert callback.answers, "the admin still gets feedback"


@pytest.mark.asyncio
async def test_refresh_reraises_any_other_api_failure(session: AsyncSession, shop: Shop) -> None:
    spy = _SpyMessage(_bad_request("Bad Request: message to edit not found"))
    callback = _SpyCallback(spy)

    with pytest.raises(TelegramBadRequest):
        await refresh_statistics(
            callback,  # type: ignore[arg-type]
            session,
            LocalizationService("en"),
            settings_for(ADMIN_ID),
        )


@pytest.mark.asyncio
async def test_refresh_survives_an_inaccessible_message(session: AsyncSession, shop: Shop) -> None:
    """An old dashboard the bot can no longer edit must not raise."""
    callback = _SpyCallback(None)

    await refresh_statistics(
        callback,  # type: ignore[arg-type]
        session,
        LocalizationService("en"),
        settings_for(ADMIN_ID),
    )

    assert callback.answers


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_refresh_button_is_localized(language: str) -> None:
    i18n = LocalizationService(language)
    keyboard = statistics_keyboard(i18n)

    button = keyboard.inline_keyboard[0][0]
    assert button.text == i18n.t("admin.stats_refresh")
    assert button.callback_data == CALLBACK_STATS_REFRESH


# ================================================== wiring: really behind the gates


def _routers(router: Router) -> set[str]:
    """Every router name in a tree, including the root."""
    found = {router.name}
    for child in router.sub_routers:
        found |= _routers(child)
    return found


def test_the_dashboard_is_mounted_inside_the_admin_tree() -> None:
    """
    Filters and middlewares only protect what is actually mounted behind them.

    Regression: the access tests above all drive the gate classes directly, so
    they stayed green when the statistics router was detached from the admin
    tree altogether. This one fails if it is unmounted or mounted anywhere else.
    """
    assert "admin_statistics" in routers_under("admin")
    assert "admin_statistics" not in routers_under("user"), (
        "the dashboard must not hang off the customer tree"
    )


def test_the_admin_tree_carries_both_gates() -> None:
    """What "mounted inside the admin tree" is worth."""
    _harness()  # compose the tree the same way production does
    admin = next(r for r in production_router().sub_routers if r.name == "admin")

    for observer in (admin.message, admin.callback_query):
        assert any(isinstance(f.callback, IsAdmin) for f in observer._handler.filters), (
            f"router-level IsAdmin filter missing on {observer.event_name}"
        )
        assert any(isinstance(m, AdminOnlyMiddleware) for m in observer.middleware), (
            f"AdminOnlyMiddleware missing on {observer.event_name}"
        )


@pytest.mark.asyncio
async def test_an_admin_reaches_the_dashboard_through_the_real_dispatcher(
    session: AsyncSession, shop: Shop
) -> None:
    """
    End to end: real router tree, real gates, real filters.

    Only the database session is stubbed — everything an update passes through
    on the way to the handler is the production wiring.
    """
    sent = await _feed(session, _message(button_text("en"), user_id=ADMIN_ID))

    assert len(sent) == 1, "the admin should get exactly one dashboard"
    assert "All time: €193.00" in sent[0]["text"]
    assert sent[0]["reply_markup"] is not None, "the Refresh button should be attached"


@pytest.mark.asyncio
async def test_a_customer_reaches_nothing_through_the_real_dispatcher(
    session: AsyncSession, shop: Shop
) -> None:
    sent = await _feed(session, _message(button_text("en"), user_id=CUSTOMER_ID))

    assert sent == [], "a customer got a reply to the statistics button"


@pytest.mark.parametrize("kind", ["group", "supergroup"])
@pytest.mark.asyncio
async def test_a_group_reaches_nothing_through_the_real_dispatcher(
    session: AsyncSession, shop: Shop, kind: str
) -> None:
    """Admin credentials, wrong chat type: still nothing."""
    sent = await _feed(session, _message(button_text("en"), kind=kind, user_id=ADMIN_ID))

    assert sent == [], f"the dashboard answered into a {kind}"


@pytest.mark.asyncio
async def test_the_button_is_refused_mid_wizard(session: AsyncSession, shop: Shop) -> None:
    """
    Regression: the wizard guard listed every menu button except this one.

    Tapping a section button while a wizard is open must say so, not silently do
    nothing and not jump to the dashboard leaving a half-built product behind.
    """
    i18n = LocalizationService("en")
    sent = await _feed(
        session,
        _message(button_text("en"), user_id=ADMIN_ID),
        state=AddProductStates.name_ru.state,
    )

    assert len(sent) == 1
    assert sent[0]["text"] == i18n.t("admin.wizard_in_progress")
    assert "Statistics" not in sent[0]["text"]


@pytest.mark.asyncio
async def test_the_dashboard_opens_once_the_wizard_is_done(
    session: AsyncSession, shop: Shop
) -> None:
    """The guard must not latch: with no state, the button works again."""
    sent = await _feed(session, _message(button_text("en"), user_id=ADMIN_ID))

    assert "All time: €193.00" in sent[0]["text"]


# ============================================== long names / mobile layout


LONG_NAME = "Ultra Premium Mango Ice Blast Extra Strong Limited Edition 2026"
LONG_CYRILLIC = "Ультра Преміум Манго Айс Бласт Екстра Міцний Лімітована Серія"


async def _seed_named(session: AsyncSession, **names: str) -> None:
    admin = AdminService(session)
    category = await admin.create_category("C")
    await session.flush()
    await admin.create_product(
        category_id=category.id,
        description_ru="d",
        description_en="d",
        description_de="d",
        description_uk="d",
        flavor="f",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("10.00"),
        **names,
    )
    await session.flush()


@pytest.mark.parametrize("name", [LONG_NAME, LONG_CYRILLIC], ids=["latin", "cyrillic"])
def test_a_long_name_is_trimmed_to_one_line(name: str) -> None:
    i18n = LocalizationService("en")

    shortened = shorten(name, i18n)

    assert len(shortened) < len(name)
    assert shortened.endswith("…")
    assert len(shortened.rstrip("…")) <= MAX_PRODUCT_NAME


def test_a_short_name_is_left_alone() -> None:
    i18n = LocalizationService("en")

    assert shorten("Mango Ice", i18n) == "Mango Ice"
    assert "…" not in shorten("x" * MAX_PRODUCT_NAME, i18n)


def test_trimming_falls_on_a_word_boundary() -> None:
    """Cutting mid-word reads as a typo; cutting at a space reads as a trim."""
    assert shorten("Ultra Premium Mango Ice Blast", LocalizationService("en")) == (
        "Ultra Premium Mango Ice…"
    )


def test_trimming_happens_before_escaping() -> None:
    """
    Cutting escaped text can slice an entity in half and leave ``&am`` on screen.

    The ampersand here sits exactly where a naive implementation would cut.
    """
    name = "Bubblegum Cola & Lime Extra" + " Long Tail"

    shortened = shorten(name, LocalizationService("en"))

    assert "&amp;" in shortened, "the ampersand must still be a whole entity"
    assert "&am…" not in shortened
    assert "&a;" not in shortened


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_long_names_do_not_widen_the_dashboard(session: AsyncSession, language: str) -> None:
    """
    Regression: this cap was only ever measured against the short fixture names.

    A real 63-character product name produced an 87-character line, which wraps
    to three rows on a phone and breaks the numbering down the left edge.
    """
    await _seed_named(
        session,
        name_ru=LONG_CYRILLIC,
        name_en=LONG_NAME,
        name_de=LONG_NAME,
        name_uk=LONG_CYRILLIC,
    )

    text = await dashboard(session, language)
    plain = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    longest = max(plain.splitlines(), key=len)

    assert len(longest) <= 40, f"{language}: {len(longest)} chars: {longest!r}"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_a_pathological_catalog_still_fits_one_message(
    session: AsyncSession, language: str
) -> None:
    """Six products with 200-character names must not overflow Telegram."""
    admin = AdminService(session)
    category = await admin.create_category("C")
    await session.flush()
    for index in range(6):
        monster = f"{index} " + ("Extremely Verbose Product Name " * 7)
        await admin.create_product(
            category_id=category.id,
            name_ru=monster,
            name_en=monster,
            name_de=monster,
            name_uk=monster,
            description_ru="d",
            description_en="d",
            description_de="d",
            description_uk="d",
            flavor="f",
            volume="30ml",
            nicotine_strength="3mg",
            price=Decimal("10.00"),
        )
    await session.flush()

    text = await dashboard(session, language)

    assert len(text) < TELEGRAM_MESSAGE_LIMIT
    assert text.count("…") >= 3, "every over-long name should be trimmed"


# ==================================================== grouping and legends


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_the_symbol_legend_appears_once_above_the_lines_that_use_it(
    session: AsyncSession, shop: Shop, language: str
) -> None:
    i18n = LocalizationService(language)
    text = await dashboard(session, language)
    legend = i18n.t("admin.stats_orders_legend")

    assert text.count(legend) == 1
    assert text.index(i18n.t("admin.stats_orders")) < text.index(legend)
    assert text.index(legend) < text.index(i18n.t("admin.stats_period_all"))


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_the_ranking_basis_is_stated_once_not_per_section(
    session: AsyncSession, shop: Shop, language: str
) -> None:
    """It used to be repeated in both product headers, which cost two lines."""
    i18n = LocalizationService(language)
    text = await dashboard(session, language)

    assert text.count(i18n.t("admin.stats_products_footnote")) == 1
    assert text.rstrip().endswith(
        i18n.t("admin.stats_hint", text=i18n.t("admin.stats_products_footnote"))
    )


@pytest.mark.asyncio
async def test_an_empty_catalog_drops_the_ranking_footnote(
    session: AsyncSession,
) -> None:
    """Nothing is ranked, so there is no ranking to explain."""
    i18n = LocalizationService("en")

    text = await dashboard(session)

    assert i18n.t("admin.stats_products_footnote") not in text


@pytest.mark.asyncio
async def test_sections_are_separated_by_exactly_one_blank_line(
    session: AsyncSession, shop: Shop
) -> None:
    """Grouping is carried by whitespace; doubled gaps make it scroll for nothing."""
    text = await dashboard(session)

    gap = "\n\n"
    assert gap + "\n" not in text, "a doubled gap makes the message scroll for nothing"
    assert text.count(gap) == 6, "title, general, orders, revenue, top, bottom, footnote"
