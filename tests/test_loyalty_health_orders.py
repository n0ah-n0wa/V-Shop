"""
The deploy check reads every loyalty row against the order it names.

The database ties the loyalty tables to each other per customer (composite
foreign keys), but a stamp, a milestone spin, a used reward or a payout also
names an order — and nothing but the services decides that the order is the
customer's own qualifying purchase. ``loyalty_health`` is the after-the-fact
check, so each kind of misbooking must move exactly its own counter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    CityChoice,
    LoyaltyTransactionType,
    OrderStatus,
    ReferralStatus,
    RewardSource,
    RewardStatus,
    RewardType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order, OrderItem
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
from app.verify_deployment import loyalty_health
from tests.factories import make_category, make_product, make_user


async def order_of(
    session: AsyncSession,
    user: User,
    total: str,
    *,
    status: OrderStatus = OrderStatus.COMPLETED,
    eligible: bool = True,
) -> Order:
    order = Order(
        user_id=user.id,
        customer_name="Test",
        city=CityChoice.BERLIN.value,
        delivery_type="pickup",
        address="Street 1",
        preferred_time="18:00",
        total_price=Decimal(total),
        status=status,
        loyalty_eligible=eligible,
    )
    session.add(order)
    await session.flush()
    return order


async def integrity(session: AsyncSession) -> dict[str, int]:
    return (await loyalty_health(session))["integrity"]


async def test_purchase_stamps_sit_only_on_the_customers_own_qualifying_order(
    session: AsyncSession,
) -> None:
    alice = await make_user(session, telegram_id=9601)
    bob = await make_user(session, telegram_id=9602)
    bobs = await order_of(session, bob, "100.00")  # qualifying — but Bob's
    still_open = await order_of(session, alice, "40.00", status=OrderStatus.SHIPPED)
    free = await order_of(session, alice, "0.00")
    before_launch = await order_of(session, alice, "40.00", eligible=False)
    own = await order_of(session, alice, "40.00")
    session.add(LoyaltyAccount(user_id=alice.id, stamp_balance=10, qualifying_purchase_count=5))
    for balance, order in enumerate((bobs, still_open, free, before_launch, own), start=1):
        session.add(
            LoyaltyTransaction(
                user_id=alice.id,
                kind=LoyaltyTransactionType.PURCHASE,
                amount=2,
                balance_after=balance * 2,
                order_id=order.id,
            )
        )
    await session.flush()

    report = await integrity(session)

    assert report["purchase_stamps_on_orders_that_do_not_qualify"] == 4  # all but her own
    assert report["balances_disagreeing_with_ledger"] == 0


async def test_a_milestone_spin_needs_its_purchase_on_the_ledger(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=9603)
    never_booked = await order_of(session, alice, "40.00")
    session.add(
        RouletteSpinGrant(
            user_id=alice.id,
            reason=SpinGrantReason.PURCHASE_MILESTONE,
            order_id=never_booked.id,
        )
    )
    await session.flush()

    assert (await integrity(session))["milestone_spins_without_their_purchase"] == 1


async def test_a_used_free_bottle_needs_its_free_line_on_the_customers_own_order(
    session: AsyncSession,
) -> None:
    alice = await make_user(session, telegram_id=9604)
    bob = await make_user(session, telegram_id=9605)
    product = await make_product(session, await make_category(session), price="15.00")
    bobs = await order_of(session, bob, "30.00")
    session.add(
        OrderItem(order_id=bobs.id, product_id=product.id, quantity=2, price=Decimal("15.00"))
    )
    session.add(
        UserReward(
            user_id=alice.id,
            kind=RewardType.FREE_BOTTLE,
            value=1,
            max_item_price=Decimal("20.00"),
            source=RewardSource.STAMP_CARD,
            status=RewardStatus.USED,
            order_id=bobs.id,
            used_at=datetime.now(UTC),
            discount_amount=Decimal("15.00"),
            redeemed_product_id=product.id,
        )
    )
    await session.flush()

    report = await integrity(session)

    assert report["used_rewards_on_another_customers_order"] == 1
    assert report["free_bottles_without_their_free_line"] == 1


async def test_a_referral_is_qualified_only_by_the_friends_qualifying_order(
    session: AsyncSession,
) -> None:
    referrer = await make_user(session, telegram_id=9606)
    friend = await make_user(session, telegram_id=9607)
    not_yet_completed = await order_of(session, friend, "40.00", status=OrderStatus.SHIPPED)
    session.add(
        Referral(
            referrer_user_id=referrer.id,
            referred_user_id=friend.id,
            status=ReferralStatus.QUALIFIED,
            qualifying_order_id=not_yet_completed.id,
            qualified_at=datetime.now(UTC),
        )
    )
    await session.flush()

    assert (await integrity(session))["referrals_qualified_by_an_order_that_does_not"] == 1
