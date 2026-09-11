"""Localization service — resolves language and translates user-facing text."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import LanguageCode
from app.models.user import User
from app.repositories.user import UserRepository
from app.utils.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    normalize_language,
    plural_category,
    translate,
)


class LocalizationService:
    """
    Translates message keys for a concrete language.

    Language is expected to come from the user record in the database.
    Handlers must use this service (or injected ``i18n``) instead of hardcoded text.
    """

    def __init__(self, language: LanguageCode | str | None = None) -> None:
        self._language = normalize_language(language)

    @property
    def language(self) -> str:
        return self._language

    @property
    def language_code(self) -> LanguageCode:
        return LanguageCode(self._language)

    def with_language(self, language: LanguageCode | str | None) -> LocalizationService:
        """Return a new service instance bound to another language."""
        return LocalizationService(language)

    def set_language(self, language: LanguageCode | str | None) -> None:
        self._language = normalize_language(language)

    def get(self, key: str, **kwargs: object) -> str:
        """Alias for :meth:`t`."""
        return self.t(key, **kwargs)

    def t(self, key: str, **kwargs: object) -> str:
        """Translate a key using the current language."""
        return translate(key, self._language, **kwargs)

    def n(self, *parts: str) -> str:
        """Join dotted key parts then translate (``i18n.n('menu', 'catalog')``)."""
        return self.t(".".join(parts))

    def plural(self, key: str, count: int, **kwargs: object) -> str:
        """
        The form of ``key`` that fits ``count`` in this language, ``{count}`` filled in.

        ``i18n.plural("invite.stamps", 5)`` reads ``invite.stamps.many`` in Russian
        ("+5 штампов") and ``invite.stamps.other`` in English ("+5 stamps").
        """
        return self.t(f"{key}.{plural_category(self._language, count)}", count=count, **kwargs)

    @classmethod
    def default(cls) -> LocalizationService:
        return cls(DEFAULT_LANGUAGE)

    @classmethod
    def from_user(cls, user: User | None) -> LocalizationService:
        """Build a service from a persisted user (language column)."""
        if user is None or user.language is None:
            return cls.default()
        return cls(user.language)

    @classmethod
    def from_code(cls, language: LanguageCode | str | None) -> LocalizationService:
        return cls(language)

    @classmethod
    async def for_telegram_user(
        cls,
        session: AsyncSession,
        telegram_id: int,
    ) -> LocalizationService:
        """Load the user from the database and bind their stored language."""
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        return cls.from_user(user)

    @staticmethod
    def supported_languages() -> tuple[str, ...]:
        return SUPPORTED_LANGUAGES

    @staticmethod
    def is_supported(language: LanguageCode | str | None) -> bool:
        if language is None:
            return False
        code = language.value if isinstance(language, LanguageCode) else str(language).lower()
        return code in SUPPORTED_LANGUAGES
