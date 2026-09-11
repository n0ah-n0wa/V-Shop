"""
The roulette prize engine: the server draws, the database remembers.

Spins go through :class:`RouletteEngine` with a ticket source each test controls,
so a test knows which prize a draw lands on; in production the only randomness
is :func:`secrets.randbelow`. Concurrent spins are raced on PostgreSQL in
``tests/test_loyalty_postgres.py``.
"""

from __future__ import annotations

import pathlib
import secrets
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    RewardSource,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.repositories.roulette_spin import RouletteSpinRepository
from app.repositories.roulette_spin_grant import RouletteSpinGrantRepository
from app.repositories.user_reward import UserRewardRepository
from app.services.admin import AdminService
from app.services.cart import CartService
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.order import OrderService
from app.services.reward import (
    RewardNotApplicableError,
    RewardService,
    RewardUnavailableError,
    percentage_off,
)
from app.services.roulette import InvalidPrizeError, RoulettePrize, RouletteService, SpinOutcome
from app.services.roulette_engine import (
    PRIZE_CATALOGUE,
    PrizeTable,
    RouletteEngine,
    RoulettePolicy,
    WeightedPrize,
)
from tests.factories import add_order_item, make_category, make_order, make_product, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
LANGS = ("ru", "en", "de", "uk")
CEILING = Decimal("20.00")
DEFAULT = RoulettePolicy.defaults()
PRIZE = {prize.code: prize for prize in PRIZE_CATALOGUE}
ALL_WEIGHTS = (
    "roulette_prize_stamp_1_weight",
    "roulette_prize_stamp_2_weight",
    "roulette_prize_discount_5_weight",
    "roulette_prize_discount_10_weight",
    "roulette_prize_free_bottle_weight",
)


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


def landing_on(code: str, policy: RoulettePolicy = DEFAULT) -> Any:
    """A ticket source whose draw lands on ``code``."""
    ticket = 0
    for entry in policy.table.entries:
        if entry.prize.code == code:
            assert entry.weight > 0, f"{code} cannot be drawn"
            break
        ticket += entry.weight

    def randbelow(total: int) -> int:
        assert total == policy.table.total_weight
        return ticket

    return randbelow


def engine(session: AsyncSession, code: str) -> RouletteEngine:
    return RouletteEngine(session, DEFAULT, randbelow=landing_on(code))


async def spin_next(session: AsyncSession, code: str, user_id: int) -> SpinOutcome | None:
    """What a roulette screen does: offer the next grant, then spend exactly that one."""
    roulette = engine(session, code)
    grant_id = await roulette.next_grant_id(user_id)
    if grant_id is None:
        return None
    return await roulette.spin(user_id, grant_id=grant_id)


async def with_spins(session: AsyncSession, telegram_id: int, count: int = 1) -> User:
    """A customer holding ``count`` spins: the welcome spin, then milestone spins."""
    user = await make_user(session, telegram_id=telegram_id)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)
    for _ in range(count - 1):
        order = await make_order(session, user)
        await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)
    return user


async def rows(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def nothing_written(session: AsyncSession) -> bool:
    """No spin, no effect of one, and not even a loyalty account."""
    models = (RouletteSpin, UserReward, LoyaltyTransaction, LoyaltyAccount)
    return [await rows(session, model) for model in models] == [0] * len(models)


async def available(session: AsyncSession, user_id: int) -> int:
    return await RouletteService(session).available_spins(user_id)


async def place(session: AsyncSession, user: User, *, reward_id: int | None = None) -> Order:
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


async def a_product(session: AsyncSession, price: str) -> Any:
    category = await make_category(session, name="Liquids")
    return await make_product(session, category, name_en=f"Bottle {price}", price=price)


def connection_lost() -> OperationalError:
    return OperationalError(
        "INSERT …", None, ConnectionResetError("server closed the connection unexpectedly")
    )


# ================================================================ catalogue


def test_the_catalogue_holds_the_five_initial_prizes() -> None:
    assert [(prize.code, prize.kind, prize.value) for prize in PRIZE_CATALOGUE] == [
        ("stamp_1", RoulettePrizeType.STAMPS, 1),
        ("stamp_2", RoulettePrizeType.STAMPS, 2),
        ("discount_5", RoulettePrizeType.DISCOUNT_PERCENT, 5),
        ("discount_10", RoulettePrizeType.DISCOUNT_PERCENT, 10),
        ("free_bottle", RoulettePrizeType.FREE_BOTTLE, 1),
    ]


@pytest.mark.parametrize("language", LANGS)
def test_every_prize_has_a_display_name_in_every_language(language: str) -> None:
    i18n = LocalizationService(language)
    english = LocalizationService("en")
    for prize in PRIZE_CATALOGUE:
        name = i18n.t(prize.name_key)
        assert name != prize.name_key and name.strip(), f"{prize.code} has no {language} name"
        if language != "en":
            assert name != english.t(prize.name_key), f"{prize.code} is untranslated"


# ============================================================= every prize


@pytest.mark.parametrize("code", list(PRIZE))
async def test_every_prize_type_is_awarded_as_drawn(session: AsyncSession, code: str) -> None:
    user = await with_spins(session, 9601)
    prize = PRIZE[code]

    outcome = await spin_next(session, code, user.id)

    assert outcome is not None and outcome.created
    spin = outcome.spin
    assert (spin.prize_code, spin.prize_type, spin.prize_value) == (
        prize.code,
        prize.kind,
        prize.value,
    ), "every result is persisted as a snapshot"
    if prize.kind == RoulettePrizeType.STAMPS:
        assert outcome.reward is None and outcome.transaction is not None
        row = outcome.transaction
        assert (row.kind, row.amount, row.spin_id) == (
            LoyaltyTransactionType.ROULETTE,
            prize.value,
            spin.id,
        )
        assert await LoyaltyService(session).balance(user.id) == prize.value
    else:
        assert outcome.transaction is None and outcome.reward is not None
        reward = outcome.reward
        kind = (
            RewardType.FREE_BOTTLE
            if prize.kind == RoulettePrizeType.FREE_BOTTLE
            else RewardType.DISCOUNT_PERCENT
        )
        assert (reward.kind, reward.value, reward.source, reward.status, reward.spin_id) == (
            kind,
            prize.value,
            RewardSource.ROULETTE,
            RewardStatus.AVAILABLE,
            spin.id,
        )
        assert reward.max_item_price == (CEILING if kind == RewardType.FREE_BOTTLE else None)
        assert await LoyaltyService(session).balance(user.id) == 0
    assert await available(session, user.id) == 0


# ========================================================= weighted selection


def test_every_ticket_lands_on_one_prize_in_proportion_to_its_weight() -> None:
    table = DEFAULT.table

    drawn = Counter(
        table.draw(lambda _, ticket=ticket: ticket).code for ticket in range(table.total_weight)
    )

    assert drawn == {entry.prize.code: entry.weight for entry in table.entries}


def test_the_default_chances_are_exact() -> None:
    assert {code: DEFAULT.table.chance(code) for code in PRIZE} == {
        "stamp_1": Fraction(40, 100),
        "stamp_2": Fraction(25, 100),
        "discount_5": Fraction(20, 100),
        "discount_10": Fraction(10, 100),
        "free_bottle": Fraction(5, 100),
    }
    with pytest.raises(KeyError):
        DEFAULT.table.chance("jackpot")


def test_the_weights_follow_the_configuration() -> None:
    policy = RoulettePolicy.from_settings(
        configured(
            roulette_prize_stamp_1_weight=1,
            roulette_prize_stamp_2_weight=0,
            roulette_prize_discount_5_weight=0,
            roulette_prize_discount_10_weight=0,
            roulette_prize_free_bottle_weight=3,
        )
    )

    assert policy.table.chance("stamp_1") == Fraction(1, 4)
    assert policy.table.chance("free_bottle") == Fraction(3, 4)
    assert policy.free_bottle_max_price == CEILING


def test_a_prize_weighted_zero_is_never_drawn() -> None:
    policy = RoulettePolicy.from_settings(configured(roulette_prize_free_bottle_weight=0))
    table = policy.table

    drawn = {
        table.draw(lambda _, ticket=ticket: ticket).code for ticket in range(table.total_weight)
    }

    assert drawn == {"stamp_1", "stamp_2", "discount_5", "discount_10"}


async def test_the_draw_uses_the_servers_cryptographic_randomness(
    session: AsyncSession,
) -> None:
    assert RouletteEngine(session, DEFAULT)._randbelow is secrets.randbelow
    assert PrizeTable.draw.__defaults__ == (secrets.randbelow,)
    assert {DEFAULT.table.draw().code for _ in range(200)} <= set(PRIZE)


@pytest.mark.parametrize("ticket", [-1, 100, 1.5, True, "7"])
def test_a_ticket_outside_the_table_is_refused(ticket: Any) -> None:
    with pytest.raises(ValueError):
        DEFAULT.table.draw(lambda _: ticket)


async def test_the_engine_needs_its_policy_and_the_offered_grant(session: AsyncSession) -> None:
    """No default odds to fall back on, and no spin that does not name its grant."""
    with pytest.raises(TypeError):
        RouletteEngine(session)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await RouletteEngine(session, DEFAULT).spin(1)  # type: ignore[call-arg]


# ====================================================== invalid configuration


@pytest.mark.parametrize(
    "overrides",
    [
        dict.fromkeys(ALL_WEIGHTS, 0),
        {"roulette_prize_stamp_1_weight": -1},
        {"roulette_prize_free_bottle_weight": 1_000_001},
    ],
    ids=["all-zero", "negative", "absurd"],
)
def test_an_unusable_prize_configuration_stops_the_bot_at_startup(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        configured(**overrides)


@pytest.mark.parametrize(
    "entries",
    [
        (),
        (WeightedPrize(PRIZE["stamp_1"], 0),),
        (WeightedPrize(PRIZE["stamp_1"], 1), WeightedPrize(PRIZE["stamp_1"], 2)),
        (WeightedPrize(PRIZE["stamp_1"], -1),),
        (WeightedPrize(PRIZE["stamp_1"], True),),
        (WeightedPrize(PRIZE["stamp_1"], 1.5),),  # type: ignore[arg-type]
    ],
    ids=["empty", "nothing-drawable", "duplicate-code", "negative", "bool", "fractional"],
)
def test_an_invalid_prize_table_is_refused(entries: tuple[WeightedPrize, ...]) -> None:
    with pytest.raises(ValueError):
        PrizeTable(entries)


@pytest.mark.parametrize(
    "prize",
    [
        RoulettePrize("discount_150", RoulettePrizeType.DISCOUNT_PERCENT, 150),
        RoulettePrize("stamp_0", RoulettePrizeType.STAMPS, 0),
        RoulettePrize("", RoulettePrizeType.STAMPS, 1),
        RoulettePrize("two_bottles", RoulettePrizeType.FREE_BOTTLE, 2),
    ],
    ids=["discount-over-100", "zero-stamps", "no-code", "two-bottles"],
)
def test_a_policy_with_a_prize_that_cannot_be_persisted_is_refused(prize: RoulettePrize) -> None:
    with pytest.raises(InvalidPrizeError):
        RoulettePolicy(PrizeTable((WeightedPrize(prize, 1),)), CEILING)


@pytest.mark.parametrize("ceiling", [Decimal("0"), Decimal("NaN"), 20.0])
def test_a_policy_needs_a_real_free_bottle_ceiling(ceiling: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        RoulettePolicy(PrizeTable((WeightedPrize(PRIZE["free_bottle"], 1),)), ceiling)


def test_the_policy_defaults_are_the_configuration_defaults() -> None:
    assert RoulettePolicy.defaults() == RoulettePolicy.from_settings(configured())


# ======================================================= manipulated prizes


@pytest.mark.parametrize(
    "prize",
    [
        RoulettePrize("stamp_1", RoulettePrizeType.STAMPS, 2),
        RoulettePrize("discount_10", RoulettePrizeType.DISCOUNT_PERCENT, 100),
        RoulettePrize("discount_5", RoulettePrizeType.STAMPS, 5),
        RoulettePrize("free_bottle", RoulettePrizeType.DISCOUNT_PERCENT, 1),
        RoulettePrize("stamp_50", RoulettePrizeType.STAMPS, 50),
    ],
    ids=["inflated-stamps", "inflated-discount", "swapped-type", "bottle-as-discount", "invented"],
)
async def test_a_prize_outside_the_catalogue_is_never_recorded(
    session: AsyncSession, prize: RoulettePrize
) -> None:
    """Well-formed, persistable — and still refused: only catalogue prizes exist."""
    user = await with_spins(session, 9613)

    with pytest.raises(InvalidPrizeError):
        await RouletteService(session).spin(user.id, prize, free_bottle_max_price=CEILING)
    with pytest.raises(InvalidPrizeError):
        RoulettePolicy(PrizeTable((WeightedPrize(prize, 1),)), CEILING)

    assert await available(session, user.id) == 1
    assert await nothing_written(session)


# ============================================================ spending spins


async def test_no_spin_means_nothing_is_written(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=9602)
    roulette = engine(session, "free_bottle")

    assert await roulette.available_spins(user.id) == 0
    assert await roulette.next_grant_id(user.id) is None
    assert await roulette.spin(user.id, grant_id=1) is None
    assert await RouletteService(session).spin(user.id, PRIZE["stamp_1"]) is None

    assert await nothing_written(session)


async def test_a_second_spin_without_another_grant_spends_nothing(session: AsyncSession) -> None:
    user = await with_spins(session, 9603)

    first = await spin_next(session, "stamp_2", user.id)
    second = await spin_next(session, "stamp_2", user.id)

    assert first is not None and second is None
    assert await LoyaltyService(session).balance(user.id) == 2
    assert await rows(session, RouletteSpin) == 1


async def test_a_repeated_spin_request_replays_its_first_result(session: AsyncSession) -> None:
    user = await with_spins(session, 9604, count=2)
    offered = await RouletteSpinGrantRepository(session).first_available(user.id)
    assert offered is not None

    first = await engine(session, "discount_10").spin(user.id, grant_id=offered.id)
    again = await engine(session, "stamp_1").spin(user.id, grant_id=offered.id)  # a new draw

    assert first is not None and again is not None
    assert (first.created, again.created) == (True, False)
    assert again.spin.id == first.spin.id
    assert again.spin.prize_code == "discount_10", "the replay shows the result, not a redraw"
    assert again.reward is not None and first.reward is not None
    assert again.reward.id == first.reward.id
    assert await rows(session, RouletteSpin) == 1
    assert await rows(session, UserReward) == 1
    assert await LoyaltyService(session).balance(user.id) == 0, "the redraw awarded nothing"
    assert await available(session, user.id) == 1, "the other spin is untouched"


async def test_a_spin_request_naming_someone_elses_grant_spends_nothing(
    session: AsyncSession,
) -> None:
    alice = await with_spins(session, 9605)
    mallory = await with_spins(session, 9606)
    alices = await RouletteSpinGrantRepository(session).first_available(alice.id)
    assert alices is not None

    assert await engine(session, "free_bottle").spin(mallory.id, grant_id=alices.id) is None
    assert await engine(session, "free_bottle").spin(mallory.id, grant_id=999_999) is None

    assert await nothing_written(session), "a refusal writes nothing, not even an account"
    assert (await available(session, alice.id), await available(session, mallory.id)) == (1, 1)


async def test_a_spins_stamps_are_booked_once_and_only_to_its_owner(
    session: AsyncSession,
) -> None:
    """The ledger itself refuses a second booking, someone else's spin, or no stamps."""
    user = await with_spins(session, 9619)
    mallory = await make_user(session, telegram_id=9620)
    outcome = await spin_next(session, "stamp_2", user.id)
    assert outcome is not None and outcome.transaction is not None
    loyalty = LoyaltyService(session)

    again = await loyalty.credit_roulette(user.id, spin_id=outcome.spin.id, stamps=2)
    assert again.created is False and again.transaction.id == outcome.transaction.id
    with pytest.raises(ValueError):
        await loyalty.credit_roulette(mallory.id, spin_id=outcome.spin.id, stamps=2)
    with pytest.raises(ValueError):
        await loyalty.credit_roulette(user.id, spin_id=outcome.spin.id, stamps=0)

    assert (await loyalty.balance(user.id), await loyalty.balance(mallory.id)) == (2, 0)
    assert await rows(session, LoyaltyTransaction) == 1


async def test_the_same_update_after_a_restart_replays_the_committed_spin(
    session: AsyncSession,
) -> None:
    """The spin committed, the bot died before answering, and the update came again."""
    user = await with_spins(session, 9614, count=2)
    user_id = user.id
    first = await spin_next(session, "discount_10", user_id)
    assert first is not None and first.reward is not None
    grant_id, spin_id, reward_id = first.spin.grant_id, first.spin.id, first.reward.id
    await session.commit()

    assert isinstance(session.bind, AsyncEngine)
    new_process = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    async with new_process() as fresh:  # nothing held in memory survives a restart
        again = await RouletteEngine(fresh, DEFAULT, randbelow=landing_on("free_bottle")).spin(
            user_id, grant_id=grant_id
        )
        assert again is not None and not again.created
        assert (again.spin.id, again.spin.prize_code) == (spin_id, "discount_10")
        assert again.reward is not None and again.reward.id == reward_id
        assert await available(fresh, user_id) == 1, "the other spin is untouched"
        assert (await rows(fresh, RouletteSpin), await rows(fresh, UserReward)) == (1, 1)


# ============================================================ failed spins


async def test_a_failed_reward_leaves_the_spin_unspent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await with_spins(session, 9607)
    user_id = user.id
    await session.commit()

    async def broken(self: UserRewardRepository, **fields: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(UserRewardRepository, "create_and_add", broken)
    with pytest.raises(RuntimeError):
        await spin_next(session, "discount_5", user_id)
    await session.rollback()  # what DatabaseMiddleware does with any exception
    monkeypatch.undo()

    assert await available(session, user_id) == 1, "the spin is not lost"
    assert (await rows(session, RouletteSpin), await rows(session, UserReward)) == (0, 0)
    retried = await spin_next(session, "discount_5", user_id)
    assert retried is not None and retried.created, "and it can be spun again, once"
    assert await rows(session, UserReward) == 1


async def test_failed_stamps_leave_the_spin_unspent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await with_spins(session, 9608)
    user_id = user.id
    await session.commit()

    async def broken(self: LoyaltyTransactionRepository, **fields: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(LoyaltyTransactionRepository, "create_and_add", broken)
    with pytest.raises(RuntimeError):
        await spin_next(session, "stamp_2", user_id)
    await session.rollback()
    monkeypatch.undo()

    assert await available(session, user_id) == 1
    assert await rows(session, RouletteSpin) == 0
    assert await LoyaltyService(session).balance(user_id) == 0


async def test_a_reward_the_database_refuses_takes_the_spin_record_with_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant update and the spin insert have run when a CHECK refuses the reward."""
    user = await with_spins(session, 9609)
    user_id = user.id
    await session.commit()
    original = UserRewardRepository.create_and_add

    async def refused(self: UserRewardRepository, **fields: Any) -> Any:
        await self.session.flush()  # the grant and the spin reach the database ...
        return await original(self, **(fields | {"value": 0}))  # ... then a CHECK says no

    monkeypatch.setattr(UserRewardRepository, "create_and_add", refused)
    with pytest.raises(IntegrityError):
        await spin_next(session, "discount_10", user_id)
    await session.rollback()
    monkeypatch.undo()

    assert await available(session, user_id) == 1
    assert (await rows(session, RouletteSpin), await rows(session, UserReward)) == (0, 0)


@pytest.mark.parametrize(
    "broken",
    [RouletteSpinRepository, UserRewardRepository],
    ids=["recording-the-spin", "issuing-the-reward"],
)
async def test_a_connection_lost_mid_spin_loses_and_duplicates_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, broken: type[Any]
) -> None:
    """The database went away mid-transaction — a restart, a failover, a network cut."""
    user = await with_spins(session, 9615)
    user_id = user.id
    grant_id = await engine(session, "discount_10").next_grant_id(user_id)
    assert grant_id is not None
    await session.commit()

    async def lost(self: Any, **fields: Any) -> None:
        await self.session.flush()  # what was written so far reaches the server ...
        raise connection_lost()  # ... which then never sees a COMMIT

    monkeypatch.setattr(broken, "create_and_add", lost)
    with pytest.raises(OperationalError):
        await engine(session, "discount_10").spin(user_id, grant_id=grant_id)
    await session.rollback()
    monkeypatch.undo()

    assert await available(session, user_id) == 1, "the spin is not lost"
    assert (await rows(session, RouletteSpin), await rows(session, UserReward)) == (0, 0)
    retried = await engine(session, "discount_10").spin(user_id, grant_id=grant_id)
    again = await engine(session, "discount_10").spin(user_id, grant_id=grant_id)
    assert retried is not None and retried.created, "the retry spends it"
    assert again is not None and not again.created, "and only once"
    assert (await rows(session, RouletteSpin), await rows(session, UserReward)) == (1, 1)


# ====================================================== persistent rewards


async def test_a_won_discount_is_a_persistent_auditable_redeemable_reward(
    session: AsyncSession,
) -> None:
    user = await with_spins(session, 9610)
    user_id = user.id
    await spin_next(session, "discount_10", user_id)
    await session.commit()
    await session.rollback()  # nothing pending: everything below is what was committed

    reward = (await session.scalars(select(UserReward))).one()
    spin = (await session.scalars(select(RouletteSpin))).one()
    grant = await session.get(RouletteSpinGrant, spin.grant_id)
    assert (reward.user_id, reward.kind, reward.value, reward.source, reward.status) == (
        user_id,
        RewardType.DISCOUNT_PERCENT,
        10,
        RewardSource.ROULETTE,
        RewardStatus.AVAILABLE,
    )
    assert reward.spin_id == spin.id, "the reward names the spin that won it"
    assert grant is not None and grant.consumed_at is not None
    assert grant.reason == SpinGrantReason.INITIAL_PROMO, "and the spin names why it existed"
    assert [s.id for s in await RouletteEngine(session, DEFAULT).history(user_id)] == [spin.id]

    bottle = await a_product(session, "20.00")
    await CartService(session).add_product(user_id, bottle, quantity=2)
    order = await place(session, user, reward_id=reward.id)

    await session.refresh(reward)
    assert order.total_price == Decimal("36.00")
    assert (reward.status, reward.order_id, reward.discount_amount, reward.redeemed_product_id) == (
        RewardStatus.USED,
        order.id,
        Decimal("4.00"),
        None,
    )

    await CartService(session).add_product(user_id, bottle, quantity=1)
    with pytest.raises(RewardUnavailableError):
        await place(session, user, reward_id=reward.id)  # a discount is used once


@pytest.mark.parametrize(
    ("total", "percent", "discount"),
    [
        ("20.00", 10, "2.00"),
        ("12.50", 5, "0.63"),
        ("0.10", 5, "0.01"),
        ("99.99", 10, "10.00"),
    ],
)
def test_a_percentage_discount_is_rounded_half_up_to_the_cent(
    total: str, percent: int, discount: str
) -> None:
    assert percentage_off(Decimal(total), percent) == Decimal(discount)


async def test_an_order_too_small_for_any_discount_is_refused_untouched(
    session: AsyncSession,
) -> None:
    user = await with_spins(session, 9611)
    outcome = await spin_next(session, "discount_5", user.id)
    assert outcome is not None and outcome.reward is not None
    penny = await a_product(session, "0.05")  # 5% of it rounds to €0.00
    await CartService(session).add_product(user.id, penny, quantity=1)

    with pytest.raises(RewardNotApplicableError):
        await place(session, user, reward_id=outcome.reward.id)

    assert outcome.reward.status == RewardStatus.AVAILABLE
    assert await rows(session, Order) == 0


async def test_stamps_after_a_discount_come_from_what_was_charged(session: AsyncSession) -> None:
    """Owner decision: stamps are earned on the charged total, after discounts."""
    user = await with_spins(session, 9612)
    outcome = await spin_next(session, "discount_10", user.id)
    assert outcome is not None and outcome.reward is not None
    bottle = await a_product(session, "20.00")
    await CartService(session).add_product(user.id, bottle, quantity=2)
    order = await place(session, user, reward_id=outcome.reward.id)  # €40 → €36
    admin = AdminService(session)
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        order = await admin.set_order_status(order, status)

    assert await LoyaltyService(session).balance(user.id) == 1, "€36 earns one stamp, not two"


# ================================================== manipulated reward values


async def test_a_free_bottle_is_recorded_at_its_products_real_price(
    session: AsyncSession,
) -> None:
    """Whoever calls it, a free bottle cannot be booked as worth more — or less."""
    user = await with_spins(session, 9616)
    outcome = await spin_next(session, "free_bottle", user.id)
    assert outcome is not None and outcome.reward is not None
    reward_id = outcome.reward.id
    cheap = await a_product(session, "12.00")
    order = await make_order(session, user)
    await add_order_item(session, order, cheap, price="0.00")
    rewards = RewardService(session)

    async def use(amount: str) -> UserReward:
        return await rewards.use_reward(
            reward_id,
            user_id=user.id,
            order_id=order.id,
            discount_amount=Decimal(amount),
            redeemed_product_id=cheap.id,
        )

    for claimed in ("20.00", "11.99"):  # within the cap, but not what the bottle costs
        with pytest.raises(RewardNotApplicableError):
            await use(claimed)
    used = await use("12.00")

    assert (used.status, used.discount_amount) == (RewardStatus.USED, Decimal("12.00"))


async def test_a_discount_is_recorded_at_exactly_its_percentage(session: AsyncSession) -> None:
    """The amount is the reward's percentage of the lines, and already off the total."""
    user = await with_spins(session, 9617)
    outcome = await spin_next(session, "discount_10", user.id)
    assert outcome is not None and outcome.reward is not None
    reward_id = outcome.reward.id
    bottle = await a_product(session, "20.00")
    order = await make_order(session, user)
    await add_order_item(session, order, bottle, quantity=2)  # €40 of lines
    rewards = RewardService(session)

    async def use(amount: str, *, total: str) -> UserReward:
        order.total_price = Decimal(total)
        return await rewards.use_reward(
            reward_id, user_id=user.id, order_id=order.id, discount_amount=Decimal(amount)
        )

    with pytest.raises(RewardNotApplicableError):
        await use("10.00", total="30.00")  # 25% off, booked as the 10% reward
    with pytest.raises(RewardNotApplicableError):
        await use("4.00", total="40.00")  # the right amount, never taken off
    used = await use("4.00", total="36.00")

    assert (used.status, used.discount_amount, used.order_id) == (
        RewardStatus.USED,
        Decimal("4.00"),
        order.id,
    )


async def test_a_discount_cannot_claim_to_have_made_a_product_free(
    session: AsyncSession,
) -> None:
    """Only a free bottle names a product: a discount booked as one is refused untouched."""
    user = await with_spins(session, 9618)
    outcome = await spin_next(session, "discount_10", user.id)
    assert outcome is not None and outcome.reward is not None
    bottle = await a_product(session, "20.00")
    order = await make_order(session, user)
    await add_order_item(session, order, bottle, price="0.00")

    with pytest.raises(RewardNotApplicableError):
        await RewardService(session).use_reward(
            outcome.reward.id,
            user_id=user.id,
            order_id=order.id,
            discount_amount=Decimal("20.00"),
            redeemed_product_id=bottle.id,
        )

    assert outcome.reward.status == RewardStatus.AVAILABLE


# ================================================================= guards


def _sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "app").rglob("*.py"))
    }


def test_only_the_engine_spins_and_no_input_can_name_a_prize() -> None:
    sources = _sources()

    assert [path for path, text in sources.items() if ".spin(" in text] == [
        "app/handlers/user/roulette.py",  # the 🎰 screen — through RouletteEngine only
        "app/services/roulette_engine.py",
    ]
    assert "RouletteService" not in sources["app/handlers/user/roulette.py"]
    assert [
        path
        for path, text in sources.items()
        if "RoulettePrize(" in text and not path.startswith("app/services/")
    ] == [], "a prize is never built from anything a client sent"
