"""Roulette prize engine — the server draws the prize; nothing a client sends decides it.

:data:`~app.services.roulette.PRIZE_CATALOGUE` defines what can be won. The
``ROULETTE_PRIZE_*_WEIGHT`` settings decide how often (a weight of 0 takes a
prize out). :meth:`RouletteEngine.spin` draws with the operating system's CSPRNG
(:func:`secrets.randbelow`) and hands the drawn prize to
:meth:`RouletteService.spin`, which spends one grant, records the spin and
applies the prize — stamps or a redeemable reward — in the caller's single
transaction.

A client only asks to spend the grant its screen offered
(:meth:`RouletteEngine.next_grant_id`). Naming it makes the request idempotent:
a double tap, or the same Telegram update processed again after a restart,
replays the first result instead of spending a second spin. A client never
supplies a prize, a reward type, a value or a balance.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.roulette import RouletteSpin
from app.services.roulette import (
    PRIZE_CATALOGUE,
    InvalidPrizeError,
    RoulettePrize,
    RouletteService,
    SpinOutcome,
    validate_prize,
)
from app.utils.validators import to_money

logger = logging.getLogger(__name__)

# The setting that weighs each prize.
WEIGHT_SETTINGS: dict[str, str] = {
    "stamp_1": "roulette_prize_stamp_1_weight",
    "stamp_2": "roulette_prize_stamp_2_weight",
    "discount_5": "roulette_prize_discount_5_weight",
    "discount_10": "roulette_prize_discount_10_weight",
    "free_bottle": "roulette_prize_free_bottle_weight",
}

# ``randbelow(n)`` returns a whole number in ``[0, n)``. Production uses
# :func:`secrets.randbelow`; tests pass a function that lands on a known ticket.
Randbelow = Callable[[int], int]


@dataclass(frozen=True, slots=True)
class WeightedPrize:
    prize: RoulettePrize
    weight: int


@dataclass(frozen=True, slots=True)
class PrizeTable:
    """
    The prizes and their weights; a prize's chance is its weight over the total.

    Weights are whole numbers, so a draw is one integer ticket in
    ``[0, total)`` and every chance is exact — no floating point involved.
    """

    entries: tuple[WeightedPrize, ...]

    def __post_init__(self) -> None:
        codes = [entry.prize.code for entry in self.entries]
        if len(set(codes)) != len(codes):
            raise ValueError(f"Prize codes must be unique: {codes}")
        for entry in self.entries:
            weight = entry.weight
            if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
                raise ValueError(f"The weight of {entry.prize.code!r} must be a whole number ≥ 0")
        if self.total_weight <= 0:
            raise ValueError("At least one prize needs a weight above 0")

    @property
    def total_weight(self) -> int:
        return sum(entry.weight for entry in self.entries)

    def chance(self, code: str) -> Fraction:
        """The exact probability of drawing ``code``."""
        for entry in self.entries:
            if entry.prize.code == code:
                return Fraction(entry.weight, self.total_weight)
        raise KeyError(code)

    def draw(self, randbelow: Randbelow = secrets.randbelow) -> RoulettePrize:
        """One prize, chosen server-side: each with probability weight / total."""
        total = self.total_weight
        ticket = randbelow(total)
        if isinstance(ticket, bool) or not isinstance(ticket, int) or not 0 <= ticket < total:
            raise ValueError(f"A draw must land on a ticket in [0, {total}), not {ticket!r}")
        for entry in self.entries:
            if ticket < entry.weight:
                return entry.prize
            ticket -= entry.weight
        raise AssertionError("unreachable: the tickets cover the whole table")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class RoulettePolicy:
    """The roulette as configured: its prize table, and the free bottle's price cap."""

    table: PrizeTable
    free_bottle_max_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "free_bottle_max_price", to_money(self.free_bottle_max_price))
        for entry in self.table.entries:
            # Every prize must be one RouletteService can persist exactly as given.
            validate_prize(entry.prize, free_bottle_max_price=self.free_bottle_max_price)
            if entry.prize not in PRIZE_CATALOGUE:
                raise InvalidPrizeError(f"{entry.prize.code!r} is not in the prize catalogue")

    @classmethod
    def from_settings(cls, settings: Settings) -> RoulettePolicy:
        return cls._build(
            {code: getattr(settings, name) for code, name in WEIGHT_SETTINGS.items()},
            settings.loyalty_free_bottle_max_price,
        )

    @classmethod
    def defaults(cls) -> RoulettePolicy:
        """The configuration defaults — one source of truth, no environment read."""
        fields = Settings.model_fields
        return cls._build(
            {code: fields[name].default for code, name in WEIGHT_SETTINGS.items()},
            fields["loyalty_free_bottle_max_price"].default,
        )

    @classmethod
    def _build(cls, weights: dict[str, int], ceiling: Decimal) -> RoulettePolicy:
        table = PrizeTable(
            tuple(WeightedPrize(prize, weights[prize.code]) for prize in PRIZE_CATALOGUE)
        )
        return cls(table=table, free_bottle_max_price=ceiling)


class RouletteEngine:
    def __init__(
        self,
        session: AsyncSession,
        policy: RoulettePolicy,
        *,
        randbelow: Randbelow = secrets.randbelow,
    ) -> None:
        """
        ``policy`` is required, never defaulted: a roulette built without it
        would draw under the default odds and silently ignore the configuration.
        """
        self.session = session
        self.policy = policy
        self.roulette = RouletteService(session)
        self._randbelow = randbelow

    async def next_grant_id(self, user_id: int) -> int | None:
        """The spin a roulette screen offers: the customer's oldest unspent grant."""
        grant = await self.roulette.grants.first_available(user_id)
        return grant.id if grant is not None else None

    async def spin(self, user_id: int, *, grant_id: int) -> SpinOutcome | None:
        """
        Spend the grant the roulette screen offered on a prize the server draws.

        ``grant_id`` is required: it makes every request idempotent. A repeated
        request — a double tap, or the same update processed again after a
        restart — finds the grant spent and gets its first result back
        (``created=False``); the new draw is discarded. A grant that is not
        this customer's, or does not exist, spends nothing (``None``).

        Consuming the grant, recording the spin and creating its stamps or
        reward happen in the caller's one transaction, which must commit before
        the customer is told: a failure rolls every part back and the spin
        stays available.
        """
        prize = self.policy.table.draw(self._randbelow)
        outcome = await self.roulette.spin(
            user_id,
            prize,
            free_bottle_max_price=self.policy.free_bottle_max_price,
            grant_id=grant_id,
        )
        if outcome is not None and outcome.created:
            logger.info(
                "Roulette spin user_id=%s grant_id=%s spin_id=%s prize=%s",
                user_id,
                outcome.spin.grant_id,
                outcome.spin.id,
                outcome.spin.prize_code,
            )
        return outcome

    async def available_spins(self, user_id: int) -> int:
        """Read-only: opening the roulette never creates a spin."""
        return await self.roulette.available_spins(user_id)

    async def history(self, user_id: int, *, limit: int = 20) -> list[RouletteSpin]:
        """The customer's spins, newest first — each with its prize snapshot."""
        return await self.roulette.history(user_id, limit=limit)
