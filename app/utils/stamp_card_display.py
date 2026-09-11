"""Render the customer's stamp card for Telegram.

Every figure comes from :class:`~app.services.stamp_card.StampCard`: the balance,
what the current card shows, how many stamps are still needed, whether a bottle
can be claimed and how many are waiting. This module only lays them out — it
never counts stamps or decides a reward.

Counts follow a label ("Stamps to go: 8") rather than being inflected
("8 stamps"), which keeps the Russian and Ukrainian catalogs free of plural-form
tables.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.localization import LocalizationService
from app.services.stamp_card import StampCard
from app.utils.i18n import feminine_accusative_ordinal
from app.utils.statistics_display import format_amount

STAMP = "🟢"
EMPTY_SLOT = "⚪️"
# Ten slots fit one line on a narrow phone. A longer configured card is drawn
# to scale; the exact figures are printed next to it.
MAX_SLOTS = 10


def progress_bar(filled: int, total: int, *, max_slots: int = MAX_SLOTS) -> str:
    """``filled`` of ``total`` as stamp slots; full only when the card is full."""
    if total <= 0:
        return ""
    filled = max(0, min(filled, total))
    if total <= max_slots:
        return STAMP * filled + EMPTY_SLOT * (total - filled)
    lit = filled * max_slots // total
    return STAMP * lit + EMPTY_SLOT * (max_slots - lit)


def format_stamp_card(
    card: StampCard,
    i18n: LocalizationService,
    *,
    purchase_threshold: Decimal,
    currency: str,
) -> str:
    """
    The card, in the order a customer reads it on a phone.

    1. where they are — the progress bar and ``filled/required``;
    2. what they get — the promo, directly beneath the bar;
    3. what to do next — stamps still needed, or a full card and the exact
       button to tap; then extra stamps and bottles already saved;
    4. how stamps are earned — one quiet line at the bottom.
    """
    amount = format_amount(purchase_threshold, i18n, currency, trim_zero_cents=True)
    lines = [
        i18n.t("stamp_card.title"),
        "",
        i18n.t(
            "stamp_card.progress",
            bar=progress_bar(card.filled, card.stamps_required),
            filled=card.filled,
            required=card.stamps_required,
        ),
        # The owner's promise names the free bottle by its ordinal — "11th",
        # "11.", "11-ю", "11-ту" — which must agree for any configured card size.
        i18n.t(
            "stamp_card.promo",
            amount=amount,
            next=feminine_accusative_ordinal(i18n.language, card.free_bottle_number),
        ),
        "",
    ]
    if card.can_claim:
        lines.append(i18n.t("stamp_card.ready"))
        lines.append(i18n.t("stamp_card.ready_hint", button=i18n.t("stamp_card.claim")))
        if card.extra_stamps:
            lines.append(i18n.t("stamp_card.extra", extra=card.extra_stamps))
    else:
        lines.append(i18n.t("stamp_card.remaining", remaining=card.remaining))
        if card.stamps == 0:
            lines.append(i18n.t("stamp_card.empty"))
    if card.free_bottles_waiting:
        lines += ["", i18n.t("stamp_card.waiting", count=card.free_bottles_waiting)]
    lines += ["", i18n.t("stamp_card.how", amount=amount)]
    return "\n".join(lines)
