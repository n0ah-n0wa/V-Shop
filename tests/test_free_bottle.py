"""
Free bottles: unlocking one for 10 stamps, and redeeming it on an order.

One mechanism serves both sources — a stamp-card free bottle and a roulette free
bottle are the same kind of reward row and redeem through the same checkout path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    RewardSource,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.product import Product
from app.models.reward import UserReward
from app.models.user import User
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.repositories.order_item import OrderItemRepository
from app.services.admin import AdminService
from app.services.cart import CartService
from app.services.loyalty import InsufficientStampsError, LoyaltyService, StaleCardError
from app.services.order import OrderService
from app.services.reward import (
    FreeBottlePlan,
    RewardNotApplicableError,
    RewardOrderError,
    RewardService,
    RewardUnavailableError,
    choose_free_bottle,
)
from app.services.roulette import RoulettePrize, RouletteService
from app.services.stamp_card import PurchaseAwardStatus, StampCardService
from tests.factories import add_order_item, make_category, make_order, make_product, make_user

CEILING = Decimal("20.00")
FREE_BOTTLE_PRIZE = RoulettePrize("free_bottle", RoulettePrizeType.FREE_BOTTLE, 1)
DISCOUNT_PRIZE = RoulettePrize("discount_10", RoulettePrizeType.DISCOUNT_PERCENT, 10)


async def give_stamps(session: AsyncSession, user: User, amount: int) -> None:
    await LoyaltyService(session).adjust(user.id, amount=amount, note="test setup")


async def claimed_bottle(session: AsyncSession, user: User) -> UserReward:
    await give_stamps(session, user, 10)
    return await StampCardService(session).claim_free_bottle(user.id)


async def products(session: AsyncSession, *prices: str) -> list[Product]:
    category = await make_category(session, name="Liquids")
    return [
        await make_product(session, category, name_en=f"Bottle {price}", price=price)
        for price in prices
    ]


async def fill_cart(session: AsyncSession, user: User, lines: list[tuple[Product, int]]) -> None:
    cart = CartService(session)
    for product, quantity in lines:
        await cart.add_product(user.id, product, quantity=quantity)


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


async def checkout(
    session: AsyncSession,
    user: User,
    lines: list[tuple[Product, int]],
    *,
    reward_id: int | None = None,
) -> Order:
    await fill_cart(session, user, lines)
    return await place(session, user, reward_id=reward_id)


async def count(session: AsyncSession, model: type, *where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int(await session.scalar(stmt) or 0)


async def balance(session: AsyncSession, user_id: int) -> int:
    """Straight from the database, never from the identity map."""
    value = await session.scalar(
        select(LoyaltyAccount.stamp_balance).where(LoyaltyAccount.user_id == user_id)
    )
    return int(value or 0)


async def reward_status(session: AsyncSession, reward_id: int) -> RewardStatus | None:
    return await session.scalar(select(UserReward.status).where(UserReward.id == reward_id))


# ============================================================ unlocking (claim)


async def test_exactly_ten_stamps_unlock_one_free_bottle(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8101)
    await give_stamps(session, user, 10)
    stamp_card = StampCardService(session)
    card = await stamp_card.card(user.id)
    assert (card.can_claim, card.free_bottles_unlocked) == (True, 1)

    reward = await stamp_card.claim_free_bottle(user.id, card_version=card.version)

    assert (reward.kind, reward.source, reward.status, reward.max_item_price) == (
        RewardType.FREE_BOTTLE,
        RewardSource.STAMP_CARD,
        RewardStatus.AVAILABLE,
        CEILING,
    )
    assert await balance(session, user.id) == 0
    debit = (
        await session.scalars(
            select(LoyaltyTransaction).where(LoyaltyTransaction.reward_id == reward.id)
        )
    ).one()
    assert (debit.kind, debit.amount, debit.balance_after) == (
        LoyaltyTransactionType.REDEMPTION,
        -10,
        0,
    ), "exactly 10 stamps, and the debit names the reward it bought"


async def test_more_than_ten_stamps_keep_the_remainder(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8102)
    await give_stamps(session, user, 13)
    stamp_card = StampCardService(session)

    await stamp_card.claim_free_bottle(user.id)

    card = await stamp_card.card(user.id)
    assert (card.stamps, card.progress, card.can_claim) == (3, 3, False)


async def test_twenty_stamps_unlock_exactly_two(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8103)
    await give_stamps(session, user, 20)
    stamp_card = StampCardService(session)

    for _ in range(2):
        card = await stamp_card.card(user.id)
        await stamp_card.claim_free_bottle(user.id, card_version=card.version)
    with pytest.raises(InsufficientStampsError):
        await stamp_card.claim_free_bottle(user.id)

    assert await balance(session, user.id) == 0
    assert await count(session, UserReward) == 2


async def test_fewer_than_ten_stamps_unlock_nothing(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8104)
    await give_stamps(session, user, 9)

    with pytest.raises(InsufficientStampsError):
        await StampCardService(session).claim_free_bottle(user.id)

    assert await balance(session, user.id) == 9
    assert await count(session, UserReward) == 0
    assert await count(session, LoyaltyTransaction) == 1, "only the setup row"


async def test_ten_stamps_cannot_pay_for_two_bottles(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8105)
    await give_stamps(session, user, 10)
    stamp_card = StampCardService(session)

    await stamp_card.claim_free_bottle(user.id)
    with pytest.raises(InsufficientStampsError):
        await stamp_card.claim_free_bottle(user.id)

    assert await balance(session, user.id) == 0
    assert await count(session, UserReward) == 1


async def test_one_card_cannot_be_claimed_twice(session: AsyncSession) -> None:
    """A double tap on one card: the second request finds the card has changed."""
    user = await make_user(session, telegram_id=8106)
    await give_stamps(session, user, 20)
    stamp_card = StampCardService(session)
    card = await stamp_card.card(user.id)

    await stamp_card.claim_free_bottle(user.id, card_version=card.version)
    with pytest.raises(StaleCardError):
        await stamp_card.claim_free_bottle(user.id, card_version=card.version)

    assert await balance(session, user.id) == 10
    assert await count(session, UserReward) == 1
    fresh = await stamp_card.card(user.id)
    await stamp_card.claim_free_bottle(user.id, card_version=fresh.version)
    assert await balance(session, user.id) == 0


async def test_a_failed_claim_leaves_stamps_and_rewards_untouched(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reward row is written first; when the debit fails, both roll back."""
    user = await make_user(session, telegram_id=8107)
    await give_stamps(session, user, 10)
    user_id = user.id
    await session.commit()

    async def broken(self: LoyaltyTransactionRepository, **fields: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(LoyaltyTransactionRepository, "create_and_add", broken)
    with pytest.raises(RuntimeError):
        await StampCardService(session).claim_free_bottle(user_id)
    await session.rollback()
    monkeypatch.undo()

    assert await balance(session, user_id) == 10
    assert await count(session, UserReward) == 0
    assert await count(session, LoyaltyTransaction) == 1


async def test_the_balance_can_never_go_negative(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8108)
    loyalty = LoyaltyService(session)

    with pytest.raises(InsufficientStampsError):
        await loyalty.adjust(user.id, amount=-1, note="overdraw attempt")
    with pytest.raises(InsufficientStampsError):
        await StampCardService(session).claim_free_bottle(user.id)
    # And the database refuses it even when the services are bypassed.
    await loyalty.get_or_create_account(user.id)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                update(LoyaltyAccount)
                .where(LoyaltyAccount.user_id == user.id)
                .values(stamp_balance=-1)
                .execution_options(synchronize_session=False)
            )

    assert await balance(session, user.id) == 0


# ======================================================== redeeming at checkout


def test_the_dearest_bottle_within_the_cap_is_chosen() -> None:
    lines = [(1, 1, Decimal("18.00")), (2, 2, Decimal("20.00")), (3, 1, Decimal("22.00"))]
    assert choose_free_bottle(lines, CEILING) == (2, Decimal("20.00"))
    tie = [(5, 1, Decimal("20.00")), (4, 1, Decimal("20.00"))]
    assert choose_free_bottle(tie, CEILING) == (4, Decimal("20.00")), "deterministic"
    assert choose_free_bottle([(1, 1, Decimal("20.01"))], CEILING) is None
    assert choose_free_bottle([], CEILING) is None


def test_a_plan_frees_exactly_one_unit() -> None:
    plan = FreeBottlePlan(reward_id=1, product_id=2, discount=Decimal("20.00"))
    lines = [(1, 1, Decimal("12.00")), (2, 3, Decimal("20.00"))]

    assert plan.apply(lines) == [
        (1, 1, Decimal("12.00")),
        (2, 2, Decimal("20.00")),
        (2, 1, Decimal("0.00")),
    ]
    assert plan.apply([(2, 1, Decimal("20.00"))]) == [(2, 1, Decimal("0.00"))]
    with pytest.raises(RewardNotApplicableError):
        plan.apply([(1, 1, Decimal("12.00"))])


async def test_a_free_bottle_is_redeemed_at_checkout(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8111)
    reward = await claimed_bottle(session, user)
    bottle, liquid = await products(session, "20.00", "12.00")

    order = await checkout(session, user, [(bottle, 3), (liquid, 1)], reward_id=reward.id)

    assert order.total_price == Decimal("52.00"), "3 × €20 + €12, one bottle free"
    assert sorted((i.product_id, i.quantity, i.price) for i in order.items) == sorted(
        [
            (bottle.id, 2, Decimal("20.00")),
            (bottle.id, 1, Decimal("0.00")),
            (liquid.id, 1, Decimal("12.00")),
        ]
    )
    used = await RewardService(session).get_for_user(reward.id, user.id)
    assert used is not None
    assert (used.status, used.order_id, used.discount_amount, used.redeemed_product_id) == (
        RewardStatus.USED,
        order.id,
        Decimal("20.00"),
        bottle.id,
    )
    assert used.used_at is not None
    assert await RewardService(session).list_available(user.id) == []


async def test_the_dearest_eligible_bottle_is_the_free_one(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8112)
    reward = await claimed_bottle(session, user)
    cheap, right, dear = await products(session, "18.00", "20.00", "22.00")

    order = await checkout(session, user, [(cheap, 1), (right, 1), (dear, 1)], reward_id=reward.id)

    assert order.total_price == Decimal("40.00")
    assert (reward.redeemed_product_id, reward.discount_amount) == (right.id, CEILING)


async def test_a_reward_cannot_be_redeemed_twice(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8113)
    reward = await claimed_bottle(session, user)
    (bottle,) = await products(session, "20.00")
    await checkout(session, user, [(bottle, 1)], reward_id=reward.id)

    with pytest.raises(RewardUnavailableError):
        await checkout(session, user, [(bottle, 1)], reward_id=reward.id)

    assert await count(session, Order) == 1
    view = await CartService(session).get_view(user.id, language="en")
    assert view is not None and len(view.lines) == 1, "the refused checkout left the cart as it was"


async def test_a_cart_without_an_eligible_bottle_is_refused_untouched(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=8114)
    reward = await claimed_bottle(session, user)
    (too_dear,) = await products(session, "22.00")

    with pytest.raises(RewardNotApplicableError):
        await checkout(session, user, [(too_dear, 1)], reward_id=reward.id)

    assert await count(session, Order) == 0
    assert await reward_status(session, reward.id) == RewardStatus.AVAILABLE
    view = await CartService(session).get_view(user.id, language="en")
    assert view is not None and len(view.lines) == 1


async def test_nobody_can_redeem_someone_elses_reward(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=8115)
    mallory = await make_user(session, telegram_id=8116)
    reward = await claimed_bottle(session, alice)
    (bottle,) = await products(session, "20.00")

    with pytest.raises(RewardUnavailableError):
        await checkout(session, mallory, [(bottle, 1)], reward_id=reward.id)

    assert await count(session, Order) == 0
    assert await reward_status(session, reward.id) == RewardStatus.AVAILABLE


async def test_a_failed_checkout_keeps_the_reward(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reward is locked and planned, then the order lines fail: nothing sticks."""
    user = await make_user(session, telegram_id=8117)
    reward = await claimed_bottle(session, user)
    (bottle,) = await products(session, "20.00")
    await fill_cart(session, user, [(bottle, 2)])
    reward_id, user_id = reward.id, user.id
    await session.commit()

    async def broken(self: OrderItemRepository, order_id: int, items: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(OrderItemRepository, "add_items", broken)
    with pytest.raises(RuntimeError):
        await place(session, user, reward_id=reward_id)
    await session.rollback()
    monkeypatch.undo()

    assert await reward_status(session, reward_id) == RewardStatus.AVAILABLE
    assert (
        await session.scalar(select(UserReward.order_id).where(UserReward.id == reward_id)) is None
    )
    assert await count(session, Order) == 0
    assert await balance(session, user_id) == 0, "the claim itself was committed before"


async def test_a_roulette_free_bottle_is_redeemed_by_the_same_mechanism(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=8118)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)
    outcome = await roulette.spin(user.id, FREE_BOTTLE_PRIZE, free_bottle_max_price=CEILING)
    assert outcome is not None and outcome.reward is not None
    (bottle,) = await products(session, "20.00")

    order = await checkout(session, user, [(bottle, 2)], reward_id=outcome.reward.id)

    assert order.total_price == Decimal("20.00")
    assert (outcome.reward.source, outcome.reward.status, outcome.reward.redeemed_product_id) == (
        RewardSource.ROULETTE,
        RewardStatus.USED,
        bottle.id,
    )


async def test_a_discount_reward_cannot_pay_for_a_bottle(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=8119)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)
    outcome = await roulette.spin(user.id, DISCOUNT_PRIZE)
    assert outcome is not None and outcome.reward is not None
    (bottle,) = await products(session, "20.00")

    with pytest.raises(RewardNotApplicableError):
        await checkout(session, user, [(bottle, 1)], reward_id=outcome.reward.id)

    assert await count(session, Order) == 0
    assert await reward_status(session, outcome.reward.id) == RewardStatus.AVAILABLE


async def test_stamps_after_a_redemption_come_from_what_was_charged(
    session: AsyncSession,
) -> None:
    """Owner decision: the free bottle itself earns no stamps."""
    user = await make_user(session, telegram_id=8120)
    (bottle,) = await products(session, "20.00")
    admin = AdminService(session)

    paid = await checkout(
        session, user, [(bottle, 3)], reward_id=(await claimed_bottle(session, user)).id
    )
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        paid = await admin.set_order_status(paid, status)
    assert paid.total_price == Decimal("40.00")
    assert await balance(session, user.id) == 2

    only_free = await checkout(
        session, user, [(bottle, 1)], reward_id=(await claimed_bottle(session, user)).id
    )
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        only_free = await admin.set_order_status(only_free, status)
    award = await StampCardService(session).award_for_order(only_free.id)

    assert only_free.total_price == Decimal("0.00")
    assert award.status == PurchaseAwardStatus.NOT_PAID
    assert await balance(session, user.id) == 2


async def test_binding_is_only_to_a_new_order_holding_its_free_unit(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=8121)
    reward = await claimed_bottle(session, user)
    (bottle,) = await products(session, "20.00")
    completed = await make_order(session, user, status=OrderStatus.COMPLETED)
    await add_order_item(session, completed, bottle, price="0.00")
    nothing_free = await make_order(session, user)
    await add_order_item(session, nothing_free, bottle)
    rewards = RewardService(session)

    def attempt(order: Order, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "user_id": user.id,
            "order_id": order.id,
            "discount_amount": CEILING,
            "redeemed_product_id": bottle.id,
        }
        return rewards.use_reward(reward.id, **(fields | overrides))

    with pytest.raises(RewardOrderError):
        await attempt(completed)
    with pytest.raises(RewardOrderError):
        await attempt(nothing_free)
    with pytest.raises(RewardNotApplicableError):
        await attempt(nothing_free, redeemed_product_id=None)
    with pytest.raises(RewardNotApplicableError):
        await attempt(nothing_free, discount_amount=Decimal("25.00"))
    with pytest.raises(TypeError):
        await attempt(nothing_free, discount_amount=20.0)

    assert await reward_status(session, reward.id) == RewardStatus.AVAILABLE
