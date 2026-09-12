"""
Stamp card engine: €20 → 1 stamp, booked once per completed order, server-side only.

Stamps are driven through the real order lifecycle (``AdminService.
set_order_status``) wherever the question is "what happens when an order is
completed", and through ``StampCardService.award_for_order`` directly where the
question is what one call does.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.enums import (
    CityChoice,
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    RewardSource,
    RewardType,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.user import User
from app.services.admin import AdminService
from app.services.cart import CartService
from app.services.loyalty import InsufficientStampsError, LoyaltyService
from app.services.order import OrderService
from app.services.referral import ReferralService
from app.services.referral_program import ReferralProgramService
from app.services.stamp_card import (
    PurchaseAwardStatus,
    StampCardPolicy,
    StampCardService,
    is_qualifying_purchase,
    purchase_disqualification,
)
from tests.factories import make_category, make_product, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "8e4c1a7b2d95_orders_loyalty_eligible.py"
POLICY = StampCardPolicy.defaults()
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)


def settings_with(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


async def place(session: AsyncSession, user: User, total: str, *, eligible: bool = True) -> Order:
    order = Order(
        user_id=user.id,
        customer_name="Test",
        city=CityChoice.BERLIN.value,
        delivery_type="pickup",
        address="Street 1",
        preferred_time="18:00",
        total_price=Decimal(total),
        status=OrderStatus.NEW,
        loyalty_eligible=eligible,
    )
    session.add(order)
    await session.flush()
    return order


async def move(
    session: AsyncSession,
    order: Order,
    *statuses: OrderStatus,
    admin: AdminService | None = None,
) -> Order:
    admin = admin or AdminService(session)
    for status in statuses:
        order = await admin.set_order_status(order, status)
    return order


async def ledger(session: AsyncSession, user_id: int) -> list[LoyaltyTransaction]:
    result = await session.scalars(
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.user_id == user_id)
        .order_by(LoyaltyTransaction.id)
    )
    return list(result.all())


async def account(session: AsyncSession, user_id: int) -> LoyaltyAccount | None:
    return await session.scalar(select(LoyaltyAccount).where(LoyaltyAccount.user_id == user_id))


# ------------------------------------------------------------ calculation


@pytest.mark.parametrize(
    ("total", "stamps"),
    [
        ("0", 0),
        ("0.01", 0),
        ("15.00", 0),
        ("19.99", 0),
        ("20.00", 1),
        ("20.01", 1),
        ("39.99", 1),
        ("40.00", 2),
        ("59.99", 2),
        ("60.00", 3),
        ("100.00", 5),
        ("1234.56", 61),
    ],
)
def test_stamps_are_whole_multiples_of_the_threshold(total: str, stamps: int) -> None:
    assert POLICY.stamps_for(Decimal(total)) == stamps


@pytest.mark.parametrize("amount", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity"), 20.0])
def test_a_nonsense_amount_is_refused(amount: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        POLICY.stamps_for(amount)


def test_the_defaults_are_the_configuration_defaults() -> None:
    assert StampCardPolicy.defaults() == StampCardPolicy.from_settings(settings_with())
    assert (POLICY.purchase_threshold, POLICY.stamps_required, POLICY.free_bottle_max_price) == (
        Decimal("20.00"),
        10,
        Decimal("20.00"),
    )


def test_the_threshold_is_configurable() -> None:
    policy = StampCardPolicy.from_settings(
        settings_with(loyalty_stamp_purchase_threshold=Decimal("25"))
    )
    assert policy.stamps_for(Decimal("49.99")) == 1
    assert policy.stamps_for(Decimal("50.00")) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"purchase_threshold": Decimal("0")},
        {"purchase_threshold": 20.0},
        {"stamps_required": 0},
        {"stamps_required": True},
        {"free_bottle_max_price": Decimal("-1")},
    ],
    ids=["zero-threshold", "float-threshold", "zero-required", "bool-required", "negative-cap"],
)
def test_an_invalid_policy_is_refused(overrides: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "purchase_threshold": Decimal("20"),
        "stamps_required": 10,
        "free_bottle_max_price": Decimal("20"),
    }
    with pytest.raises((TypeError, ValueError)):
        StampCardPolicy(**(fields | overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"loyalty_stamp_purchase_threshold": Decimal("0")},
        {"loyalty_stamp_purchase_threshold": Decimal("0.005")},
        {"loyalty_stamp_purchase_threshold": Decimal("0.50")},
        {"loyalty_stamp_purchase_threshold": Decimal("20.005")},
        {"loyalty_stamp_purchase_threshold": Decimal("100000000")},
        {"loyalty_stamps_required": 0},
        {"loyalty_free_bottle_max_price": Decimal("0")},
        {"loyalty_free_bottle_max_price": Decimal("0.001")},
        {"loyalty_free_bottle_max_price": Decimal("19.999")},
        {"loyalty_free_bottle_max_price": Decimal("100000000")},
    ],
    ids=[
        "threshold-zero",
        "threshold-below-a-cent",
        "threshold-below-a-euro",
        "threshold-sub-cent",
        "threshold-too-large",
        "no-stamps-required",
        "ceiling-zero",
        "ceiling-below-a-cent",
        "ceiling-sub-cent",
        "ceiling-too-large",
    ],
)
def test_invalid_settings_stop_the_process_at_startup(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        settings_with(**overrides)


@pytest.mark.parametrize("value", ["1", "1.00", "19.99", "20", "99999999.99"])
def test_every_accepted_setting_is_usable_by_the_stamp_card(value: str) -> None:
    """Regression: 0.005 passed the settings and failed only at the first completion."""
    policy = StampCardPolicy.from_settings(
        settings_with(
            loyalty_stamp_purchase_threshold=Decimal(value),
            loyalty_free_bottle_max_price=Decimal(value),
        )
    )
    assert policy.purchase_threshold == policy.free_bottle_max_price == Decimal(value)


# --------------------------------------------------- completing an order


@pytest.mark.parametrize(
    ("total", "stamps"),
    [("15.00", 0), ("20.00", 1), ("39.99", 1), ("40.00", 2), ("60.00", 3)],
)
async def test_completing_an_order_books_its_stamps(
    session: AsyncSession, total: str, stamps: int
) -> None:
    user = await make_user(session, telegram_id=8001)
    order = await place(session, user, total)

    await move(session, order, *TO_COMPLETED)

    rows = await ledger(session, user.id)
    assert [(row.kind, row.amount, row.order_id) for row in rows] == [
        (LoyaltyTransactionType.PURCHASE, stamps, order.id)
    ]
    card = await account(session, user.id)
    assert card is not None
    assert (card.stamp_balance, card.qualifying_purchase_count) == (stamps, 1)


async def test_a_zero_total_order_is_not_a_purchase(session: AsyncSession) -> None:
    """€0 — e.g. an order holding only a free bottle — earns nothing and counts nothing."""
    user = await make_user(session, telegram_id=8002)
    order = await place(session, user, "0.00")

    await move(session, order, *TO_COMPLETED)
    award = await StampCardService(session).award_for_order(order.id)

    assert award.status == PurchaseAwardStatus.NOT_PAID
    assert await ledger(session, user.id) == []
    card = await account(session, user.id)
    assert card is None or card.qualifying_purchase_count == 0


async def test_the_same_order_never_awards_twice(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8003)
    order = await place(session, user, "40.00")
    order = await move(session, order, *TO_COMPLETED)
    stamp_card = StampCardService(session)

    for _ in range(3):
        again = await stamp_card.award_for_order(order.id)
        assert (again.status, again.stamps, again.awarded) == (
            PurchaseAwardStatus.ALREADY_AWARDED,
            2,
            False,
        )
    # Re-applying Completed is a no-op, not a second completion.
    await AdminService(session).set_order_status(order, OrderStatus.COMPLETED)

    assert len(await ledger(session, user.id)) == 1
    assert await LoyaltyService(session).balance(user.id) == 2


@pytest.mark.parametrize(
    "path",
    [
        (),
        (OrderStatus.ACCEPTED,),
        (OrderStatus.ACCEPTED, OrderStatus.SHIPPED),
        (OrderStatus.CANCELLED,),
        (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.CANCELLED),
    ],
    ids=["new", "accepted", "shipped", "cancelled", "cancelled-after-shipping"],
)
async def test_an_order_that_is_not_completed_earns_nothing(
    session: AsyncSession, path: tuple[OrderStatus, ...]
) -> None:
    user = await make_user(session, telegram_id=8004)
    order = await place(session, user, "60.00")
    await move(session, order, *path)

    award = await StampCardService(session).award_for_order(order.id)

    assert award.status == PurchaseAwardStatus.NOT_COMPLETED
    assert await ledger(session, user.id) == []
    assert await LoyaltyService(session).balance(user.id) == 0


async def test_a_reopened_order_earns_once_when_it_is_finally_completed(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=8005)
    order = await place(session, user, "40.00")

    order = await move(session, order, OrderStatus.CANCELLED)
    assert await ledger(session, user.id) == []
    order = await move(session, order, OrderStatus.NEW, *TO_COMPLETED)

    assert [row.amount for row in await ledger(session, user.id)] == [2]


async def test_an_order_placed_before_launch_earns_nothing(session: AsyncSession) -> None:
    """Owner decision: orders already in progress at launch stay as they are."""
    user = await make_user(session, telegram_id=8006)
    order = await place(session, user, "60.00", eligible=False)

    await move(session, order, *TO_COMPLETED)
    award = await StampCardService(session).award_for_order(order.id)

    assert award.status == PurchaseAwardStatus.NOT_ELIGIBLE
    assert await ledger(session, user.id) == []


async def test_multiple_completed_orders_accumulate(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8007)
    totals = ["20.00", "45.50", "15.00", "60.00", "0.00"]
    for total in totals:
        await move(session, await place(session, user, total), *TO_COMPLETED)

    rows = await ledger(session, user.id)
    assert [row.amount for row in rows] == [1, 2, 0, 3], "the €0 order is not a purchase"
    running = 0
    for row in rows:
        running += row.amount
        assert row.balance_after == running
    card = await account(session, user.id)
    assert card is not None
    assert (card.stamp_balance, card.qualifying_purchase_count) == (6, 4)
    assert await LoyaltyService(session).ledger_balance(user.id) == 6


async def test_stamps_go_to_the_customer_who_ordered(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=8008)
    bob = await make_user(session, telegram_id=8009)

    await move(session, await place(session, alice, "20.00"), *TO_COMPLETED)

    loyalty = LoyaltyService(session)
    assert (await loyalty.balance(alice.id), await loyalty.balance(bob.id)) == (1, 0)


async def test_stamps_come_from_the_charged_total_in_the_database(session: AsyncSession) -> None:
    """Owner decision: the charged total after any discount — read from the row, not an input."""
    user = await make_user(session, telegram_id=8010)
    order = await place(session, user, "40.00")
    await session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(total_price=Decimal("35.99"))
        .execution_options(synchronize_session=False)
    )

    await move(session, order, *TO_COMPLETED)

    assert [row.amount for row in await ledger(session, user.id)] == [1]


async def test_a_configured_threshold_is_honoured_end_to_end(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8011)
    admin = AdminService(
        session, settings=settings_with(loyalty_stamp_purchase_threshold=Decimal("25.00"))
    )

    for total in ("50.00", "49.99"):
        await move(session, await place(session, user, total), *TO_COMPLETED, admin=admin)

    assert [row.amount for row in await ledger(session, user.id)] == [2, 1]


async def test_a_failed_award_rolls_the_completion_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status and the stamps are one unit of work."""
    user = await make_user(session, telegram_id=8012)
    order = await place(session, user, "40.00")
    order = await move(session, order, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)
    order_id = order.id

    async def broken(self: StampCardService, order_id: int) -> None:
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(StampCardService, "award_for_order", broken)
    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await AdminService(session).set_order_status(order, OrderStatus.COMPLETED)

    status = await session.scalar(select(Order.status).where(Order.id == order_id))
    assert status == OrderStatus.SHIPPED
    assert await ledger(session, user.id) == []


async def test_new_orders_are_eligible_and_pre_existing_rows_are_not(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=8013)
    category = await make_category(session, name="Liquids")
    product = await make_product(session, category, price="20.00")
    await CartService(session).add_product(user.id, product, quantity=1)
    placed = await OrderService(session).place_order_from_cart(
        user,
        customer_name="Anna",
        delivery_type="pickup",
        address="Street 1",
        preferred_time="18:00",
        phone=None,
        payment_method=PaymentMethod.CASH,
    )
    assert placed.loyalty_eligible is True

    # A row that existed before the column: the database default applies.
    await session.execute(
        text(
            "INSERT INTO orders (user_id, customer_name, city, delivery_type, address, "
            "total_price, status) VALUES (:user_id, 'Legacy', 'berlin', 'pickup', 'X', 40, 'New')"
        ),
        {"user_id": user.id},
    )
    legacy = await session.scalar(
        select(Order.loyalty_eligible).where(Order.customer_name == "Legacy")
    )
    assert legacy is False


# --------------------------------------------------------- card and claim


async def test_the_card_shows_progress_and_unlocked_bottles(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8014)
    newcomer = await make_user(session, telegram_id=8015)
    await LoyaltyService(session).adjust(user.id, amount=13, note="test setup")
    stamp_card = StampCardService(session)

    card = await stamp_card.card(user.id)
    empty = await stamp_card.card(newcomer.id)

    assert (card.stamps, card.stamps_required, card.free_bottles_unlocked) == (13, 10, 1)
    assert (card.progress, card.can_claim) == (3, True)
    assert (empty.stamps, empty.progress, empty.can_claim) == (0, 0, False)
    assert await account(session, newcomer.id) is None, "looking never creates anything"


async def test_claiming_spends_the_configured_stamps(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8016)
    await LoyaltyService(session).adjust(user.id, amount=10, note="test setup")
    stamp_card = StampCardService(session)

    reward = await stamp_card.claim_free_bottle(user.id)

    assert (reward.kind, reward.source, reward.max_item_price) == (
        RewardType.FREE_BOTTLE,
        RewardSource.STAMP_CARD,
        Decimal("20.00"),
    )
    assert (await stamp_card.card(user.id)).stamps == 0
    with pytest.raises(InsufficientStampsError):
        await stamp_card.claim_free_bottle(user.id)


async def test_claim_rules_follow_the_configuration(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8017)
    await LoyaltyService(session).adjust(user.id, amount=8, note="test setup")
    policy = StampCardPolicy.from_settings(
        settings_with(loyalty_stamps_required=8, loyalty_free_bottle_max_price=Decimal("18.50"))
    )

    reward = await StampCardService(session, policy).claim_free_bottle(user.id)

    assert reward.max_item_price == Decimal("18.50")
    assert await LoyaltyService(session).balance(user.id) == 0


# ---------------------------------------------------------------- guards


# Calls that book stamps or bind a reward, each with the one module allowed to make it.
SINGLE_TRIGGER = {
    ".award_for_order(": "app/services/admin/orders.py",  # order completion
    ".grant_for_completed_order(": "app/services/admin/orders.py",  # its milestone spin
    ".settle_for_completed_order(": "app/services/admin/orders.py",  # its referral payout
    ".record_purchase(": "app/services/stamp_card.py",
    ".redeem(": "app/services/order.py",  # checkout
    ".use_reward(": "app/services/reward.py",
}
# Loyalty mutations no module outside app/services may make at all.
SERVICE_ONLY = (
    *SINGLE_TRIGGER,
    ".credit_referral(",
    ".credit_roulette(",
    ".adjust(",
    ".grant_purchase_milestone_spin(",
    ".grant_referral_spin(",
    ".grant_for_referral(",
)


def app_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "app").rglob("*.py"))
    }


def test_no_input_can_grant_stamps_or_bind_rewards() -> None:
    """Handlers, keyboards and middlewares never move loyalty state; services do."""
    offenders = [
        f"{path}: {call}"
        for path, source in app_sources().items()
        if not path.startswith("app/services/")
        for call in SERVICE_ONLY
        if call in source
    ]
    assert offenders == []


@pytest.mark.parametrize(("call", "owner"), list(SINGLE_TRIGGER.items()))
def test_each_booking_has_one_trigger(call: str, owner: str) -> None:
    assert [path for path, source in app_sources().items() if call in source] == [owner]


def test_order_completion_gets_the_configured_stamp_rules() -> None:
    """
    ``AdminService(session)`` without ``settings`` completes orders under the
    *default* stamp rules, silently ignoring the configuration. Every handler
    that changes an order's status must pass them.
    """
    checked = 0
    offenders: list[str] = []
    for path in sorted((ROOT / "app" / "handlers").rglob("*.py")):
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            if not any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr in {"set_order_status", "change_order_status"}
                for call in calls
            ):
                continue
            checked += 1
            services = [
                call
                for call in calls
                if isinstance(call.func, ast.Name)
                and call.func.id in {"AdminService", "AdminOrderService"}
            ]
            if not services or not all(
                {keyword.arg for keyword in call.keywords} & {"settings", "stamp_policy"}
                for call in services
            ):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{function.name}")
    assert checked, "no handler changes an order's status any more — update this guard"
    assert offenders == []


def _function(name: str) -> ast.FunctionDef:
    for node in ast.parse(MIGRATION.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{MIGRATION.name} has no {name}()")


def test_the_migration_only_adds_the_eligibility_column() -> None:
    calls = [
        node
        for node in ast.walk(_function("upgrade"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    assert [call.func.attr for call in calls if isinstance(call.func, ast.Attribute)] == [
        "add_column"
    ]
    table, column = calls[0].args
    assert isinstance(table, ast.Constant) and table.value == "orders"
    assert isinstance(column, ast.Call) and isinstance(column.args[0], ast.Constant)
    assert column.args[0].value == "loyalty_eligible"
    keywords = {kw.arg: kw.value for kw in column.keywords}
    nullable = keywords["nullable"]
    assert isinstance(nullable, ast.Constant) and nullable.value is False
    default = keywords["server_default"]
    assert isinstance(default, ast.Call) and isinstance(default.func, ast.Attribute)
    assert default.func.attr == "false", "existing orders must read as ineligible"


def test_the_downgrade_is_guarded() -> None:
    first = next(n for n in _function("downgrade").body if isinstance(n, ast.Expr)).value
    assert isinstance(first, ast.Call) and isinstance(first.func, ast.Name)
    assert first.func.id == "_refuse_to_forget_eligibility"


# ------------------------------------------------------------ one rule, one place


@pytest.mark.parametrize(
    ("status", "eligible", "total", "why_not"),
    [
        (OrderStatus.COMPLETED, True, "20.00", None),
        (OrderStatus.COMPLETED, True, "0.01", None),  # 0 stamps, still a purchase
        (OrderStatus.SHIPPED, True, "20.00", PurchaseAwardStatus.NOT_COMPLETED),
        (OrderStatus.CANCELLED, True, "20.00", PurchaseAwardStatus.NOT_COMPLETED),
        (OrderStatus.COMPLETED, False, "20.00", PurchaseAwardStatus.NOT_ELIGIBLE),
        (OrderStatus.COMPLETED, True, "0.00", PurchaseAwardStatus.NOT_PAID),
    ],
)
async def test_one_rule_decides_what_a_qualifying_purchase_is(
    session: AsyncSession,
    status: OrderStatus,
    eligible: bool,
    total: str,
    why_not: PurchaseAwardStatus | None,
) -> None:
    """Stamps and referral payouts ask the same question, so they can never disagree."""
    referrer = await make_user(session, telegram_id=8301)
    friend = await make_user(session, telegram_id=8302)
    await ReferralService(session).attribute(
        referrer_user_id=referrer.id, referred_user_id=friend.id
    )
    order = await place(session, friend, total, eligible=eligible)
    order.status = status
    await session.flush()

    assert purchase_disqualification(order) == why_not
    assert is_qualifying_purchase(order) is (why_not is None)
    award = await StampCardService(session, POLICY).award_for_order(order.id)
    assert (award.status == PurchaseAwardStatus.AWARDED) is (why_not is None)
    payout = await ReferralProgramService(session).settle_for_completed_order(order.id)
    assert (payout is not None) is (why_not is None)


def test_the_referral_modules_do_not_restate_the_rule() -> None:
    """Changing what a qualifying purchase is must stay one edit, in stamp_card.py."""
    for name in ("referral.py", "referral_program.py"):
        source = (ROOT / "app" / "services" / name).read_text(encoding="utf-8")
        assert "loyalty_eligible" not in source, f"{name} spells out the rule again"
        assert "is_qualifying_purchase" in source, f"{name} no longer asks the one rule"
