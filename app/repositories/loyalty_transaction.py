"""LoyaltyTransaction (stamp ledger) repository."""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import LoyaltyTransactionType
from app.models.loyalty import LoyaltyTransaction
from app.repositories.base import BaseRepository


class LoyaltyTransactionRepository(BaseRepository[LoyaltyTransaction]):
    model = LoyaltyTransaction

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def _first(self, *conditions: ColumnElement[bool]) -> LoyaltyTransaction | None:
        result = await self.session.scalars(select(LoyaltyTransaction).where(*conditions))
        return result.first()

    # --- lookups by the event that caused a row (the idempotency keys) ---------

    async def get_purchase_for_order(self, order_id: int) -> LoyaltyTransaction | None:
        return await self._first(LoyaltyTransaction.order_id == order_id)

    async def get_for_referral(self, referral_id: int, user_id: int) -> LoyaltyTransaction | None:
        return await self._first(
            LoyaltyTransaction.referral_id == referral_id,
            LoyaltyTransaction.user_id == user_id,
        )

    async def get_for_spin(self, spin_id: int) -> LoyaltyTransaction | None:
        return await self._first(LoyaltyTransaction.spin_id == spin_id)

    async def get_for_reward(self, reward_id: int) -> LoyaltyTransaction | None:
        return await self._first(LoyaltyTransaction.reward_id == reward_id)

    # --- history and audit --------------------------------------------------------

    async def list_for_user(
        self,
        user_id: int,
        *,
        limit: int | None = None,
    ) -> list[LoyaltyTransaction]:
        """Newest first."""
        stmt = (
            select(LoyaltyTransaction)
            .where(LoyaltyTransaction.user_id == user_id)
            .order_by(LoyaltyTransaction.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def sum_for_user(self, user_id: int) -> int:
        """The balance as the ledger explains it — must equal the cached one."""
        value = await self.session.scalar(
            select(func.coalesce(func.sum(LoyaltyTransaction.amount), 0)).where(
                LoyaltyTransaction.user_id == user_id
            )
        )
        return int(value or 0)

    async def count_for_user(self, user_id: int, kind: LoyaltyTransactionType) -> int:
        value = await self.session.scalar(
            select(func.count())
            .select_from(LoyaltyTransaction)
            .where(LoyaltyTransaction.user_id == user_id, LoyaltyTransaction.kind == kind)
        )
        return int(value or 0)
