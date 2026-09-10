"""LoyaltyAccount repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loyalty import LoyaltyAccount
from app.repositories.base import BaseRepository


class LoyaltyAccountRepository(BaseRepository[LoyaltyAccount]):
    model = LoyaltyAccount

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_user_id(
        self,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> LoyaltyAccount | None:
        stmt = select(LoyaltyAccount).where(LoyaltyAccount.user_id == user_id)
        if for_update:
            # A row lock only helps if we then act on the values it protects,
            # not on whatever an earlier query left in the identity map.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self.session.scalars(stmt)
        return result.first()

    async def get_or_create_for_user(self, user_id: int) -> tuple[LoyaltyAccount, bool]:
        """Return (account, created). Safe against a concurrent first access."""
        account = await self.get_by_user_id(user_id)
        if account is not None:
            return account, False
        try:
            async with self.session.begin_nested():
                account = await self.create_and_add(user_id=user_id)
            return account, True
        except IntegrityError:
            account = await self.get_by_user_id(user_id)
            if account is None:
                raise
            return account, False

    async def get_by_referral_code(self, code: str) -> LoyaltyAccount | None:
        result = await self.session.scalars(
            select(LoyaltyAccount).where(LoyaltyAccount.referral_code == code)
        )
        return result.first()
