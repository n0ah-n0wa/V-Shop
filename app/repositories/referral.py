"""Referral repository."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.referral import Referral
from app.repositories.base import BaseRepository

# The PostgreSQL advisory lock every attribution takes. Its value is arbitrary;
# it only has to be the same everywhere.
ATTRIBUTION_LOCK_KEY = 7_345_119_001


class ReferralRepository(BaseRepository[Referral]):
    model = Referral

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_referred_user_id(self, user_id: int) -> Referral | None:
        result = await self.session.scalars(
            select(Referral).where(Referral.referred_user_id == user_id)
        )
        return result.first()

    async def lock_attributions(self) -> None:
        """
        Serialise referral attribution until this transaction ends.

        Transaction-scoped (``pg_advisory_xact_lock``): the next attribution
        waits until this one commits or rolls back, then sees its referral.
        PostgreSQL only; the SQLite test suite runs without it.
        """
        if self.session.get_bind().dialect.name == "postgresql":
            await self.session.execute(select(func.pg_advisory_xact_lock(ATTRIBUTION_LOCK_KEY)))

    async def get_for_update(self, referral_id: int) -> Referral | None:
        result = await self.session.scalars(
            select(Referral)
            .where(Referral.id == referral_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.first()
