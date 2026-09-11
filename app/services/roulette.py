"""Roulette persistence — spin entitlements, and spending them.

Prize *selection* (weights, randomness) is decided by the caller, server-side;
this module receives the prize that was chosen. What it guarantees is the part
that must never go wrong: a grant is spent at most once, every spin is recorded
with a snapshot of its prize, and the prize's effect — stamps or a reward — is
written in the same transaction as the spin that produced it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    ReferralStatus,
    RewardSource,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyTransaction
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.repositories.order import OrderRepository
from app.repositories.referral import ReferralRepository
from app.repositories.roulette_spin import RouletteSpinRepository
from app.repositories.roulette_spin_grant import RouletteSpinGrantRepository
from app.repositories.user_reward import UserRewardRepository
from app.services.loyalty import LoyaltyError, LoyaltyService
from app.utils.validators import to_money

PRIZE_CODE_MAX_LENGTH = 32

# What each non-stamp prize becomes.
_REWARD_FOR_PRIZE = {
    RoulettePrizeType.DISCOUNT_PERCENT: RewardType.DISCOUNT_PERCENT,
    RoulettePrizeType.FREE_BOTTLE: RewardType.FREE_BOTTLE,
}


class InvalidPrizeError(LoyaltyError):
    """Raised before any write when a prize could not be persisted as given."""


@dataclass(frozen=True, slots=True)
class RoulettePrize:
    """What a spin won: a stable code plus the terms to snapshot."""

    code: str
    kind: RoulettePrizeType
    value: int

    @property
    def name_key(self) -> str:
        """Locale key of the prize's display name."""
        return prize_name_key(self.code)


def prize_name_key(code: str) -> str:
    """Locale key of a prize's display name — for a prize, or a spin's snapshot of one."""
    return f"roulette.prize.{code}"


# Every prize the roulette can award — and the only ones a spin may record.
# Codes are stable: each spin snapshots its prize, and each code names the
# prize's display text (``roulette.prize.<code>``). How often each is drawn is
# configuration (``ROULETTE_PRIZE_*_WEIGHT``, see ``app.services.roulette_engine``).
PRIZE_CATALOGUE: tuple[RoulettePrize, ...] = (
    RoulettePrize("stamp_1", RoulettePrizeType.STAMPS, 1),
    RoulettePrize("stamp_2", RoulettePrizeType.STAMPS, 2),
    RoulettePrize("discount_5", RoulettePrizeType.DISCOUNT_PERCENT, 5),
    RoulettePrize("discount_10", RoulettePrizeType.DISCOUNT_PERCENT, 10),
    RoulettePrize("free_bottle", RoulettePrizeType.FREE_BOTTLE, 1),
)


@dataclass(frozen=True, slots=True)
class SpinGrantResult:
    grant: RouletteSpinGrant
    created: bool


@dataclass(frozen=True, slots=True)
class SpinOutcome:
    spin: RouletteSpin
    transaction: LoyaltyTransaction | None  # set for stamp prizes
    reward: UserReward | None  # set for discount and free-bottle prizes
    # False: a repeated request for a grant already spent, answered with its spin.
    created: bool = True


def validate_prize(
    prize: RoulettePrize, *, free_bottle_max_price: Decimal | None
) -> RoulettePrizeType:
    """
    Reject a prize that could not be persisted exactly as given, before any write.

    Returns the prize type as the enum member. Comparing the raw value by
    identity would send a plain ``"stamps"`` string down the wrong branch.
    """
    try:
        kind = RoulettePrizeType(prize.kind)
    except ValueError as exc:
        raise InvalidPrizeError(f"Unknown prize type {prize.kind!r}") from exc
    if kind != RoulettePrizeType.STAMPS and kind not in _REWARD_FOR_PRIZE:
        raise InvalidPrizeError(f"No reward is defined for prize type {kind}")
    if not prize.code or len(prize.code) > PRIZE_CODE_MAX_LENGTH:
        raise InvalidPrizeError(f"Prize code must be 1-{PRIZE_CODE_MAX_LENGTH} characters")
    if prize.value <= 0:
        raise InvalidPrizeError("Prize value must be positive")
    if kind == RoulettePrizeType.DISCOUNT_PERCENT and prize.value > 100:
        raise InvalidPrizeError("A discount cannot exceed 100%")
    if kind == RoulettePrizeType.FREE_BOTTLE:
        if prize.value != 1:
            raise InvalidPrizeError("A free-bottle prize is exactly one bottle")
        if free_bottle_max_price is None:
            raise InvalidPrizeError("A free-bottle prize needs a price ceiling")
        try:
            to_money(free_bottle_max_price)
        except (TypeError, ValueError) as exc:
            raise InvalidPrizeError(f"Invalid free-bottle price ceiling: {exc}") from exc
    return kind


class RouletteService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.grants = RouletteSpinGrantRepository(session)
        self.spins = RouletteSpinRepository(session)
        self.rewards = UserRewardRepository(session)
        self.orders = OrderRepository(session)
        self.referrals = ReferralRepository(session)
        self.loyalty = LoyaltyService(session)

    # --- granting -----------------------------------------------------------------

    async def grant_initial_spin(self, user_id: int) -> SpinGrantResult:
        """The one-time welcome spin. A second call returns the first grant."""
        return await self._grant(
            lambda: self.grants.get_initial_for_user(user_id),
            user_id=user_id,
            reason=SpinGrantReason.INITIAL_PROMO,
        )

    async def grant_purchase_milestone_spin(
        self, user_id: int, *, order_id: int
    ) -> SpinGrantResult:
        """A spin earned by the order that reached a purchase milestone. Once per order."""
        order = await self.orders.get_by_id(order_id)
        if order is None or order.user_id != user_id:
            raise ValueError(f"Order {order_id} does not belong to user {user_id}")
        return await self._grant(
            lambda: self.grants.get_for_order(order_id),
            user_id=user_id,
            reason=SpinGrantReason.PURCHASE_MILESTONE,
            order_id=order_id,
        )

    async def grant_referral_spin(self, user_id: int, *, referral_id: int) -> SpinGrantResult:
        """A spin earned through a referral. Once per referral and recipient."""
        referral = await self.referrals.get_by_id(referral_id)
        if referral is None or user_id not in (
            referral.referrer_user_id,
            referral.referred_user_id,
        ):
            raise ValueError(f"User {user_id} is not a party to referral {referral_id}")
        if referral.status != ReferralStatus.QUALIFIED:
            raise ValueError(f"Referral {referral_id} has not qualified yet")
        return await self._grant(
            lambda: self.grants.get_for_referral(referral_id, user_id),
            user_id=user_id,
            reason=SpinGrantReason.REFERRAL,
            referral_id=referral_id,
        )

    async def available_spins(self, user_id: int) -> int:
        """Read-only: opening the roulette screen never creates spins."""
        return await self.grants.count_available(user_id)

    async def history(self, user_id: int, *, limit: int = 20) -> list[RouletteSpin]:
        return await self.spins.list_for_user(user_id, limit=limit)

    # --- spending -----------------------------------------------------------------

    async def spin(
        self,
        user_id: int,
        prize: RoulettePrize,
        *,
        free_bottle_max_price: Decimal | None = None,
        grant_id: int | None = None,
    ) -> SpinOutcome | None:
        """
        Spend a grant on ``prize``; ``None`` if there is none to spend.

        Without ``grant_id`` the oldest available grant is spent. With it, only
        that grant — the one a roulette screen offered, and only if it is this
        customer's — and when it was already spent, the spin it paid for comes
        back with ``created=False``: a repeated request replays the first
        result instead of spending another grant.

        Consumption, the spin record and the prize's effect are one unit of work:
        if any part fails, the caller's rollback returns the grant untouched.
        Only a prize in :data:`PRIZE_CATALOGUE` can be recorded, and a refused
        spin — no grant, someone else's grant, an unknown prize — writes
        nothing at all, not even the customer's loyalty account.
        """
        kind = validate_prize(prize, free_bottle_max_price=free_bottle_max_price)
        if prize not in PRIZE_CATALOGUE:
            raise InvalidPrizeError(f"{prize.code!r} is not a prize the roulette offers")
        # Everything the prize's effect needs is settled before the first write.
        max_item_price = (
            to_money(free_bottle_max_price)
            if kind == RoulettePrizeType.FREE_BOTTLE and free_bottle_max_price is not None
            else None
        )

        # Read-only checks first, so a refusal leaves nothing behind.
        if grant_id is None:
            if await self.grants.count_available(user_id) == 0:
                return None
        else:
            offered = await self.grants.get_for_user(grant_id, user_id)
            if offered is None:
                return None
            if offered.consumed_at is not None:
                return await self._replay(offered.id)

        await self.loyalty.lock_account(user_id)
        if grant_id is None:
            grant = await self.grants.first_available(user_id, for_update=True)
        else:
            grant = await self.grants.get_for_user(grant_id, user_id, for_update=True)
            if grant is not None and grant.consumed_at is not None:
                return await self._replay(grant.id)  # spent while this request waited
        if grant is None:
            return None

        grant.consumed_at = datetime.now(UTC)
        spin = await self.spins.create_and_add(
            user_id=user_id,
            grant_id=grant.id,
            prize_code=prize.code,
            prize_type=kind,
            prize_value=prize.value,
        )

        if kind == RoulettePrizeType.STAMPS:
            posting = await self.loyalty.credit_roulette(
                user_id,
                spin_id=spin.id,
                stamps=prize.value,
            )
            return SpinOutcome(spin=spin, transaction=posting.transaction, reward=None)

        reward = await self.rewards.create_and_add(
            user_id=user_id,
            kind=_REWARD_FOR_PRIZE[kind],
            value=prize.value,
            max_item_price=max_item_price,
            source=RewardSource.ROULETTE,
            status=RewardStatus.AVAILABLE,
            spin_id=spin.id,
        )
        return SpinOutcome(spin=spin, transaction=None, reward=reward)

    # --- internals ----------------------------------------------------------------

    async def _replay(self, grant_id: int) -> SpinOutcome | None:
        """The spin a spent grant paid for, and what it awarded — writing nothing."""
        spin = await self.spins.get_by_grant_id(grant_id)
        if spin is None:  # pragma: no cover - a grant is consumed only with its spin
            return None
        return SpinOutcome(
            spin=spin,
            transaction=await self.loyalty.transactions.get_for_spin(spin.id),
            reward=await self.rewards.get_for_spin(spin.id),
            created=False,
        )

    async def _grant(
        self,
        lookup: Callable[[], Awaitable[RouletteSpinGrant | None]],
        **fields: object,
    ) -> SpinGrantResult:
        """Insert a grant unless its source already produced one."""
        existing = await lookup()
        if existing is not None:
            return SpinGrantResult(existing, created=False)
        try:
            async with self.session.begin_nested():
                grant = await self.grants.create_and_add(**fields)
            return SpinGrantResult(grant, created=True)
        except IntegrityError:
            # A concurrent request inserted the same grant first; a unique
            # constraint rejected ours. Anything else is a real error.
            existing = await lookup()
            if existing is None:
                raise
            return SpinGrantResult(existing, created=False)
