"""RouletteService — spin entitlements, and spending each one exactly once."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    RewardSource,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
)
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.services.loyalty import LoyaltyService
from app.services.referral import ReferralService
from app.services.roulette import InvalidPrizeError, RoulettePrize, RouletteService
from tests.factories import make_order, make_user

CEILING = Decimal("20.00")
STAMP_2 = RoulettePrize(code="stamp_2", kind=RoulettePrizeType.STAMPS, value=2)
DISCOUNT_10 = RoulettePrize(code="discount_10", kind=RoulettePrizeType.DISCOUNT_PERCENT, value=10)
FREE_BOTTLE = RoulettePrize(code="free_bottle", kind=RoulettePrizeType.FREE_BOTTLE, value=1)
PRIZE_FOR = {
    RoulettePrizeType.STAMPS: STAMP_2,
    RoulettePrizeType.DISCOUNT_PERCENT: DISCOUNT_10,
    RoulettePrizeType.FREE_BOTTLE: FREE_BOTTLE,
}


async def count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# --------------------------------------------------------------------- granting


async def test_the_welcome_spin_is_granted_once(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7401)
    roulette = RouletteService(session)

    first = await roulette.grant_initial_spin(user.id)
    again = await roulette.grant_initial_spin(user.id)

    assert (first.created, again.created) == (True, False)
    assert again.grant.id == first.grant.id
    assert await roulette.available_spins(user.id) == 1


async def test_looking_at_the_roulette_never_creates_spins(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7402)
    roulette = RouletteService(session)
    for _ in range(5):
        assert await roulette.available_spins(user.id) == 0
    assert await count(session, RouletteSpinGrant) == 0


async def test_a_milestone_spin_is_granted_once_per_order(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7403)
    stranger = await make_user(session, telegram_id=7404)
    order = await make_order(session, user)
    roulette = RouletteService(session)

    first = await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)
    again = await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)

    assert (first.created, again.created) == (True, False)
    assert await roulette.available_spins(user.id) == 1
    with pytest.raises(ValueError):
        await roulette.grant_purchase_milestone_spin(stranger.id, order_id=order.id)


async def test_a_referral_spin_is_granted_once_per_referral_and_recipient(
    session: AsyncSession,
) -> None:
    alice = await make_user(session, telegram_id=7405)
    bob = await make_user(session, telegram_id=7406)
    carol = await make_user(session, telegram_id=7407)
    referrals = ReferralService(session)
    referral = (
        await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
    ).referral
    roulette = RouletteService(session)
    with pytest.raises(ValueError):  # nothing is granted before the referral qualifies
        await roulette.grant_referral_spin(alice.id, referral_id=referral.id)
    bob_order = await make_order(session, bob, status=OrderStatus.COMPLETED)
    assert await referrals.qualify(referral.id, order_id=bob_order.id)

    first = await roulette.grant_referral_spin(alice.id, referral_id=referral.id)
    again = await roulette.grant_referral_spin(alice.id, referral_id=referral.id)
    other_side = await roulette.grant_referral_spin(bob.id, referral_id=referral.id)

    assert (first.created, again.created, other_side.created) == (True, False, True)
    assert await roulette.available_spins(alice.id) == 1
    with pytest.raises(ValueError):
        await roulette.grant_referral_spin(carol.id, referral_id=referral.id)


# --------------------------------------------------------------------- spending


async def test_spinning_without_a_grant_changes_nothing(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7408)

    assert await RouletteService(session).spin(user.id, STAMP_2) is None
    assert await count(session, RouletteSpin) == 0
    assert await LoyaltyService(session).balance(user.id) == 0


async def test_spins_spend_the_oldest_grant_exactly_once(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7409)
    order = await make_order(session, user)
    roulette = RouletteService(session)
    welcome = (await roulette.grant_initial_spin(user.id)).grant
    milestone = (await roulette.grant_purchase_milestone_spin(user.id, order_id=order.id)).grant

    first = await roulette.spin(user.id, STAMP_2)
    second = await roulette.spin(user.id, STAMP_2)
    third = await roulette.spin(user.id, STAMP_2)

    assert first is not None and second is not None and third is None
    assert (first.spin.grant_id, second.spin.grant_id) == (welcome.id, milestone.id)
    assert welcome.consumed_at is not None and milestone.consumed_at is not None
    assert await roulette.available_spins(user.id) == 0
    assert await LoyaltyService(session).balance(user.id) == 4


async def test_a_stamp_prize_is_booked_against_its_spin(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7410)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)

    outcome = await roulette.spin(user.id, STAMP_2)

    assert outcome is not None and outcome.reward is None
    spin, row = outcome.spin, outcome.transaction
    assert (spin.prize_code, spin.prize_type, spin.prize_value) == ("stamp_2", "stamps", 2)
    assert row is not None
    assert (row.kind, row.amount, row.spin_id) == (LoyaltyTransactionType.ROULETTE, 2, spin.id)


async def test_a_discount_prize_becomes_a_redeemable_reward(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7411)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)

    outcome = await roulette.spin(user.id, DISCOUNT_10)

    assert outcome is not None and outcome.transaction is None
    reward = outcome.reward
    assert reward is not None
    assert (reward.kind, reward.value, reward.max_item_price) == (
        RewardType.DISCOUNT_PERCENT,
        10,
        None,
    )
    assert (reward.source, reward.status, reward.spin_id) == (
        RewardSource.ROULETTE,
        RewardStatus.AVAILABLE,
        outcome.spin.id,
    )


async def test_a_free_bottle_prize_snapshots_its_price_ceiling(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7412)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)

    outcome = await roulette.spin(user.id, FREE_BOTTLE, free_bottle_max_price=Decimal("20"))

    assert outcome is not None and outcome.reward is not None
    assert (outcome.reward.kind, outcome.reward.value, outcome.reward.max_item_price) == (
        RewardType.FREE_BOTTLE,
        1,
        CEILING,
    )


@pytest.mark.parametrize(
    ("prize", "ceiling"),
    [
        (RoulettePrize("discount_150", RoulettePrizeType.DISCOUNT_PERCENT, 150), None),
        (RoulettePrize("stamp_0", RoulettePrizeType.STAMPS, 0), None),
        (RoulettePrize("", RoulettePrizeType.STAMPS, 1), None),
        (RoulettePrize("x" * 33, RoulettePrizeType.STAMPS, 1), None),
        (FREE_BOTTLE, None),
        (RoulettePrize("two_bottles", RoulettePrizeType.FREE_BOTTLE, 2), CEILING),
        (FREE_BOTTLE, 20.0),
        (FREE_BOTTLE, Decimal("NaN")),
        (RoulettePrize("jackpot", "jackpot", 1), None),  # type: ignore[arg-type]
    ],
    ids=[
        "discount>100",
        "zero-stamps",
        "no-code",
        "long-code",
        "no-ceiling",
        "two-bottles",
        "float-ceiling",
        "nan-ceiling",
        "unknown-type",
    ],
)
async def test_an_invalid_prize_spends_nothing(
    session: AsyncSession, prize: RoulettePrize, ceiling: Decimal | None
) -> None:
    user = await make_user(session, telegram_id=7413)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)

    with pytest.raises(InvalidPrizeError):
        await roulette.spin(user.id, prize, free_bottle_max_price=ceiling)

    assert await roulette.available_spins(user.id) == 1
    assert await count(session, RouletteSpin) == 0


async def test_a_prize_type_given_as_plain_text_is_honoured(session: AsyncSession) -> None:
    """
    Regression: the type was compared by identity, so the string "stamps" —
    equal to the enum member, but not the same object — fell through to the
    free-bottle branch and issued a bottle instead of stamps.
    """
    user = await make_user(session, telegram_id=7415)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)

    prize = RoulettePrize("stamp_2", "stamps", 2)  # type: ignore[arg-type]
    outcome = await roulette.spin(user.id, prize, free_bottle_max_price=CEILING)

    assert outcome is not None and outcome.reward is None
    assert outcome.transaction is not None and outcome.transaction.amount == 2
    assert outcome.spin.prize_type is RoulettePrizeType.STAMPS


@pytest.mark.parametrize("kind", list(RoulettePrizeType))
async def test_every_prize_type_can_be_won_and_has_exactly_one_effect(
    session: AsyncSession, kind: RoulettePrizeType
) -> None:
    user = await make_user(session, telegram_id=7414)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)

    outcome = await roulette.spin(user.id, PRIZE_FOR[kind], free_bottle_max_price=CEILING)

    assert outcome is not None
    assert (outcome.transaction is None) != (outcome.reward is None)
    assert outcome.spin.prize_type is kind
