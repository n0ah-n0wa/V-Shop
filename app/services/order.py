"""Order service — place orders from the cart and persist to PostgreSQL."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cart import Cart
from app.models.enums import CityChoice, DeliveryType, OrderStatus, PaymentMethod
from app.models.order import Order
from app.models.user import User
from app.repositories.cart import CartRepository
from app.repositories.cart_item import CartItemRepository
from app.repositories.order import OrderRepository
from app.repositories.order_item import OrderItemRepository
from app.repositories.product import ProductRepository
from app.services.loyalty import LoyaltyService
from app.services.reward import Line, RewardOption, RewardPlan, RewardService

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


@dataclass(frozen=True, slots=True)
class CheckoutQuote:
    """
    What confirming checkout will charge — computed as placing the order computes it.

    ``reward`` is the reward the customer chose, while it still applies to the
    cart; the total then has its saving taken off.
    """

    subtotal: Decimal
    reward: RewardOption | None = None

    @property
    def total(self) -> Decimal:
        return self.subtotal - self.reward.saving if self.reward is not None else self.subtotal


def _lines_total(lines: Sequence[Line]) -> Decimal:
    return sum((price * quantity for _, quantity, price in lines), Decimal("0"))


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

        ``reward_id`` redeems one of the customer's rewards on this order — a
        free bottle charges one eligible unit €0, a percentage discount lowers
        the total — and binds it to the order in the same transaction. A reward
        that is unavailable, not the
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
        # The customer's loyalty lock, which a referral attribution takes too:
        # this order and a /start through a friend's link are decided one after
        # the other, never both on the view that the customer has not ordered.
        await LoyaltyService(self.session).lock_account(user.id)

        line_items = await self._priced_lines(cart)
        total = _lines_total(line_items)

        rewards = RewardService(self.session)
        plan: RewardPlan | None = None
        if reward_id is not None:
            plan = await rewards.plan(reward_id, user_id=user.id, lines=line_items)
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

    async def reward_options(self, user: User) -> list[RewardOption]:
        """
        Read-only: the customer's rewards that could be used on the cart as it stands.

        Empty when the cart cannot be ordered — confirming then says why.
        """
        lines = await self._orderable_lines(user.id)
        if lines is None:
            return []
        return await RewardService(self.session).options(user.id, lines=lines)

    async def quote(self, user: User, *, reward_id: int | None = None) -> CheckoutQuote | None:
        """
        Read-only: what the cart would be charged, with ``reward_id`` taken off.

        ``None`` when the cart cannot be ordered. A reward that no longer applies
        — used meanwhile, or the cart changed — is left out of the quote, and
        :meth:`place_order_from_cart` re-checks the chosen one under lock.
        """
        lines = await self._orderable_lines(user.id)
        if lines is None:
            return None
        chosen: RewardOption | None = None
        if reward_id is not None:
            options = await RewardService(self.session).options(user.id, lines=lines)
            chosen = next((option for option in options if option.reward_id == reward_id), None)
        return CheckoutQuote(subtotal=_lines_total(lines), reward=chosen)

    async def _priced_lines(self, cart: Cart) -> list[Line]:
        """
        The cart as order lines at today's prices — the one pricing checkout uses.

        Raises :class:`InactiveProductError` if something in it can no longer be
        sold, and :class:`EmptyCartError` if nothing is left to order.
        """
        # One query decides sellability for the whole cart: a product hidden by
        # its brand or category must not be sold, only browsed-away.
        unsellable = await self.products.list_unsellable_ids(
            [item.product_id for item in cart.items]
        )

        lines: list[Line] = []
        for item in cart.items:
            product = item.product
            if product is None:
                continue
            if product.id in unsellable:
                raise InactiveProductError(
                    f"Product {product.id} is not available and cannot be ordered"
                )
            lines.append((product.id, item.quantity, Decimal(product.price)))

        if not lines:
            raise EmptyCartError("Cart is empty")
        return lines

    async def _orderable_lines(self, user_id: int) -> list[Line] | None:
        """The cart as :meth:`place_order_from_cart` would price it, or ``None``."""
        cart = await self.carts.get_by_user_id_with_items(user_id)
        if cart is None or not cart.items:
            return None
        try:
            return await self._priced_lines(cart)
        except (EmptyCartError, InactiveProductError):
            return None
