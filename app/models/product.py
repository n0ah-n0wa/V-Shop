"""Product model."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UpdatedAtMixin

if TYPE_CHECKING:
    from app.models.cart import CartItem
    from app.models.category import Category, Subcategory
    from app.models.order import OrderItem


class Product(Base, TimestampMixin, UpdatedAtMixin):
    """
    Catalog product with multilingual fields.

    A product belongs to exactly one :class:`Subcategory`. ``category_id`` is the
    pre-hierarchy link, retained so existing handlers keep working until the
    catalog UI moves to the new hierarchy; a later contract migration drops it.
    ``subcategory_id`` is nullable for the same reason — it is backfilled for
    every existing row and becomes NOT NULL once product creation collects one.
    """

    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_category_id_is_active", "category_id", "is_active"),
        Index("ix_products_subcategory_id_is_active", "subcategory_id", "is_active"),
        Index("ix_products_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    subcategory_id: Mapped[int | None] = mapped_column(
        ForeignKey("subcategories.id", ondelete="RESTRICT"),
        nullable=True,
        # No single-column index: ix_products_subcategory_id_is_active leads
        # with subcategory_id and serves every lookup on it.
    )
    # Legacy direct category link (deprecated; superseded by subcategory_id).
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    name_ru: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str] = mapped_column(String(255), nullable=False)
    name_de: Mapped[str] = mapped_column(String(255), nullable=False)
    name_uk: Mapped[str] = mapped_column(String(255), nullable=False)
    description_ru: Mapped[str] = mapped_column(Text, nullable=False)
    description_en: Mapped[str] = mapped_column(Text, nullable=False)
    description_de: Mapped[str] = mapped_column(Text, nullable=False)
    description_uk: Mapped[str] = mapped_column(Text, nullable=False)
    flavor: Mapped[str] = mapped_column(String(255), nullable=False)
    volume: Mapped[str] = mapped_column(String(64), nullable=False)
    nicotine_strength: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    image_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )

    subcategory: Mapped[Subcategory | None] = relationship(back_populates="products")
    category: Mapped[Category] = relationship(back_populates="products")
    cart_items: Mapped[list[CartItem]] = relationship(back_populates="product")
    order_items: Mapped[list[OrderItem]] = relationship(back_populates="product")

    def __repr__(self) -> str:
        return f"<Product id={self.id} name_en={self.name_en!r}>"


def text_limit(field: str) -> int | None:
    """
    The most characters ``Product.<field>`` holds — ``None`` for unbounded text.

    PostgreSQL refuses a longer value outright, while SQLite, which the tests
    run on, stores it; the admin wizards check against this before saving.
    """
    length = getattr(Product.__table__.c[field].type, "length", None)
    return length if isinstance(length, int) else None
