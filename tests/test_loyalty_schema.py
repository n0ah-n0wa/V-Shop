"""
The loyalty schema's own guarantees, proven at the database level.

Rows are inserted directly, bypassing the services, because the constraints
exist to hold even when application code is wrong. SQLite enforces CHECK and
UNIQUE constraints — partial unique indexes included — so every rule here runs
on every test run. SQLite does not enforce foreign keys; the PostgreSQL checks
cover those (``tests/test_loyalty_postgres.py`` and the migration round trip).

The second half pins the migration to the models: ``alembic check`` compares
tables, columns, indexes and unique constraints, but not CHECK constraints, and
the migration must not touch any table it did not create.
"""

from __future__ import annotations

import ast
import pathlib
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, UniqueConstraint, delete, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (populates the metadata)
from app.database.base import Base
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    ReferralStatus,
    RewardSource,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.product import Product
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from tests.factories import make_category, make_order, make_product, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "3b9d6f2a8c14_loyalty_foundation.py"
# Later migrations that extend the loyalty tables (never an existing table).
REDEMPTION_MIGRATION = ROOT / "alembic" / "versions" / "c5d2e8f1a6b3_reward_redemption_record.py"
LOYALTY_TABLES = frozenset(
    {
        "loyalty_accounts",
        "loyalty_transactions",
        "referrals",
        "roulette_spin_grants",
        "roulette_spins",
        "user_rewards",
    }
)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
CEILING = Decimal("20.00")


async def rejected(session: AsyncSession, *entities: object) -> bool:
    """Whether the database refuses the rows. The session stays usable either way."""
    try:
        async with session.begin_nested():
            session.add_all(entities)
            await session.flush()
    except IntegrityError:
        return True
    return False


@dataclass
class World:
    alice: User
    bob: User
    order: Order
    other_order: Order
    referral: Referral
    grant: RouletteSpinGrant
    spin: RouletteSpin
    spare_spin: RouletteSpin
    reward: UserReward
    product: Product


async def _spin(session: AsyncSession, user: User, reason: SpinGrantReason) -> RouletteSpin:
    grant = RouletteSpinGrant(user_id=user.id, reason=reason)
    session.add(grant)
    await session.flush()
    spin = RouletteSpin(
        user_id=user.id,
        grant_id=grant.id,
        prize_code="discount_5",
        prize_type=RoulettePrizeType.DISCOUNT_PERCENT,
        prize_value=5,
    )
    session.add(spin)
    await session.flush()
    return spin


@pytest_asyncio.fixture
async def world(session: AsyncSession) -> World:
    """Alice referred Bob; Alice has one spin with a reward and one without."""
    alice = await make_user(session, telegram_id=7001)
    bob = await make_user(session, telegram_id=7002)
    order = await make_order(session, alice, status=OrderStatus.COMPLETED)
    other_order = await make_order(session, alice)
    referral = Referral(referrer_user_id=alice.id, referred_user_id=bob.id)
    session.add(referral)
    await session.flush()

    spin = await _spin(session, alice, SpinGrantReason.INITIAL_PROMO)
    grant = await session.get(RouletteSpinGrant, spin.grant_id)
    assert grant is not None
    reward = UserReward(
        user_id=alice.id,
        kind=RewardType.DISCOUNT_PERCENT,
        value=5,
        source=RewardSource.ROULETTE,
        spin_id=spin.id,
    )
    session.add(reward)
    await session.flush()

    milestone = RouletteSpinGrant(
        user_id=alice.id,
        reason=SpinGrantReason.PURCHASE_MILESTONE,
        order_id=other_order.id,
    )
    session.add(milestone)
    await session.flush()
    spare_spin = RouletteSpin(
        user_id=alice.id,
        grant_id=milestone.id,
        prize_code="stamp_1",
        prize_type=RoulettePrizeType.STAMPS,
        prize_value=1,
    )
    session.add(spare_spin)
    await session.flush()
    product = await make_product(session, await make_category(session, name="Liquids"))
    return World(alice, bob, order, other_order, referral, grant, spin, spare_spin, reward, product)


# --------------------------------------------------------------------- accounts


async def test_account_invariants(session: AsyncSession, world: World) -> None:
    assert not await rejected(session, LoyaltyAccount(user_id=world.alice.id))
    assert await rejected(session, LoyaltyAccount(user_id=world.alice.id)), "one per user"
    assert await rejected(session, LoyaltyAccount(user_id=world.bob.id, stamp_balance=-1))
    assert await rejected(
        session, LoyaltyAccount(user_id=world.bob.id, qualifying_purchase_count=-1)
    )


async def test_referral_codes_are_optional_but_unique(session: AsyncSession) -> None:
    users = [await make_user(session, telegram_id=7100 + i) for i in range(4)]
    assert not await rejected(
        session,
        LoyaltyAccount(user_id=users[0].id),
        LoyaltyAccount(user_id=users[1].id),
    ), "any number of accounts may be without a code"
    assert not await rejected(
        session, LoyaltyAccount(user_id=users[2].id, referral_code="Ab-12_xyz")
    )
    assert await rejected(session, LoyaltyAccount(user_id=users[3].id, referral_code="Ab-12_xyz"))


# ----------------------------------------------------------------------- ledger


def entry(world: World, **overrides: Any) -> LoyaltyTransaction:
    fields: dict[str, Any] = {
        "user_id": world.alice.id,
        "kind": LoyaltyTransactionType.ADJUSTMENT,
        "amount": 1,
        "balance_after": 1,
        "note": "test",
    }
    fields.update(overrides)
    return LoyaltyTransaction(**fields)


async def test_every_ledger_kind_is_accepted_with_its_own_source(
    session: AsyncSession, world: World
) -> None:
    """Also proves the enum values and the CHECK literals agree."""
    rows = [
        entry(world, kind=LoyaltyTransactionType.PURCHASE, amount=0, order_id=world.order.id),
        entry(world, kind=LoyaltyTransactionType.REFERRAL, amount=2, referral_id=world.referral.id),
        entry(world, kind=LoyaltyTransactionType.ROULETTE, amount=1, spin_id=world.spare_spin.id),
        entry(world, kind=LoyaltyTransactionType.REDEMPTION, amount=-3, reward_id=world.reward.id),
        entry(world, kind=LoyaltyTransactionType.ADJUSTMENT, amount=-5, note="correction"),
    ]
    for row in rows:
        assert not await rejected(session, row), row


INVALID_LEDGER_ROWS: dict[str, Callable[[World], LoyaltyTransaction]] = {
    "purchase without an order": lambda w: entry(w, kind=LoyaltyTransactionType.PURCHASE),
    "purchase with negative stamps": lambda w: entry(
        w, kind=LoyaltyTransactionType.PURCHASE, amount=-1, order_id=w.order.id
    ),
    "purchase that also names a referral": lambda w: entry(
        w,
        kind=LoyaltyTransactionType.PURCHASE,
        order_id=w.order.id,
        referral_id=w.referral.id,
    ),
    "referral bonus of zero": lambda w: entry(
        w, kind=LoyaltyTransactionType.REFERRAL, amount=0, referral_id=w.referral.id
    ),
    "roulette stamps without a spin": lambda w: entry(w, kind=LoyaltyTransactionType.ROULETTE),
    "redemption that adds stamps": lambda w: entry(
        w, kind=LoyaltyTransactionType.REDEMPTION, reward_id=w.reward.id
    ),
    "adjustment without a note": lambda w: entry(w, note=None),
    "adjustment of zero": lambda w: entry(w, amount=0),
    "adjustment pointing at an order": lambda w: entry(w, order_id=w.order.id),
    "negative running balance": lambda w: entry(w, amount=-1, balance_after=-1),
}


@pytest.mark.parametrize("case", sorted(INVALID_LEDGER_ROWS))
async def test_inconsistent_ledger_rows_are_refused(
    session: AsyncSession, world: World, case: str
) -> None:
    assert await rejected(session, INVALID_LEDGER_ROWS[case](world))


async def test_each_ledger_event_is_bookable_once(session: AsyncSession, world: World) -> None:
    def purchase() -> LoyaltyTransaction:
        return entry(world, kind=LoyaltyTransactionType.PURCHASE, order_id=world.order.id)

    def referral_bonus(user_id: int) -> LoyaltyTransaction:
        return entry(
            world,
            user_id=user_id,
            kind=LoyaltyTransactionType.REFERRAL,
            amount=2,
            referral_id=world.referral.id,
        )

    def spin_stamps() -> LoyaltyTransaction:
        return entry(world, kind=LoyaltyTransactionType.ROULETTE, spin_id=world.spare_spin.id)

    def redemption() -> LoyaltyTransaction:
        return entry(
            world, kind=LoyaltyTransactionType.REDEMPTION, amount=-1, reward_id=world.reward.id
        )

    for make in (purchase, spin_stamps, redemption):
        assert not await rejected(session, make())
        assert await rejected(session, make()), make.__name__

    assert not await rejected(session, referral_bonus(world.alice.id))
    assert not await rejected(session, referral_bonus(world.bob.id)), (
        "the other side is its own event"
    )
    assert await rejected(session, referral_bonus(world.alice.id))


# ---------------------------------------------------------------- spin grants


async def test_one_welcome_spin_per_customer(session: AsyncSession, world: World) -> None:
    def initial(user: User) -> RouletteSpinGrant:
        return RouletteSpinGrant(user_id=user.id, reason=SpinGrantReason.INITIAL_PROMO)

    assert await rejected(session, initial(world.alice)), "the world already gave Alice one"
    assert not await rejected(session, initial(world.bob))
    assert await rejected(session, initial(world.bob))


async def test_grant_sources_are_consistent_and_unique(session: AsyncSession, world: World) -> None:
    def grant(user: User, reason: SpinGrantReason, **source: int) -> RouletteSpinGrant:
        return RouletteSpinGrant(user_id=user.id, reason=reason, **source)

    milestone = SpinGrantReason.PURCHASE_MILESTONE
    referral = SpinGrantReason.REFERRAL

    assert await rejected(session, grant(world.alice, milestone)), "milestone without an order"
    assert await rejected(
        session, grant(world.bob, SpinGrantReason.INITIAL_PROMO, order_id=world.order.id)
    ), "welcome spin cannot point at an order"
    assert await rejected(session, grant(world.alice, referral)), "referral grant without referral"

    assert not await rejected(session, grant(world.alice, milestone, order_id=world.order.id))
    assert await rejected(session, grant(world.alice, milestone, order_id=world.order.id))

    assert not await rejected(session, grant(world.alice, referral, referral_id=world.referral.id))
    assert not await rejected(session, grant(world.bob, referral, referral_id=world.referral.id))
    assert await rejected(session, grant(world.alice, referral, referral_id=world.referral.id))


# ----------------------------------------------------------------------- spins


async def test_a_grant_is_spent_once(session: AsyncSession, world: World) -> None:
    again = RouletteSpin(
        user_id=world.alice.id,
        grant_id=world.grant.id,
        prize_code="stamp_2",
        prize_type=RoulettePrizeType.STAMPS,
        prize_value=2,
    )
    assert await rejected(session, again)


async def test_a_spin_prize_is_positive(session: AsyncSession, world: World) -> None:
    grant = RouletteSpinGrant(user_id=world.bob.id, reason=SpinGrantReason.INITIAL_PROMO)
    session.add(grant)
    await session.flush()
    zero = RouletteSpin(
        user_id=world.bob.id,
        grant_id=grant.id,
        prize_code="stamp_0",
        prize_type=RoulettePrizeType.STAMPS,
        prize_value=0,
    )
    assert await rejected(session, zero)


# --------------------------------------------------------------------- rewards


def reward(world: World, **overrides: Any) -> UserReward:
    fields: dict[str, Any] = {
        "user_id": world.alice.id,
        "kind": RewardType.FREE_BOTTLE,
        "value": 1,
        "max_item_price": CEILING,
        "source": RewardSource.STAMP_CARD,
        "status": RewardStatus.AVAILABLE,
    }
    fields.update(overrides)
    return UserReward(**fields)


async def test_valid_rewards_are_accepted(session: AsyncSession, world: World) -> None:
    assert not await rejected(session, reward(world))
    assert not await rejected(
        session,
        reward(
            world,
            kind=RewardType.DISCOUNT_PERCENT,
            value=10,
            max_item_price=None,
            source=RewardSource.ROULETTE,
            spin_id=world.spare_spin.id,
        ),
    )
    assert not await rejected(session, used(world, world.order))
    assert not await rejected(
        session,
        reward(
            world,
            kind=RewardType.DISCOUNT_PERCENT,
            value=10,
            max_item_price=None,
            status=RewardStatus.USED,
            order_id=world.other_order.id,
            used_at=NOW,
            discount_amount=Decimal("3.20"),
        ),
    ), "a used discount records its value and names no product"


def used(world: World, order: Order, **overrides: Any) -> UserReward:
    """A properly redeemed free bottle."""
    fields: dict[str, Any] = {
        "status": RewardStatus.USED,
        "order_id": order.id,
        "used_at": NOW,
        "discount_amount": CEILING,
        "redeemed_product_id": world.product.id,
    }
    return reward(world, **(fields | overrides))


INVALID_REWARDS: dict[str, Callable[[World], UserReward]] = {
    "used without a discount record": lambda w: used(w, w.order, discount_amount=None),
    "used free bottle naming no product": lambda w: used(w, w.order, redeemed_product_id=None),
    "negative discount": lambda w: used(w, w.order, discount_amount=Decimal("-1.00")),
    "available but carrying a discount": lambda w: reward(w, discount_amount=CEILING),
    "available but naming a product": lambda w: reward(w, redeemed_product_id=w.product.id),
    "discount reward naming a product": lambda w: used(
        w, w.order, kind=RewardType.DISCOUNT_PERCENT, value=10, max_item_price=None
    ),
    "discount above 100%": lambda w: reward(
        w, kind=RewardType.DISCOUNT_PERCENT, value=101, max_item_price=None
    ),
    "discount of zero": lambda w: reward(
        w, kind=RewardType.DISCOUNT_PERCENT, value=0, max_item_price=None
    ),
    "discount with a price ceiling": lambda w: reward(w, kind=RewardType.DISCOUNT_PERCENT, value=5),
    "free bottle without a ceiling": lambda w: reward(w, max_item_price=None),
    "free bottle with a zero ceiling": lambda w: reward(w, max_item_price=Decimal("0.00")),
    "two bottles in one reward": lambda w: reward(w, value=2),
    "used without an order": lambda w: used(w, w.order, order_id=None),
    "used without a timestamp": lambda w: used(w, w.order, used_at=None),
    "available but bound to an order": lambda w: reward(w, order_id=w.order.id),
    "roulette reward without a spin": lambda w: reward(w, source=RewardSource.ROULETTE),
    "stamp-card reward pointing at a spin": lambda w: reward(w, spin_id=w.spare_spin.id),
}


@pytest.mark.parametrize("case", sorted(INVALID_REWARDS))
async def test_inconsistent_rewards_are_refused(
    session: AsyncSession, world: World, case: str
) -> None:
    assert await rejected(session, INVALID_REWARDS[case](world))


async def test_one_reward_per_order_and_per_spin(session: AsyncSession, world: World) -> None:
    assert not await rejected(session, used(world, world.order))
    assert await rejected(session, used(world, world.order)), "an order carries one reward"

    from_spin = reward(
        world,
        kind=RewardType.DISCOUNT_PERCENT,
        value=5,
        max_item_price=None,
        source=RewardSource.ROULETTE,
        spin_id=world.spin.id,
    )
    assert await rejected(session, from_spin), "the world's spin already produced a reward"


# ------------------------------------------------------------------- referrals


async def test_referral_rules(session: AsyncSession, world: World) -> None:
    carol = await make_user(session, telegram_id=7003)
    dave = await make_user(session, telegram_id=7004)

    assert await rejected(session, Referral(referrer_user_id=carol.id, referred_user_id=carol.id))
    assert await rejected(
        session, Referral(referrer_user_id=carol.id, referred_user_id=world.bob.id)
    ), "Bob already has a referrer"
    assert await rejected(
        session,
        Referral(
            referrer_user_id=world.alice.id,
            referred_user_id=carol.id,
            status=ReferralStatus.QUALIFIED,
        ),
    ), "qualified needs its order and time"
    assert await rejected(
        session,
        Referral(
            referrer_user_id=world.alice.id,
            referred_user_id=carol.id,
            qualifying_order_id=world.order.id,
        ),
    ), "a pending referral cannot carry an order"

    def qualified(referred: User) -> Referral:
        return Referral(
            referrer_user_id=world.alice.id,
            referred_user_id=referred.id,
            status=ReferralStatus.QUALIFIED,
            qualifying_order_id=world.order.id,
            qualified_at=NOW,
        )

    assert not await rejected(session, qualified(carol))
    assert await rejected(session, qualified(dave)), "one order qualifies one referral"


# ------------------------------------------------- foreign keys (enforced)


@pytest_asyncio.fixture
async def fk_session() -> AsyncIterator[AsyncSession]:
    """A SQLite session with foreign keys enforced — SQLite leaves them off by default."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_the_database_refuses_cross_customer_references(fk_session: AsyncSession) -> None:
    """A spin, reward or ledger row can only point at its own customer's rows."""
    session = fk_session
    alice = await make_user(session, telegram_id=7601)
    mallory = await make_user(session, telegram_id=7602)
    alice_spin = await _spin(session, alice, SpinGrantReason.INITIAL_PROMO)
    mallory_grant = RouletteSpinGrant(user_id=mallory.id, reason=SpinGrantReason.INITIAL_PROMO)
    alice_reward = UserReward(
        user_id=alice.id,
        kind=RewardType.FREE_BOTTLE,
        value=1,
        max_item_price=CEILING,
        source=RewardSource.STAMP_CARD,
    )
    session.add_all([mallory_grant, alice_reward])
    await session.flush()

    def spin_on(grant: RouletteSpinGrant, user: User) -> RouletteSpin:
        return RouletteSpin(
            user_id=user.id,
            grant_id=grant.id,
            prize_code="stamp_1",
            prize_type=RoulettePrizeType.STAMPS,
            prize_value=1,
        )

    def roulette_reward(user: User) -> UserReward:
        return UserReward(
            user_id=user.id,
            kind=RewardType.DISCOUNT_PERCENT,
            value=5,
            source=RewardSource.ROULETTE,
            spin_id=alice_spin.id,
        )

    def spin_stamps(user: User) -> LoyaltyTransaction:
        return LoyaltyTransaction(
            user_id=user.id,
            kind=LoyaltyTransactionType.ROULETTE,
            amount=1,
            balance_after=1,
            spin_id=alice_spin.id,
        )

    def redemption(user: User) -> LoyaltyTransaction:
        return LoyaltyTransaction(
            user_id=user.id,
            kind=LoyaltyTransactionType.REDEMPTION,
            amount=-1,
            balance_after=0,
            reward_id=alice_reward.id,
        )

    assert await rejected(session, spin_on(mallory_grant, alice)), "spin on someone's grant"
    for make in (roulette_reward, spin_stamps, redemption):
        assert await rejected(session, make(mallory)), make.__name__
        assert not await rejected(session, make(alice)), f"{make.__name__} for the owner"


async def test_loyalty_history_blocks_deleting_users_and_orders(fk_session: AsyncSession) -> None:
    """RESTRICT: a customer or an order with loyalty history can never be deleted."""
    session = fk_session
    buyer = await make_user(session, telegram_id=7603)
    member = await make_user(session, telegram_id=7604)  # an account, no orders
    order = await make_order(session, buyer, status=OrderStatus.COMPLETED)
    session.add_all(
        [
            LoyaltyAccount(user_id=member.id),
            LoyaltyTransaction(
                user_id=buyer.id,
                kind=LoyaltyTransactionType.PURCHASE,
                amount=1,
                balance_after=1,
                order_id=order.id,
            ),
        ]
    )
    await session.flush()

    for model, row_id in ((Order, order.id), (User, member.id)):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(delete(model).where(model.id == row_id))


# ------------------------------------------------------- migration vs. models


def _tree(path: pathlib.Path = MIGRATION) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(name: str, path: pathlib.Path = MIGRATION) -> ast.FunctionDef:
    for node in _tree(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} has no {name}()")


def _calls(scope: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute | ast.Name)
        and (node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id) == name
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _string(node: ast.expr | None) -> str:
    """A literal string, or the literal inside ``op.f("...")`` / ``sa.text("...")``."""
    if isinstance(node, ast.Call) and node.args:
        node = node.args[0]
    assert isinstance(node, ast.Constant) and isinstance(node.value, str), ast.dump(
        node or ast.Pass()
    )
    return node.value


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _model_constraints(kind: type) -> list[Any]:
    return [
        constraint
        for table in LOYALTY_TABLES
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, kind)
    ]


def test_check_constraints_are_identical_in_models_and_migration() -> None:
    migration = {
        _string(_keyword(call, "name")): _normalize(_string(call.args[0]))
        for call in _calls(_function("upgrade"), "CheckConstraint")
    }
    for call in _calls(_function("upgrade", REDEMPTION_MIGRATION), "create_check_constraint"):
        migration[_string(call.args[0])] = _normalize(_string(call.args[2]))
    models = {c.name: _normalize(str(c.sqltext)) for c in _model_constraints(CheckConstraint)}
    assert migration == models


def test_unique_constraints_are_identical_in_models_and_migration() -> None:
    migration = {
        _string(_keyword(call, "name")): tuple(_string(arg) for arg in call.args)
        for call in _calls(_function("upgrade"), "UniqueConstraint")
    }
    models = {
        c.name: tuple(column.name for column in c.columns)
        for c in _model_constraints(UniqueConstraint)
    }
    assert migration == models


def test_indexes_are_identical_in_models_and_migration() -> None:
    migration = {}
    for call in _calls(_function("upgrade"), "create_index"):
        columns = call.args[2]
        assert isinstance(columns, ast.List)
        where = _keyword(call, "postgresql_where")
        unique = _keyword(call, "unique")
        migration[_string(call.args[0])] = (
            _string(call.args[1]),
            tuple(_string(column) for column in columns.elts),
            isinstance(unique, ast.Constant) and unique.value is True,
            _string(where) if where is not None else None,
        )

    models = {}
    for table in LOYALTY_TABLES:
        for index in Base.metadata.tables[table].indexes:
            where = index.dialect_options["postgresql"]["where"]
            models[index.name] = (
                table,
                tuple(column.name for column in index.columns),
                bool(index.unique),
                str(where) if where is not None else None,
            )
    assert migration == models


def test_foreign_keys_are_identical_in_models_and_migration() -> None:
    migration = set()
    for call in _calls(_function("upgrade"), "create_table"):
        table = _string(call.args[0])
        for fk in _calls(call, "ForeignKeyConstraint"):
            columns, targets = fk.args[0], fk.args[1]
            assert isinstance(columns, ast.List) and isinstance(targets, ast.List)
            ondelete = _keyword(fk, "ondelete")
            migration.add(
                (
                    table,
                    tuple(_string(column) for column in columns.elts),
                    tuple(_string(target) for target in targets.elts),
                    _string(ondelete) if ondelete is not None else None,
                )
            )
    for call in _calls(_function("upgrade", REDEMPTION_MIGRATION), "create_foreign_key"):
        _name, source, referent, local, remote = call.args
        assert isinstance(local, ast.List) and isinstance(remote, ast.List)
        ondelete = _keyword(call, "ondelete")
        migration.add(
            (
                _string(source),
                tuple(_string(column) for column in local.elts),
                tuple(f"{_string(referent)}.{_string(column)}" for column in remote.elts),
                _string(ondelete) if ondelete is not None else None,
            )
        )

    models = {
        (
            table,
            tuple(fk.column_keys),
            tuple(element.target_fullname for element in fk.elements),
            fk.ondelete,
        )
        for table in LOYALTY_TABLES
        for fk in Base.metadata.tables[table].foreign_key_constraints
    }
    assert migration == models
    assert {ondelete for *_, ondelete in models} == {"RESTRICT"}, "loyalty history never cascades"


def test_the_downgrade_checks_for_customer_data_before_dropping_anything() -> None:
    body = [node for node in _function("downgrade").body if isinstance(node, ast.Expr)]
    first = body[0].value
    assert isinstance(first, ast.Call) and isinstance(first.func, ast.Name)
    assert first.func.id == "_refuse_to_destroy_customer_activity"


def test_the_redemption_migration_only_extends_user_rewards() -> None:
    for call in ast.walk(_function("upgrade", REDEMPTION_MIGRATION)):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "op"
        ):
            continue
        name = call.func.attr
        if name == "add_column":
            assert _string(call.args[0]) == "user_rewards"
        elif name in ("create_foreign_key", "create_check_constraint"):
            assert _string(call.args[1]) == "user_rewards"
        else:
            raise AssertionError(f"unexpected op.{name}() in upgrade()")

    body = [n for n in _function("downgrade", REDEMPTION_MIGRATION).body if isinstance(n, ast.Expr)]
    first = body[0].value
    assert isinstance(first, ast.Call) and isinstance(first.func, ast.Name)
    assert first.func.id == "_refuse_to_lose_redemption_records"


def test_the_migration_writes_only_to_the_tables_it_creates() -> None:
    """No existing table — users, orders, catalog — is altered or written to."""
    upgrade = _function("upgrade")
    for call in ast.walk(upgrade):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "op"
        ):
            continue
        name = call.func.attr
        if name == "create_table":
            assert _string(call.args[0]) in LOYALTY_TABLES
        elif name == "create_index":
            assert _string(call.args[1]) in LOYALTY_TABLES
        elif name == "execute":
            sql = _string(call.args[0])
            target = re.match(r"INSERT INTO (\w+) ", sql)
            assert target and target.group(1) in LOYALTY_TABLES, sql
        elif name != "f":
            raise AssertionError(f"unexpected op.{name}() in upgrade()")


def _backfill_statements() -> list[str]:
    return [_string(call.args[0]) for call in _calls(_function("upgrade"), "execute")]


async def test_backfill_gives_every_user_one_account_and_one_welcome_spin(
    session: AsyncSession,
) -> None:
    users = [await make_user(session, telegram_id=7300 + i) for i in range(3)]
    # One customer already has both, as if the backfill had run before.
    session.add(LoyaltyAccount(user_id=users[0].id))
    session.add(RouletteSpinGrant(user_id=users[0].id, reason=SpinGrantReason.INITIAL_PROMO))
    await session.flush()

    statements = _backfill_statements()
    assert len(statements) == 2
    for _ in range(2):  # re-running must not duplicate anything
        for sql in statements:
            await session.execute(text(sql))

    accounts = dict(
        (
            await session.execute(
                select(LoyaltyAccount.user_id, func.count()).group_by(LoyaltyAccount.user_id)
            )
        ).all()
    )
    welcome = dict(
        (
            await session.execute(
                select(RouletteSpinGrant.user_id, func.count())
                .where(RouletteSpinGrant.reason == SpinGrantReason.INITIAL_PROMO)
                .group_by(RouletteSpinGrant.user_id)
            )
        ).all()
    )
    expected = {user.id: 1 for user in users}
    assert accounts == expected
    assert welcome == expected
    balances = (await session.scalars(select(LoyaltyAccount.stamp_balance))).all()
    assert set(balances) == {0}
