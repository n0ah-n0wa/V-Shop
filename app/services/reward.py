"""Customer rewards — listing them, and binding one to an order.

Issuing rewards happens where they are earned: :meth:`LoyaltyService.
redeem_free_bottle` (stamp card) and :meth:`RouletteService.spin` (roulette).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import RewardStatus
from app.models.order import Order
from app.models.reward import UserReward
from app.repositories.user_reward import UserRewardRepository
from app.services.loyalty import LoyaltyService


class RewardUnavailableError(ValueError):
    """The reward does not exist, is not this customer's, or was already used."""


class RewardOrderError(ValueError):
    """The order is missing, belongs to someone else, or already carries a reward."""


class RewardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.rewards = UserRewardRepository(session)
        self.loyalty = LoyaltyService(session)

    async def list_available(self, user_id: int) -> list[UserReward]:
        return await self.rewards.list_available_for_user(user_id)

    async def get_for_user(self, reward_id: int, user_id: int) -> UserReward | None:
        return await self.rewards.get_for_user(reward_id, user_id)

    async def use_reward(self, reward_id: int, *, user_id: int, order_id: int) -> UserReward:
        """
        Mark a reward used on ``order_id``. Exactly once, one reward per order.

        Ownership is checked on both sides: the reward and the order must belong
        to ``user_id``, so a crafted id can neither spend nor receive someone
        else's reward. The binding is permanent — see :class:`UserReward`.
        """
        await self.loyalty.lock_account(user_id)
        reward = await self.rewards.get_for_user(reward_id, user_id, for_update=True)
        if reward is None or reward.status != RewardStatus.AVAILABLE:
            raise RewardUnavailableError(f"Reward {reward_id} is not available to user {user_id}")

        order = await self.session.get(Order, order_id)
        if order is None or order.user_id != user_id:
            raise RewardOrderError(f"Order {order_id} does not belong to user {user_id}")
        if await self.rewards.get_for_order(order_id) is not None:
            raise RewardOrderError(f"Order {order_id} already carries a reward")

        reward.status = RewardStatus.USED
        reward.used_at = datetime.now(UTC)
        reward.order_id = order_id
        await self.session.flush()
        return reward
