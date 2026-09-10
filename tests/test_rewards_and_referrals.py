"""RewardService and ReferralService — binding rewards to orders; referral attribution."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, ReferralStatus, RewardStatus
from app.models.order import Order
from app.models.product import Product
from app.models.reward import UserReward
from app.models.user import User
from app.repositories.order import OrderRepository
from app.services.loyalty import LoyaltyService
from app.services.referral import (
    REFERRAL_CODE_PATTERN,
    ReferralLoopError,
    ReferralService,
    SelfReferralError,
    is_valid_referral_code,
)
from app.services.reward import RewardOrderError, RewardService, RewardUnavailableError
from tests.factories import add_order_item, make_category, make_order, make_product, make_user

CEILING = Decimal("20.00")


async def free_bottle(session: AsyncSession, user_id: int) -> int:
    """Give ``user_id`` one available free-bottle reward, the way the card does."""
    loyalty = LoyaltyService(session)
    await loyalty.adjust(user_id, amount=10, note="test setup")
    reward = await loyalty.claim_free_bottle(user_id, stamps_required=10, max_item_price=CEILING)
    return reward.id


async def a_bottle(session: AsyncSession) -> Product:
    category = await make_category(session, name="Liquids")
    return await make_product(session, category, name_en="Mango", price="20.00")


async def bottle_order(session: AsyncSession, user: User, bottle: Product) -> Order:
    """A just-placed order holding ``bottle`` at €0 — what checkout builds."""
    order = await make_order(session, user)
    await add_order_item(session, order, bottle, price="0.00")
    return order


async def use(
    rewards: RewardService, reward_id: int, user: User, order: Order, bottle: Product
) -> UserReward:
    return await rewards.use_reward(
        reward_id,
        user_id=user.id,
        order_id=order.id,
        discount_amount=CEILING,
        redeemed_product_id=bottle.id,
    )


# --------------------------------------------------------------------- rewards


async def test_a_reward_is_used_once(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7501)
    bottle = await a_bottle(session)
    order = await bottle_order(session, user, bottle)
    other_order = await bottle_order(session, user, bottle)
    reward_id = await free_bottle(session, user.id)
    rewards = RewardService(session)

    used = await use(rewards, reward_id, user, order, bottle)

    assert (used.status, used.order_id) == (RewardStatus.USED, order.id)
    assert (used.discount_amount, used.redeemed_product_id) == (CEILING, bottle.id)
    assert used.used_at is not None
    with pytest.raises(RewardUnavailableError):
        await use(rewards, reward_id, user, other_order, bottle)
    assert await rewards.list_available(user.id) == []


async def test_nobody_can_spend_someone_elses_reward(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7502)
    mallory = await make_user(session, telegram_id=7503)
    bottle = await a_bottle(session)
    mallory_order = await bottle_order(session, mallory, bottle)
    reward_id = await free_bottle(session, alice.id)
    rewards = RewardService(session)

    with pytest.raises(RewardUnavailableError):
        await use(rewards, reward_id, mallory, mallory_order, bottle)

    reward = await rewards.get_for_user(reward_id, alice.id)
    assert reward is not None and reward.status is RewardStatus.AVAILABLE


async def test_a_reward_cannot_be_put_on_someone_elses_order(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7504)
    bob = await make_user(session, telegram_id=7505)
    bottle = await a_bottle(session)
    bob_order = await bottle_order(session, bob, bottle)
    reward_id = await free_bottle(session, alice.id)
    rewards = RewardService(session)

    with pytest.raises(RewardOrderError):
        await use(rewards, reward_id, alice, bob_order, bottle)
    assert [reward.id for reward in await rewards.list_available(alice.id)] == [reward_id]


async def test_an_order_carries_at_most_one_reward(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7506)
    bottle = await a_bottle(session)
    order = await bottle_order(session, user, bottle)
    first = await free_bottle(session, user.id)
    second = await free_bottle(session, user.id)
    rewards = RewardService(session)

    await use(rewards, first, user, order, bottle)
    with pytest.raises(RewardOrderError):
        await use(rewards, second, user, order, bottle)

    assert [reward.id for reward in await rewards.list_available(user.id)] == [second]


async def test_a_used_reward_stays_used_when_its_order_is_cancelled(
    session: AsyncSession,
) -> None:
    """Owner decision: cancelling an order forfeits the reward used on it."""
    user = await make_user(session, telegram_id=7507)
    bottle = await a_bottle(session)
    order = await bottle_order(session, user, bottle)
    reward_id = await free_bottle(session, user.id)
    rewards = RewardService(session)
    await use(rewards, reward_id, user, order, bottle)

    await OrderRepository(session).update_status(order, OrderStatus.CANCELLED)

    assert await rewards.list_available(user.id) == []
    reward = await rewards.get_for_user(reward_id, user.id)
    assert reward is not None
    assert (reward.status, reward.order_id) == (RewardStatus.USED, order.id)


# ------------------------------------------------------------------- referrals


async def test_a_referral_code_is_stable_unique_and_well_formed(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7508)
    bob = await make_user(session, telegram_id=7509)
    referrals = ReferralService(session)

    code = await referrals.get_or_create_referral_code(alice.id)

    assert code == await referrals.get_or_create_referral_code(alice.id)
    assert REFERRAL_CODE_PATTERN.fullmatch(code)
    assert code != await referrals.get_or_create_referral_code(bob.id)
    assert await referrals.find_referrer_user_id(code) == alice.id


@pytest.mark.parametrize(
    "code",
    [None, "", "short", "x" * 33, "has space1", "'; DROP TABLE users;--", "ref_abc$def"],
)
async def test_a_malformed_code_resolves_to_nobody(session: AsyncSession, code: str | None) -> None:
    assert not is_valid_referral_code(code)
    assert await ReferralService(session).find_referrer_user_id(code) is None


async def test_an_unknown_code_resolves_to_nobody(session: AsyncSession) -> None:
    assert await ReferralService(session).find_referrer_user_id("Unknown_Code1") is None


async def test_attribution_is_pending_and_permanent(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7510)
    bob = await make_user(session, telegram_id=7511)
    carol = await make_user(session, telegram_id=7512)
    referrals = ReferralService(session)

    first = await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
    again = await referrals.attribute(referrer_user_id=carol.id, referred_user_id=bob.id)

    assert first.created and first.referral.status is ReferralStatus.PENDING
    assert not again.created
    assert again.referral.id == first.referral.id
    assert again.referral.referrer_user_id == alice.id, "the referrer is never changed"


async def test_nobody_can_refer_themselves(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7513)
    with pytest.raises(SelfReferralError):
        await ReferralService(session).attribute(
            referrer_user_id=alice.id, referred_user_id=alice.id
        )


async def test_a_referral_loop_is_refused(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7514)
    bob = await make_user(session, telegram_id=7515)
    referrals = ReferralService(session)
    await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)

    with pytest.raises(ReferralLoopError):
        await referrals.attribute(referrer_user_id=bob.id, referred_user_id=alice.id)


async def test_a_referral_qualifies_once(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7516)
    bob = await make_user(session, telegram_id=7517)
    first_order = await make_order(session, bob, status=OrderStatus.COMPLETED)
    second_order = await make_order(session, bob, status=OrderStatus.COMPLETED)
    referrals = ReferralService(session)
    referral = (
        await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
    ).referral

    assert await referrals.qualify(referral.id, order_id=first_order.id) is True
    assert await referrals.qualify(referral.id, order_id=second_order.id) is False

    stored = await referrals.get_for_referred_user(bob.id)
    assert stored is not None
    assert (stored.status, stored.qualifying_order_id) == (
        ReferralStatus.QUALIFIED,
        first_order.id,
    )
    assert stored.qualified_at is not None


async def test_only_the_referred_customers_order_can_qualify_it(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7518)
    bob = await make_user(session, telegram_id=7519)
    alice_order = await make_order(session, alice, status=OrderStatus.COMPLETED)
    referrals = ReferralService(session)
    referral = (
        await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
    ).referral

    with pytest.raises(ValueError):
        await referrals.qualify(referral.id, order_id=alice_order.id)
    stored = await referrals.get_for_referred_user(bob.id)
    assert stored is not None and stored.status is ReferralStatus.PENDING


@pytest.mark.parametrize(
    "order_state",
    [
        {"status": OrderStatus.NEW},
        {"status": OrderStatus.SHIPPED},
        {"status": OrderStatus.CANCELLED},
        {"status": OrderStatus.COMPLETED, "total_price": Decimal("0.00")},
        {"status": OrderStatus.COMPLETED, "loyalty_eligible": False},
    ],
    ids=["new", "shipped", "cancelled", "charged-nothing", "placed-before-launch"],
)
async def test_only_a_completed_paid_order_qualifies_a_referral(
    session: AsyncSession, order_state: dict[str, object]
) -> None:
    """Owner decision: bonuses follow the referred customer's first real purchase."""
    alice = await make_user(session, telegram_id=7520)
    bob = await make_user(session, telegram_id=7521)
    order = await make_order(session, bob)
    for field, value in order_state.items():
        setattr(order, field, value)
    await session.flush()
    referrals = ReferralService(session)
    referral = (
        await referrals.attribute(referrer_user_id=alice.id, referred_user_id=bob.id)
    ).referral

    with pytest.raises(ValueError):
        await referrals.qualify(referral.id, order_id=order.id)
    stored = await referrals.get_for_referred_user(bob.id)
    assert stored is not None and stored.status == ReferralStatus.PENDING
