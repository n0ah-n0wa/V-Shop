"""RouletteSpin (spin history) repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.roulette import RouletteSpin
from app.repositories.base import BaseRepository


class RouletteSpinRepository(BaseRepository[RouletteSpin]):
    model = RouletteSpin

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_grant_id(self, grant_id: int) -> RouletteSpin | None:
        result = await self.session.scalars(
            select(RouletteSpin).where(RouletteSpin.grant_id == grant_id)
        )
        return result.first()

    async def list_for_user(self, user_id: int, *, limit: int | None = None) -> list[RouletteSpin]:
        """Newest first."""
        stmt = (
            select(RouletteSpin)
            .where(RouletteSpin.user_id == user_id)
            .order_by(RouletteSpin.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())
