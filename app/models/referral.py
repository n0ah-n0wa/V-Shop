"""Referral relationship: who invited whom, and whether it has paid out."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database.base import Base, TimestampMixin
from app.models.enums import ReferralStatus
from app.models.types import enum_values


class Referral(Base, TimestampMixin):
    """
    Attributed when a new customer arrives through a referral link (``pending``).

    Becomes ``qualified`` at that customer's first completed paid order, which is
    when both sides are rewarded. A customer can be referred at most once, and
    the referrer can never be changed afterwards.
    """

    __tablename__ = "referrals"
    __table_args__ = (
        CheckConstraint(
            "referrer_user_id <> referred_user_id",
            name="ck_referrals_not_self",
        ),
        CheckConstraint(
            "((status = 'qualified') = (qualifying_order_id IS NOT NULL))"
            " AND ((status = 'qualified') = (qualified_at IS NOT NULL))",
            name="ck_referrals_qualified_state",
        ),
        UniqueConstraint("referred_user_id", name="uq_referrals_referred_user_id"),
        UniqueConstraint("qualifying_order_id", name="uq_referrals_qualifying_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    referrer_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    referred_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(
            ReferralStatus,
            name="referral_status",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
        default=ReferralStatus.PENDING,
        server_default=ReferralStatus.PENDING.value,
    )
    qualifying_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    qualified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # What a referral records never changes once set: who invited whom, and the
    # order that qualified it. The schema enforces one referral per customer and
    # one qualifying order; changes made in raw SQL show in loyalty_health.
    @validates("referrer_user_id", "referred_user_id", "qualifying_order_id")
    def _set_once(self, key: str, value: int | None) -> int | None:
        current = self.__dict__.get(key)
        if current is not None and value != current:
            raise ValueError(f"Referral.{key} never changes once set")
        return value

    @validates("status")
    def _never_back(self, key: str, value: ReferralStatus) -> ReferralStatus:
        if self.__dict__.get(key) == ReferralStatus.QUALIFIED and value != ReferralStatus.QUALIFIED:
            raise ValueError("A qualified referral stays qualified")
        return value

    def __repr__(self) -> str:
        return (
            f"<Referral id={self.id} referrer={self.referrer_user_id} "
            f"referred={self.referred_user_id} status={self.status}>"
        )
