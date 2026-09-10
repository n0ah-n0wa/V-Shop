"""Ukrainian localization: selection, persistence, switching, notifications."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors.classify import locale_key_for_error
from app.errors.notify import safe_user_error_text
from app.keyboards.inline import language_keyboard
from app.models.enums import LanguageCode
from app.models.user import User
from app.repositories.category import CategoryRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.services.localization import LocalizationService
from app.services.user import UserService
from app.utils.i18n import SUPPORTED_LANGUAGES, assert_locales_in_sync, locale_keys
from app.utils.product_display import (
    format_product_card,
    localized_product_description,
    localized_product_name,
)
from tests.factories import make_user

EXPECTED_LANGUAGES = ("ru", "en", "de", "uk")


# --------------------------------------------------------------- catalogue


def test_exactly_four_languages_are_supported() -> None:
    assert SUPPORTED_LANGUAGES == EXPECTED_LANGUAGES
    assert {c.value for c in LanguageCode} == set(EXPECTED_LANGUAGES)


def test_ukrainian_catalog_is_complete_and_in_sync() -> None:
    assert_locales_in_sync()
    assert locale_keys("uk") == locale_keys("en")


def test_ukrainian_covers_every_user_facing_area() -> None:
    """Each area named in the localization requirements resolves in Ukrainian."""
    uk = LocalizationService.from_code("uk")
    en = LocalizationService.from_code("en")
    areas = {
        "onboarding": [
            "onboarding.choose_language",
            "onboarding.welcome",
            "onboarding.choose_city",
            "onboarding.city_saved",
        ],
        "main menu": ["menu.catalog", "menu.cart", "menu.stamp_card", "menu.info"],
        "stamp card": [
            "stamp_card.title",
            "stamp_card.how",
            "stamp_card.empty",
            "stamp_card.claim",
            "stamp_card.claimed",
            "stamp_card.already_claimed",
            "stamp_card.changed",
        ],
        "catalog": ["catalog.choose_category", "catalog.empty", "catalog.category_empty"],
        "cart": ["cart.title", "cart.empty", "cart.checkout", "cart.item_removed"],
        "checkout": [
            "checkout.ask_name",
            "checkout.ask_address",
            "checkout.success",
            "checkout.cancelled",
            "checkout.invalid_phone",
        ],
        "payment methods": [
            "checkout.delivery_pickup",
            "checkout.delivery_courier",
            "checkout.delivery_postal",
            "checkout.delivery_service",
            "info.berlin.payment",
            "info.other.payment",
        ],
        "information": [
            "info.title",
            "info.btn_delivery",
            "info.btn_contacts",
            "info.change_language",
            "info.change_city",
        ],
        "order statuses": [
            "admin.order_status_new",
            "admin.order_status_accepted",
            "admin.order_status_completed",
            "admin.order_status_cancelled",
            "admin.order_status_changed",
        ],
        "errors": [
            "error.generic",
            "error.database",
            "error.telegram",
            "error.network",
            "error.invalid_callback",
        ],
        "confirmations": [
            "common.confirm",
            "common.cancel",
            "checkout.confirm_prompt",
            "product.added",
            "cart.updated",
        ],
    }
    for area, keys in areas.items():
        for key in keys:
            text = uk.t(key)
            assert text != key, f"{area}: {key} missing from uk"
            assert text.strip(), f"{area}: {key} is blank in uk"
            assert text != en.t(key), f"{area}: {key} not translated (identical to en)"


def test_ukrainian_preserves_format_placeholders() -> None:
    """
    A mistranslated placeholder would silently break message rendering.

    Compared as sets, not sequences: reordering placeholders is legitimate
    translation work — ``€{amount}`` becomes ``{amount} €`` — while inventing or
    dropping one is the failure this guards against. Counts are compared too, so
    a placeholder repeated in one language and not the other still fails.
    """
    import re
    from collections import Counter

    uk, en = locale_keys("uk"), locale_keys("en")
    assert uk == en
    from app.utils.i18n import load_locale

    uk_cat, en_cat = load_locale("uk"), load_locale("en")
    pattern = re.compile(r"\{(\w+)\}")
    for key in sorted(uk_cat):
        assert Counter(pattern.findall(uk_cat[key])) == Counter(pattern.findall(en_cat[key])), key


# --------------------------------------------------------------- selecting


def test_language_keyboard_offers_ukrainian() -> None:
    markup = language_keyboard(LocalizationService.from_code("uk"))
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert payloads == [f"lang:{c}" for c in EXPECTED_LANGUAGES]
    assert "🇺🇦 Українська" in labels


def test_callback_payload_parses_to_ukrainian() -> None:
    """The /start handler turns 'lang:uk' into LanguageCode.UK."""
    raw = "lang:uk".split(":", maxsplit=1)[-1]
    assert LanguageCode(raw) is LanguageCode.UK


@pytest.mark.asyncio
async def test_selecting_ukrainian_saves_it(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=5001, language=LanguageCode.EN)
    service = UserService(session)

    await service.save_language(user, LanguageCode.UK)

    assert user.language is LanguageCode.UK
    assert UserService.is_onboarded(user) is True


# -------------------------------------------------------------- persisting


@pytest.mark.asyncio
async def test_ukrainian_persists_as_the_string_uk(session: AsyncSession) -> None:
    """Stored by value, so it survives a restart and any ORM round trip."""
    user = await make_user(session, telegram_id=5002, language=LanguageCode.EN)
    await UserService(session).save_language(user, LanguageCode.UK)
    await session.flush()

    raw = await session.scalar(select(User.language).where(User.telegram_id == 5002))
    assert raw is LanguageCode.UK
    assert LanguageCode(raw).value == "uk"

    session.expunge_all()
    reloaded = await UserRepository(session).get_by_telegram_id(5002)
    assert reloaded is not None
    assert reloaded.language is LanguageCode.UK
    assert LocalizationService.from_user(reloaded).language == "uk"


@pytest.mark.asyncio
async def test_ukrainian_survives_reload_with_city(session: AsyncSession) -> None:
    await make_user(session, telegram_id=5003, language=LanguageCode.UK)
    await session.flush()
    session.expunge_all()

    reloaded = await UserRepository(session).get_by_telegram_id(5003)
    assert reloaded is not None
    assert reloaded.language is LanguageCode.UK
    assert reloaded.selected_city is not None


# --------------------------------------------------------------- switching


@pytest.mark.asyncio
async def test_switching_to_ukrainian_changes_rendered_text(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=5004, language=LanguageCode.RU)
    before = LocalizationService.from_user(user).t("menu.cart")

    await UserService(session).save_language(user, LanguageCode.UK)
    after = LocalizationService.from_user(user).t("menu.cart")

    assert before == "🛒 Корзина"
    assert after == "🛒 Кошик"
    assert before != after


@pytest.mark.asyncio
async def test_switching_away_from_ukrainian_and_back(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=5005, language=LanguageCode.UK)
    service = UserService(session)

    uk_menu = LocalizationService.from_user(user).t("menu.info")
    await service.save_language(user, LanguageCode.DE)
    de_menu = LocalizationService.from_user(user).t("menu.info")
    await service.save_language(user, LanguageCode.UK)
    back_menu = LocalizationService.from_user(user).t("menu.info")

    assert uk_menu == back_menu == "ℹ Інформація"
    assert de_menu != uk_menu
    assert user.language is LanguageCode.UK


def test_with_language_switches_without_mutating_source() -> None:
    ru = LocalizationService.from_code("ru")
    uk = ru.with_language(LanguageCode.UK)

    assert uk.language == "uk"
    assert ru.language == "ru"
    assert uk.t("common.cancel") == "❌ Скасувати"


# ----------------------------------------------------- notifications in uk


@pytest.mark.asyncio
async def test_user_notifications_render_in_ukrainian(session: AsyncSession) -> None:
    """Outgoing user messages resolve through the stored language."""
    user = await make_user(session, telegram_id=5006, language=LanguageCode.UK)
    i18n = LocalizationService.from_user(user)

    order_confirmation = i18n.t("checkout.success", order_id=42)
    assert "Замовлення оформлено" in order_confirmation
    assert "42" in order_confirmation

    assert i18n.t("cart.updated") == "✅ Кошик оновлено."
    assert i18n.t("product.added") == "✅ Товар додано до кошика."


@pytest.mark.asyncio
async def test_error_notifications_render_in_ukrainian(session: AsyncSession) -> None:
    """The error-notification path (middleware -> notify_user_of_error)."""
    user = await make_user(session, telegram_id=5007, language=LanguageCode.UK)
    i18n = LocalizationService.from_user(user)

    for exc, key in (
        (RuntimeError("boom"), "error.generic"),
        (ConnectionError("net"), "error.network"),
    ):
        assert locale_key_for_error(exc) == key
        text = safe_user_error_text(i18n, exc)
        assert text == i18n.t(key)
        assert text != LocalizationService.from_code("en").t(key)
        assert "boom" not in text and "net" not in text


@pytest.mark.asyncio
async def test_order_status_notifications_render_in_ukrainian(
    session: AsyncSession,
) -> None:
    """Status labels a customer notification would embed."""
    user = await make_user(session, telegram_id=5008, language=LanguageCode.UK)
    i18n = LocalizationService.from_user(user)

    from app.models.enums import OrderStatus
    from app.utils.order_status import status_label

    expected = {
        OrderStatus.NEW: "🆕 Нове",
        OrderStatus.ACCEPTED: "👍 Прийнято",
        OrderStatus.COMPLETED: "✅ Завершено",
        OrderStatus.CANCELLED: "❌ Скасовано",
    }
    for status, label in expected.items():
        assert status_label(i18n, status) == label

    changed = i18n.t(
        "admin.order_status_changed", order_id=7, status=status_label(i18n, OrderStatus.ACCEPTED)
    )
    assert "Замовлення #7" in changed
    assert "Прийнято" in changed


# ------------------------------------------------------- product content


@pytest.mark.asyncio
async def test_product_content_resolves_ukrainian_columns(
    session: AsyncSession,
) -> None:
    category = await CategoryRepository(session).create_category("Liquids")
    product = await ProductRepository(session).create_product(
        category_id=category.id,
        name_ru="Манго RU",
        name_en="Mango EN",
        name_de="Mango DE",
        name_uk="Манго UA",
        description_ru="опис RU",
        description_en="desc EN",
        description_de="besch DE",
        description_uk="опис UA",
        flavor="Mango",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("12.50"),
    )
    await session.flush()

    assert localized_product_name(product, "uk") == "Манго UA"
    assert localized_product_description(product, "uk") == "опис UA"

    card = format_product_card(product, LocalizationService.from_code("uk"))
    assert "Манго UA" in card
    assert "Смак" in card and "Ціна" in card


@pytest.mark.asyncio
async def test_unknown_language_still_falls_back_to_english(
    session: AsyncSession,
) -> None:
    category = await CategoryRepository(session).create_category("Liquids")
    product = await ProductRepository(session).create_product(
        category_id=category.id,
        name_ru="RU",
        name_en="EN",
        name_de="DE",
        name_uk="UA",
        description_ru="r",
        description_en="e",
        description_de="d",
        description_uk="u",
        flavor="f",
        volume="30ml",
        nicotine_strength="3mg",
        price=Decimal("1.00"),
    )
    await session.flush()

    assert localized_product_name(product, "pl") == "EN"
    assert LocalizationService.from_code("pl").language == "en"
