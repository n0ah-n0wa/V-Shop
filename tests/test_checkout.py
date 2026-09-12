"""Checkout / order placement tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.user.checkout import build_checkout_summary
from app.models.enums import CityChoice, DeliveryType, OrderStatus
from app.repositories.cart import CartRepository
from app.repositories.order import OrderRepository
from app.services.cart import CartLineView, CartService, CartView
from app.services.localization import LocalizationService
from app.services.order import (
    EmptyCartError,
    InactiveProductError,
    InvalidDeliveryError,
    OrderService,
)
from tests.factories import make_category, make_product, make_user


@pytest.mark.asyncio
async def test_place_order_from_cart_persists_and_clears(
    session: AsyncSession,
) -> None:
    user = await make_user(session, city=CityChoice.BERLIN)
    category = await make_category(session)
    product = await make_product(session, category, price="12.50")
    await CartService(session).add_product(user.id, product, quantity=2)

    order = await OrderService(session).place_order_from_cart(
        user,
        customer_name="  Alice  ",
        delivery_type=DeliveryType.PICKUP.value,
        address="  Main St 1 ",
        preferred_time=" Tonight ",
        phone="+491234567890",
    )

    assert order.id is not None
    assert order.status == OrderStatus.NEW
    assert order.customer_name == "Alice"
    assert order.address == "Main St 1"
    assert order.preferred_time == "Tonight"
    assert order.total_price == Decimal("25.00")
    assert len(order.items) == 1
    assert order.items[0].product_id == product.id
    assert order.items[0].quantity == 2
    assert order.items[0].price == Decimal("12.50")

    cart = await CartRepository(session).get_by_user_id_with_items(user.id)
    assert cart is not None
    assert cart.items == []

    saved = await OrderRepository(session).get_with_items(order.id)
    assert saved is not None
    assert saved.user is not None
    assert saved.user.id == user.id


@pytest.mark.asyncio
async def test_place_order_empty_cart_raises(session: AsyncSession) -> None:
    user = await make_user(session)
    with pytest.raises(EmptyCartError):
        await OrderService(session).place_order_from_cart(
            user,
            customer_name="Alice",
            delivery_type=DeliveryType.PICKUP.value,
            address="Addr",
            preferred_time="Now",
            phone=None,
        )


@pytest.mark.asyncio
async def test_place_order_requires_city(session: AsyncSession) -> None:
    user = await make_user(session, city=None)
    category = await make_category(session)
    product = await make_product(session, category)
    await CartService(session).add_product(user.id, product)

    with pytest.raises(ValueError, match="city"):
        await OrderService(session).place_order_from_cart(
            user,
            customer_name="Alice",
            delivery_type=DeliveryType.PICKUP.value,
            address="Addr",
            preferred_time="Now",
            phone=None,
        )


@pytest.mark.asyncio
async def test_second_checkout_after_clear_fails(session: AsyncSession) -> None:
    user = await make_user(session)
    category = await make_category(session)
    product = await make_product(session, category, price="3.00")
    await CartService(session).add_product(user.id, product, quantity=1)

    service = OrderService(session)
    first = await service.place_order_from_cart(
        user,
        customer_name="Alice",
        delivery_type=DeliveryType.COURIER.value,
        address="Addr",
        preferred_time="Soon",
        phone="+49111",
    )
    assert first.total_price == Decimal("3.00")

    with pytest.raises(EmptyCartError):
        await service.place_order_from_cart(
            user,
            customer_name="Alice",
            delivery_type=DeliveryType.COURIER.value,
            address="Addr",
            preferred_time="Soon",
            phone="+49111",
        )


@pytest.mark.asyncio
async def test_place_order_rejects_inactive_product(session: AsyncSession) -> None:
    user = await make_user(session, city=CityChoice.BERLIN)
    category = await make_category(session)
    product = await make_product(session, category, price="5.00", is_active=True)
    await CartService(session).add_product(user.id, product, quantity=1)
    product.is_active = False
    await session.flush()

    with pytest.raises(InactiveProductError):
        await OrderService(session).place_order_from_cart(
            user,
            customer_name="Alice",
            delivery_type=DeliveryType.PICKUP.value,
            address="Addr",
            preferred_time="Now",
            phone=None,
        )


@pytest.mark.asyncio
async def test_place_order_rejects_invalid_delivery(session: AsyncSession) -> None:
    user = await make_user(session, city=CityChoice.BERLIN)
    category = await make_category(session)
    product = await make_product(session, category)
    await CartService(session).add_product(user.id, product)

    with pytest.raises(InvalidDeliveryError):
        await OrderService(session).place_order_from_cart(
            user,
            customer_name="Alice",
            delivery_type=DeliveryType.POSTAL.value,
            address="Addr",
            preferred_time="Now",
            phone=None,
        )


def test_build_checkout_summary_contains_core_fields() -> None:
    i18n = LocalizationService("en")
    user = type(
        "U",
        (),
        {"selected_city": CityChoice.BERLIN},
    )()
    view = CartView(
        cart_id=1,
        lines=[
            CartLineView(
                item_id=1,
                product_id=10,
                name="Test Juice",
                quantity=2,
                unit_price=Decimal("5.00"),
                line_total=Decimal("10.00"),
            )
        ],
        total=Decimal("10.00"),
    )
    text = build_checkout_summary(
        i18n,
        data={
            "customer_name": "Alice",
            "delivery_type": DeliveryType.PICKUP.value,
            "address": "Berlin St",
            "preferred_time": "18:00",
            "phone": "+491234",
        },
        user=user,  # type: ignore[arg-type]
        view=view,
    )
    assert "Alice" in text
    assert "Berlin St" in text
    assert "Test Juice" in text
    assert i18n.t("checkout.summary_total", total=Decimal("10.00")) in text
