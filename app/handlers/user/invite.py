"""👥 Invite a Friend — the customer's personal referral link, ready to share.

The link, what it pays each side and the friends it has brought in all come
from :meth:`ReferralProgramService.invitation`; this screen only lays them out.
The link carries a random code, never an id. Nothing here can attribute or pay
a referral — that happens at /start and when an order completes — and the
screen's only callback closes it.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.user.navigation import ensure_onboarded_user
from app.keyboards.invite import CALLBACK_INVITE_CLOSE, invite_keyboard
from app.services.localization import LocalizationService
from app.services.referral_program import ReferralPolicy, ReferralProgramService
from app.services.spin_entitlement import SpinPolicy
from app.utils.invite_display import format_invite, share_text
from app.utils.telegram_ui import as_message, clear_inline_markup

logger = logging.getLogger(__name__)

router = Router(name="user_invite")


async def render_invite(
    target: Message,
    *,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    user_id: int,
    i18n: LocalizationService,
) -> None:
    """Send the customer's invite screen, under the configured rewards."""
    me = await bot.me()  # cached getMe: the deep link needs the bot's username
    programme = ReferralProgramService(
        session,
        ReferralPolicy.from_settings(settings),
        spin_policy=SpinPolicy.from_settings(settings),
    )
    invitation = await programme.invitation(user_id, bot_username=me.username or "")
    # The first open creates the customer's code under their account lock: it
    # is durable, and the lock released, before the link reaches anyone.
    await session.commit()
    await target.answer(
        format_invite(invitation, i18n),
        reply_markup=invite_keyboard(
            i18n, link=invitation.link, share_text=share_text(invitation, i18n)
        ),
        # The link is the point of the message; a preview card of the bot is noise.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(LocalizedText("menu.invite"))
async def open_invite(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
    bot: Bot,
) -> None:
    """A fresh screen on every tap: the first creates the customer's code, later ones read it."""
    await state.clear()
    ready = await ensure_onboarded_user(message, session, state)
    if ready is None:
        return
    user, i18n = ready
    await render_invite(
        message, session=session, settings=settings, bot=bot, user_id=user.id, i18n=i18n
    )


@router.callback_query(F.data == CALLBACK_INVITE_CLOSE)
async def close_invite(callback: CallbackQuery) -> None:
    """⬅️ Back: the screen goes away, leaving the main menu below."""
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        # A bot may delete its messages for 48 hours; after that the screen
        # stays, without its buttons.
        await clear_inline_markup(message)
