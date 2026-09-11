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
from app.services.loyalty_activation import ActivationReport, LoyaltyActivationService
from app.services.spin_entitlement import SpinPolicy

logger = logging.getLogger(__name__)


async def activate_loyalty(settings: Settings) -> ActivationReport | None:
    """
    Bring every existing customer into the loyalty programme: an account, and
    their one welcome roulette spin.

    Idempotent, so it runs on every start: one statement each that skips
    customers who already have the row — a welcome spin spent long ago
    included — behind a unique constraint that rules out a second, so restarts
    and redeploys add nothing. Orders are never read or written. A failure
    (migrations not applied yet, say) is logged and never stops the bot.
    Returns what was added, or ``None`` when it could not run.
    """
    try:
        async with get_session_factory()() as session:
            report = await LoyaltyActivationService(
                session, SpinPolicy.from_settings(settings)
            ).activate_everyone()
            await session.commit()
    except Exception:
        logger.exception(
            "Could not activate loyalty for existing customers (accounts, welcome spins); "
            "the bot starts without them"
        )
        return None
    logger.info(
        "Loyalty activation: accounts_opened=%s welcome_spins_granted=%s",
        report.accounts_opened,
        report.welcome_spins_granted,
    )
    return report


async def on_startup(bot: Bot, settings: Settings) -> None:
    """
    Run once before polling begins.

    - Initialize DB engine / session factory
    - Verify PostgreSQL connectivity
    - Record which database cluster we attached to (read-only, diagnostic)
    - Bring existing customers into the loyalty programme: any missing account
      and welcome roulette spin (idempotent; orders untouched)
    - Drop webhook (long-polling mode)
    - Confirm Telegram authorization via getMe
    """
    logger.info("Startup: initializing infrastructure (env=%s)", settings.app_env)

    await init_db()
    await check_db_connection()
    await log_database_identity()
    await activate_loyalty(settings)

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
