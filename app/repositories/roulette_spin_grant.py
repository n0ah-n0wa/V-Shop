"""RouletteSpinGrant repository."""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SpinGrantReason
from app.models.roulette import RouletteSpinGrant
from app.repositories.base import BaseRepository


class RouletteSpinGrantRepository(BaseRepository[RouletteSpinGrant]):
    model = RouletteSpinGrant

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def _first(self, *conditions: ColumnElement[bool]) -> RouletteSpinGrant | None:
        result = await self.session.scalars(select(RouletteSpinGrant).where(*conditions))
        return result.first()

    async def get_initial_for_user(self, user_id: int) -> RouletteSpinGrant | None:
        return await self._first(
            RouletteSpinGrant.user_id == user_id,
            RouletteSpinGrant.reason == SpinGrantReason.INITIAL_PROMO,
        )

    async def get_for_order(self, order_id: int) -> RouletteSpinGrant | None:
        return await self._first(RouletteSpinGrant.order_id == order_id)

    async def get_for_referral(self, referral_id: int, user_id: int) -> RouletteSpinGrant | None:
        return await self._first(
            RouletteSpinGrant.referral_id == referral_id,
            RouletteSpinGrant.user_id == user_id,
        )

    async def first_available(
        self,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> RouletteSpinGrant | None:
        """The oldest unspent grant — spins are consumed in the order granted."""
        stmt = (
            select(RouletteSpinGrant)
            .where(
                RouletteSpinGrant.user_id == user_id,
                RouletteSpinGrant.consumed_at.is_(None),
            )
            .order_by(RouletteSpinGrant.id.asc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self.session.scalars(stmt)
        return result.first()

    async def count_available(self, user_id: int) -> int:
        value = await self.session.scalar(
            select(func.count())
            .select_from(RouletteSpinGrant)
            .where(
                RouletteSpinGrant.user_id == user_id,
                RouletteSpinGrant.consumed_at.is_(None),
            )
        )
        return int(value or 0)

    async def list_for_user(self, user_id: int) -> list[RouletteSpinGrant]:
        result = await self.session.scalars(
            select(RouletteSpinGrant)
            .where(RouletteSpinGrant.user_id == user_id)
            .order_by(RouletteSpinGrant.id.asc())
        )
        return list(result.all())
