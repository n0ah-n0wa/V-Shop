"""Referral repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.referral import Referral
from app.repositories.base import BaseRepository


class ReferralRepository(BaseRepository[Referral]):
    model = Referral

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_referred_user_id(self, user_id: int) -> Referral | None:
        result = await self.session.scalars(
            select(Referral).where(Referral.referred_user_id == user_id)
        )
        return result.first()

    async def get_for_update(self, referral_id: int) -> Referral | None:
        result = await self.session.scalars(
            select(Referral)
            .where(Referral.id == referral_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.first()
