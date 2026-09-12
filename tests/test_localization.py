"""Localization / i18n tests."""

from __future__ import annotations

import string

import pytest

from app.models.enums import LanguageCode
from app.services.localization import LocalizationService
from app.utils.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    assert_locales_in_sync,
    clear_locale_cache,
    flatten_locale,
    load_locale,
    locale_keys,
    normalize_language,
    translate,
    translations_for,
)


@pytest.fixture(autouse=True)
def _clear_locale_cache() -> None:
    clear_locale_cache()
    yield
    clear_locale_cache()


def test_locales_are_in_sync() -> None:
    assert_locales_in_sync()


def test_supported_languages_match_enum() -> None:
    assert set(SUPPORTED_LANGUAGES) == {code.value for code in LanguageCode}


def test_flatten_locale_nested_keys() -> None:
    flat = flatten_locale({"menu": {"catalog": "Catalog", "cart": "Cart"}, "ok": "OK"})
    assert flat == {
        "menu.catalog": "Catalog",
        "menu.cart": "Cart",
        "ok": "OK",
    }


def test_normalize_language_fallbacks() -> None:
    assert normalize_language(None) == DEFAULT_LANGUAGE
    assert normalize_language("RU") == "ru"
    assert normalize_language(LanguageCode.DE) == "de"
    assert normalize_language("xx") == DEFAULT_LANGUAGE


def test_translate_known_key_all_languages() -> None:
    for code in SUPPORTED_LANGUAGES:
        text = translate("menu.catalog", code)
        assert text
        assert text != "menu.catalog"


def test_translate_missing_key_returns_key() -> None:
    assert translate("does.not.exist", "en") == "does.not.exist"


def test_translate_format_kwargs() -> None:
    # Uses a key that exists with placeholders in locales.
    keys = locale_keys("en")
    assert "checkout.success" in keys
    text = translate("checkout.success", "en", order_id=42)
    assert "42" in text


def test_translations_for_returns_variants() -> None:
    values = translations_for("menu.catalog")
    assert len(values) >= 1
    assert all(isinstance(v, str) and v for v in values)


def test_localization_service_from_code_and_default() -> None:
    default = LocalizationService.default()
    assert default.language == DEFAULT_LANGUAGE

    ru = LocalizationService.from_code(LanguageCode.RU)
    assert ru.language == "ru"
    assert ru.t("menu.catalog") != default.t("menu.catalog")


def test_localization_service_with_language() -> None:
    en = LocalizationService("en")
    de = en.with_language("de")
    assert de.language == "de"
    assert en.language == "en"


def test_localization_service_n_joins_parts() -> None:
    i18n = LocalizationService("en")
    assert i18n.n("menu", "catalog") == i18n.t("menu.catalog")


def test_load_locale_contains_core_keys() -> None:
    catalog = load_locale("en")
    for key in ("menu.catalog", "menu.cart", "menu.info", "admin.access_denied"):
        assert key in catalog


@pytest.mark.parametrize("code", SUPPORTED_LANGUAGES)
def test_every_text_renders_with_its_placeholders_filled(code: str) -> None:
    """
    Every template, filled in as a call site fills it, renders — in every language.

    ``translate`` hands back the raw template when formatting fails, so a broken
    one shows its braces; and a placeholder named like a parameter of ``t`` —
    ``{language}`` was — raised instead, failing the screen for every admin.
    """
    i18n = LocalizationService(code)
    broken = []
    for key, template in load_locale(code).items():
        fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
        values = {name: f"<{name}>" for name in fields}
        rendered = i18n.t(key, **values)
        if not all(value in rendered for value in values.values()):
            broken.append(key)
    assert broken == []


@pytest.mark.parametrize("code", SUPPORTED_LANGUAGES)
def test_no_text_carries_an_escaped_newline(code: str) -> None:
    """A doubled backslash in the JSON reaches the chat as a literal backslash-n."""
    offenders = sorted(key for key, text in load_locale(code).items() if "\\n" in text)
    assert offenders == []
