"""🪪 My Stamp Card — the customer's card, and claiming a free bottle from it.

Nothing here counts stamps or decides a reward. The card — balance, progress,
stamps still needed, whether a bottle can be claimed, bottles waiting — comes
from :class:`StampCardService`, and a claim is the service's decision, taken
under the customer's account lock.

The claim button carries the version of the card it was drawn from (the latest
ledger id). A double tap, or a card left open while the balance changed, is
refused by the service and the card is redrawn: a bottle is never claimed twice,
and never from stamps the customer was not shown.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.user.navigation import ensure_onboarded_user
from app.keyboards.stamp_card import (
    CALLBACK_STAMP_CLAIM_PREFIX,
    CALLBACK_STAMP_OPEN,
    stamp_card_keyboard,
)
from app.services.localization import LocalizationService
from app.services.loyalty import AlreadyClaimedError, InsufficientStampsError, StaleCardError
from app.services.stamp_card import StampCardPolicy, StampCardService
from app.services.user import UserService
from app.utils.concurrency import keyed_lock
from app.utils.stamp_card_display import format_stamp_card
from app.utils.telegram_ui import as_message
from app.utils.validators import parse_nonnegative_int

logger = logging.getLogger(__name__)

router = Router(name="user_stamp_card")


def _stamp_card(session: AsyncSession, settings: Settings) -> StampCardService:
    """The service under the configured rules — never the defaults."""
    return StampCardService(session, StampCardPolicy.from_settings(settings))


async def _onboarded_user_id(
    session: AsyncSession, tg_user: TgUser
) -> tuple[int, LocalizationService] | None:
    """The acting customer's id and language, or ``None`` before onboarding is done."""
    user = await UserService(session).ensure_user(tg_user)
    if not UserService.is_onboarded(user):
        return None
    return user.id, LocalizationService.from_user(user)


async def _show(target: Message, text: str, markup: InlineKeyboardMarkup, *, edit: bool) -> bool:
    """Put the card on screen; ``False`` when the card showing was already current."""
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return False
            logger.debug("Could not edit the stamp card; sending a new one", exc_info=True)
    await target.answer(text, reply_markup=markup)
    return True


async def render_stamp_card(
    target: Message,
    *,
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    i18n: LocalizationService,
    edit: bool,
) -> bool:
    """Draw the customer's current card; ``False`` when the one on screen already was."""
    service = _stamp_card(session, settings)
    card = await service.card(user_id)
    text = format_stamp_card(
        card,
        i18n,
        purchase_threshold=service.policy.purchase_threshold,
        currency=settings.currency_symbol,
    )
    return await _show(target, text, stamp_card_keyboard(i18n, card), edit=edit)


@router.message(LocalizedText("menu.stamp_card"))
async def open_stamp_card(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
) -> None:
    """A fresh card on every tap — reading it writes nothing, so repeats are harmless."""
    await state.clear()
    ready = await ensure_onboarded_user(message, session, state)
    if ready is None:
        return
    user, i18n = ready
    await render_stamp_card(
        message, session=session, settings=settings, user_id=user.id, i18n=i18n, edit=False
    )


@router.callback_query(F.data == CALLBACK_STAMP_OPEN)
async def refresh_stamp_card(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or message is None:
        await callback.answer()
        return
    ready = await _onboarded_user_id(session, callback.from_user)
    if ready is None:
        await callback.answer(i18n.t("common.not_available"), show_alert=True)
        return
    user_id, localized = ready
    changed = await render_stamp_card(
        message, session=session, settings=settings, user_id=user_id, i18n=localized, edit=True
    )
    # Answered after drawing, so a tap that changed nothing still gets a reply.
    await callback.answer(None if changed else localized.t("stamp_card.up_to_date"))


@router.callback_query(F.data.startswith(CALLBACK_STAMP_CLAIM_PREFIX))
async def claim_free_bottle(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    i18n: LocalizationService,
) -> None:
    """
    Ask the backend to claim a free bottle from the card this button was on.

    The payload is only the card's version, and nothing in it grants anything:
    the service re-checks the balance under the account lock, so a crafted or
    replayed callback can at most be refused.
    """
    message = as_message(callback)
    if callback.from_user is None or callback.data is None or message is None:
        await callback.answer()
        return
    version = parse_nonnegative_int(callback.data.removeprefix(CALLBACK_STAMP_CLAIM_PREFIX))
    if version is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    ready = await _onboarded_user_id(session, callback.from_user)
    if ready is None:
        await callback.answer(i18n.t("common.not_available"), show_alert=True)
        return
    user_id, localized = ready

    # One claim at a time per customer in this process; the account row lock
    # does the same across processes. The commit happens inside the lock, so a
    # second tap waiting here sees the first claim's outcome, never half of it.
    async with keyed_lock(f"stamp_claim:{user_id}"):
        try:
            reward = await _stamp_card(session, settings).claim_free_bottle(
                user_id, card_version=version
            )
        except AlreadyClaimedError:
            notice = localized.t("stamp_card.already_claimed")
        except StaleCardError:
            notice = localized.t("stamp_card.changed")
        except InsufficientStampsError:
            notice = localized.t("stamp_card.not_enough")
        else:
            reward_id = reward.id
            # Durable before the customer is told, as at checkout.
            await session.commit()
            logger.info("Free bottle claimed user_id=%s reward_id=%s", user_id, reward_id)
            notice = localized.t("stamp_card.claimed")

    await callback.answer(notice, show_alert=True)
    await render_stamp_card(
        message, session=session, settings=settings, user_id=user_id, i18n=localized, edit=True
    )
