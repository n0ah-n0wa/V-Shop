"""Roulette spin entitlements and the permanent record of every spin."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin
from app.models.enums import RoulettePrizeType, SpinGrantReason
from app.models.types import enum_values


class RouletteSpinGrant(Base, TimestampMixin):
    """
    One spin a customer is entitled to, and why.

    A grant is available while ``consumed_at`` is NULL. Duplicate grants are
    impossible by construction: at most one initial-promo grant per customer
    (partial unique index), one milestone grant per order, one referral grant
    per referral and recipient.
    """

    __tablename__ = "roulette_spin_grants"
    __table_args__ = (
        CheckConstraint(
            "((reason = 'purchase_milestone') = (order_id IS NOT NULL))"
            " AND ((reason = 'referral') = (referral_id IS NOT NULL))",
            name="ck_roulette_spin_grants_source_matches_reason",
        ),
        UniqueConstraint("order_id", name="uq_roulette_spin_grants_order_id"),
        UniqueConstraint(
            "referral_id",
            "user_id",
            name="uq_roulette_spin_grants_referral_id_user_id",
        ),
        # Target of the owner-checked reference from roulette_spins.
        UniqueConstraint("id", "user_id", name="uq_roulette_spin_grants_id_user_id"),
        # "How many spins does this customer have?" — leads with user_id.
        Index("ix_roulette_spin_grants_user_id_consumed_at", "user_id", "consumed_at"),
        Index(
            "ix_roulette_spin_grants_initial_promo_user_id",
            "user_id",
            unique=True,
            postgresql_where=text("reason = 'initial_promo'"),
            sqlite_where=text("reason = 'initial_promo'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[SpinGrantReason] = mapped_column(
        Enum(
            SpinGrantReason,
            name="spin_grant_reason",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    referral_id: Mapped[int | None] = mapped_column(
        ForeignKey("referrals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<RouletteSpinGrant id={self.id} user_id={self.user_id} "
            f"reason={self.reason} consumed={self.consumed_at is not None}>"
        )


class RouletteSpin(Base, TimestampMixin):
    """
    A spin that happened: which grant it used and what it awarded.

    The prize is stored as a snapshot (code, type, value), so history stays
    correct when the prize configuration changes later. What the prize turned
    into — stamps or a reward — points back here via ``spin_id``.
    """

    __tablename__ = "roulette_spins"
    __table_args__ = (
        CheckConstraint("prize_value > 0", name="ck_roulette_spins_prize_value_positive"),
        # A grant is spent exactly once, and only by the customer who holds it.
        UniqueConstraint("grant_id", name="uq_roulette_spins_grant_id"),
        ForeignKeyConstraint(
            ["grant_id", "user_id"],
            ["roulette_spin_grants.id", "roulette_spin_grants.user_id"],
            ondelete="RESTRICT",
            name="fk_roulette_spins_grant_owner",
        ),
        # Target of the owner-checked references from rewards and the ledger.
        UniqueConstraint("id", "user_id", name="uq_roulette_spins_id_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # References (id, user_id) of roulette_spin_grants — see __table_args__.
    grant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    prize_code: Mapped[str] = mapped_column(String(32), nullable=False)
    prize_type: Mapped[RoulettePrizeType] = mapped_column(
        Enum(
            RoulettePrizeType,
            name="roulette_prize_type",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    prize_value: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<RouletteSpin id={self.id} user_id={self.user_id} prize={self.prize_code}>"
