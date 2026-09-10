"""
The owner's loyalty scenarios, end to end.

1. a €20 completed order earns 1 stamp
2. a €40 completed order earns 2 stamps
3. a duplicate completion event books nothing twice
4. a cancelled order earns nothing
5. 10 stamps make a free-bottle reward available
6. claiming it consumes exactly 10 stamps
7. a duplicate redemption is refused
8. of concurrent redemptions, exactly one succeeds
9. a failed transaction leaves no partial ledger mutation

The tests drive the production paths — checkout (``OrderService``), the admin
status change (``AdminService`` built with the settings, as the handler builds
it), the stamp card and redemption at checkout — and read every outcome straight
from the database.

Scenario 8 needs real concurrency, which SQLite cannot give: it serialises
writers and ignores ``FOR UPDATE``. Here it is checked structurally, on every
run: each redemption path takes the customer's account lock before its first
loyalty write. ``tests/test_loyalty_postgres.py`` races it for real
(``test_concurrent_redemptions_cannot_overdraw``,
``test_concurrent_claims_from_one_card_unlock_once``,
``test_concurrent_checkouts_redeem_a_reward_once``,
``test_a_reward_is_bound_to_one_order_under_contention``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import ORMExecuteState

from app.config import Settings
from app.models.cart import CartItem
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    RewardStatus,
    RewardType,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.product import Product
from app.models.reward import UserReward
from app.models.user import User
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.services.admin import AdminService, InvalidStatusTransitionError
from app.services.cart import CartService
from app.services.loyalty import InsufficientStampsError, StaleCardError
from app.services.order import OrderService
from app.services.reward import RewardService, RewardUnavailableError
from app.services.stamp_card import PurchaseAwardStatus, StampCardService
from tests.factories import make_category, make_product, make_user

PURCHASE = LoyaltyTransactionType.PURCHASE
REDEMPTION = LoyaltyTransactionType.REDEMPTION
LOYALTY_TABLES = frozenset(
    {
        "loyalty_accounts",
        "loyalty_transactions",
        "user_rewards",
        "roulette_spin_grants",
        "roulette_spins",
        "referrals",
    }
)
_WRITE = re.compile(r"\s*(INSERT INTO|UPDATE|DELETE FROM)\s+\"?(\w+)", re.IGNORECASE)


# ------------------------------------------------------------------ helpers


def configured_admin(session: AsyncSession) -> AdminService:
    """The admin service exactly as the status-change handler builds it."""
    settings = Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
    )
    return AdminService(session, settings=settings)


async def a_bottle(session: AsyncSession) -> Product:
    category = await make_category(session, name="Liquids")
    return await make_product(session, category, name_en="Bottle", price="20.00")


async def fill(session: AsyncSession, user: User, bottle: Product, quantity: int) -> None:
    await CartService(session).add_product(user.id, bottle, quantity=quantity)


async def place(session: AsyncSession, user: User, *, reward_id: int | None = None) -> Order:
    """The customer confirms checkout. Commits, as production does."""
    return await OrderService(session).place_order_from_cart(
        user,
        customer_name="Anna",
        delivery_type="pickup",
        address="Street 1",
        preferred_time="18:00",
        phone=None,
        payment_method=PaymentMethod.CASH,
        reward_id=reward_id,
    )


async def ship(
    session: AsyncSession,
    user: User,
    bottle: Product,
    quantity: int,
    *,
    reward_id: int | None = None,
) -> Order:
    """``quantity`` €20 bottles, ordered, accepted and shipped: one tap from Completed."""
    await fill(session, user, bottle, quantity)
    order = await place(session, user, reward_id=reward_id)
    admin = configured_admin(session)
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED):
        order = await admin.set_order_status(order, status)
    return order


async def buy(
    session: AsyncSession,
    user: User,
    bottle: Product,
    quantity: int,
    *,
    reward_id: int | None = None,
) -> Order:
    """Checkout, then the admin takes the order all the way to Completed."""
    order = await ship(session, user, bottle, quantity, reward_id=reward_id)
    return await configured_admin(session).set_order_status(order, OrderStatus.COMPLETED)


async def card_state(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """(stamp balance, qualifying purchases), straight from the database."""
    row = (
        await session.execute(
            select(LoyaltyAccount.stamp_balance, LoyaltyAccount.qualifying_purchase_count).where(
                LoyaltyAccount.user_id == user_id
            )
        )
    ).one_or_none()
    return (0, 0) if row is None else (row[0], row[1])


async def ledger(session: AsyncSession, user_id: int) -> list[tuple[Any, ...]]:
    """(kind, amount, balance_after, order_id, reward_id) for each row, oldest first."""
    rows = await session.execute(
        select(
            LoyaltyTransaction.kind,
            LoyaltyTransaction.amount,
            LoyaltyTransaction.balance_after,
            LoyaltyTransaction.order_id,
            LoyaltyTransaction.reward_id,
        )
        .where(LoyaltyTransaction.user_id == user_id)
        .order_by(LoyaltyTransaction.id)
    )
    return [tuple(row) for row in rows]


async def count(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@contextmanager
def locks_and_writes(engine: AsyncEngine, session: AsyncSession) -> Iterator[list[str]]:
    """
    Record, in order, each row lock taken (``lock <table>``) and each write
    (``insert <table>``, ``update …``, ``delete …``).

    SQLite drops ``FOR UPDATE`` when it compiles a statement, so locks are read
    off the statement as PostgreSQL would receive it.
    """
    log: list[str] = []

    def on_select(state: ORMExecuteState) -> None:
        if (
            state.is_select
            and not state.is_relationship_load
            and "FOR UPDATE" in str(state.statement.compile(dialect=postgresql.dialect()))
        ):
            log.extend(f"lock {mapper.class_.__tablename__}" for mapper in state.all_mappers)

    def on_statement(_connection: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if match := _WRITE.match(statement):
            log.append(f"{match[1].split()[0].lower()} {match[2]}")

    event.listen(session.sync_session, "do_orm_execute", on_select)
    event.listen(engine.sync_engine, "before_cursor_execute", on_statement)
    try:
        yield log
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", on_statement)
        event.remove(session.sync_session, "do_orm_execute", on_select)


def first_loyalty_write(log: list[str]) -> int:
    """Index of the first write to a loyalty table, creating the account aside."""
    for index, entry in enumerate(log):
        verb, table = entry.split()
        if verb != "lock" and table in LOYALTY_TABLES and entry != "insert loyalty_accounts":
            return index
    raise AssertionError(f"no loyalty write in {log}")


def refuse_the_ledger_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Let every write before the ledger row reach the database, then have the
    database itself refuse the row (its ``balance_after >= 0`` CHECK).
    """
    original = LoyaltyTransactionRepository.create_and_add

    async def create_and_add(self: LoyaltyTransactionRepository, **fields: Any) -> Any:
        await self.session.flush()
        return await original(self, **(fields | {"balance_after": -1}))

    monkeypatch.setattr(LoyaltyTransactionRepository, "create_and_add", create_and_add)


# ---------------------------------------------------------------- 1-4: earning


async def test_scenario_1_a_20_euro_completed_order_earns_1_stamp(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8701)

    order = await buy(session, user, await a_bottle(session), 1)

    assert order.total_price == Decimal("20.00")
    assert await card_state(session, user.id) == (1, 1)
    assert await ledger(session, user.id) == [(PURCHASE, 1, 1, order.id, None)]


async def test_scenario_2_a_40_euro_completed_order_earns_2_stamps(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8702)

    order = await buy(session, user, await a_bottle(session), 2)

    assert order.total_price == Decimal("40.00")
    assert await card_state(session, user.id) == (2, 1)
    assert await ledger(session, user.id) == [(PURCHASE, 2, 2, order.id, None)]


async def test_scenario_3_a_duplicate_completion_event_books_nothing_twice(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=8703)
    order = await buy(session, user, await a_bottle(session), 2)
    booked = await ledger(session, user.id)

    # The same event again: a second tap on "Complete", or a second admin ...
    again = await configured_admin(session).set_order_status(order, OrderStatus.COMPLETED)
    # ... and a replayed completion reaching the engine directly.
    replay = await StampCardService(session).award_for_order(order.id)

    assert again.status == OrderStatus.COMPLETED
    assert (replay.status, replay.stamps) == (PurchaseAwardStatus.ALREADY_AWARDED, 2)
    assert await card_state(session, user.id) == (2, 1)
    assert await ledger(session, user.id) == booked


async def test_scenario_4_a_cancelled_order_earns_nothing(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8704)
    bottle = await a_bottle(session)
    admin = configured_admin(session)
    await fill(session, user, bottle, 2)
    at_once = await admin.set_order_status(await place(session, user), OrderStatus.CANCELLED)
    after_shipping = await admin.set_order_status(
        await ship(session, user, bottle, 2), OrderStatus.CANCELLED
    )

    for order in (at_once, after_shipping):
        award = await StampCardService(session).award_for_order(order.id)
        assert award.status == PurchaseAwardStatus.NOT_COMPLETED
    assert await card_state(session, user.id) == (0, 0)
    assert await ledger(session, user.id) == []

    # Completed is final, so booked stamps can never sit on a cancelled order.
    done = await buy(session, user, bottle, 1)
    with pytest.raises(InvalidStatusTransitionError):
        await admin.set_order_status(done, OrderStatus.CANCELLED)
    assert await card_state(session, user.id) == (1, 1)


# ---------------------------------------------------------- 5-7: free bottles


async def test_scenario_5_ten_stamps_make_a_reward_available(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8705)
    bottle = await a_bottle(session)
    stamp_card = StampCardService(session)

    await buy(session, user, bottle, 9)
    nine = await stamp_card.card(user.id)
    assert (nine.stamps, nine.can_claim) == (9, False)
    with pytest.raises(InsufficientStampsError):
        await stamp_card.claim_free_bottle(user.id, card_version=nine.version)
    assert await RewardService(session).list_available(user.id) == []

    await buy(session, user, bottle, 1)
    ten = await stamp_card.card(user.id)
    assert (ten.stamps, ten.can_claim, ten.free_bottles_unlocked) == (10, True, 1)
    reward = await stamp_card.claim_free_bottle(user.id, card_version=ten.version)

    assert (reward.kind, reward.status, reward.max_item_price) == (
        RewardType.FREE_BOTTLE,
        RewardStatus.AVAILABLE,
        Decimal("20.00"),
    )
    assert [r.id for r in await RewardService(session).list_available(user.id)] == [reward.id]


async def test_scenario_6_a_redemption_consumes_exactly_10_stamps(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8706)
    bottle = await a_bottle(session)
    await buy(session, user, bottle, 13)

    reward = await StampCardService(session).claim_free_bottle(user.id)

    assert await card_state(session, user.id) == (3, 1)
    assert (await ledger(session, user.id))[-1] == (REDEMPTION, -10, 3, None, reward.id)

    # Using the reward at checkout spends no further stamps, and the €0 bottle
    # earns none: two bottles, one charged, one stamp.
    order = await buy(session, user, bottle, 2, reward_id=reward.id)
    used = (
        await session.execute(
            select(
                UserReward.status,
                UserReward.order_id,
                UserReward.discount_amount,
                UserReward.redeemed_product_id,
            ).where(UserReward.id == reward.id)
        )
    ).one()
    assert tuple(used) == (RewardStatus.USED, order.id, Decimal("20.00"), bottle.id)
    assert order.total_price == Decimal("20.00")
    assert await card_state(session, user.id) == (4, 2)


async def test_scenario_7_a_duplicate_redemption_is_refused(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8707)
    bottle = await a_bottle(session)
    await buy(session, user, bottle, 20)
    stamp_card = StampCardService(session)

    # Claiming: one rendered card claims once, even with stamps for two.
    card = await stamp_card.card(user.id)
    reward = await stamp_card.claim_free_bottle(user.id, card_version=card.version)
    with pytest.raises(StaleCardError):
        await stamp_card.claim_free_bottle(user.id, card_version=card.version)
    assert await card_state(session, user.id) == (10, 1)
    assert await count(session, UserReward) == 1

    # Redeeming: a used reward cannot pay for a second order.
    await fill(session, user, bottle, 1)
    first = await place(session, user, reward_id=reward.id)
    await fill(session, user, bottle, 1)
    await session.commit()
    reward_id, first_id = reward.id, first.id
    with pytest.raises(RewardUnavailableError):
        await place(session, user, reward_id=reward_id)
    await session.rollback()  # what DatabaseMiddleware does with any exception

    assert await count(session, Order) == 2, "the refused checkout created no order"
    assert await count(session, CartItem) == 1, "and left the cart as it was"
    bound = await session.scalar(select(UserReward.order_id).where(UserReward.id == reward_id))
    assert bound == first_id


# ------------------------------------------------------------ 8: concurrency


async def test_scenario_8_every_redemption_path_locks_the_account_before_writing(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    user = await make_user(session, telegram_id=8708)
    bottle = await a_bottle(session)
    await buy(session, user, bottle, 10)
    await fill(session, user, bottle, 2)

    with locks_and_writes(engine, session) as claim:
        reward = await StampCardService(session).claim_free_bottle(user.id)
    with locks_and_writes(engine, session) as redeem:
        order = await place(session, user, reward_id=reward.id)
    admin = configured_admin(session)
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED):
        order = await admin.set_order_status(order, status)
    with locks_and_writes(engine, session) as completion:
        await admin.set_order_status(order, OrderStatus.COMPLETED)

    for log in (claim, redeem, completion):
        assert "lock loyalty_accounts" in log, log
        assert log.index("lock loyalty_accounts") < first_loyalty_write(log), log
    # The reward row is locked before it is bound ...
    assert redeem.index("lock user_rewards") < redeem.index("update user_rewards"), redeem
    # ... and completion takes the order before the account, never the reverse.
    assert completion.index("lock orders") < completion.index("lock loyalty_accounts"), completion


# ------------------------------------------------------- 9: failed transactions


async def test_scenario_9_a_failed_completion_leaves_no_partial_mutation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp write fails mid-completion: status, account and ledger all roll back."""
    user = await make_user(session, telegram_id=8709)
    order = await ship(session, user, await a_bottle(session), 2)
    await session.commit()
    user_id, order_id = user.id, order.id

    async def broken(self: LoyaltyTransactionRepository, **fields: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(LoyaltyTransactionRepository, "create_and_add", broken)
    admin = configured_admin(session)
    with pytest.raises(RuntimeError):
        await admin.set_order_status(order, OrderStatus.COMPLETED)
    await session.rollback()  # what DatabaseMiddleware does with any exception
    monkeypatch.undo()

    status = await session.scalar(select(Order.status).where(Order.id == order_id))
    assert status == OrderStatus.SHIPPED
    assert await card_state(session, user_id) == (0, 0)
    assert await ledger(session, user_id) == []

    # Nothing half-done is left to trip over: the retry books the order once.
    reloaded = await session.get(Order, order_id)
    assert reloaded is not None
    await admin.set_order_status(reloaded, OrderStatus.COMPLETED)
    assert await card_state(session, user_id) == (2, 1)
    assert await ledger(session, user_id) == [(PURCHASE, 2, 2, order_id, None)]


async def test_scenario_9_a_ledger_row_the_database_refuses_takes_the_balance_with_it(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failure inside the database: the balance and purchase-count UPDATE has
    already executed when a CHECK refuses the ledger row. Neither survives.
    """
    user = await make_user(session, telegram_id=8710)
    bottle = await a_bottle(session)
    await buy(session, user, bottle, 2)
    order = await ship(session, user, bottle, 3)
    await session.commit()
    user_id, order_id = user.id, order.id
    before = (await card_state(session, user_id), await ledger(session, user_id))
    assert before[0] == (2, 1)

    refuse_the_ledger_row(monkeypatch)
    with locks_and_writes(engine, session) as log, pytest.raises(IntegrityError):
        await configured_admin(session).set_order_status(order, OrderStatus.COMPLETED)
    await session.rollback()
    monkeypatch.undo()

    assert "update loyalty_accounts" in log, "the balance change did reach the database"
    assert (await card_state(session, user_id), await ledger(session, user_id)) == before
    status = await session.scalar(select(Order.status).where(Order.id == order_id))
    assert status == OrderStatus.SHIPPED


async def test_scenario_9_a_claim_whose_debit_the_database_refuses_leaves_no_reward(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await make_user(session, telegram_id=8711)
    await buy(session, user, await a_bottle(session), 10)
    await session.commit()
    user_id = user.id
    before = (await card_state(session, user_id), await ledger(session, user_id))

    refuse_the_ledger_row(monkeypatch)
    with locks_and_writes(engine, session) as log, pytest.raises(IntegrityError):
        await StampCardService(session).claim_free_bottle(user_id)
    await session.rollback()
    monkeypatch.undo()

    assert {"insert user_rewards", "update loyalty_accounts"} <= set(log), log
    assert (await card_state(session, user_id), await ledger(session, user_id)) == before
    assert await count(session, UserReward) == 0
