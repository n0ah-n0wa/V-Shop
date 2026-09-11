"""/start onboarding flow: language → city → main menu (FSM)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.inline import city_keyboard, language_keyboard
from app.keyboards.reply import main_menu_keyboard
from app.models.enums import CityChoice, LanguageCode
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.referral_notification import ReferralNotificationService
from app.services.referral_program import (
    ReferralAttempt,
    ReferralOutcome,
    ReferralPolicy,
    ReferralProgramService,
)
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy
from app.services.user import UserService
from app.states.onboarding import OnboardingStates
from app.utils.invite_display import own_link_note
from app.utils.telegram_ui import as_message

logger = logging.getLogger(__name__)

router = Router(name="user_start")


async def _ask_language(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.language)
    await message.answer(
        i18n.t("onboarding.choose_language"),
        reply_markup=language_keyboard(i18n),
    )


async def _ask_city(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.city)
    await message.answer(
        i18n.t("onboarding.choose_city"),
        reply_markup=city_keyboard(i18n),
    )


async def _show_main_menu(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        i18n.t("onboarding.welcome"),
        reply_markup=main_menu_keyboard(i18n),
    )


async def _continue_onboarding(
    message: Message,
    user: User,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    """Resume onboarding at the first incomplete step."""
    if UserService.needs_language(user):
        await _ask_language(message, i18n, state)
        return
    if UserService.needs_city(user):
        await _ask_city(message, LocalizationService.from_user(user), state)
        return
    await _show_main_menu(message, LocalizationService.from_user(user), state)


async def _clear_inline_keyboard(callback: CallbackQuery) -> None:
    message = as_message(callback)
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear inline keyboard", exc_info=True)


async def _follow_up_referral_link(
    attempt: ReferralAttempt,
    message: Message,
    user: User,
    *,
    session: AsyncSession,
    settings: Settings,
    bot: Bot | None,
) -> None:
    """What a referral link adds once the customer has had their usual answer."""
    if attempt.outcome == ReferralOutcome.SELF_REFERRAL:
        # Only the code's owner can land here, so this tells nobody anything
        # new — and customers do open their own link to check that it works.
        await message.answer(own_link_note(LocalizationService.from_user(user)))
    elif (
        attempt.outcome == ReferralOutcome.ATTRIBUTED
        and attempt.referral is not None
        and bot is not None  # aiogram always passes it; a direct call may not
    ):
        # To the referrer, never the newcomer. Read-only; swallows its failures.
        await ReferralNotificationService(session, bot, settings=settings).friend_joined(
            attempt.referral
        )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    command: CommandObject | None = None,
    bot: Bot | None = None,
) -> None:
    """
    Entry point.

    First launch: language → city → main menu.
    Returning users skip steps already saved in the database.
    A friend's referral link arrives as ``/start ref_<code>``. Whatever the
    code, the newcomer is answered exactly as for a plain /start — no reply
    tells a guesser that a code was real — and the referrer is told apart.
    """
    if message.from_user is None:
        return

    service = UserService(session)
    user = await service.ensure_user(message.from_user)
    # The customer's one welcome spin: granted on the first /start, and every
    # later /start finds it already there — it never grants a second.
    await SpinEntitlementService(session, SpinPolicy.from_settings(settings)).grant_welcome_spin(
        user.id
    )
    attempt: ReferralAttempt | None = None
    if command is not None and command.args:
        # Untrusted: anything that is not a valid, applicable referral is an
        # outcome, never an error, and onboarding carries on regardless.
        attempt = await ReferralProgramService(
            session, ReferralPolicy.from_settings(settings)
        ).attribute_from_start(user.id, command.args)
        logger.info("/start referral telegram_id=%s outcome=%s", user.telegram_id, attempt.outcome)
    # Durable before anyone is answered or the referrer told — and the locks an
    # attribution takes (the customer's account, the attribution lock every
    # /start with a link queues on) are released before the bot waits on Telegram.
    await session.commit()
    i18n = LocalizationService.from_user(user)

    logger.info(
        "/start telegram_id=%s language=%s city=%s",
        user.telegram_id,
        user.language,
        user.selected_city,
    )
    await _continue_onboarding(message, user, i18n, state)
    if attempt is not None:
        await _follow_up_referral_link(
            attempt, message, user, session=session, settings=settings, bot=bot
        )


@router.callback_query(F.data.startswith("lang:"))
async def on_language_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Persist language, then ask for city (or open main menu if city exists)."""
    message = as_message(callback)
    if callback.from_user is None or callback.data is None or message is None:
        await callback.answer()
        return

    raw = callback.data.split(":", maxsplit=1)[-1]
    try:
        language = LanguageCode(raw)
    except ValueError:
        await callback.answer(
            LocalizationService.default().t("error.invalid_callback"),
            show_alert=True,
        )
        return

    service = UserService(session)
    user = await service.ensure_user(callback.from_user)
    await service.save_language(user, language)

    i18n = LocalizationService.from_code(language)
    await callback.answer()
    await _clear_inline_keyboard(callback)
    await message.answer(i18n.t("onboarding.language_saved"))

    if UserService.needs_city(user):
        await _ask_city(message, i18n, state)
        return

    await _show_main_menu(message, i18n, state)


@router.callback_query(F.data.startswith("city:"))
async def on_city_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Persist city and show the main menu."""
    message = as_message(callback)
    if callback.from_user is None or callback.data is None or message is None:
        await callback.answer()
        return

    raw = callback.data.split(":", maxsplit=1)[-1]
    try:
        city = CityChoice(raw)
    except ValueError:
        await callback.answer(
            LocalizationService.default().t("error.invalid_callback"),
            show_alert=True,
        )
        return

    service = UserService(session)
    user = await service.ensure_user(callback.from_user)

    if UserService.needs_language(user):
        i18n = LocalizationService.default()
        await callback.answer()
        await _clear_inline_keyboard(callback)
        await _ask_language(message, i18n, state)
        return

    await service.save_city(user, city)
    i18n = LocalizationService.from_user(user)

    await callback.answer()
    await _clear_inline_keyboard(callback)
    await message.answer(i18n.t("onboarding.city_saved"))
    await _show_main_menu(message, i18n, state)
