"""Application startup and shutdown hooks (aiogram 3 lifecycle)."""

from __future__ import annotations

import logging

from aiogram import Bot

from app.config import Settings
from app.database.session import (
    check_db_connection,
    close_db,
    get_session_factory,
    init_db,
    log_database_identity,
)
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy

logger = logging.getLogger(__name__)


async def grant_missing_welcome_spins(settings: Settings) -> int | None:
    """
    Give every customer still without one their welcome roulette spin.

    Idempotent, so it runs on every start: one statement that skips anyone who
    has had a welcome spin, spent or not, and a unique index that rules out a
    second — restarts and redeploys never grant again. A failure (migrations
    not applied yet, say) is logged and never stops the bot. Returns how many
    were granted, or ``None`` when it could not run.
    """
    try:
        async with get_session_factory()() as session:
            created = await SpinEntitlementService(
                session, SpinPolicy.from_settings(settings)
            ).grant_missing_welcome_spins()
            await session.commit()
    except Exception:
        logger.exception("Could not grant missing welcome spins; the bot starts without them")
        return None
    return created


async def on_startup(bot: Bot, settings: Settings) -> None:
    """
    Run once before polling begins.

    - Initialize DB engine / session factory
    - Verify PostgreSQL connectivity
    - Record which database cluster we attached to (read-only, diagnostic)
    - Give customers still without one their welcome roulette spin (idempotent)
    - Drop webhook (long-polling mode)
    - Confirm Telegram authorization via getMe
    """
    logger.info("Startup: initializing infrastructure (env=%s)", settings.app_env)

    await init_db()
    await check_db_connection()
    await log_database_identity()
    await grant_missing_welcome_spins(settings)

    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    logger.info(
        "Startup complete: bot=@%s id=%s can_join_groups=%s",
        me.username,
        me.id,
        me.can_join_groups,
    )


async def on_shutdown() -> None:
    """Run once when the dispatcher stops (Ctrl+C / process exit)."""
    logger.info("Shutdown: releasing resources")
    await close_db()
    logger.info("Shutdown complete")
