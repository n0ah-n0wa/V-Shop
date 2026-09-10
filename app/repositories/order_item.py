"""OrderItem repository."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import OrderItem
from app.repositories.base import BaseRepository


class OrderItemRepository(BaseRepository[OrderItem]):
    model = OrderItem

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def list_by_order(
        self,
        order_id: int,
        *,
        with_product: bool = True,
    ) -> list[OrderItem]:
        stmt = select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id.asc())
        if with_product:
            stmt = stmt.options(selectinload(OrderItem.product))
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def has_unit_at_price(self, order_id: int, product_id: int, price: Decimal) -> bool:
        """Whether the order holds ``product_id`` at exactly ``price`` — e.g. a free unit."""
        found = await self.session.scalar(
            select(OrderItem.id)
            .where(
                OrderItem.order_id == order_id,
                OrderItem.product_id == product_id,
                OrderItem.price == price,
            )
            .limit(1)
        )
        return found is not None

    async def add_item(
        self,
        *,
        order_id: int,
        product_id: int,
        quantity: int,
        price: Decimal | str | float,
    ) -> OrderItem:
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        return await self.create_and_add(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            price=Decimal(str(price)),
        )

    async def add_items(
        self,
        order_id: int,
        items: Sequence[tuple[int, int, Decimal | str | float]],
    ) -> list[OrderItem]:
        """
        Bulk-add line items.

        Each tuple is (product_id, quantity, price).
        """
        entities = [
            self.create(
                order_id=order_id,
                product_id=product_id,
                quantity=quantity,
                price=Decimal(str(price)),
            )
            for product_id, quantity, price in items
        ]
        return await self.add_all(entities)

    async def update_quantity(self, item: OrderItem, quantity: int) -> OrderItem:
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        return await self.update(item, quantity=quantity)

    async def update_price(
        self,
        item: OrderItem,
        price: Decimal | str | float,
    ) -> OrderItem:
        return await self.update(item, price=Decimal(str(price)))
