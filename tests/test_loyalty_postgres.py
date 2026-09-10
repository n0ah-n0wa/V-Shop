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
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (populates the metadata)
from app.database.base import Base
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    RewardSource,
    RewardType,
    RoulettePrizeType,
)
from app.models.loyalty import LoyaltyTransaction
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.services.loyalty import InsufficientStampsError, LedgerPosting, LoyaltyService
from app.services.referral import ReferralAttribution, ReferralService
from app.services.reward import RewardService, RewardUnavailableError
from app.services.roulette import RoulettePrize, RouletteService, SpinGrantResult
from tests.factories import make_order, make_user

URL = os.environ.get("VSHOP_TEST_POSTGRES_URL", "")
RACERS = 8
CEILING = Decimal("20.00")
STAMP_1 = RoulettePrize(code="stamp_1", kind=RoulettePrizeType.STAMPS, value=1)

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
        lambda s, _: LoyaltyService(s).redeem_free_bottle(
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
    async def build(session: AsyncSession) -> tuple[int, int, list[int]]:
        user = await make_user(session, telegram_id=9004)
        orders = [await make_order(session, user) for _ in range(RACERS)]
        loyalty = LoyaltyService(session)
        await loyalty.adjust(user.id, amount=10, note="test setup")
        reward = await loyalty.redeem_free_bottle(
            user.id, stamps_required=10, max_item_price=CEILING
        )
        return user.id, reward.id, [order.id for order in orders]

    user_id, reward_id, order_ids = await seed(pg, build)

    results = await race(
        pg,
        lambda s, i: RewardService(s).use_reward(reward_id, user_id=user_id, order_id=order_ids[i]),
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
