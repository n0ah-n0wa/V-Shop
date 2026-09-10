"""Stamp card engine — purchases in, stamps out, free bottles claimed.

Every stamp-card rule lives here. Configuration arrives through
:class:`StampCardPolicy`, built from the ``LOYALTY_*`` settings — never from a
handler.

Stamps are computed from the order row alone: its status, whether it was placed
after the programme launched, and its charged total. Nothing a customer sends —
a callback, a message — reaches that calculation, so no input can grant stamps.
The one path that awards them is :meth:`StampCardService.award_for_order`,
called by ``AdminOrderService.set_order_status`` when an order becomes
Completed, inside the same transaction as the status change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.enums import OrderStatus
from app.models.loyalty import LoyaltyTransaction
from app.models.reward import UserReward
from app.repositories.order import OrderRepository
from app.services.loyalty import LoyaltyService
from app.utils.validators import to_money

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StampCardPolicy:
    """The stamp card's rules, as configured. Validated on construction."""

    purchase_threshold: Decimal
    stamps_required: int
    free_bottle_max_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "purchase_threshold", to_money(self.purchase_threshold))
        object.__setattr__(self, "free_bottle_max_price", to_money(self.free_bottle_max_price))
        if isinstance(self.stamps_required, bool) or not isinstance(self.stamps_required, int):
            raise TypeError("stamps_required must be an integer")
        if self.stamps_required < 1:
            raise ValueError("stamps_required must be at least 1")

    @classmethod
    def from_settings(cls, settings: Settings) -> StampCardPolicy:
        return cls(
            purchase_threshold=settings.loyalty_stamp_purchase_threshold,
            stamps_required=settings.loyalty_stamps_required,
            free_bottle_max_price=settings.loyalty_free_bottle_max_price,
        )

    @classmethod
    def defaults(cls) -> StampCardPolicy:
        """The configuration defaults — one source of truth, no environment read."""
        fields = Settings.model_fields
        return cls(
            purchase_threshold=fields["loyalty_stamp_purchase_threshold"].default,
            stamps_required=fields["loyalty_stamps_required"].default,
            free_bottle_max_price=fields["loyalty_free_bottle_max_price"].default,
        )

    def stamps_for(self, charged_total: Decimal) -> int:
        """Whole thresholds only, never rounded up: €19.99 → 0, €39.99 → 1, €40 → 2."""
        if isinstance(charged_total, float | bool) or not isinstance(charged_total, Decimal | int):
            raise TypeError(f"Money must be a Decimal, not {type(charged_total).__name__}")
        amount = Decimal(charged_total)
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"{charged_total!r} is not a charged total")
        return int(amount // self.purchase_threshold)


class PurchaseAwardStatus(StrEnum):
    """What processing one order did. Not persisted."""

    AWARDED = "awarded"
    ALREADY_AWARDED = "already_awarded"
    NOT_COMPLETED = "not_completed"
    NOT_ELIGIBLE = "not_eligible"  # placed before the programme launched
    NOT_PAID = "not_paid"  # charged nothing, e.g. only a free bottle: not a purchase


@dataclass(frozen=True, slots=True)
class PurchaseAward:
    status: PurchaseAwardStatus
    order_id: int
    stamps: int = 0
    transaction: LoyaltyTransaction | None = None

    @property
    def awarded(self) -> bool:
        return self.status == PurchaseAwardStatus.AWARDED


@dataclass(frozen=True, slots=True)
class StampCard:
    """What the customer's card shows."""

    stamps: int
    stamps_required: int
    # The ledger version the card was read at; pass it back when claiming so
    # one rendered card can be claimed at most once (a double tap is refused).
    version: int = 0

    @property
    def free_bottles_unlocked(self) -> int:
        return self.stamps // self.stamps_required

    @property
    def progress(self) -> int:
        """Stamps collected towards the next free bottle."""
        return self.stamps % self.stamps_required

    @property
    def can_claim(self) -> bool:
        return self.stamps >= self.stamps_required


class StampCardService:
    def __init__(self, session: AsyncSession, policy: StampCardPolicy | None = None) -> None:
        self.session = session
        self.policy = policy or StampCardPolicy.defaults()
        self.orders = OrderRepository(session)
        self.loyalty = LoyaltyService(session)

    async def award_for_order(self, order_id: int) -> PurchaseAward:
        """
        Book the stamps a completed order earned. Safe to call any number of times.

        A Completed order placed after launch with a charged total above zero is
        a purchase: it earns ``floor(total / threshold)`` stamps — possibly 0,
        which still counts towards purchase milestones. Anything else earns
        nothing and writes nothing. The order row is locked and re-read, so the
        decision is made on its current status and total.
        """
        await self.session.flush()
        order = await self.orders.get_for_update(order_id)
        if order is None:
            raise LookupError(f"Order {order_id} does not exist")
        if order.status != OrderStatus.COMPLETED:
            return PurchaseAward(PurchaseAwardStatus.NOT_COMPLETED, order.id)
        if not order.loyalty_eligible:
            return PurchaseAward(PurchaseAwardStatus.NOT_ELIGIBLE, order.id)
        if order.total_price <= 0:
            return PurchaseAward(PurchaseAwardStatus.NOT_PAID, order.id)

        stamps = self.policy.stamps_for(order.total_price)
        posting = await self.loyalty.record_purchase(
            order.user_id,
            order_id=order.id,
            stamps=stamps,
        )
        if not posting.created:
            return PurchaseAward(
                PurchaseAwardStatus.ALREADY_AWARDED,
                order.id,
                stamps=posting.transaction.amount,
                transaction=posting.transaction,
            )
        logger.info(
            "Stamps awarded order_id=%s user_id=%s stamps=%s balance=%s",
            order.id,
            order.user_id,
            stamps,
            posting.transaction.balance_after,
        )
        return PurchaseAward(
            PurchaseAwardStatus.AWARDED,
            order.id,
            stamps=stamps,
            transaction=posting.transaction,
        )

    async def card(self, user_id: int) -> StampCard:
        """Read-only: looking at the card never creates anything."""
        return StampCard(
            stamps=await self.loyalty.balance(user_id),
            stamps_required=self.policy.stamps_required,
            version=await self.loyalty.ledger_version(user_id),
        )

    async def claim_free_bottle(
        self,
        user_id: int,
        *,
        card_version: int | None = None,
    ) -> UserReward:
        """
        Spend exactly ``stamps_required`` stamps on one free-bottle reward.

        Raises :class:`~app.services.loyalty.InsufficientStampsError` below the
        requirement, and :class:`~app.services.loyalty.StaleCardError` when
        ``card_version`` no longer matches the ledger. The price cap is
        snapshotted onto the reward, which is then redeemed at checkout by
        :class:`~app.services.reward.RewardService` — the same path a roulette
        free bottle takes.
        """
        return await self.loyalty.claim_free_bottle(
            user_id,
            stamps_required=self.policy.stamps_required,
            max_item_price=self.policy.free_bottle_max_price,
            expected_version=card_version,
        )
