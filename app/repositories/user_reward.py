"""UserReward repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import RewardStatus
from app.models.reward import UserReward
from app.repositories.base import BaseRepository


class UserRewardRepository(BaseRepository[UserReward]):
    model = UserReward

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_for_user(
        self,
        reward_id: int,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> UserReward | None:
        """Load a reward only if it belongs to the given user."""
        stmt = select(UserReward).where(UserReward.id == reward_id, UserReward.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self.session.scalars(stmt)
        return result.first()

    async def list_available_for_user(self, user_id: int) -> list[UserReward]:
        """Oldest first, so the list reads in the order the rewards were won."""
        result = await self.session.scalars(
            select(UserReward)
            .where(UserReward.user_id == user_id, UserReward.status == RewardStatus.AVAILABLE)
            .order_by(UserReward.id.asc())
        )
        return list(result.all())

    async def get_for_order(self, order_id: int) -> UserReward | None:
        result = await self.session.scalars(
            select(UserReward).where(UserReward.order_id == order_id)
        )
        return result.first()

    async def get_for_spin(self, spin_id: int) -> UserReward | None:
        result = await self.session.scalars(select(UserReward).where(UserReward.spin_id == spin_id))
        return result.first()
