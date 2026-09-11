"""Order repository."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import OrderStatus
from app.models.order import Order, OrderItem
from app.models.user import User
from app.repositories.base import BaseRepository


class OrderRepository(BaseRepository[Order]):
    model = Order

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_with_items(self, order_id: int) -> Order | None:
        result = await self.session.scalars(
            select(Order)
            .where(Order.id == order_id)
            .options(
                selectinload(Order.items).selectinload(OrderItem.product),
                selectinload(Order.user),
            )
        )
        return result.first()

    async def get_for_update(self, order_id: int) -> Order | None:
        """
        Lock an order row for the rest of the transaction and reload it.

        ``populate_existing`` matters: the lock only helps if the caller then acts
        on the row as it is now, not on whatever an earlier query left in the
        identity map (a stale screen showing Shipped for an order already
        Completed).
        """
        result = await self.session.scalars(
            select(Order)
            .where(Order.id == order_id)
            .options(
                selectinload(Order.items).selectinload(OrderItem.product),
                selectinload(Order.user),
            )
            .with_for_update(of=Order)
            .execution_options(populate_existing=True)
        )
        return result.first()

    async def has_any_for_user(self, user_id: int) -> bool:
        """Whether the customer has ever placed an order, in any status."""
        found = await self.session.scalar(select(Order.id).where(Order.user_id == user_id).limit(1))
        return found is not None

    async def list_by_status(
        self,
        status: OrderStatus | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Order]:
        stmt = (
            select(Order)
            .where(Order.status == status)
            .options(selectinload(Order.user))
            .order_by(Order.created_at.desc())
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def count_by_status(self, status: OrderStatus | str) -> int:
        result = await self.session.scalar(
            select(func.count()).select_from(Order).where(Order.status == status)
        )
        return int(result or 0)

    async def list_new(self, *, offset: int = 0, limit: int | None = None) -> list[Order]:
        return await self.list_by_status(OrderStatus.NEW, offset=offset, limit=limit)

    async def list_completed(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Order]:
        return await self.list_by_status(
            OrderStatus.COMPLETED,
            offset=offset,
            limit=limit,
        )

    async def list_by_user(
        self,
        user_id: int,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Order]:
        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .options(selectinload(Order.user))
            .order_by(Order.created_at.desc())
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_recent(
        self,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Order]:
        result = await self.session.scalars(
            select(Order)
            .options(selectinload(Order.user))
            .order_by(Order.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.all())

    async def search(self, query: str, *, limit: int = 20) -> list[Order]:
        """
        Search by order ID, customer name, or phone.

        Numeric queries prefer an exact ID match, then fall back to phone/name.
        """
        cleaned = query.strip()
        if not cleaned:
            return []

        results: list[Order] = []
        seen: set[int] = set()

        if cleaned.isdigit():
            order = await self.get_with_items(int(cleaned))
            if order is not None:
                # An exact order number is the search an admin runs most, and it
                # can only ever match one row. Falling through would add a
                # leading-wildcard ILIKE — a sequential scan of every order —
                # for nothing. A numeric query that matches no id still falls
                # through, because phone numbers are digits too.
                return [order]

        pattern = f"%{cleaned}%"
        stmt = (
            select(Order)
            .where(
                or_(
                    Order.customer_name.ilike(pattern),
                    Order.phone.ilike(pattern),
                )
            )
            .options(
                selectinload(Order.items).selectinload(OrderItem.product),
                selectinload(Order.user),
            )
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        for order in await self.session.scalars(stmt):
            if order.id not in seen:
                results.append(order)
                seen.add(order.id)
            if len(results) >= limit:
                break
        return results

    async def create_order(
        self,
        *,
        user_id: int,
        customer_name: str,
        city: str,
        delivery_type: str,
        address: str,
        total_price: Decimal | str | float,
        preferred_time: str | None = None,
        phone: str | None = None,
        status: OrderStatus = OrderStatus.NEW,
        items: list[dict[str, Any]] | None = None,
    ) -> Order:
        """
        Create an order and optional line items.

        Each item dict expects: product_id, quantity, price.
        """
        order = await self.create_and_add(
            user_id=user_id,
            customer_name=customer_name,
            city=city,
            delivery_type=delivery_type,
            address=address,
            preferred_time=preferred_time,
            phone=phone,
            total_price=Decimal(str(total_price)),
            status=status,
        )

        if items:
            for item in items:
                self.session.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=int(item["product_id"]),
                        quantity=int(item["quantity"]),
                        price=Decimal(str(item["price"])),
                    )
                )
            await self.session.flush()

        return order

    async def update_status(self, order: Order, status: OrderStatus) -> Order:
        return await self.update(order, status=status)

    async def accept(self, order: Order) -> Order:
        return await self.update_status(order, OrderStatus.ACCEPTED)

    async def ship(self, order: Order) -> Order:
        return await self.update_status(order, OrderStatus.SHIPPED)

    async def complete(self, order: Order) -> Order:
        return await self.update_status(order, OrderStatus.COMPLETED)

    async def cancel(self, order: Order) -> Order:
        return await self.update_status(order, OrderStatus.CANCELLED)

    async def search_by_id(self, order_id: int) -> Order | None:
        """Admin search alias for get_with_items."""
        return await self.get_with_items(order_id)

    async def get_with_user(self, order_id: int) -> Order | None:
        result = await self.session.scalars(
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.user).selectinload(User.cart))
        )
        return result.first()
