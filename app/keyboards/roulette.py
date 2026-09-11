"""Lucky Roulette inline keyboards."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.catalog import CALLBACK_CATALOG_OPEN
from app.keyboards.stamp_card import CALLBACK_STAMP_OPEN
from app.services.localization import LocalizationService

CALLBACK_ROULETTE_OPEN = "roulette:open"
CALLBACK_ROULETTE_CLOSE = "roulette:close"
# Followed by the id of the spin the screen offered — all a tap ever sends. The
# server draws the prize, and a repeated tap replays that spin's result.
CALLBACK_ROULETTE_SPIN_PREFIX = "roulette:spin:"


def _spin_button(text: str, grant_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=f"{CALLBACK_ROULETTE_SPIN_PREFIX}{grant_id}"
    )


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def roulette_keyboard(i18n: LocalizationService, next_grant_id: int | None) -> InlineKeyboardMarkup:
    """
    A full-width Spin button only when the backend offers a spin; otherwise the
    catalog, where spins are earned. Below it, always, the way back.
    """
    first = (
        _spin_button(i18n.t("roulette.spin"), next_grant_id)
        if next_grant_id is not None
        else _button(i18n.t("menu.catalog"), CALLBACK_CATALOG_OPEN)
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[[first], [_button(i18n.t("common.back"), CALLBACK_ROULETTE_CLOSE)]]
    )


def spin_result_keyboard(
    i18n: LocalizationService, next_grant_id: int | None, *, stamps_won: bool
) -> InlineKeyboardMarkup:
    """
    After a spin, the next step first: the next spin, if there is one; the
    stamp card, when stamps were won; with no spins left, the catalog, where
    more are earned. Last, back to the roulette.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if next_grant_id is not None:
        rows.append([_spin_button(i18n.t("roulette.spin_again"), next_grant_id)])
    if stamps_won:
        rows.append([_button(i18n.t("menu.stamp_card"), CALLBACK_STAMP_OPEN)])
    back = _button(i18n.t("roulette.back"), CALLBACK_ROULETTE_OPEN)
    if next_grant_id is None:
        rows.append([_button(i18n.t("menu.catalog"), CALLBACK_CATALOG_OPEN), back])
    else:
        rows.append([back])
    return InlineKeyboardMarkup(inline_keyboard=rows)
