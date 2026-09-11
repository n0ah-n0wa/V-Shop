"""
Roulette spin entitlements — the welcome spin, every Nth purchase, referrals.

Granting is driven through the real entry points — ``/start``, the admin's order
completion and the start-up top-up — against the real schema, whose unique
constraints are what make every source grant at most once. Concurrent attempts
are raced on PostgreSQL in ``tests/test_loyalty_postgres.py``.
"""

from __future__ import annotations

import logging
import pathlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import lifecycle
from app.config import Settings
from app.handlers.user.start import cmd_start
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.admin import AdminService
from app.services.referral import ReferralService
from app.services.roulette import RoulettePrize, RouletteService
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy
from app.services.stamp_card import StampCardService
from tests.factories import make_order, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
STAMP_1 = RoulettePrize("stamp_1", RoulettePrizeType.STAMPS, 1)
INITIAL = SpinGrantReason.INITIAL_PROMO
MILESTONE = SpinGrantReason.PURCHASE_MILESTONE
REFERRAL = SpinGrantReason.REFERRAL


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


class Chatting(Message):
    """A /start message; records what the bot answered."""

    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        self.__dict__.setdefault("sent", []).append(text)
        return self


async def send_start(
    session: AsyncSession, telegram_id: int, *, settings: Settings | None = None
) -> None:
    """The customer sends /start — the first contact for a new one."""
    message = Chatting(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=TgUser(id=telegram_id, is_bot=False, first_name="Test"),
        text="/start",
    )
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
    )
    await cmd_start(
        message=message, state=state, session=session, settings=settings or configured()
    )


async def user_by_telegram(session: AsyncSession, telegram_id: int) -> User:
    user = await UserRepository(session).get_by_telegram_id(telegram_id)
    assert user is not None
    return user


async def grants(
    session: AsyncSession,
    *,
    reason: SpinGrantReason | None = None,
    user_id: int | None = None,
) -> list[RouletteSpinGrant]:
    statement = select(RouletteSpinGrant).order_by(RouletteSpinGrant.id)
    if reason is not None:
        statement = statement.where(RouletteSpinGrant.reason == reason)
    if user_id is not None:
        statement = statement.where(RouletteSpinGrant.user_id == user_id)
    return list((await session.scalars(statement)).all())


async def complete(
    session: AsyncSession,
    user: User,
    *,
    total: str = "20.00",
    eligible: bool = True,
    admin: AdminService | None = None,
) -> Order:
    """An order the admin takes all the way to Completed."""
    order = await make_order(session, user)
    order.total_price = Decimal(total)
    order.loyalty_eligible = eligible
    admin = admin or AdminService(session, settings=configured())
    for status in TO_COMPLETED:
        order = await admin.set_order_status(order, status)
    return order


async def milestone_orders(session: AsyncSession, user_id: int) -> list[int | None]:
    return [grant.order_id for grant in await grants(session, reason=MILESTONE, user_id=user_id)]


async def qualified_referral(session: AsyncSession, referrer: User, referred: User) -> int:
    referrals = ReferralService(session)
    referral = (
        await referrals.attribute(referrer_user_id=referrer.id, referred_user_id=referred.id)
    ).referral
    order = await make_order(session, referred, status=OrderStatus.COMPLETED)
    assert await referrals.qualify(referral.id, order_id=order.id)
    return referral.id


def callers_of(call: str) -> list[str]:
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if call in path.read_text(encoding="utf-8")
    )


# =============================================================== welcome spin


async def test_a_new_customer_gets_the_welcome_spin_on_first_contact(
    session: AsyncSession,
) -> None:
    await send_start(session, 9501)

    user = await user_by_telegram(session, 9501)
    welcome = await grants(session, reason=INITIAL, user_id=user.id)
    assert len(welcome) == 1 and welcome[0].consumed_at is None
    balance = await SpinEntitlementService(session).balance(user.id)
    assert (balance.available, balance.used, dict(balance.granted_by_reason)) == (
        1,
        0,
        {INITIAL: 1},
    )


async def test_repeated_starts_never_grant_another(session: AsyncSession) -> None:
    for _ in range(3):
        await send_start(session, 9502)
    user = await user_by_telegram(session, 9502)
    assert len(await grants(session, reason=INITIAL, user_id=user.id)) == 1

    await RouletteService(session).spin(user.id, STAMP_1)  # the customer spends it
    await send_start(session, 9502)

    assert len(await grants(session, reason=INITIAL, user_id=user.id)) == 1, (
        "a spent welcome spin is never replaced"
    )
    assert (await SpinEntitlementService(session).balance(user.id)).available == 0


async def test_an_existing_customer_gets_the_welcome_spin_at_their_next_start(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9503)  # signed up before spins existed
    assert await grants(session, user_id=user.id) == []

    await send_start(session, 9503)

    assert len(await grants(session, reason=INITIAL, user_id=user.id)) == 1


async def test_the_welcome_spin_can_be_switched_off(session: AsyncSession) -> None:
    settings = configured(roulette_initial_free_spin=False)
    await make_user(session, telegram_id=9505)

    await send_start(session, 9504, settings=settings)
    top_up = await SpinEntitlementService(
        session, SpinPolicy.from_settings(settings)
    ).grant_missing_welcome_spins()

    assert top_up == 0
    assert await grants(session) == []


@pytest.fixture
def bot_sessions(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> async_sessionmaker[AsyncSession]:
    """The bot's own session factory, pointed at the test database."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: factory)
    return factory


async def test_every_bot_start_tops_up_customers_without_a_welcome_spin(
    bot_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with bot_sessions() as session:
        missed = [await make_user(session, telegram_id=9510 + i) for i in range(3)]
        spent = await make_user(session, telegram_id=9520)
        await RouletteService(session).grant_initial_spin(spent.id)
        await RouletteService(session).spin(spent.id, STAMP_1)
        await session.commit()
        user_ids = [user.id for user in missed] + [spent.id]

    first = await lifecycle.grant_missing_welcome_spins(configured())
    restart = await lifecycle.grant_missing_welcome_spins(configured())

    assert (first, restart) == (3, 0), "a restart grants nothing"
    async with bot_sessions() as session:
        for user_id in user_ids:
            assert len(await grants(session, reason=INITIAL, user_id=user_id)) == 1
        assert (await SpinEntitlementService(session).balance(spent.id)).available == 0


async def test_start_and_the_start_up_top_up_never_add_up_to_two(
    bot_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with bot_sessions() as session:
        await send_start(session, 9530)
        await session.commit()
    assert await lifecycle.grant_missing_welcome_spins(configured()) == 0

    async with bot_sessions() as session:
        await make_user(session, telegram_id=9531)
        await session.commit()
    assert await lifecycle.grant_missing_welcome_spins(configured()) == 1

    async with bot_sessions() as session:
        await send_start(session, 9531)
        await session.commit()
        assert len(await grants(session, reason=INITIAL)) == 2, "one per customer"


async def test_the_top_up_runs_at_every_bot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Settings] = []

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    async def top_up(settings: Settings) -> int:
        ran.append(settings)
        return 0

    for name in ("init_db", "check_db_connection", "log_database_identity"):
        monkeypatch.setattr(lifecycle, name, nothing)
    monkeypatch.setattr(lifecycle, "grant_missing_welcome_spins", top_up)
    bot = SimpleNamespace(
        delete_webhook=nothing,
        get_me=lambda: _me(),
    )
    settings = configured()

    await lifecycle.on_startup(bot, settings)  # type: ignore[arg-type]

    assert ran == [settings]


async def _me() -> SimpleNamespace:
    return SimpleNamespace(username="vshop", id=1, can_join_groups=False)


async def test_a_failed_top_up_never_stops_the_bot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable() -> Any:
        raise RuntimeError("the loyalty migration is not applied")

    monkeypatch.setattr(lifecycle, "get_session_factory", unavailable)

    with caplog.at_level(logging.ERROR, logger="app.lifecycle"):
        assert await lifecycle.grant_missing_welcome_spins(configured()) is None
    assert "welcome spins" in caplog.text


# ================================================================== purchases


async def test_the_fifth_qualifying_purchase_grants_a_spin(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9540)

    orders = [await complete(session, user) for _ in range(5)]

    assert await milestone_orders(session, user.id) == [orders[4].id]


async def test_the_tenth_purchase_grants_the_second(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9541)

    orders = [await complete(session, user) for _ in range(10)]

    assert await milestone_orders(session, user.id) == [orders[4].id, orders[9].id]
    balance = await SpinEntitlementService(session).balance(user.id)
    assert dict(balance.granted_by_reason) == {MILESTONE: 2}


async def test_processing_the_same_purchase_again_grants_nothing_more(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=9542)
    fifth = [await complete(session, user) for _ in range(5)][-1]

    await AdminService(session, settings=configured()).set_order_status(
        fifth, OrderStatus.COMPLETED
    )  # a second tap on "Complete"
    await StampCardService(session).award_for_order(fifth.id)  # a replayed hook
    replay = await SpinEntitlementService(session).grant_for_completed_order(fifth.id)

    assert replay is not None and replay.created is False
    assert await milestone_orders(session, user.id) == [fifth.id]


async def test_only_qualifying_purchases_are_counted(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9543)
    await complete(session, user, total="10.00")  # 0 stamps, but still a purchase
    for _ in range(3):
        await complete(session, user)
    await complete(session, user, total="0.00")  # only a free bottle: not a purchase
    await complete(session, user, eligible=False)  # placed before the launch
    cancelled = await make_order(session, user)
    await AdminService(session).set_order_status(cancelled, OrderStatus.CANCELLED)
    assert await milestone_orders(session, user.id) == []

    fifth = await complete(session, user)

    assert await milestone_orders(session, user.id) == [fifth.id]


async def test_purchases_are_numbered_per_customer(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9544)
    bob = await make_user(session, telegram_id=9545)
    alice_orders = []
    for index in range(5):
        alice_orders.append(await complete(session, alice))
        if index < 4:
            await complete(session, bob)

    assert await milestone_orders(session, alice.id) == [alice_orders[4].id]
    assert await milestone_orders(session, bob.id) == []


@pytest.mark.parametrize(
    ("every", "milestones"),
    [(3, [2, 5]), (1, [0, 1, 2, 3, 4, 5]), (0, [])],
    ids=["every-3rd", "every-purchase", "off"],
)
async def test_the_purchase_interval_is_configurable(
    session: AsyncSession, every: int, milestones: list[int]
) -> None:
    user = await make_user(session, telegram_id=9546)
    admin = AdminService(session, settings=configured(roulette_spin_every_n_purchases=every))

    orders = [await complete(session, user, admin=admin) for _ in range(6)]

    assert await milestone_orders(session, user.id) == [orders[i].id for i in milestones]


async def test_the_spin_and_the_completion_are_one_transaction(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the milestone spin cannot be written, the completion is undone with it."""
    user = await make_user(session, telegram_id=9547)
    for _ in range(4):
        await complete(session, user)
    fifth = await make_order(session, user)
    fifth.total_price = Decimal("20.00")
    admin = AdminService(session, settings=configured())
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED):
        fifth = await admin.set_order_status(fifth, status)
    await session.commit()
    user_id, fifth_id = user.id, fifth.id

    async def broken(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(RouletteService, "grant_purchase_milestone_spin", broken)
    with pytest.raises(RuntimeError):
        await admin.set_order_status(fifth, OrderStatus.COMPLETED)
    await session.rollback()  # what DatabaseMiddleware does with any exception
    monkeypatch.undo()

    status = await session.scalar(select(Order.status).where(Order.id == fifth_id))
    purchases = await session.scalar(
        select(func.count())
        .select_from(LoyaltyTransaction)
        .where(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.kind == LoyaltyTransactionType.PURCHASE,
        )
    )
    assert (status, purchases) == (OrderStatus.SHIPPED, 4)
    assert await milestone_orders(session, user_id) == []


# ================================================================== referrals


async def test_a_referral_spin_goes_to_the_referrer_once(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9550)
    bob = await make_user(session, telegram_id=9551)
    referral_id = await qualified_referral(session, referrer=alice, referred=bob)
    spins = SpinEntitlementService(session)

    first = await spins.grant_for_referral(referral_id)
    again = await spins.grant_for_referral(referral_id)

    assert first is not None and again is not None
    assert (first.created, again.created) == (True, False)
    assert again.grant.id == first.grant.id
    assert [grant.user_id for grant in await grants(session, reason=REFERRAL)] == [alice.id]


async def test_a_referral_spin_waits_for_the_referral_to_qualify(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9552)
    bob = await make_user(session, telegram_id=9553)
    referral = (
        await ReferralService(session).attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
    ).referral

    with pytest.raises(ValueError):
        await SpinEntitlementService(session).grant_for_referral(referral.id)
    assert await grants(session) == []


async def test_referral_spins_can_be_switched_off(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9554)
    bob = await make_user(session, telegram_id=9555)
    referral_id = await qualified_referral(session, referrer=alice, referred=bob)
    policy = SpinPolicy.from_settings(configured(referral_spins=0))

    assert await SpinEntitlementService(session, policy).grant_for_referral(referral_id) is None
    assert await grants(session) == []


async def test_a_referral_that_does_not_exist_grants_nothing(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await SpinEntitlementService(session).grant_for_referral(999_999)
    assert await grants(session) == []


# ============================================================ balance and audit


async def test_the_balance_and_history_explain_every_spin(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9560)
    bob = await make_user(session, telegram_id=9561)
    spins = SpinEntitlementService(session)
    await spins.grant_welcome_spin(alice.id)
    fifth = [await complete(session, alice) for _ in range(5)][-1]
    referral_id = await qualified_referral(session, referrer=alice, referred=bob)
    await spins.grant_for_referral(referral_id)
    await RouletteService(session).spin(alice.id, STAMP_1)  # spends the oldest

    balance = await spins.balance(alice.id)

    assert (balance.available, balance.used, balance.granted) == (2, 1, 3)
    assert dict(balance.granted_by_reason) == {INITIAL: 1, MILESTONE: 1, REFERRAL: 1}
    history = [
        (grant.reason, grant.order_id, grant.referral_id, grant.consumed_at is not None)
        for grant in await spins.history(alice.id)
    ]
    assert history == [
        (INITIAL, None, None, True),
        (MILESTONE, fifth.id, None, False),
        (REFERRAL, None, referral_id, False),
    ]


async def test_reading_the_balance_creates_nothing(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9562)
    spins = SpinEntitlementService(session)

    for _ in range(3):
        balance = await spins.balance(user.id)

    assert (balance.available, balance.used, dict(balance.granted_by_reason)) == (0, 0, {})
    assert await grants(session) == []


async def test_the_countdown_follows_the_customers_purchases(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9563)
    spins = SpinEntitlementService(session)

    assert await spins.purchases_to_next_spin(user.id) == 5
    assert await session.scalar(select(func.count()).select_from(LoyaltyAccount)) == 0, (
        "asking creates nothing"
    )
    for _ in range(3):
        await complete(session, user)
    assert await spins.purchases_to_next_spin(user.id) == 2
    for _ in range(2):
        await complete(session, user)
    assert await spins.purchases_to_next_spin(user.id) == 5, "the 5th earned its spin"
    assert await milestone_orders(session, user.id) != []

    off = SpinEntitlementService(
        session, SpinPolicy.from_settings(configured(roulette_spin_every_n_purchases=0))
    )
    assert await off.purchases_to_next_spin(user.id) is None


# ============================================================ configuration


def test_the_policy_defaults_are_the_configuration_defaults() -> None:
    assert SpinPolicy.defaults() == SpinPolicy.from_settings(configured())
    assert SpinPolicy.defaults() == SpinPolicy(
        initial_free_spin=True, every_n_purchases=5, referral_spins=1
    )


def test_milestones_are_every_nth_purchase() -> None:
    policy = SpinPolicy(initial_free_spin=True, every_n_purchases=5, referral_spins=1)
    assert [n for n in range(0, 21) if policy.is_purchase_milestone(n)] == [5, 10, 15, 20]
    off = SpinPolicy(initial_free_spin=True, every_n_purchases=0, referral_spins=1)
    assert not any(off.is_purchase_milestone(n) for n in range(0, 21))


def test_the_countdown_to_the_next_purchase_spin() -> None:
    policy = SpinPolicy(initial_free_spin=True, every_n_purchases=5, referral_spins=1)
    assert [policy.purchases_to_next_spin(n) for n in (0, 1, 4, 5, 6, 10)] == [5, 4, 1, 5, 4, 5]
    off = SpinPolicy(initial_free_spin=True, every_n_purchases=0, referral_spins=1)
    assert off.purchases_to_next_spin(3) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"referral_spins": 2},
        {"referral_spins": -1},
        {"roulette_spin_every_n_purchases": -1},
    ],
    ids=["two-referral-spins", "negative-referral-spins", "negative-interval"],
)
def test_unusable_spin_settings_stop_the_bot_at_startup(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        configured(**overrides)


@pytest.mark.parametrize(
    "fields",
    [{"every_n_purchases": -1}, {"referral_spins": 2}, {"referral_spins": -1}],
    ids=["negative-interval", "two-referral-spins", "negative-referral-spins"],
)
def test_an_unusable_spin_policy_is_refused(fields: dict[str, int]) -> None:
    """The policy validates itself, not only the settings it usually comes from."""
    valid: dict[str, Any] = {"initial_free_spin": True, "every_n_purchases": 5, "referral_spins": 1}
    with pytest.raises(ValueError):
        SpinPolicy(**(valid | fields))


# ================================================================= guards


def test_welcome_spins_have_exactly_two_entry_points() -> None:
    """/start for the customer at hand; the start-up top-up for everyone missed."""
    assert callers_of(".grant_welcome_spin(") == ["app/handlers/user/start.py"]
    assert callers_of(".grant_missing_welcome_spins(") == ["app/lifecycle.py"]
