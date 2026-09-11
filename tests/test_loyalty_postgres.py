"""
Loyalty concurrency on a real PostgreSQL — opt-in.

Every other test runs on SQLite, which serialises all writers and silently
ignores ``SELECT … FOR UPDATE``. It therefore cannot show that the locking rule
in ``app/services/loyalty.py`` holds when requests really overlap. These tests
race independent transactions against each other on PostgreSQL and check that
exactly one of them wins every time.

They run only when ``VSHOP_TEST_POSTGRES_URL`` names a database whose name ends
in ``_test`` and which contains no tables. The schema is created here and
dropped afterwards, so never point it at a real deployment::

    VSHOP_TEST_POSTGRES_URL=postgresql+asyncpg://vshop:pw@127.0.0.1:55432/loyalty_test \\
        python -m pytest tests/test_loyalty_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from aiogram.types import User as TgUser
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (populates the metadata)
from app.database.base import Base
from app.models.cart import Cart
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    RewardSource,
    RewardType,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyTransaction
from app.models.order import Order
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.services.admin import AdminService, InvalidStatusTransitionError
from app.services.cart import CartService
from app.services.loyalty import (
    InsufficientStampsError,
    LedgerPosting,
    LoyaltyService,
    StaleCardError,
)
from app.services.order import EmptyCartError, OrderService
from app.services.referral import ReferralAttribution, ReferralService, referral_payload
from app.services.referral_program import ReferralOutcome, ReferralProgramService
from app.services.reward import RewardService, RewardUnavailableError
from app.services.roulette import RoulettePrize, RouletteService, SpinGrantResult, SpinOutcome
from app.services.roulette_engine import RouletteEngine, RoulettePolicy
from app.services.spin_entitlement import SpinEntitlementService
from app.services.stamp_card import StampCardService
from app.services.user import UserService
from tests.factories import add_order_item, make_category, make_order, make_product, make_user

URL = os.environ.get("VSHOP_TEST_POSTGRES_URL", "")
RACERS = 8
CEILING = Decimal("20.00")
STAMP_1 = RoulettePrize(code="stamp_1", kind=RoulettePrizeType.STAMPS, value=1)
POLICY = RoulettePolicy.defaults()

Factory = async_sessionmaker[AsyncSession]


def _refusal() -> str | None:
    if not URL:
        return "set VSHOP_TEST_POSTGRES_URL to run the PostgreSQL concurrency tests"
    url = make_url(URL)
    if url.get_backend_name() != "postgresql":
        return "VSHOP_TEST_POSTGRES_URL must be a PostgreSQL URL"
    if not (url.database or "").endswith("_test"):
        return "refusing: the test database name must end in _test"
    return None


pytestmark = pytest.mark.skipif(_refusal() is not None, reason=_refusal() or "")


@pytest_asyncio.fixture
async def pg() -> AsyncIterator[Factory]:
    engine = create_async_engine(URL)
    async with engine.connect() as connection:
        existing = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    if existing:
        await engine.dispose()
        pytest.skip(f"refusing: {make_url(URL).database} is not empty: {sorted(existing)[:5]}")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        # Only reached when this fixture created the schema itself.
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def seed(pg: Factory, build: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    async with pg() as session:
        value = await build(session)
        await session.commit()
        return value


async def race(pg: Factory, operation: Callable[[AsyncSession, int], Awaitable[Any]]) -> list[Any]:
    """Run ``operation`` in RACERS concurrent transactions; errors are results too."""

    async def one(index: int) -> Any:
        async with pg() as session:
            try:
                result = await operation(session, index)
                await session.commit()
                return result
            except Exception as exc:  # a loser's error is part of what we assert on
                await session.rollback()
                return exc

    return list(await asyncio.gather(*(one(index) for index in range(RACERS))))


async def scalar(pg: Factory, statement: Any) -> Any:
    async with pg() as session:
        return await session.scalar(statement)


def errors(results: list[Any]) -> list[Exception]:
    return [result for result in results if isinstance(result, Exception)]


# ------------------------------------------------------------------------------


async def test_a_replayed_purchase_is_booked_once(pg: Factory) -> None:
    async def build(session: AsyncSession) -> tuple[int, int]:
        user = await make_user(session, telegram_id=9001)
        order = await make_order(session, user, status=OrderStatus.COMPLETED)
        return user.id, order.id

    user_id, order_id = await seed(pg, build)

    results = await race(
        pg,
        lambda s, _: LoyaltyService(s).record_purchase(user_id, order_id=order_id, stamps=2),
    )

    assert errors(results) == []
    assert sum(isinstance(r, LedgerPosting) and r.created for r in results) == 1
    async with pg() as session:
        account = await LoyaltyService(session).get_or_create_account(user_id)
        assert (account.stamp_balance, account.qualifying_purchase_count) == (2, 1)
        assert await LoyaltyService(session).ledger_balance(user_id) == 2


async def test_concurrent_spins_spend_each_grant_once(pg: Factory) -> None:
    async def build(session: AsyncSession) -> int:
        user = await make_user(session, telegram_id=9002)
        roulette = RouletteService(session)
        await roulette.grant_initial_spin(user.id)
        for _ in range(2):
            order = await make_order(session, user)
            await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)
        return user.id

    user_id = await seed(pg, build)

    results = await race(pg, lambda s, _: RouletteService(s).spin(user_id, STAMP_1))

    assert errors(results) == []
    assert sum(result is not None for result in results) == 3
    assert await scalar(pg, select(func.count()).select_from(RouletteSpin)) == 3
    assert await scalar(pg, select(func.count(func.distinct(RouletteSpin.grant_id)))) == 3, (
        "no grant spent twice"
    )
    assert (
        await scalar(
            pg,
            select(func.count())
            .select_from(RouletteSpinGrant)
            .where(RouletteSpinGrant.consumed_at.is_(None)),
        )
        == 0
    )
    async with pg() as session:
        assert await LoyaltyService(session).balance(user_id) == 3
        assert await LoyaltyService(session).ledger_balance(user_id) == 3


async def test_concurrent_redemptions_cannot_overdraw(pg: Factory) -> None:
    async def build(session: AsyncSession) -> int:
        user = await make_user(session, telegram_id=9003)
        await LoyaltyService(session).adjust(user.id, amount=10, note="test setup")
        return user.id

    user_id = await seed(pg, build)

    results = await race(
        pg,
        lambda s, _: LoyaltyService(s).claim_free_bottle(
            user_id, stamps_required=10, max_item_price=CEILING
        ),
    )

    assert sum(isinstance(r, UserReward) for r in results) == 1
    assert sum(isinstance(r, InsufficientStampsError) for r in results) == RACERS - 1
    assert await scalar(pg, select(func.count()).select_from(UserReward)) == 1
    async with pg() as session:
        assert await LoyaltyService(session).balance(user_id) == 0
        assert await LoyaltyService(session).ledger_balance(user_id) == 0


async def test_a_reward_is_bound_to_one_order_under_contention(pg: Factory) -> None:
    async def build(session: AsyncSession) -> tuple[int, int, int, list[int]]:
        user = await make_user(session, telegram_id=9004)
        bottle = await make_product(session, await make_category(session), price="20.00")
        orders = [await make_order(session, user) for _ in range(RACERS)]
        for order in orders:
            await add_order_item(session, order, bottle, price="0.00")
        loyalty = LoyaltyService(session)
        await loyalty.adjust(user.id, amount=10, note="test setup")
        reward = await loyalty.claim_free_bottle(
            user.id, stamps_required=10, max_item_price=CEILING
        )
        return user.id, reward.id, bottle.id, [order.id for order in orders]

    user_id, reward_id, bottle_id, order_ids = await seed(pg, build)

    results = await race(
        pg,
        lambda s, i: RewardService(s).use_reward(
            reward_id,
            user_id=user_id,
            order_id=order_ids[i],
            discount_amount=CEILING,
            redeemed_product_id=bottle_id,
        ),
    )

    assert sum(isinstance(r, UserReward) for r in results) == 1
    assert sum(isinstance(r, RewardUnavailableError) for r in results) == RACERS - 1
    assert (
        await scalar(
            pg, select(func.count()).select_from(UserReward).where(UserReward.order_id.is_not(None))
        )
        == 1
    )


async def test_concurrent_welcome_spin_grants_create_one(pg: Factory) -> None:
    async def build(session: AsyncSession) -> int:
        return (await make_user(session, telegram_id=9005)).id

    user_id = await seed(pg, build)

    results = await race(pg, lambda s, _: RouletteService(s).grant_initial_spin(user_id))

    assert errors(results) == []
    assert sum(isinstance(r, SpinGrantResult) and r.created for r in results) == 1
    assert len({r.grant.id for r in results}) == 1
    assert await scalar(pg, select(func.count()).select_from(RouletteSpinGrant)) == 1


async def test_concurrent_attributions_keep_a_single_referrer(pg: Factory) -> None:
    async def build(session: AsyncSession) -> tuple[int, list[int]]:
        referred = await make_user(session, telegram_id=9006)
        referrers = [await make_user(session, telegram_id=9100 + i) for i in range(RACERS)]
        return referred.id, [user.id for user in referrers]

    referred_id, referrer_ids = await seed(pg, build)

    results = await race(
        pg,
        lambda s, i: ReferralService(s).attribute(
            referrer_user_id=referrer_ids[i], referred_user_id=referred_id
        ),
    )

    assert errors(results) == []
    assert sum(isinstance(r, ReferralAttribution) and r.created for r in results) == 1
    assert len({r.referral.id for r in results}) == 1
    assert await scalar(pg, select(func.count()).select_from(Referral)) == 1


async def test_a_referral_pays_out_once_under_contention(pg: Factory) -> None:
    """The flow the order-completion hook will run, raced against itself."""

    async def build(session: AsyncSession) -> tuple[int, int, int, int]:
        referrer = await make_user(session, telegram_id=9007)
        referred = await make_user(session, telegram_id=9008)
        order = await make_order(session, referred, status=OrderStatus.COMPLETED)
        referral = await ReferralService(session).attribute(
            referrer_user_id=referrer.id, referred_user_id=referred.id
        )
        return referrer.id, referred.id, referral.referral.id, order.id

    referrer_id, referred_id, referral_id, order_id = await seed(pg, build)

    async def settle(session: AsyncSession, _: int) -> bool:
        if not await ReferralService(session).qualify(referral_id, order_id=order_id):
            return False
        loyalty = LoyaltyService(session)
        await loyalty.credit_referral(referrer_id, referral_id=referral_id, stamps=2)
        await loyalty.credit_referral(referred_id, referral_id=referral_id, stamps=2)
        await RouletteService(session).grant_referral_spin(referrer_id, referral_id=referral_id)
        return True

    results = await race(pg, settle)

    assert errors(results) == []
    assert results.count(True) == 1
    async with pg() as session:
        loyalty = LoyaltyService(session)
        assert await loyalty.balance(referrer_id) == 2
        assert await loyalty.balance(referred_id) == 2
        assert await RouletteService(session).available_spins(referrer_id) == 1
    assert (
        await scalar(
            pg,
            select(func.count())
            .select_from(LoyaltyTransaction)
            .where(LoyaltyTransaction.referral_id == referral_id),
        )
        == 2
    )


async def test_postgres_refuses_cross_customer_references(pg: Factory) -> None:
    """The owner-checked composite foreign keys, on the real engine."""

    async def build(session: AsyncSession) -> tuple[int, int, int, int, int]:
        alice = await make_user(session, telegram_id=9009)
        mallory = await make_user(session, telegram_id=9010)
        roulette = RouletteService(session)
        alice_grant = (await roulette.grant_initial_spin(alice.id)).grant
        mallory_grant = (await roulette.grant_initial_spin(mallory.id)).grant
        # A spin and a reward with no effect rows yet, so only the owner check
        # can object to the references below.
        spin = RouletteSpin(
            user_id=alice.id,
            grant_id=alice_grant.id,
            prize_code="stamp_1",
            prize_type=RoulettePrizeType.STAMPS,
            prize_value=1,
        )
        reward = UserReward(
            user_id=alice.id,
            kind=RewardType.FREE_BOTTLE,
            value=1,
            max_item_price=CEILING,
            source=RewardSource.STAMP_CARD,
        )
        session.add_all([spin, reward])
        await session.flush()
        return alice.id, mallory.id, mallory_grant.id, spin.id, reward.id

    alice_id, mallory_id, mallory_grant_id, alice_spin_id, alice_reward_id = await seed(pg, build)

    attempts: dict[str, Callable[[], object]] = {
        "fk_roulette_spins_grant_owner": lambda: RouletteSpin(
            user_id=alice_id,
            grant_id=mallory_grant_id,
            prize_code="stamp_1",
            prize_type=RoulettePrizeType.STAMPS,
            prize_value=1,
        ),
        "fk_user_rewards_spin_owner": lambda: UserReward(
            user_id=mallory_id,
            kind=RewardType.DISCOUNT_PERCENT,
            value=5,
            source=RewardSource.ROULETTE,
            spin_id=alice_spin_id,
        ),
        "fk_loyalty_transactions_spin_owner": lambda: LoyaltyTransaction(
            user_id=mallory_id,
            kind=LoyaltyTransactionType.ROULETTE,
            amount=1,
            balance_after=1,
            spin_id=alice_spin_id,
        ),
        "fk_loyalty_transactions_reward_owner": lambda: LoyaltyTransaction(
            user_id=mallory_id,
            kind=LoyaltyTransactionType.REDEMPTION,
            amount=-1,
            balance_after=0,
            reward_id=alice_reward_id,
        ),
    }
    for constraint, make in attempts.items():
        async with pg() as session:
            session.add(make())
            with pytest.raises(IntegrityError, match=constraint):
                await session.flush()
            await session.rollback()


async def test_concurrent_claims_from_one_card_unlock_once(pg: Factory) -> None:
    """A double tap, for real: 20 stamps, one rendered card, many claims."""

    async def build(session: AsyncSession) -> tuple[int, int]:
        user = await make_user(session, telegram_id=9013)
        loyalty = LoyaltyService(session)
        await loyalty.adjust(user.id, amount=20, note="test setup")
        return user.id, await loyalty.ledger_version(user.id)

    user_id, version = await seed(pg, build)

    results = await race(
        pg,
        lambda s, _: StampCardService(s).claim_free_bottle(user_id, card_version=version),
    )

    assert sum(isinstance(r, UserReward) for r in results) == 1
    assert sum(isinstance(r, StaleCardError) for r in results) == RACERS - 1
    async with pg() as session:
        assert await LoyaltyService(session).balance(user_id) == 10
        assert await LoyaltyService(session).ledger_balance(user_id) == 10


async def test_concurrent_checkouts_redeem_a_reward_once(pg: Factory) -> None:
    async def build(session: AsyncSession) -> tuple[int, int]:
        user = await make_user(session, telegram_id=9014)
        bottle = await make_product(session, await make_category(session), price="20.00")
        await CartService(session).add_product(user.id, bottle, quantity=2)
        loyalty = LoyaltyService(session)
        await loyalty.adjust(user.id, amount=10, note="test setup")
        reward = await loyalty.claim_free_bottle(
            user.id, stamps_required=10, max_item_price=CEILING
        )
        return user.id, reward.id

    user_id, reward_id = await seed(pg, build)

    async def checkout(session: AsyncSession, _: int) -> Order:
        user = await session.get(User, user_id)
        assert user is not None
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

    results = await race(pg, checkout)

    orders = [r for r in results if isinstance(r, Order)]
    assert len(orders) == 1 and orders[0].total_price == Decimal("20.00")
    assert all(isinstance(e, EmptyCartError) for e in errors(results)), errors(results)
    assert (
        await scalar(
            pg,
            select(func.count()).select_from(UserReward).where(UserReward.status == "used"),
        )
        == 1
    )


async def _set_status(session: AsyncSession, order_id: int, target: OrderStatus) -> OrderStatus:
    admin = AdminService(session)
    order = await admin.get_order(order_id)
    assert order is not None
    return (await admin.set_order_status(order, target)).status


async def _shipped_order(pg: Factory, telegram_id: int) -> tuple[int, int]:
    async def build(session: AsyncSession) -> tuple[int, int]:
        user = await make_user(session, telegram_id=telegram_id)
        order = await make_order(session, user, status=OrderStatus.SHIPPED)
        order.total_price = Decimal("40.00")
        await session.flush()
        return user.id, order.id

    result: tuple[int, int] = await seed(pg, build)
    return result


async def test_racing_completions_award_once(pg: Factory) -> None:
    user_id, order_id = await _shipped_order(pg, 9011)

    results = await race(pg, lambda s, _: _set_status(s, order_id, OrderStatus.COMPLETED))

    assert errors(results) == []
    async with pg() as session:
        assert await LoyaltyService(session).balance(user_id) == 2
        assert await LoyaltyService(session).ledger_balance(user_id) == 2
    assert (
        await scalar(
            pg,
            select(func.count())
            .select_from(LoyaltyTransaction)
            .where(LoyaltyTransaction.order_id == order_id),
        )
        == 1
    )


async def test_a_racing_cancel_cannot_undo_a_completed_award(pg: Factory) -> None:
    """
    Complete and cancel race on one Shipped order. Whichever commits first wins;
    the other is refused against the locked, re-read status — so stamps exist if
    and only if the order ends Completed.
    """
    user_id, order_id = await _shipped_order(pg, 9012)

    results = await race(
        pg,
        lambda s, i: _set_status(
            s, order_id, OrderStatus.COMPLETED if i % 2 == 0 else OrderStatus.CANCELLED
        ),
    )

    assert all(isinstance(r, InvalidStatusTransitionError) for r in errors(results)), errors(
        results
    )
    final = await scalar(pg, select(Order.status).where(Order.id == order_id))
    booked = await scalar(
        pg,
        select(func.count())
        .select_from(LoyaltyTransaction)
        .where(LoyaltyTransaction.order_id == order_id),
    )
    assert (final == OrderStatus.COMPLETED) == (booked == 1)
    assert booked in (0, 1)
    async with pg() as session:
        assert await LoyaltyService(session).balance(user_id) == 2 * booked


# --------------------------------------------------------------- spin grants


async def _customer_with_purchases(
    pg: Factory, telegram_id: int, *, purchases: int, shipped: int
) -> tuple[int, list[int]]:
    """A customer with ``purchases`` completed purchases and ``shipped`` orders to complete."""

    async def build(session: AsyncSession) -> tuple[int, list[int]]:
        user = await make_user(session, telegram_id=telegram_id)
        admin = AdminService(session)
        for _ in range(purchases):
            order = await make_order(session, user)
            order.total_price = Decimal("20.00")
            for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
                order = await admin.set_order_status(order, status)
        pending = []
        for _ in range(shipped):
            order = await make_order(session, user, status=OrderStatus.SHIPPED)
            order.total_price = Decimal("20.00")
            pending.append(order)
        await session.flush()
        return user.id, [order.id for order in pending]

    result: tuple[int, list[int]] = await seed(pg, build)
    return result


async def _milestone_orders(pg: Factory, user_id: int) -> list[int | None]:
    async with pg() as session:
        rows = await session.scalars(
            select(RouletteSpinGrant.order_id).where(
                RouletteSpinGrant.user_id == user_id,
                RouletteSpinGrant.reason == SpinGrantReason.PURCHASE_MILESTONE,
            )
        )
        return list(rows.all())


async def test_racing_completions_of_a_fifth_order_grant_one_spin(pg: Factory) -> None:
    user_id, (fifth,) = await _customer_with_purchases(pg, 9015, purchases=4, shipped=1)

    results = await race(pg, lambda s, _: _set_status(s, fifth, OrderStatus.COMPLETED))

    assert errors(results) == []
    assert await _milestone_orders(pg, user_id) == [fifth]


async def test_a_fifth_and_sixth_order_completing_together_grant_one_spin(pg: Factory) -> None:
    """Which order is the 5th purchase is settled under the account lock, not by luck."""
    user_id, orders = await _customer_with_purchases(pg, 9016, purchases=4, shipped=2)

    results = await race(pg, lambda s, i: _set_status(s, orders[i % 2], OrderStatus.COMPLETED))

    assert errors(results) == []
    granted = await _milestone_orders(pg, user_id)
    assert len(granted) == 1 and granted[0] in orders
    purchases = await scalar(
        pg,
        select(func.count())
        .select_from(LoyaltyTransaction)
        .where(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.kind == LoyaltyTransactionType.PURCHASE,
        ),
    )
    assert purchases == 6


async def test_the_start_up_top_up_racing_starts_grants_one_welcome_spin_each(
    pg: Factory,
) -> None:
    async def build(session: AsyncSession) -> list[int]:
        return [(await make_user(session, telegram_id=9200 + i)).id for i in range(RACERS)]

    user_ids: list[int] = await seed(pg, build)

    async def grant(session: AsyncSession, index: int) -> object:
        spins = SpinEntitlementService(session)
        if index % 2 == 0:
            return await spins.grant_missing_welcome_spins()  # a bot start
        return await spins.grant_welcome_spin(user_ids[index])  # a /start

    results = await race(pg, grant)

    assert errors(results) == []
    async with pg() as session:
        per_user = await session.execute(
            select(RouletteSpinGrant.user_id, func.count())
            .where(RouletteSpinGrant.reason == SpinGrantReason.INITIAL_PROMO)
            .group_by(RouletteSpinGrant.user_id)
        )
        assert {user_id: count for user_id, count in per_user.all()} == dict.fromkeys(user_ids, 1)


# ------------------------------------------------------------ the prize engine


async def screen_spin(session: AsyncSession, user_id: int) -> SpinOutcome | None:
    """What the roulette screen will do: offer the next grant, then spend exactly it."""
    engine = RouletteEngine(session, POLICY)
    grant_id = await engine.next_grant_id(user_id)
    return None if grant_id is None else await engine.spin(user_id, grant_id=grant_id)


async def test_racing_spins_on_one_grant_draw_one_prize(pg: Factory) -> None:
    async def build(session: AsyncSession) -> int:
        user = await make_user(session, telegram_id=9017)
        await RouletteService(session).grant_initial_spin(user.id)
        return user.id

    user_id = await seed(pg, build)

    results = await race(pg, lambda s, _: screen_spin(s, user_id))

    assert errors(results) == []
    outcomes = [r for r in results if isinstance(r, SpinOutcome)]
    assert sum(outcome.created for outcome in outcomes) == 1
    assert len({outcome.spin.id for outcome in outcomes}) == 1, "a late tap sees the same spin"
    assert len(outcomes) + results.count(None) == RACERS
    assert await scalar(pg, select(func.count()).select_from(RouletteSpin)) == 1


async def test_a_double_tap_on_one_grant_replays_instead_of_spending_another(
    pg: Factory,
) -> None:
    async def build(session: AsyncSession) -> tuple[int, int]:
        user = await make_user(session, telegram_id=9018)
        roulette = RouletteService(session)
        first = (await roulette.grant_initial_spin(user.id)).grant
        order = await make_order(session, user)
        await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)
        return user.id, first.id

    user_id, grant_id = await seed(pg, build)

    results = await race(
        pg, lambda s, _: RouletteEngine(s, POLICY).spin(user_id, grant_id=grant_id)
    )

    assert errors(results) == []
    outcomes = [r for r in results if isinstance(r, SpinOutcome)]
    assert len(outcomes) == RACERS
    assert sum(outcome.created for outcome in outcomes) == 1
    assert len({outcome.spin.id for outcome in outcomes}) == 1, "every tap sees the same spin"
    async with pg() as session:
        assert await RouletteService(session).available_spins(user_id) == 1


async def test_grants_and_spins_racing_keep_the_balance_exact(pg: Factory) -> None:
    """Spins earned while others are spent: no grant spent twice, none lost."""

    async def build(session: AsyncSession) -> tuple[int, list[int]]:
        user = await make_user(session, telegram_id=9019)
        await RouletteService(session).grant_initial_spin(user.id)
        orders = [await make_order(session, user) for _ in range(RACERS // 2)]
        return user.id, [order.id for order in orders]

    user_id, order_ids = await seed(pg, build)

    async def earn_or_spend(session: AsyncSession, index: int) -> object:
        if index % 2 == 0:
            return await RouletteService(session).grant_purchase_milestone_spin(
                user_id, order_id=order_ids[index // 2]
            )
        return await screen_spin(session, user_id)

    results = await race(pg, earn_or_spend)

    assert errors(results) == []
    granted = 1 + len(order_ids)
    created = sum(isinstance(r, SpinOutcome) and r.created for r in results)
    spins = await scalar(pg, select(func.count()).select_from(RouletteSpin))
    consumed = await scalar(
        pg,
        select(func.count())
        .select_from(RouletteSpinGrant)
        .where(RouletteSpinGrant.consumed_at.is_not(None)),
    )
    assert await scalar(pg, select(func.count()).select_from(RouletteSpinGrant)) == granted
    assert spins == consumed == created >= 1
    assert await scalar(pg, select(func.count(func.distinct(RouletteSpin.grant_id)))) == spins
    async with pg() as session:
        assert await RouletteService(session).available_spins(user_id) == granted - spins
        loyalty = LoyaltyService(session)
        assert await loyalty.balance(user_id) == await loyalty.ledger_balance(user_id)


async def test_a_first_start_delivered_many_times_onboards_once(pg: Factory) -> None:
    """One brand-new customer's /start, processed repeatedly and concurrently."""
    tg_user = TgUser(id=9020, is_bot=False, first_name="Anna", username="anna")

    async def start(session: AsyncSession, _: int) -> int:
        user = await UserService(session).ensure_user(tg_user)
        await SpinEntitlementService(session).grant_welcome_spin(user.id)
        return user.id

    results = await race(pg, start)

    assert errors(results) == []
    assert len(set(results)) == 1, "every delivery found the same customer"
    assert await scalar(pg, select(func.count()).select_from(User)) == 1
    assert await scalar(pg, select(func.count()).select_from(Cart)) == 1
    assert await scalar(pg, select(func.count()).select_from(RouletteSpinGrant)) == 1


# ----------------------------------------------------------------- referrals


async def test_a_first_start_through_a_link_attributes_once(pg: Factory) -> None:
    """A brand-new customer's /start ref_<code>, delivered many times at once."""

    async def build(session: AsyncSession) -> str:
        referrer = await make_user(session, telegram_id=9021)
        return referral_payload(await ReferralProgramService(session).referral_code(referrer.id))

    payload: str = await seed(pg, build)
    newcomer = TgUser(id=9022, is_bot=False, first_name="Friend")

    async def start(session: AsyncSession, _: int) -> ReferralOutcome:
        user = await UserService(session).ensure_user(newcomer)
        attempt = await ReferralProgramService(session).attribute_from_start(user.id, payload)
        return attempt.outcome

    results = await race(pg, start)

    assert errors(results) == []
    assert results.count(ReferralOutcome.ATTRIBUTED) == 1
    assert results.count(ReferralOutcome.ALREADY_REFERRED) == RACERS - 1
    assert await scalar(pg, select(func.count()).select_from(Referral)) == 1


async def _referred_customer(
    pg: Factory, telegram_id: int, *, shipped: int
) -> tuple[int, int, list[int]]:
    """A referrer, their attributed friend, and the friend's shipped €20 orders."""

    async def build(session: AsyncSession) -> tuple[int, int, list[int]]:
        referrer = await make_user(session, telegram_id=telegram_id)
        friend = await make_user(session, telegram_id=telegram_id + 1)
        await ReferralService(session).attribute(
            referrer_user_id=referrer.id, referred_user_id=friend.id
        )
        orders = []
        for _ in range(shipped):
            order = await make_order(session, friend, status=OrderStatus.SHIPPED)
            order.total_price = Decimal("20.00")
            orders.append(order)
        await session.flush()
        return referrer.id, friend.id, [order.id for order in orders]

    result: tuple[int, int, list[int]] = await seed(pg, build)
    return result


async def _paid_once(pg: Factory, referrer_id: int, friend_id: int) -> None:
    for user_id in (referrer_id, friend_id):
        bonuses = await scalar(
            pg,
            select(func.count())
            .select_from(LoyaltyTransaction)
            .where(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.kind == LoyaltyTransactionType.REFERRAL,
            ),
        )
        assert bonuses == 1, f"user {user_id}: exactly one referral bonus"
    spins = await scalar(
        pg,
        select(func.count())
        .select_from(RouletteSpinGrant)
        .where(RouletteSpinGrant.reason == SpinGrantReason.REFERRAL),
    )
    assert spins == 1
    async with pg() as session:
        loyalty = LoyaltyService(session)
        assert await loyalty.balance(referrer_id) == 2
        assert await loyalty.balance(friend_id) == await loyalty.ledger_balance(friend_id)


async def test_racing_completions_of_the_first_order_pay_the_referral_once(pg: Factory) -> None:
    referrer_id, friend_id, (order_id,) = await _referred_customer(pg, 9023, shipped=1)

    results = await race(pg, lambda s, _: _set_status(s, order_id, OrderStatus.COMPLETED))

    assert errors(results) == []
    await _paid_once(pg, referrer_id, friend_id)


async def test_two_first_orders_completing_together_pay_the_referral_once(pg: Factory) -> None:
    """Which order qualifies the referral is settled under its row lock, not by luck."""
    referrer_id, friend_id, orders = await _referred_customer(pg, 9025, shipped=2)

    results = await race(pg, lambda s, i: _set_status(s, orders[i % 2], OrderStatus.COMPLETED))

    assert errors(results) == []
    await _paid_once(pg, referrer_id, friend_id)
    qualifying = await scalar(
        pg, select(Referral.qualifying_order_id).where(Referral.referred_user_id == friend_id)
    )
    assert qualifying in orders


async def test_crossed_links_opened_together_never_form_a_loop(pg: Factory) -> None:
    """Two brand-new customers open each other's link at the same moment."""

    async def build(session: AsyncSession) -> tuple[int, int, str, str]:
        alice = await make_user(session, telegram_id=9027)
        bob = await make_user(session, telegram_id=9028)
        programme = ReferralProgramService(session)
        return (
            alice.id,
            bob.id,
            referral_payload(await programme.referral_code(alice.id)),
            referral_payload(await programme.referral_code(bob.id)),
        )

    alice_id, bob_id, alice_link, bob_link = await seed(pg, build)

    async def open_link(session: AsyncSession, index: int) -> ReferralOutcome:
        programme = ReferralProgramService(session)
        if index % 2 == 0:
            return (await programme.attribute_from_start(alice_id, bob_link)).outcome
        return (await programme.attribute_from_start(bob_id, alice_link)).outcome

    results = await race(pg, open_link)

    assert errors(results) == []
    assert await scalar(pg, select(func.count()).select_from(Referral)) == 1, (
        "one direction wins; the other would close a loop"
    )
    assert results.count(ReferralOutcome.ATTRIBUTED) == 1


async def test_a_chain_closed_from_both_ends_at_once_never_loops(pg: Factory) -> None:
    """
    Alice referred Bob and Carol referred Dave. At the same moment Carol opens
    Bob's link and Alice opens Dave's: both would close Alice → Bob → Carol →
    Dave → Alice. Locking only the two customers of each referral cannot see
    that — the two attempts share nobody.
    """

    async def build(session: AsyncSession) -> tuple[int, int, str, str]:
        alice, bob, carol, dave = [await make_user(session, telegram_id=9029 + i) for i in range(4)]
        referrals = ReferralService(session)
        await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
        await referrals.attribute(referrer_user_id=carol.id, referred_user_id=dave.id)
        programme = ReferralProgramService(session)
        return (
            alice.id,
            carol.id,
            referral_payload(await programme.referral_code(bob.id)),
            referral_payload(await programme.referral_code(dave.id)),
        )

    alice_id, carol_id, bob_link, dave_link = await seed(pg, build)

    async def open_link(session: AsyncSession, index: int) -> ReferralOutcome:
        programme = ReferralProgramService(session)
        if index % 2 == 0:
            return (await programme.attribute_from_start(carol_id, bob_link)).outcome
        return (await programme.attribute_from_start(alice_id, dave_link)).outcome

    results = await race(pg, open_link)

    assert errors(results) == []
    assert await scalar(pg, select(func.count()).select_from(Referral)) == 3, (
        "exactly one of the two closing referrals is recorded"
    )
    assert ReferralOutcome.LOOP in results
