"""Telegram handlers package."""

from __future__ import annotations

import logging

from aiogram import Router

from app.config import Settings, get_settings
from app.handlers import fallback
from app.handlers.admin import get_admin_router
from app.handlers.user import get_user_router

logger = logging.getLogger(__name__)


def setup_routers(settings: Settings | None = None) -> Router:
    """Compose and return the root application router."""
    cfg = settings or get_settings()
    root = Router(name="root")
    root.include_router(get_user_router())
    root.include_router(get_admin_router(cfg))
    # Last: answers button taps nothing above handled (stale keyboards).
    root.include_router(fallback.router)
    logger.debug(
        "Routers mounted: %s",
        [router.name for router in root.sub_routers],
    )
    return root
