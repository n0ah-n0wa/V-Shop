"""/start onboarding flow: language → city → main menu (FSM)."""

from __future__ import annotations

import logging

from aiogram import F, Router
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
from app.services.referral_program import ReferralPolicy, ReferralProgramService
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy
from app.services.user import UserService
from app.states.onboarding import OnboardingStates
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


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    command: CommandObject | None = None,
) -> None:
    """
    Entry point.

    First launch: language → city → main menu.
    Returning users skip steps already saved in the database.
    A friend's referral link arrives as ``/start ref_<code>``.
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
    if command is not None and command.args:
        # Untrusted: anything that is not a valid, applicable referral is an
        # outcome, never an error, and onboarding carries on regardless.
        attempt = await ReferralProgramService(
            session, ReferralPolicy.from_settings(settings)
        ).attribute_from_start(user.id, command.args)
        logger.info("/start referral telegram_id=%s outcome=%s", user.telegram_id, attempt.outcome)
    i18n = LocalizationService.from_user(user)

    logger.info(
        "/start telegram_id=%s language=%s city=%s",
        user.telegram_id,
        user.language,
        user.selected_city,
    )
    await _continue_onboarding(message, user, i18n, state)


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
