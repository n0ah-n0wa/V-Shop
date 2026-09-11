"""LoyaltyAccount repository."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Executable, exists, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loyalty import LoyaltyAccount
from app.models.user import User
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

    async def insert_missing_accounts(self) -> int:
        """
        Open an empty account for every user who has none, in one statement.

        Zero stamps, zero purchases, no referral code — the row lazy creation
        makes. ``ON CONFLICT DO NOTHING`` absorbs an account another transaction
        opens at the same moment (``user_id`` is unique), so the statement never
        fails, never duplicates and never touches an existing account. Returns
        the rows inserted.
        """
        missing = (
            select(User.id)
            .where(~exists().where(LoyaltyAccount.user_id == User.id))
            # In one fixed order: two backfills racing (two instances starting)
            # then queue behind each other's rows instead of deadlocking.
            .order_by(User.id)
        )
        dialect = self.session.get_bind().dialect.name
        statement: Executable
        if dialect == "postgresql":
            statement = (
                postgresql.insert(LoyaltyAccount)
                .from_select(["user_id"], missing)
                .on_conflict_do_nothing()
            )
        elif dialect == "sqlite":
            statement = (
                sqlite.insert(LoyaltyAccount)
                .from_select(["user_id"], missing)
                .on_conflict_do_nothing()
            )
        else:  # pragma: no cover - production is PostgreSQL, the suite SQLite
            raise NotImplementedError(f"No idempotent bulk account backfill for {dialect}")
        result = cast(CursorResult[Any], await self.session.execute(statement))
        return max(result.rowcount, 0)

    async def get_referral_code(self, user_id: int) -> str | None:
        """The customer's stored code, read from the row itself: no lock, no identity map."""
        return await self.session.scalar(
            select(LoyaltyAccount.referral_code).where(LoyaltyAccount.user_id == user_id)
        )

    async def get_by_referral_code(self, code: str) -> LoyaltyAccount | None:
        result = await self.session.scalars(
            select(LoyaltyAccount).where(LoyaltyAccount.referral_code == code)
        )
        return result.first()
