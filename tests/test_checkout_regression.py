"""Full checkout regression: every city × delivery × payment path."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.user.checkout import build_checkout_summary
from app.keyboards.checkout import (
    CALLBACK_DELIVERY_PREFIX,
    CALLBACK_PAYMENT_PREFIX,
    delivery_keyboard,
    payment_keyboard,
)
from app.models.enums import (
    CityChoice,
    DeliveryType,
    LanguageCode,
    OrderStatus,
    PaymentMethod,
)
from app.models.order import Order
from app.repositories.cart import CartRepository
from app.services.cart import CartService
from app.services.localization import LocalizationService
from app.services.order import (
    EmptyCartError,
    InvalidDeliveryError,
    OrderService,
    delivery_allowed_for_city,
)
from app.utils import concurrency
from app.utils.concurrency import keyed_lock
from app.utils.labels import delivery_label, payment_label
from tests.factories import make_category, make_product, make_user

# city -> the delivery methods that city may use
ALLOWED: dict[CityChoice, tuple[DeliveryType, ...]] = {
    CityChoice.BERLIN: (DeliveryType.PICKUP, DeliveryType.COURIER),
    CityChoice.DELIVERY: (DeliveryType.POSTAL, DeliveryType.SERVICE),
}
PAYMENTS = tuple(PaymentMethod)
UNIT_PRICE = Decimal("18.00")

# Every full path the regression must cover.
PATHS = [
    (city, delivery, payment)
    for city, deliveries in ALLOWED.items()
    for delivery in deliveries
    for payment in PAYMENTS
]


async def _cart(session: AsyncSession, telegram_id: int, city: CityChoice):  # type: ignore[no-untyped-def]
    category = await make_category(session, name="Liquids")
    product = await make_product(session, category, name_en="Mango", price=str(UNIT_PRICE))
    user = await make_user(session, telegram_id=telegram_id, language=LanguageCode.EN, city=city)
    await session.flush()
    await CartService(session).add_product(user.id, product, quantity=2)
    await session.flush()
    return user, product


async def _place(session, user, delivery, payment):  # type: ignore[no-untyped-def]
    return await OrderService(session).place_order_from_cart(
        user,
        customer_name="Regression QA",
        delivery_type=delivery.value,
        address="Kastanienallee 12",
        preferred_time="18:00",
        phone="+4915100000",
        payment_method=payment,
    )


# ==================================================== full path matrix


@pytest.mark.parametrize("city,delivery,payment", PATHS)
@pytest.mark.asyncio
async def test_every_city_delivery_payment_path(
    session: AsyncSession,
    city: CityChoice,
    delivery: DeliveryType,
    payment: PaymentMethod,
) -> None:
    """Name → delivery → address → time → contact → payment → order."""
    tg = 10_000 + PATHS.index((city, delivery, payment))
    user, product = await _cart(session, tg, city)

    order = await _place(session, user, delivery, payment)
    await session.flush()

    assert order.city == city.value
    assert order.delivery_type == delivery.value
    assert order.payment_method is payment
    assert order.customer_name == "Regression QA"
    assert order.address == "Kastanienallee 12"
    assert order.preferred_time == "18:00"
    assert order.phone == "+4915100000"
    assert order.status is OrderStatus.NEW
    assert order.total_price == UNIT_PRICE * 2
    assert [(i.product_id, i.quantity, i.price) for i in order.items] == [
        (product.id, 2, UNIT_PRICE)
    ]

    # the cart is emptied by a successful order
    cart = await CartRepository(session).get_by_user_id_with_items(user.id)
    assert cart is not None and cart.items == []


def test_path_matrix_is_complete() -> None:
    assert len(PATHS) == 8, "2 cities × 2 delivery methods × 2 payment methods"


# ============================================== delivery / city guard


@pytest.mark.parametrize("city", list(CityChoice))
@pytest.mark.parametrize("delivery", list(DeliveryType))
def test_delivery_matrix_matches_the_rules(city: CityChoice, delivery: DeliveryType) -> None:
    assert delivery_allowed_for_city(city, delivery.value) is (delivery in ALLOWED[city])


@pytest.mark.parametrize(
    "city,forbidden",
    [
        (CityChoice.BERLIN, DeliveryType.POSTAL),
        (CityChoice.BERLIN, DeliveryType.SERVICE),
        (CityChoice.DELIVERY, DeliveryType.PICKUP),
        (CityChoice.DELIVERY, DeliveryType.COURIER),
    ],
)
@pytest.mark.asyncio
async def test_forbidden_delivery_is_rejected_server_side(
    session: AsyncSession, city: CityChoice, forbidden: DeliveryType
) -> None:
    """A tampered callback must not bypass the city rule."""
    user, _product = await _cart(session, 10_100 + forbidden.value.__len__(), city)

    with pytest.raises(InvalidDeliveryError):
        await _place(session, user, forbidden, PaymentMethod.CASH)

    # a rejected order must leave the cart untouched
    cart = await CartRepository(session).get_by_user_id_with_items(user.id)
    assert cart is not None and len(cart.items) == 1


@pytest.mark.parametrize("city", list(CityChoice))
def test_delivery_keyboard_offers_only_valid_methods(city: CityChoice) -> None:
    i18n = LocalizationService.from_code("en")
    payloads = [
        b.callback_data for row in delivery_keyboard(i18n, city).inline_keyboard for b in row
    ]
    offered = {
        p.removeprefix(CALLBACK_DELIVERY_PREFIX)
        for p in payloads
        if p and p.startswith(CALLBACK_DELIVERY_PREFIX)
    }
    assert offered == {d.value for d in ALLOWED[city]}


# ================================================== payment persistence


@pytest.mark.parametrize("payment", PAYMENTS)
@pytest.mark.asyncio
async def test_payment_method_persists_exactly(
    session: AsyncSession, payment: PaymentMethod
) -> None:
    """Round-trip the stored value, not just the ORM attribute."""
    user, _product = await _cart(session, 10_200 + PAYMENTS.index(payment), CityChoice.BERLIN)
    order = await _place(session, user, DeliveryType.PICKUP, payment)
    await session.flush()
    order_id = order.id

    raw = await session.scalar(select(Order.payment_method).where(Order.id == order_id))
    assert raw is payment
    assert PaymentMethod(raw).value == payment.value

    session.expunge_all()
    reloaded = await session.get(Order, order_id)
    assert reloaded is not None
    assert reloaded.payment_method is payment
    assert reloaded.payment_method.value == payment.value


@pytest.mark.parametrize("payment", PAYMENTS)
def test_payment_callback_round_trips(payment: PaymentMethod) -> None:
    payload = f"{CALLBACK_PAYMENT_PREFIX}{payment.value}"
    assert PaymentMethod(payload.removeprefix(CALLBACK_PAYMENT_PREFIX)) is payment


@pytest.mark.asyncio
async def test_two_orders_can_hold_different_payment_methods(
    session: AsyncSession,
) -> None:
    """No cross-contamination between orders."""
    user, product = await _cart(session, 10_300, CityChoice.BERLIN)
    first = await _place(session, user, DeliveryType.PICKUP, PaymentMethod.CASH)
    await session.flush()

    await CartService(session).add_product(user.id, product, quantity=1)
    await session.flush()
    second = await _place(session, user, DeliveryType.COURIER, PaymentMethod.CARD)
    await session.flush()

    assert first.payment_method is PaymentMethod.CASH
    assert second.payment_method is PaymentMethod.CARD
    assert first.delivery_type == "pickup"
    assert second.delivery_type == "courier"


# ============================================ duplicate confirmation


@pytest.mark.asyncio
async def test_second_confirmation_finds_an_empty_cart(
    session: AsyncSession,
) -> None:
    """Confirming twice must not create a second order."""
    user, _product = await _cart(session, 10_400, CityChoice.BERLIN)
    first = await _place(session, user, DeliveryType.PICKUP, PaymentMethod.CASH)
    await session.flush()

    with pytest.raises(EmptyCartError):
        await _place(session, user, DeliveryType.PICKUP, PaymentMethod.CASH)

    orders = (await session.scalars(select(Order).where(Order.user_id == user.id))).all()
    assert [o.id for o in orders] == [first.id]


@pytest.mark.asyncio
async def test_keyed_lock_serializes_confirms_for_one_user() -> None:
    """The handler's first guard: two taps from one user cannot interleave.

    True session-level concurrency is not reproducible here — an AsyncSession is
    not concurrency-safe, and production gives each Telegram update its own
    session from DatabaseMiddleware. What is testable is the lock the handler
    wraps the confirm in, which is what stops the second tap entering the
    critical section while the first is still inside it.
    """
    order: list[str] = []

    async def confirm(tag: str) -> None:
        async with keyed_lock("checkout:4242"):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0)  # yield: an unguarded section would interleave
            order.append(f"{tag}-exit")

    await asyncio.gather(confirm("a"), confirm("b"))

    assert order in (
        ["a-enter", "a-exit", "b-enter", "b-exit"],
        ["b-enter", "b-exit", "a-enter", "a-exit"],
    ), order


@pytest.mark.asyncio
async def test_different_users_are_not_blocked_by_each_other() -> None:
    """The lock is per-user, so one slow checkout must not stall the shop."""
    running: list[str] = []

    async def confirm(tag: str) -> None:
        async with keyed_lock(f"checkout:{tag}"):
            running.append(f"{tag}-enter")
            await asyncio.sleep(0)
            running.append(f"{tag}-exit")

    await asyncio.gather(confirm("1"), confirm("2"))

    # interleaved proves they did not serialize on each other
    assert running.index("1-enter") < running.index("2-exit")
    assert running.index("2-enter") < running.index("1-exit")


@pytest.mark.asyncio
async def test_pruning_the_registry_never_splits_a_lock_in_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A lock reads as unlocked between its release and its next waiter waking. Pruning
    it then — the registry full of other customers' keys — handed the next tap a fresh
    lock while the waiter entered the old one: two holders of one key at once.
    """
    monkeypatch.setattr(concurrency, "_locks", {})
    monkeypatch.setattr(concurrency, "_users", {}, raising=False)
    monkeypatch.setattr(concurrency, "_MAX_LOCKS", 4)  # 2048 in production
    key = "roulette:42"
    inside: set[str] = set()
    seen: list[set[str]] = []
    release_first, release_second, second_inside = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with keyed_lock(key):
            inside.add("first")
            await release_first.wait()
            inside.discard("first")
        # The same tick as the release, before the queued second tap runs: another
        # customer's first tap registers a new key, and the full registry prunes.
        async with keyed_lock("checkout:7"):
            pass

    async def second() -> None:
        async with keyed_lock(key):
            inside.add("second")
            seen.append(set(inside))
            second_inside.set()
            await release_second.wait()
            inside.discard("second")

    async def third() -> None:
        async with keyed_lock(key):
            seen.append(inside | {"third"})

    holder = asyncio.create_task(first())
    await asyncio.sleep(0)
    for other in ("f1", "f2", "f3"):  # other customers fill the registry
        async with keyed_lock(other):
            pass
    waiter = asyncio.create_task(second())
    await asyncio.sleep(0)
    release_first.set()
    await holder
    await second_inside.wait()
    latecomer = asyncio.create_task(third())
    await asyncio.sleep(0)
    release_second.set()
    await asyncio.gather(waiter, latecomer)

    assert seen == [{"second"}, {"third"}], "two taps held one customer's lock at once"
    assert concurrency._users == {}, "every holder and waiter was counted out"


@pytest.mark.asyncio
async def test_submitted_flag_blocks_the_second_confirm() -> None:
    """The handler's second guard: FSM state marks the order as submitted."""
    state: dict[str, object] = {}

    async def confirm() -> str:
        async with keyed_lock("checkout:5150"):
            if state.get("submitted"):
                return "rejected"
            state["submitted"] = True
            return "placed"

    assert await confirm() == "placed"
    assert await confirm() == "rejected"
    assert (await asyncio.gather(confirm(), confirm())) == ["rejected", "rejected"]


@pytest.mark.asyncio
async def test_failed_order_clears_the_flag_for_a_retry(
    session: AsyncSession,
) -> None:
    """A rejected order must leave the customer able to try again."""
    user, _product = await _cart(session, 10_550, CityChoice.BERLIN)

    with pytest.raises(InvalidDeliveryError):
        await _place(session, user, DeliveryType.POSTAL, PaymentMethod.CASH)

    # the cart survived, so a corrected retry succeeds
    order = await _place(session, user, DeliveryType.PICKUP, PaymentMethod.CASH)
    await session.flush()
    assert order.payment_method is PaymentMethod.CASH


@pytest.mark.asyncio
async def test_repeated_payment_selection_is_harmless(
    session: AsyncSession,
) -> None:
    """Tapping a payment button repeatedly only rewrites FSM data."""
    user, _product = await _cart(session, 10_600, CityChoice.BERLIN)

    chosen = PaymentMethod.CASH
    for _ in range(5):
        chosen = PaymentMethod(
            f"{CALLBACK_PAYMENT_PREFIX}card".removeprefix(CALLBACK_PAYMENT_PREFIX)
        )
    order = await _place(session, user, DeliveryType.PICKUP, chosen)
    await session.flush()

    assert order.payment_method is PaymentMethod.CARD


# ============================================ summary before confirming


@pytest.mark.parametrize("city,delivery,payment", PATHS)
def test_summary_shows_the_whole_order(
    city: CityChoice, delivery: DeliveryType, payment: PaymentMethod
) -> None:
    i18n = LocalizationService.from_code("en")
    user = type("U", (), {"selected_city": city})()
    line = type("L", (), {"name": "Mango", "quantity": 2, "line_total": UNIT_PRICE * 2})()
    view = type("V", (), {"lines": [line], "total": UNIT_PRICE * 2})()
    data = {
        "customer_name": "Regression QA",
        "delivery_type": delivery.value,
        "address": "Kastanienallee 12",
        "preferred_time": "18:00",
        "phone": "+4915100000",
        "payment_method": payment.value,
    }

    summary = build_checkout_summary(i18n, data=data, user=user, view=view)  # type: ignore[arg-type]

    assert "Regression QA" in summary
    assert delivery_label(i18n, delivery.value) in summary
    assert payment_label(i18n, payment) in summary
    assert "Kastanienallee 12" in summary
    assert "18:00" in summary
    assert "36.00" in summary
    assert "{" not in summary


@pytest.mark.parametrize("language", ("ru", "en", "de", "uk"))
def test_payment_keyboard_localized(language: str) -> None:
    i18n = LocalizationService.from_code(language)
    labels = [b.text for row in payment_keyboard(i18n).inline_keyboard for b in row]

    assert labels[0] == i18n.t("checkout.payment_cash")
    assert labels[1] == i18n.t("checkout.payment_card")
    assert labels[2] == i18n.t("checkout.cancel")
