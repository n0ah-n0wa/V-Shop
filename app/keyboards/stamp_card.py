"""Stamp card inline keyboard."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.catalog import CALLBACK_CATALOG_OPEN
from app.services.localization import LocalizationService
from app.services.stamp_card import StampCard

CALLBACK_STAMP_OPEN = "stamp:open"
# Followed by the card's version (its latest ledger id): a claim is honoured
# only for the card the customer was looking at, never for a stale one.
CALLBACK_STAMP_CLAIM_PREFIX = "stamp:claim:"


def stamp_card_keyboard(i18n: LocalizationService, card: StampCard) -> InlineKeyboardMarkup:
    """
    A full-width claim button only when the backend says a bottle can be claimed.

    Below it, always, the next step — the catalog, where stamps are earned, with
    the same label as the main menu — and a refresh for after an order completes.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if card.can_claim:
        rows.append(
            [
                InlineKeyboardButton(
                    text=i18n.t("stamp_card.claim"),
                    callback_data=f"{CALLBACK_STAMP_CLAIM_PREFIX}{card.version}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=i18n.t("menu.catalog"),
                callback_data=CALLBACK_CATALOG_OPEN,
            ),
            InlineKeyboardButton(
                text=i18n.t("stamp_card.refresh"),
                callback_data=CALLBACK_STAMP_OPEN,
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
