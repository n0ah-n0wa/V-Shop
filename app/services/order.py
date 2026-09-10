"""Order service — place orders from the cart and persist to PostgreSQL."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CityChoice, DeliveryType, OrderStatus, PaymentMethod
from app.models.order import Order
from app.models.user import User
from app.repositories.cart import CartRepository
from app.repositories.cart_item import CartItemRepository
from app.repositories.order import OrderRepository
from app.repositories.order_item import OrderItemRepository
from app.repositories.product import ProductRepository
from app.services.reward import FreeBottlePlan, RewardService

logger = logging.getLogger(__name__)

_BERLIN_DELIVERY = frozenset({DeliveryType.PICKUP.value, DeliveryType.COURIER.value})
_OTHER_DELIVERY = frozenset({DeliveryType.POSTAL.value, DeliveryType.SERVICE.value})


class EmptyCartError(ValueError):
    """Raised when checkout is attempted with an empty cart."""


class InactiveProductError(ValueError):
    """Raised when the cart contains a product that is no longer sellable."""


class InvalidDeliveryError(ValueError):
    """Raised when delivery type is not allowed for the user's city."""


def delivery_allowed_for_city(city: str | CityChoice, delivery_type: str) -> bool:
    city_value = city.value if isinstance(city, CityChoice) else str(city)
    if city_value == CityChoice.BERLIN.value:
        return delivery_type in _BERLIN_DELIVERY
    if city_value == CityChoice.DELIVERY.value:
        return delivery_type in _OTHER_DELIVERY
    return False


class OrderService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.order_items = OrderItemRepository(session)
        self.carts = CartRepository(session)
        self.cart_items = CartItemRepository(session)
        self.products = ProductRepository(session)

    async def place_order_from_cart(
        self,
        user: User,
        *,
        customer_name: str,
        delivery_type: str,
        address: str,
        preferred_time: str,
        phone: str | None,
        payment_method: PaymentMethod | None = None,
        reward_id: int | None = None,
    ) -> Order:
        """
        Persist a completed checkout:

        1. Create ``orders`` row
        2. Create ``order_items`` rows from the cart
        3. Clear ``cart_items``
        4. Flush + commit to PostgreSQL
        5. Return the saved order (with items loaded)

        ``reward_id`` redeems one of the customer's free-bottle rewards on this
        order: one eligible unit is charged €0 and the reward is bound to the
        order in the same transaction. A reward that is unavailable, not the
        customer's, or not applicable to the cart is refused before anything is
        written — see :mod:`app.services.reward`.
        """
        if user.selected_city is None:
            raise ValueError("User city is not set")

        if not delivery_allowed_for_city(user.selected_city, delivery_type):
            raise InvalidDeliveryError(
                f"Delivery {delivery_type!r} is not allowed for city {user.selected_city!r}"
            )

        cart = await self.carts.get_by_user_id_with_items(user.id, for_update=True)
        if cart is None or not cart.items:
            raise EmptyCartError("Cart is empty")

        # One query decides sellability for the whole cart: a product hidden by
        # its brand or category must not be sold, only browsed-away.
        unsellable = await self.products.list_unsellable_ids(
            [item.product_id for item in cart.items]
        )

        line_items: list[tuple[int, int, Decimal]] = []
        total = Decimal("0")
        for item in cart.items:
            product = item.product
            if product is None:
                continue
            if product.id in unsellable:
                raise InactiveProductError(
                    f"Product {product.id} is not available and cannot be ordered"
                )
            unit_price = Decimal(product.price)
            total += unit_price * item.quantity
            line_items.append((product.id, item.quantity, unit_price))

        if not line_items:
            raise EmptyCartError("Cart is empty")

        rewards = RewardService(self.session)
        plan: FreeBottlePlan | None = None
        if reward_id is not None:
            plan = await rewards.plan_free_bottle(reward_id, user_id=user.id, lines=line_items)
            line_items = plan.apply(line_items)
            total -= plan.discount

        city_value = (
            user.selected_city.value
            if hasattr(user.selected_city, "value")
            else str(user.selected_city)
        )

        order = await self.orders.create_and_add(
            user_id=user.id,
            customer_name=customer_name.strip(),
            city=city_value,
            delivery_type=delivery_type,
            address=address.strip(),
            preferred_time=preferred_time.strip(),
            phone=phone,
            payment_method=payment_method,
            total_price=total,
            status=OrderStatus.NEW,
        )

        await self.order_items.add_items(order.id, line_items)
        if plan is not None:
            await rewards.redeem(plan, user_id=user.id, order_id=order.id)
        await self.carts.clear(cart)
        await self.session.commit()

        saved = await self.orders.get_with_items(order.id)
        if saved is None:
            raise RuntimeError(f"Order {order.id} was not found after commit")

        logger.info(
            "Order persisted order_id=%s user_id=%s items=%s total=%s",
            saved.id,
            user.id,
            len(saved.items),
            saved.total_price,
        )
        return saved
