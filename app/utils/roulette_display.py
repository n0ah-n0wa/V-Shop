"""Render the Lucky Roulette for Telegram.

Every figure comes from the backend: the spins available, the spin on offer,
the orders still needed for the next spin, which prizes can be won — and, after
a spin, what it won (read back from the saved spin and reward) and the stamp
card it went onto. This module only lays them out; it never draws a prize,
counts a spin or decides a reward.

Counts follow a label ("Spins left: 2") rather than being inflected, which keeps
the Russian and Ukrainian catalogs free of plural-form tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import RoulettePrizeType
from app.services.localization import LocalizationService
from app.services.stamp_card import StampCard
from app.utils.stamp_card_display import progress_bar
from app.utils.statistics_display import format_amount

# One icon per kind of prize, on the result as on the prize list: the stamp is
# the stamp card's own, and the free bottle its 🎁.
ICONS: dict[RoulettePrizeType, str] = {
    RoulettePrizeType.STAMPS: "🟢",
    RoulettePrizeType.DISCOUNT_PERCENT: "🏷",
    RoulettePrizeType.FREE_BOTTLE: "🎁",
}
# The suspense: the reels turn, then a drumroll. The same frames on every spin,
# never showing the prize — the result appears only once it is saved.
SUSPENSE = (
    ("roulette.spinning", "🍒 | 🍋 | 🔔"),
    ("roulette.drumroll", "💎 | 🍀 | 🍒"),
)
VALUE_SEPARATOR = " · "


@dataclass(frozen=True, slots=True)
class RouletteView:
    """The roulette screen, as the backend reports it."""

    available: int
    next_grant_id: int | None  # the spin the Spin button offers
    orders_to_next_spin: int | None  # None: purchases earn no spins
    prizes: tuple[tuple[RoulettePrizeType, int], ...]  # (kind, value) of each prize to win
    free_bottle_max_price: Decimal


@dataclass(frozen=True, slots=True)
class SpinResultView:
    """What one spin won, read back from its saved rows — and where things stand now."""

    prize_key: str
    kind: RoulettePrizeType
    free_bottle_max_price: Decimal | None
    remaining: int
    next_grant_id: int | None  # the next spin on offer, if any
    orders_to_next_spin: int | None
    stamp_card: StampCard  # the customer's card after the spin


def format_roulette(view: RouletteView, i18n: LocalizationService, *, currency: str) -> str:
    """
    The roulette, in the order a customer reads it on a phone.

    1. what they have — their spins, and the button to tap, or that there are
       none; then the completed orders still needed for the next spin;
    2. what they can win — one line per kind of prize, with its values.
    """
    lines = [i18n.t("roulette.title"), "", i18n.t("roulette.spins", count=view.available)]
    if view.next_grant_id is not None:
        lines.append(i18n.t("roulette.intro", button=i18n.t("roulette.spin")))
    else:
        lines.append(i18n.t("roulette.no_spins"))
    if view.orders_to_next_spin is not None:
        lines.append(i18n.t("roulette.next_spin", orders=view.orders_to_next_spin))
    lines += ["", i18n.t("roulette.prizes"), *_prize_list(view, i18n, currency)]
    return "\n".join(lines)


def _prize_list(view: RouletteView, i18n: LocalizationService, currency: str) -> list[str]:
    def values(kind: RoulettePrizeType, key: str) -> str:
        found = sorted(value for prize_kind, value in view.prizes if prize_kind == kind)
        return VALUE_SEPARATOR.join(i18n.t(key, value=value) for value in found)

    lines: list[str] = []
    stamps = values(RoulettePrizeType.STAMPS, "roulette.value_stamps")
    if stamps:
        lines.append(i18n.t("roulette.group_stamps", values=stamps))
    discounts = values(RoulettePrizeType.DISCOUNT_PERCENT, "roulette.value_percent")
    if discounts:
        lines.append(i18n.t("roulette.group_discount", values=discounts))
    if any(kind == RoulettePrizeType.FREE_BOTTLE for kind, _ in view.prizes):
        amount = format_amount(view.free_bottle_max_price, i18n, currency, trim_zero_cents=True)
        lines.append(i18n.t("roulette.group_free_bottle", amount=amount))
    return lines


def format_spin_result(result: SpinResultView, i18n: LocalizationService, *, currency: str) -> str:
    """
    The reveal, then what happens next.

    1. the headline and the prize with its icon — a free bottle, the biggest
       prize, is a jackpot;
    2. where it went: stamps onto the card, drawn as the card's own progress
       bar; a discount or a free bottle saved as a reward;
    3. the spins left — and, with none left, when the next one comes.
    """
    free_bottle = result.kind == RoulettePrizeType.FREE_BOTTLE
    lines = [
        i18n.t("roulette.title"),
        "",
        i18n.t("roulette.jackpot" if free_bottle else "roulette.won"),
        i18n.t("roulette.prize_line", icon=ICONS[result.kind], prize=i18n.t(result.prize_key)),
        *_where_it_went(result, i18n, currency),
        "",
        i18n.t("roulette.remaining", count=result.remaining),
    ]
    if result.remaining == 0 and result.orders_to_next_spin is not None:
        lines.append(i18n.t("roulette.next_spin", orders=result.orders_to_next_spin))
    return "\n".join(lines)


def _where_it_went(result: SpinResultView, i18n: LocalizationService, currency: str) -> list[str]:
    if result.kind == RoulettePrizeType.STAMPS:
        card = result.stamp_card
        lines = [
            i18n.t("roulette.reward_stamps"),
            i18n.t(
                "stamp_card.progress",
                bar=progress_bar(card.filled, card.stamps_required),
                filled=card.filled,
                required=card.stamps_required,
            ),
        ]
        if card.can_claim:
            # Named exactly as the button below and the main menu say it.
            lines.append(i18n.t("roulette.card_full", menu=i18n.t("menu.stamp_card")))
        return lines
    if result.kind == RoulettePrizeType.FREE_BOTTLE and result.free_bottle_max_price is not None:
        amount = format_amount(result.free_bottle_max_price, i18n, currency, trim_zero_cents=True)
        return [i18n.t("roulette.reward_free_bottle", amount=amount)]
    return [i18n.t("roulette.reward_saved")]


def suspense_frames(i18n: LocalizationService) -> list[str]:
    """The reels turning, then a drumroll — decoration only, identical on every spin."""
    return [i18n.t(key, reels=reels) for key, reels in SUSPENSE]
