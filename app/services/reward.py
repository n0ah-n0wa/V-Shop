"""Customer rewards — redeeming them on an order, and recording what they gave.

Rewards are issued where they are earned — :meth:`StampCardService.
claim_free_bottle` (the stamp card) and :meth:`RouletteService.spin` — and
redeemed here by one mechanism whatever their source: a roulette free bottle and
a stamp-card free bottle are the same kind of row and go through the same code.

Redemption happens at checkout, inside the transaction that creates the order
(:meth:`OrderService.place_order_from_cart`):

1. :meth:`RewardService.plan` — before anything is written — locks the reward,
   checks it is the customer's and still available, and decides what it does:
   a free bottle picks the unit it will pay for, a percentage discount the
   amount it takes off the total;
2. the order is created with that unit at €0, or with its total lowered;
3. :meth:`RewardService.redeem` binds the reward to the order and records what
   it was worth (and, for a free bottle, which product it made free).

If any step fails the whole checkout rolls back: the reward stays available and
the order never exists.

What checkout *offers* comes from :meth:`RewardService.options`, which runs the
same planning without locking or writing — so an offer is exactly what
confirming does, and confirming re-checks it under lock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, RewardStatus, RewardType
from app.models.reward import UserReward
from app.repositories.order import OrderRepository
from app.repositories.order_item import OrderItemRepository
from app.repositories.product import ProductRepository
from app.repositories.user_reward import UserRewardRepository
from app.services.loyalty import LoyaltyError, LoyaltyService
from app.utils.validators import MIN_PRICE, MONEY_QUANTUM, to_money

FREE = Decimal("0.00")
PERCENT = Decimal("100")

# (product_id, quantity, unit_price) — the shape checkout builds order lines in.
Line = tuple[int, int, Decimal]


class RewardUnavailableError(LoyaltyError):
    """The reward does not exist, is not this customer's, or was already used."""


class RewardNotApplicableError(LoyaltyError):
    """The reward cannot pay for anything in this order."""


class RewardOrderError(LoyaltyError):
    """The order is missing, not this customer's, not new, or cannot take the reward."""


def choose_free_bottle(
    lines: Sequence[Line], max_item_price: Decimal
) -> tuple[int, Decimal] | None:
    """
    The unit a free bottle pays for: the dearest product within its price cap.

    Owner decision: a free bottle covers one product costing €20 or less. The
    dearest eligible one gives the customer the full value; ties go to the
    lowest product id, so the choice is deterministic.
    """
    eligible = [
        (price, product_id)
        for product_id, quantity, price in lines
        if quantity > 0 and price <= max_item_price
    ]
    if not eligible:
        return None
    price, product_id = max(eligible, key=lambda item: (item[0], -item[1]))
    return product_id, price


@dataclass(frozen=True, slots=True)
class FreeBottlePlan:
    """What a free-bottle reward will do to an order — decided before any write."""

    reward_id: int
    product_id: int
    discount: Decimal

    @property
    def redeemed_product_id(self) -> int:
        return self.product_id

    def apply(self, lines: Sequence[Line]) -> list[Line]:
        """Split one unit of the chosen product off at €0; leave everything else."""
        applied: list[Line] = []
        done = False
        for product_id, quantity, price in lines:
            if not done and product_id == self.product_id and price == self.discount:
                if quantity > 1:
                    applied.append((product_id, quantity - 1, price))
                applied.append((product_id, 1, FREE))
                done = True
            else:
                applied.append((product_id, quantity, price))
        if not done:
            raise RewardNotApplicableError(
                f"Product {self.product_id} at {self.discount} is not in the order"
            )
        return applied


def percentage_off(total: Decimal, percent: int) -> Decimal:
    """``percent``% of ``total``, rounded half up to the cent: 5% of €12.50 is €0.63."""
    return (total * percent / PERCENT).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class DiscountPlan:
    """What a percentage-discount reward will do to an order — decided before any write."""

    reward_id: int
    percent: int
    discount: Decimal

    @property
    def redeemed_product_id(self) -> None:
        return None

    def apply(self, lines: Sequence[Line]) -> list[Line]:
        """The lines stay as they are: the discount comes off the order total."""
        return list(lines)


RewardPlan = FreeBottlePlan | DiscountPlan


def _free_bottle_plan(reward: UserReward, lines: Sequence[Line]) -> FreeBottlePlan:
    if reward.kind != RewardType.FREE_BOTTLE or reward.max_item_price is None:
        raise RewardNotApplicableError(f"Reward {reward.id} is not a free bottle")
    choice = choose_free_bottle(lines, reward.max_item_price)
    if choice is None:
        raise RewardNotApplicableError(
            f"No product in the order costs {reward.max_item_price} or less"
        )
    product_id, price = choice
    return FreeBottlePlan(reward_id=reward.id, product_id=product_id, discount=price)


def _discount_plan(reward: UserReward, lines: Sequence[Line]) -> DiscountPlan:
    total = sum((price * quantity for _, quantity, price in lines), FREE)
    discount = percentage_off(total, reward.value)
    if discount < MIN_PRICE:
        raise RewardNotApplicableError(f"{reward.value}% of {total} is less than a cent")
    return DiscountPlan(reward_id=reward.id, percent=reward.value, discount=discount)


def _plan(reward: UserReward, lines: Sequence[Line]) -> RewardPlan:
    """What ``reward`` would do to ``lines`` — one decision, for offers and for orders."""
    if reward.kind == RewardType.DISCOUNT_PERCENT:
        return _discount_plan(reward, lines)
    return _free_bottle_plan(reward, lines)


def _plan_or_none(reward: UserReward, lines: Sequence[Line]) -> RewardPlan | None:
    try:
        return _plan(reward, lines)
    except RewardNotApplicableError:
        return None


@dataclass(frozen=True, slots=True)
class RewardOption:
    """A reward the customer could use on the order at hand, and what it would do."""

    reward: UserReward
    plan: RewardPlan

    @property
    def reward_id(self) -> int:
        return self.reward.id

    @property
    def saving(self) -> Decimal:
        return self.plan.discount


class RewardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.rewards = UserRewardRepository(session)
        self.orders = OrderRepository(session)
        self.order_items = OrderItemRepository(session)
        self.products = ProductRepository(session)
        self.loyalty = LoyaltyService(session)

    async def list_available(self, user_id: int) -> list[UserReward]:
        return await self.rewards.list_available_for_user(user_id)

    async def get_for_user(self, reward_id: int, user_id: int) -> UserReward | None:
        return await self.rewards.get_for_user(reward_id, user_id)

    async def options(self, user_id: int, *, lines: Sequence[Line]) -> list[RewardOption]:
        """
        Read-only: the customer's available rewards that can do something for ``lines``.

        The decision :meth:`plan` makes when the order is placed, made without
        locking or writing — so what checkout offers is what placing the order
        does, and :meth:`plan` re-checks it under lock. Biggest saving first,
        then the oldest reward.
        """
        options = [
            RewardOption(reward, plan)
            for reward in await self.rewards.list_available_for_user(user_id)
            if (plan := _plan_or_none(reward, lines)) is not None
        ]
        return sorted(options, key=lambda option: (-option.saving, option.reward_id))

    async def plan(
        self,
        reward_id: int,
        *,
        user_id: int,
        lines: Sequence[Line],
    ) -> RewardPlan:
        """
        Lock one of the customer's rewards and decide what it does to ``lines``.

        Writes nothing. A free bottle pays for the dearest unit within its price
        cap; a percentage discount comes off the lines' total, rounded half up
        to the cent. Raises :class:`RewardUnavailableError` for a reward that is
        not this customer's or already used, and
        :class:`RewardNotApplicableError` for one that can do nothing here — no
        product within the cap, or an order too small for a whole cent off.
        """
        reward = await self._lock_available(reward_id, user_id)
        return _plan(reward, lines)

    async def plan_free_bottle(
        self,
        reward_id: int,
        *,
        user_id: int,
        lines: Sequence[Line],
    ) -> FreeBottlePlan:
        """Like :meth:`plan`, for a reward that must be a free bottle."""
        reward = await self._lock_available(reward_id, user_id)
        return _free_bottle_plan(reward, lines)

    async def redeem(self, plan: RewardPlan, *, user_id: int, order_id: int) -> UserReward:
        """Carry out a plan on the order it was made for."""
        return await self.use_reward(
            plan.reward_id,
            user_id=user_id,
            order_id=order_id,
            discount_amount=plan.discount,
            redeemed_product_id=plan.redeemed_product_id,
        )

    async def use_reward(
        self,
        reward_id: int,
        *,
        user_id: int,
        order_id: int,
        discount_amount: Decimal,
        redeemed_product_id: int | None = None,
    ) -> UserReward:
        """
        Bind a reward to a newly placed order and record what it gave. Exactly once.

        Ownership is checked on both sides; the order must be New and carry no
        other reward. The recorded value must be the true one, whoever the
        caller: a free bottle is worth its product's price — within its cap —
        and that product must be in the order at €0; a discount is exactly its
        percentage of the order's lines, already taken off the order total.
        Every check runs before the reward is touched. The binding is permanent
        — a reward used on an order that is later cancelled stays used (owner
        decision).
        """
        reward = await self._lock_available(reward_id, user_id)
        amount = to_money(discount_amount)
        if reward.kind == RewardType.FREE_BOTTLE:
            if redeemed_product_id is None:
                raise RewardNotApplicableError("A free bottle must name the product it made free")
            if reward.max_item_price is not None and amount > reward.max_item_price:
                raise RewardNotApplicableError(
                    f"{amount} exceeds the free bottle's cap of {reward.max_item_price}"
                )
            product = await self.products.get_by_id(redeemed_product_id)
            if product is None or product.price != amount:
                raise RewardNotApplicableError(
                    f"A free bottle is worth its product's price, not {amount}"
                )
        elif redeemed_product_id is not None:
            raise RewardNotApplicableError("Only a free bottle makes a product free")

        order = await self.orders.get_by_id(order_id)
        if order is None or order.user_id != user_id:
            raise RewardOrderError(f"Order {order_id} does not belong to user {user_id}")
        if order.status != OrderStatus.NEW:
            raise RewardOrderError(f"Order {order_id} is {order.status}; rewards apply at checkout")
        if await self.rewards.get_for_order(order_id) is not None:
            raise RewardOrderError(f"Order {order_id} already carries a reward")
        if redeemed_product_id is not None and not await self.order_items.has_unit_at_price(
            order_id, redeemed_product_id, FREE
        ):
            raise RewardOrderError(f"Order {order_id} has no free unit of {redeemed_product_id}")
        if reward.kind == RewardType.DISCOUNT_PERCENT:
            lines_total = await self._lines_total(order_id)
            if amount != percentage_off(lines_total, reward.value) or (
                order.total_price != lines_total - amount
            ):
                raise RewardNotApplicableError(
                    f"A {reward.value}% discount on {lines_total} is not {amount}"
                )

        reward.status = RewardStatus.USED
        reward.used_at = datetime.now(UTC)
        reward.order_id = order_id
        reward.discount_amount = amount
        reward.redeemed_product_id = redeemed_product_id
        await self.session.flush()
        return reward

    # --- internals ----------------------------------------------------------------

    async def _lock_available(self, reward_id: int, user_id: int) -> UserReward:
        """Per-customer lock first (the loyalty locking rule), then the reward row."""
        await self.loyalty.lock_account(user_id)
        reward = await self.rewards.get_for_user(reward_id, user_id, for_update=True)
        if reward is None or reward.status != RewardStatus.AVAILABLE:
            raise RewardUnavailableError(f"Reward {reward_id} is not available to user {user_id}")
        return reward

    async def _lines_total(self, order_id: int) -> Decimal:
        """What an order's lines add up to: each price times its quantity."""
        items = await self.order_items.list_by_order(order_id, with_product=False)
        return sum((item.price * item.quantity for item in items), FREE)
