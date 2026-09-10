"""Application configuration loaded from environment variables."""

import json
from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.utils.periods import DEFAULT_TIMEZONE


def _parse_admin_ids(value: object) -> list[int]:
    """Accept comma-separated strings, JSON lists, or native lists for ADMIN_IDS."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [int(item) for item in value]
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise TypeError(f"Unsupported ADMIN_IDS JSON value: {value!r}")
            return [int(item) for item in parsed]
        parts = [part.strip() for part in stripped.split(",") if part.strip()]
        return [int(part) for part in parts]
    raise TypeError(f"Unsupported ADMIN_IDS value: {value!r}")


class Settings(BaseSettings):
    """Runtime settings for the V-Shop bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(..., description="Telegram Bot API token")
    database_url: str = Field(
        ...,
        description="SQLAlchemy async database URL (postgresql+asyncpg://...)",
    )
    admin_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description=("Telegram user IDs with admin access; also receive new-order notifications"),
    )
    manager_chat_id: int = Field(
        ...,
        description=("Manager group or private admin chat ID for new order notifications"),
    )
    review_group_chat_id: int | None = Field(
        default=None,
        description=(
            "Private customer reviews group. The bot must be an administrator "
            "there with can_invite_users to mint invite links. Unset disables "
            "the Reviews button."
        ),
    )
    review_invite_link: str | None = Field(
        default=None,
        description=(
            "Optional pre-made invite link. Used verbatim when set, so the "
            "feature works even if the bot cannot create links itself."
        ),
    )
    app_timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        description=(
            "IANA zone used for statistics month boundaries. Never rely on the "
            "server clock: a Berlin shop and a UTC server disagree for one or "
            "two hours every day."
        ),
    )
    currency_symbol: str = Field(
        default="€",
        description=(
            "Currency shown next to money figures. Placement follows the "
            "language: '€12.34' in English, '12,34 €' in German/Russian/Ukrainian."
        ),
    )
    # Money settings carry Numeric(10, 2) precision, so a value the stamp card
    # could not use (20.005, 0.001) stops the bot at startup, not at the first
    # completed order.
    loyalty_stamp_purchase_threshold: Decimal = Field(
        default=Decimal("20.00"),
        # At least €1: a mistyped threshold (0.2 for 20) would mint stamps on
        # every order, and the floor keeps one order's stamps within the
        # ledger's 32-bit amount column.
        ge=Decimal("1.00"),
        max_digits=10,
        decimal_places=2,
        description=(
            "Charged order total that earns one stamp, in whole multiples: "
            "€20 → 1, €39.99 → 1, €40 → 2."
        ),
    )
    loyalty_stamps_required: int = Field(
        default=10,
        ge=1,
        description="Stamps that unlock one free bottle.",
    )
    loyalty_free_bottle_max_price: Decimal = Field(
        default=Decimal("20.00"),
        gt=0,
        max_digits=10,
        decimal_places=2,
        description="Most expensive product a free bottle may cover.",
    )
    app_env: str = Field(default="development", description="Application environment name")
    log_level: str = Field(default="INFO", description="Root logging level")
    telegram_ssl_verify: bool = Field(
        default=True,
        description="Verify TLS certificates when talking to api.telegram.org",
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> list[int]:
        return _parse_admin_ids(value)

    @property
    def reviews_enabled(self) -> bool:
        """Reviews need either a group to mint links in, or a static link."""
        return self.review_group_chat_id is not None or bool(self.review_invite_link)

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "local"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
