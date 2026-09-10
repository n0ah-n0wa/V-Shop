"""Redeemable customer rewards: discounts and free bottles."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin
from app.models.enums import RewardSource, RewardStatus, RewardType
from app.models.types import enum_values


class UserReward(Base, TimestampMixin):
    """
    A reward the customer holds until they choose to use it at checkout.

    Rewards do not expire. Using one binds it to the order; the binding is
    unique, so an order carries at most one reward, and it is never undone —
    a reward used on an order that is later cancelled stays used.

    The terms are snapshotted when the reward is issued (``value``, and
    ``max_item_price`` for a free bottle), so changing the shop's settings later
    never changes a promise already made.
    """

    __tablename__ = "user_rewards"
    __table_args__ = (
        CheckConstraint(
            "value > 0"
            " AND (kind <> 'discount_percent'"
            " OR (value <= 100 AND max_item_price IS NULL))"
            " AND (kind <> 'free_bottle'"
            " OR (value = 1 AND max_item_price IS NOT NULL AND max_item_price > 0))",
            name="ck_user_rewards_terms",
        ),
        CheckConstraint(
            "((status = 'used') = (order_id IS NOT NULL))"
            " AND ((status = 'used') = (used_at IS NOT NULL))",
            name="ck_user_rewards_used_state",
        ),
        CheckConstraint(
            "(source = 'roulette') = (spin_id IS NOT NULL)",
            name="ck_user_rewards_source_matches_spin",
        ),
        UniqueConstraint("spin_id", name="uq_user_rewards_spin_id"),
        UniqueConstraint("order_id", name="uq_user_rewards_order_id"),
        # A roulette reward belongs to the customer who spun.
        ForeignKeyConstraint(
            ["spin_id", "user_id"],
            ["roulette_spins.id", "roulette_spins.user_id"],
            ondelete="RESTRICT",
            name="fk_user_rewards_spin_owner",
        ),
        # Target of the owner-checked reference from the ledger.
        UniqueConstraint("id", "user_id", name="uq_user_rewards_id_user_id"),
        # "Which rewards can this customer use?" — leads with user_id.
        Index("ix_user_rewards_user_id_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[RewardType] = mapped_column(
        Enum(
            RewardType,
            name="reward_type",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    # Percent for a discount; number of bottles (always 1) for a free bottle.
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    # Free bottle only: the most expensive product it may be applied to.
    max_item_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    source: Mapped[RewardSource] = mapped_column(
        Enum(
            RewardSource,
            name="reward_source",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    status: Mapped[RewardStatus] = mapped_column(
        Enum(
            RewardStatus,
            name="reward_status",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
        default=RewardStatus.AVAILABLE,
        server_default=RewardStatus.AVAILABLE.value,
    )
    # References (id, user_id) of roulette_spins — see __table_args__.
    spin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<UserReward id={self.id} user_id={self.user_id} kind={self.kind} "
            f"value={self.value} status={self.status}>"
        )
