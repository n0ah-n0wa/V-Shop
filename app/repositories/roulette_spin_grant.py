"""RouletteSpinGrant repository."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import ColumnElement, Executable, exists, func, literal, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SpinGrantReason
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
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

    async def get_for_user(
        self,
        grant_id: int,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> RouletteSpinGrant | None:
        """A grant only if it belongs to the given customer."""
        stmt = select(RouletteSpinGrant).where(
            RouletteSpinGrant.id == grant_id,
            RouletteSpinGrant.user_id == user_id,
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self.session.scalars(stmt)
        return result.first()

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

    async def tally_for_user(self, user_id: int) -> list[tuple[SpinGrantReason, int, int]]:
        """Per reason: ``(reason, granted, spent)``."""
        rows = await self.session.execute(
            select(
                RouletteSpinGrant.reason,
                func.count(),
                func.count(RouletteSpinGrant.consumed_at),
            )
            .where(RouletteSpinGrant.user_id == user_id)
            .group_by(RouletteSpinGrant.reason)
        )
        return [(reason, int(total), int(spent)) for reason, total, spent in rows.all()]

    async def insert_missing_initial_grants(self) -> int:
        """
        Insert the welcome grant for every user who has none, in one statement.

        ``ON CONFLICT DO NOTHING`` absorbs a welcome grant another transaction
        makes at the same moment (the partial unique index allows one per user),
        so the statement neither fails nor duplicates. Returns the rows inserted.
        """
        reason = literal(SpinGrantReason.INITIAL_PROMO, RouletteSpinGrant.__table__.c.reason.type)
        missing = (
            select(User.id, reason)
            .where(
                ~exists().where(
                    RouletteSpinGrant.user_id == User.id,
                    RouletteSpinGrant.reason == SpinGrantReason.INITIAL_PROMO,
                )
            )
            # In one fixed order: two top-ups racing (two instances starting)
            # then queue behind each other's rows instead of deadlocking.
            .order_by(User.id)
        )
        columns = ["user_id", "reason"]
        dialect = self.session.get_bind().dialect.name
        statement: Executable
        if dialect == "postgresql":
            statement = (
                postgresql.insert(RouletteSpinGrant)
                .from_select(columns, missing)
                .on_conflict_do_nothing()
            )
        elif dialect == "sqlite":
            statement = (
                sqlite.insert(RouletteSpinGrant)
                .from_select(columns, missing)
                .on_conflict_do_nothing()
            )
        else:  # pragma: no cover - production is PostgreSQL, the suite SQLite
            raise NotImplementedError(f"No idempotent bulk grant for the {dialect} dialect")
        result = cast(CursorResult[Any], await self.session.execute(statement))
        return max(result.rowcount, 0)

    async def list_for_user(self, user_id: int) -> list[RouletteSpinGrant]:
        result = await self.session.scalars(
            select(RouletteSpinGrant)
            .where(RouletteSpinGrant.user_id == user_id)
            .order_by(RouletteSpinGrant.id.asc())
        )
        return list(result.all())
