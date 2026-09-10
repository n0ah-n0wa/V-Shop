"""Loyalty account and stamp ledger.

``loyalty_transactions`` is the source of truth for a customer's stamps.
``loyalty_accounts.stamp_balance`` caches its running total: it is written in
the same flush as every ledger row, each row records ``balance_after``, and both
are CHECKed non-negative, so any balance can be explained row by row.

Idempotency is structural rather than a convention. Every earning or spending
event points at the row that caused it — an order, a referral, a roulette spin,
a reward — and a unique constraint on that reference means the same event can
be booked at most once, however often it is replayed.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UpdatedAtMixin
from app.models.enums import LoyaltyTransactionType
from app.models.types import enum_values


class LoyaltyAccount(Base, TimestampMixin, UpdatedAtMixin):
    """One per user: cached stamp balance, purchase counter, referral code.

    Every mutation of a customer's loyalty state locks this row first
    (``SELECT … FOR UPDATE``), which serialises that customer's stamp, spin and
    reward operations inside PostgreSQL.
    """

    __tablename__ = "loyalty_accounts"
    __table_args__ = (
        CheckConstraint(
            "stamp_balance >= 0",
            name="ck_loyalty_accounts_stamp_balance_non_negative",
        ),
        CheckConstraint(
            "qualifying_purchase_count >= 0",
            name="ck_loyalty_accounts_purchase_count_non_negative",
        ),
        UniqueConstraint("referral_code", name="uq_loyalty_accounts_referral_code"),
    )
    # updated_at is set by the database on every UPDATE. Fetch it back with
    # RETURNING, or reading it afterwards needs implicit IO — which AsyncSession
    # refuses with MissingGreenlet.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
        nullable=False,
    )
    stamp_balance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # Completed paid orders booked to the ledger; drives the every-Nth-purchase spin.
    qualifying_purchase_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    # Assigned on first use, never exposed as a sequential id.
    referral_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def __repr__(self) -> str:
        return f"<LoyaltyAccount user_id={self.user_id} stamps={self.stamp_balance}>"


class LoyaltyTransaction(Base, TimestampMixin):
    """Append-only ledger row: one stamp movement and the event behind it."""

    __tablename__ = "loyalty_transactions"
    __table_args__ = (
        # Written as implications so a future kind is only bound by the last line.
        CheckConstraint(
            "(kind <> 'purchase' OR amount >= 0)"
            " AND (kind NOT IN ('referral', 'roulette') OR amount > 0)"
            " AND (kind <> 'redemption' OR amount < 0)"
            " AND (kind = 'purchase' OR amount <> 0)",
            name="ck_loyalty_transactions_amount_sign",
        ),
        # Each kind carries exactly its own source reference, and nothing else.
        CheckConstraint(
            "((kind = 'purchase') = (order_id IS NOT NULL))"
            " AND ((kind = 'referral') = (referral_id IS NOT NULL))"
            " AND ((kind = 'roulette') = (spin_id IS NOT NULL))"
            " AND ((kind = 'redemption') = (reward_id IS NOT NULL))",
            name="ck_loyalty_transactions_source_matches_kind",
        ),
        CheckConstraint(
            "kind <> 'adjustment' OR note IS NOT NULL",
            name="ck_loyalty_transactions_adjustment_has_note",
        ),
        CheckConstraint(
            "balance_after >= 0",
            name="ck_loyalty_transactions_balance_after_non_negative",
        ),
        UniqueConstraint("order_id", name="uq_loyalty_transactions_order_id"),
        UniqueConstraint(
            "referral_id",
            "user_id",
            name="uq_loyalty_transactions_referral_id_user_id",
        ),
        UniqueConstraint("spin_id", name="uq_loyalty_transactions_spin_id"),
        UniqueConstraint("reward_id", name="uq_loyalty_transactions_reward_id"),
        # The owner is part of the reference: a row can only point at a spin or
        # a reward that belongs to the same customer.
        ForeignKeyConstraint(
            ["spin_id", "user_id"],
            ["roulette_spins.id", "roulette_spins.user_id"],
            ondelete="RESTRICT",
            name="fk_loyalty_transactions_spin_owner",
        ),
        ForeignKeyConstraint(
            ["reward_id", "user_id"],
            ["user_rewards.id", "user_rewards.user_id"],
            ondelete="RESTRICT",
            name="fk_loyalty_transactions_reward_owner",
        ),
        # A customer's history, newest first by id — rows written in one
        # transaction share created_at. Leads with user_id: no separate index.
        Index("ix_loyalty_transactions_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[LoyaltyTransactionType] = mapped_column(
        Enum(
            LoyaltyTransactionType,
            name="loyalty_transaction_type",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    # Signed: stamps earned are positive, stamps redeemed negative. A purchase
    # below the threshold books 0 — the row is still the record that it counted.
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)

    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    referral_id: Mapped[int | None] = mapped_column(
        ForeignKey("referrals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Both reference (id, user_id) of their table — see __table_args__.
    spin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reward_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<LoyaltyTransaction id={self.id} user_id={self.user_id} "
            f"kind={self.kind} amount={self.amount}>"
        )
