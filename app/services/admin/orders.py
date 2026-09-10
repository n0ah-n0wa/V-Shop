"""Admin order operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus
from app.models.order import Order
from app.repositories.order import OrderRepository
from app.repositories.order_item import OrderItemRepository
from app.services.admin.exceptions import InvalidStatusTransitionError
from app.services.stamp_card import StampCardPolicy, StampCardService
from app.utils.order_status import allowed_transitions, can_transition


class AdminOrderService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        stamp_policy: StampCardPolicy | None = None,
    ) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.order_items = OrderItemRepository(session)
        self.stamp_card = StampCardService(session, stamp_policy)

    async def list_orders_by_status(
        self,
        status: OrderStatus,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[Order]:
        return await self.orders.list_by_status(status, offset=offset, limit=limit)

    async def count_orders_by_status(self, status: OrderStatus) -> int:
        return await self.orders.count_by_status(status)

    async def page_orders_by_status(
        self,
        status: OrderStatus,
        *,
        offset: int = 0,
        limit: int,
    ) -> tuple[int, list[Order]]:
        total = await self.count_orders_by_status(status)
        items = await self.list_orders_by_status(status, offset=offset, limit=limit)
        return total, items

    async def get_order(self, order_id: int) -> Order | None:
        return await self.orders.get_with_items(order_id)

    async def search_orders(self, query: str, *, limit: int = 20) -> list[Order]:
        return await self.orders.search(query, limit=limit)

    async def set_order_status(self, order: Order, status: OrderStatus) -> Order:
        """
        Move an order to ``status``, refusing transitions the lifecycle forbids.

        The keyboard only offers legal moves, but a stale message or a crafted
        callback could still ask for an illegal one, so the rule is enforced
        here rather than in the UI. The order row is locked and re-read first:
        two admins acting at once are applied one after the other against the
        real current status, so a stale screen cannot cancel an order someone
        has just completed.

        Completing an order books its loyalty stamps in the same transaction —
        the status and the stamps become durable together or not at all.
        """
        await self.session.flush()
        current = await self.orders.get_for_update(order.id)
        if current is None:
            raise LookupError(f"Order {order.id} does not exist")
        if current.status == status:
            return current
        if not can_transition(current.status, status):
            raise InvalidStatusTransitionError(current.status, status)
        updated = await self.orders.update_status(current, status)
        if status == OrderStatus.COMPLETED:
            await self.stamp_card.award_for_order(updated.id)
        return updated

    @staticmethod
    def allowed_next_statuses(order: Order) -> tuple[OrderStatus, ...]:
        """Statuses this order may move to, in lifecycle order."""
        return allowed_transitions(order.status)
