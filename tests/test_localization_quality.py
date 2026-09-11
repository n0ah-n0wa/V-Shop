"""
Localization quality — the conventions each catalog keeps, checked on every run.

The parity tests prove the catalogs share keys, placeholders and markup. These
prove each language reads as one voice: customers are addressed the same way
throughout, quotes and percent signs follow the language's own typography, every
key shows the same icons in every language, the loyalty features use one word
per concept, plural families are complete, amounts come from the money
formatter, and the stamp card's ordinal agrees with its noun for any configured
card size.
"""

from __future__ import annotations

import collections
import re
import unicodedata
from decimal import Decimal

import pytest

from app.services.localization import LocalizationService
from app.services.stamp_card import StampCard
from app.utils.i18n import (
    PLURAL_FORMS,
    SUPPORTED_LANGUAGES,
    feminine_accusative_ordinal,
    load_locale,
)
from app.utils.stamp_card_display import format_stamp_card
from app.utils.statistics_display import format_amount

TRANSLATIONS = [language for language in SUPPORTED_LANGUAGES if language != "en"]
PLACEHOLDER = re.compile(r"\{\w+\}")
NBSP = "\u00a0"
NNBSP = "\u202f"


def customer_facing(catalog: dict[str, str]) -> dict[str, str]:
    """Everything a customer can read — the admin panel speaks to staff."""
    return {
        key: value
        for key, value in catalog.items()
        if not key.startswith(("admin.", "language.", "format."))
    }


# The loyalty features: stamp card, roulette, referrals, and rewards at checkout.
LOYALTY_PREFIXES = (
    "menu.stamp_card",
    "menu.roulette",
    "menu.invite",
    "stamp_card.",
    "roulette.",
    "invite.",
    "checkout.ask_reward",
    "checkout.reward_",
    "checkout.summary_reward_",
    "admin.order_reward_",
)


def loyalty(catalog: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in catalog.items() if key.startswith(LOYALTY_PREFIXES)}


# ------------------------------------------------------------------ register

# The message a customer forwards to a friend is theirs, one friend to another.
FRIEND_TO_FRIEND = {"invite.share_text", "invite.share_text_plain"}
INFORMAL = {
    "de": re.compile(
        r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|deines)\b"
        r"|\b(wähle|gib|sende|tippe|teile|nutze|aktualisiere|starte|versuche|hole?|öffne"
        r"|bestätige|schreibe?|füge|drücke|klicke|komm)\b",
        re.IGNORECASE,
    ),
    "ru": re.compile(r"\b(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твою|твоего|твоей)\b", re.I),
    "uk": re.compile(r"\b(ти|тебе|тобі|тобою|твій|твоя|твоє|твої|твою|твого|твоєї)\b", re.I),
}


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_customers_are_addressed_formally_throughout(language: str) -> None:
    """
    Regression: German mixed "du" and "Sie" within one checkout.

    Onboarding and the checkout prompts said "du", the payment step, the order
    notifications and every loyalty screen "Sie". Customers now hear one voice:
    "Sie", "вы", "ви".
    """
    informal = sorted(
        key
        for key, value in customer_facing(load_locale(language)).items()
        if key not in FRIEND_TO_FRIEND and INFORMAL[language].search(value)
    )
    assert informal == [], f"{language}: informal address to a customer: {informal}"


def test_the_friend_to_friend_message_is_the_one_informal_exception() -> None:
    german = load_locale("de")
    assert all(INFORMAL["de"].search(german[key]) for key in FRIEND_TO_FRIEND), (
        "no longer informal — take it off FRIEND_TO_FRIEND"
    )


def test_german_calls_a_button_a_schaltflaeche() -> None:
    offenders = sorted(
        key for key, value in customer_facing(load_locale("de")).items() if "Button" in value
    )
    assert offenders == []


# ---------------------------------------------------------------- typography

QUOTES = {"en": ("“", "”"), "de": ("„", "“"), "ru": ("«", "»"), "uk": ("«", "»")}
ANY_QUOTE = re.compile(r"[\"“”„«»‚‘]")


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_quotes_follow_the_languages_typography(language: str) -> None:
    """English “…”, German „…“, Russian and Ukrainian «…» — balanced, never straight."""
    opening, closing = QUOTES[language]
    wrong: dict[str, str] = {}
    for key, value in load_locale(language).items():
        depth = 0
        for mark in ANY_QUOTE.findall(value):
            if mark not in (opening, closing):
                wrong[key] = value
                break
            depth += 1 if mark == opening else -1
            if depth not in (0, 1):
                wrong[key] = value
                break
        else:
            if depth:
                wrong[key] = value
    assert wrong == {}, f"{language}: quotes that are not the language's own: {wrong}"


PERCENT = re.compile(r"(?<=[\d}])(\s?)%")
# German sets a non-breaking space before "%" (5 % Rabatt); the others none.
PERCENT_SPACING = {"de": NBSP}


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_percent_signs_follow_the_languages_typography(language: str) -> None:
    """Regression: German wrote "5 % Rabatt" on the roulette and "5% Rabatt" at checkout."""
    expected = PERCENT_SPACING.get(language, "")
    wrong = sorted(
        key
        for key, value in load_locale(language).items()
        for match in PERCENT.finditer(value)
        if match.group(1) != expected
    )
    assert wrong == [], f"{language}: percent signs spaced unlike the language: {wrong}"


def icons(text: str) -> collections.Counter[str]:
    return collections.Counter(ch for ch in text if unicodedata.category(ch) == "So")


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_every_key_shows_the_same_icons_in_every_language(language: str) -> None:
    english = load_locale("en")
    catalog = load_locale(language)
    different = sorted(key for key, value in catalog.items() if icons(value) != icons(english[key]))
    assert different == [], f"{language}: icons differ from English: {different}"


# --------------------------------------------------------------- terminology

# One word per loyalty concept in each language: whenever the English names the
# concept, the translation names it with the same word.
CONCEPTS = {
    "stamp": (r"(?i)\bstamp", {"ru": r"(?i)штамп", "de": r"Stempel", "uk": r"(?i)штамп"}),
    "free bottle": (
        r"(?i)\bfree bottle",
        {"ru": r"(?i)бесплатн\w* бутыл", "de": r"Gratisflasche", "uk": r"(?i)безкоштовн\w* пляш"},
    ),
    "reward": (r"(?i)\breward", {"ru": r"(?i)наград", "de": r"Prämie", "uk": r"(?i)нагород"}),
    "spin": (
        r"(?i)\bspin",
        {"ru": r"(?i)вращени|крут", "de": r"(?i)dreh", "uk": r"(?i)обертан|крут"},
    ),
}
# The words a translator reaches for instead.
SYNONYMS = {
    "ru": r"(?i)печат|отметк|балл|бонус|купон|спин|прокрут",
    "de": (
        r"(?i)\b(belohnung\w*|punkte?|gutschein\w*|spins?"
        r"|kostenlose flasche|gratis-flasche|freiflasche)\b"
    ),
    "uk": r"(?i)печатк|бонус|купон|спін|прокрут|бутилк|бутылк|винагород",
}


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_loyalty_texts_use_one_word_per_concept(language: str) -> None:
    """Regression: Russian and Ukrainian said "подарок" where every other line said the bottle."""
    english = load_locale("en")
    missing = sorted(
        f"{key} ({concept})"
        for key, value in loyalty(load_locale(language)).items()
        for concept, (trigger, words) in CONCEPTS.items()
        if re.search(trigger, PLACEHOLDER.sub("", english[key]))
        and not re.search(words[language], PLACEHOLDER.sub("", value))
    )
    assert missing == [], f"{language}: a loyalty concept named with another word: {missing}"


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_loyalty_texts_avoid_synonyms(language: str) -> None:
    offenders = sorted(
        key
        for key, value in loyalty(load_locale(language)).items()
        if re.search(SYNONYMS[language], value)
    )
    assert offenders == [], f"{language}: a synonym instead of the loyalty term: {offenders}"


# ---------------------------------------------------------------- plurals


def plural_families(catalog: dict[str, str]) -> dict[str, dict[str, str]]:
    families: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for key, value in catalog.items():
        stem, _, form = key.rpartition(".")
        if form in PLURAL_FORMS:
            families[stem][form] = value
    return {stem: forms for stem, forms in families.items() if "one" in forms}


def test_there_are_plural_families_to_check() -> None:
    assert "invite.stamps" in plural_families(load_locale("en"))


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_every_plural_family_has_every_form(language: str) -> None:
    incomplete = {
        stem: sorted(forms)
        for stem, forms in plural_families(load_locale(language)).items()
        if set(forms) != set(PLURAL_FORMS)
    }
    assert incomplete == {}


@pytest.mark.parametrize(("language", "distinct"), [("ru", 3), ("uk", 3), ("en", 2)])
def test_plural_forms_inflect_where_the_language_does(language: str, distinct: int) -> None:
    """Russian and Ukrainian: one ≠ few ≠ many; English: one ≠ other."""
    forms = ("one", "few", "many") if distinct == 3 else ("one", "other")
    flat = sorted(
        stem
        for stem, family in plural_families(load_locale(language)).items()
        if len({family[form] for form in forms}) != distinct
    )
    assert flat == [], f"{language}: plural forms that do not inflect: {flat}"


# ---------------------------------------------------------------- ordinals


@pytest.mark.parametrize(
    ("language", "number", "written"),
    [
        ("en", 1, "1st"),
        ("en", 2, "2nd"),
        ("en", 3, "3rd"),
        ("en", 4, "4th"),
        ("en", 11, "11th"),
        ("en", 12, "12th"),
        ("en", 13, "13th"),
        ("en", 21, "21st"),
        ("en", 22, "22nd"),
        ("en", 23, "23rd"),
        ("en", 101, "101st"),
        ("en", 111, "111th"),
        ("de", 11, "11."),
        ("de", 21, "21."),
        ("ru", 3, "3-ю"),  # третью
        ("ru", 11, "11-ю"),
        ("ru", 21, "21-ю"),  # двадцать первую
        ("uk", 2, "2-гу"),  # другу
        ("uk", 3, "3-тю"),  # третю
        ("uk", 5, "5-ту"),  # п'яту
        ("uk", 7, "7-му"),  # сьому
        ("uk", 8, "8-му"),  # восьму
        ("uk", 11, "11-ту"),  # одинадцяту
        ("uk", 13, "13-ту"),  # тринадцяту
        ("uk", 20, "20-ту"),  # двадцяту
        ("uk", 21, "21-шу"),  # двадцять першу
        ("uk", 22, "22-гу"),  # двадцять другу
        ("uk", 40, "40-ву"),  # сорокову
        ("uk", 100, "100-ту"),  # соту
        ("uk", 1000, "1000-ну"),  # тисячну
    ],
)
def test_ordinals_agree_with_a_feminine_noun(language: str, number: int, written: str) -> None:
    assert feminine_accusative_ordinal(language, number) == written


@pytest.mark.parametrize(
    ("language", "promise"),
    [
        ("en", "collect stamps and get your 21st bottle free!"),
        ("de", "sammeln Sie Stempel und erhalten Sie die 21. Flasche gratis!"),
        ("ru", "собирайте штампы и получите 21-ю бутылку в подарок!"),
        ("uk", "збирайте штампи й отримайте 21-шу пляшку в подарунок!"),
    ],
)
def test_the_promise_reads_right_for_any_card_size(language: str, promise: str) -> None:
    """Regression: a 20-stamp card promised the "21th" bottle, and "21-ту пляшку"."""
    text = format_stamp_card(
        StampCard(stamps=0, stamps_required=20),
        LocalizationService(language),
        purchase_threshold=Decimal("20.00"),
        currency="€",
    )
    assert promise in text


# ------------------------------------------------------------------- money

CURRENCY = re.compile(r"[€$£₴₽]|\bEUR\b")


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_loyalty_texts_take_amounts_from_the_money_formatter(language: str) -> None:
    """A currency written into a template would ignore ``CURRENCY_SYMBOL`` and the language."""
    offenders = sorted(
        key for key, value in loyalty(load_locale(language)).items() if CURRENCY.search(value)
    )
    assert offenders == []


@pytest.mark.parametrize(
    ("language", "amount", "trim", "shown"),
    [
        ("en", Decimal("20.00"), True, "€20"),
        ("de", Decimal("20.00"), True, "20 €"),
        ("ru", Decimal("20.00"), True, "20 €"),
        ("uk", Decimal("20.00"), True, "20 €"),
        ("en", Decimal("1234.50"), False, "€1,234.50"),
        ("de", Decimal("1234.50"), False, "1.234,50 €"),
        ("ru", Decimal("1234.50"), False, f"1{NNBSP}234,50 €"),
        ("uk", Decimal("1234.50"), False, f"1{NNBSP}234,50 €"),
    ],
)
def test_amounts_read_in_the_customers_format(
    language: str, amount: Decimal, trim: bool, shown: str
) -> None:
    formatted = format_amount(amount, LocalizationService(language), "€", trim_zero_cents=trim)
    assert formatted == shown
