"""User service — registration and onboarding orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import CityChoice, LanguageCode
from app.models.user import User
from app.repositories.cart import CartRepository
from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.repositories.user import UserRepository

# Avoid rewriting last_seen on every update within a short window.
_LAST_SEEN_TOUCH_INTERVAL = timedelta(minutes=5)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.carts = CartRepository(session)
        self.loyalty_accounts = LoyaltyAccountRepository(session)

    async def ensure_user(self, tg_user: TgUser) -> User:
        """
        Get or create the DB user and refresh profile fields from Telegram.

        A new user gets their cart and their (empty) loyalty account at once, so
        every customer is in the loyalty programme from their first contact —
        whichever handler saw them first. Both are get-or-create, safe to repeat.
        """
        user, created = await self.users.get_or_create_by_telegram(
            tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
        )
        if created:
            await self.carts.get_or_create_for_user(user.id)
            await self.loyalty_accounts.get_or_create_for_user(user.id)
            return user

        profile_changed = user.username != tg_user.username or user.first_name != tg_user.first_name
        last_seen = user.last_seen
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        stale = last_seen is None or (datetime.now(UTC) - last_seen >= _LAST_SEEN_TOUCH_INTERVAL)
        if profile_changed or stale:
            await self.users.update_profile(
                user,
                username=tg_user.username,
                first_name=tg_user.first_name,
            )
        return user

    @staticmethod
    def needs_language(user: User) -> bool:
        return user.language is None

    @staticmethod
    def needs_city(user: User) -> bool:
        return user.selected_city is None

    @staticmethod
    def is_onboarded(user: User) -> bool:
        return user.language is not None and user.selected_city is not None

    async def save_language(self, user: User, language: LanguageCode) -> User:
        return await self.users.set_language(user, language)

    async def save_city(self, user: User, city: CityChoice) -> User:
        return await self.users.set_city(user, city)
