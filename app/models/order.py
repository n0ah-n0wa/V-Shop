"""Order and OrderItem models."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import OrderStatus, PaymentMethod
from app.models.types import enum_values

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.reward import UserReward
    from app.models.user import User


class Order(Base, TimestampMixin):
    """Customer order placed through checkout."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_price >= 0", name="ck_orders_total_price_non_negative"),
        Index("ix_orders_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_type: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_method: Mapped[PaymentMethod | None] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=True,
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
        default=OrderStatus.NEW,
        server_default=OrderStatus.NEW.value,
        # No single-column index: ix_orders_status_created_at leads with status
        # and serves every lookup on it, including count(*) as an index-only scan.
    )
    # Set at placement: every order the application creates can earn loyalty
    # stamps. The server default is false so orders that already existed when
    # the programme launched — including ones still in progress — never do
    # (owner decision; migration 8e4c1a7b2d95).
    loyalty_eligible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=false(),
    )

    user: Mapped[User] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    # The reward redeemed on this order, if any (user_rewards.order_id is unique).
    # Read-only here — only RewardService binds a reward to an order — and loaded
    # with every order, so staff screens show it without another query.
    reward: Mapped[UserReward | None] = relationship(viewonly=True, lazy="selectin")

    def __repr__(self) -> str:
        return f"<Order id={self.id} status={self.status} total={self.total_price}>"


class OrderItem(Base):
    """Snapshot of a product line in an order."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("price >= 0", name="ck_order_items_price_non_negative"),
        # Serves the popularity query: GROUP BY product_id with
        # COUNT(DISTINCT order_id) is answered from this index alone.
        Index("ix_order_items_product_id_order_id", "product_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        # No single-column index: ix_order_items_product_id_order_id below leads
        # with product_id and serves every lookup on it.
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="order_items")

    def __repr__(self) -> str:
        return (
            f"<OrderItem id={self.id} order_id={self.order_id} "
            f"product_id={self.product_id} qty={self.quantity}>"
        )
