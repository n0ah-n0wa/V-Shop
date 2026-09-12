"""🎰 Lucky Roulette — the customer's spins, and playing one.

Nothing here draws a prize, counts spins or decides a reward. The screen shows
what the backend reports — spins available, the spin on offer, orders to the
next spin, the prizes that exist — and a spin is :meth:`RouletteEngine.spin`:
the server draws the prize, spends the spin and books what it won, in one
transaction.

The Spin button carries only the id of the spin the screen offered. Nothing in
it names a prize, a type or a value, and nothing a client sends can: an id that
is not this customer's spends nothing, and a repeated tap — a double tap, a
stale screen, the same update delivered again — replays the first result
instead of spending another spin.

The spin is committed before the customer is shown anything. The short suspense
that follows is presentation only: the prize is already drawn and saved, so a
failed animation, a closed chat or a crash cannot lose it — the same button
shows it again.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.user.navigation import ensure_onboarded_user
from app.keyboards.roulette import (
    CALLBACK_ROULETTE_CLOSE,
    CALLBACK_ROULETTE_OPEN,
    CALLBACK_ROULETTE_SPIN_PREFIX,
    roulette_keyboard,
    spin_result_keyboard,
)
from app.models.enums import RoulettePrizeType
from app.services.localization import LocalizationService
from app.services.roulette import SpinOutcome, prize_name_key
from app.services.roulette_engine import RouletteEngine, RoulettePolicy
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy
from app.services.stamp_card import StampCardPolicy, StampCardService
from app.services.user import UserService
from app.utils.concurrency import keyed_lock
from app.utils.roulette_display import (
    RouletteView,
    SpinResultView,
    format_roulette,
    format_spin_result,
    suspense_frames,
)
from app.utils.telegram_ui import as_message, clear_inline_markup
from app.utils.validators import parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="user_roulette")

# Seconds each suspense frame stays up. Two frames and the result are three
# edits in under two seconds — well within Telegram's pace for one chat.
FRAME_DELAY = 0.8


def _engine(session: AsyncSession, settings: Settings) -> RouletteEngine:
    """The roulette under the configured odds — never the defaults."""
    return RouletteEngine(session, RoulettePolicy.from_settings(settings))


async def _onboarded_user_id(
    session: AsyncSession, tg_user: TgUser
) -> tuple[int, LocalizationService] | None:
    """The acting customer's id and language, or ``None`` before onboarding is done."""
    user = await UserService(session).ensure_user(tg_user)
    if not UserService.is_onboarded(user):
        return None
    return user.id, LocalizationService.from_user(user)


async def _view(
    session: AsyncSession, settings: Settings, engine: RouletteEngine, user_id: int
) -> RouletteView:
    """The roulette as the backend reports it. Read-only."""
    spins = SpinEntitlementService(session, SpinPolicy.from_settings(settings))
    policy = engine.policy
    return RouletteView(
        available=await engine.available_spins(user_id),
        next_grant_id=await engine.next_grant_id(user_id),
        orders_to_next_spin=await spins.purchases_to_next_spin(user_id),
        prizes=tuple(
            (RoulettePrizeType(entry.prize.kind), entry.prize.value)
            for entry in policy.table.entries
            if entry.weight > 0
        ),
        free_bottle_max_price=policy.free_bottle_max_price,
    )


async def _result(
    session: AsyncSession,
    settings: Settings,
    engine: RouletteEngine,
    user_id: int,
    outcome: SpinOutcome,
) -> SpinResultView:
    """What a spin won, from its saved rows — never from anything the client sent."""
    spin = outcome.spin
    reward = outcome.reward
    spins = SpinEntitlementService(session, SpinPolicy.from_settings(settings))
    stamp_card = StampCardService(session, StampCardPolicy.from_settings(settings))
    return SpinResultView(
        prize_key=prize_name_key(spin.prize_code),
        kind=RoulettePrizeType(spin.prize_type),
        free_bottle_max_price=reward.max_item_price if reward is not None else None,
        remaining=await engine.available_spins(user_id),
        next_grant_id=await engine.next_grant_id(user_id),
        orders_to_next_spin=await spins.purchases_to_next_spin(user_id),
        stamp_card=await stamp_card.card(user_id),
    )


async def _show(target: Message, text: str, markup: InlineKeyboardMarkup, *, edit: bool) -> bool:
    """Put a screen up; ``False`` when the one showing was already current."""
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return True
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc):
                return False
            logger.debug("Could not edit the roulette; sending a new one", exc_info=True)
    await target.answer(text, reply_markup=markup)
    return True


async def render_roulette(
    target: Message,
    *,
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    i18n: LocalizationService,
    edit: bool,
) -> bool:
    """Draw the customer's roulette as it is now."""
    view = await _view(session, settings, _engine(session, settings), user_id)
    text = format_roulette(view, i18n, currency=settings.currency_symbol)
    return await _show(target, text, roulette_keyboard(i18n, view.next_grant_id), edit=edit)


async def _suspense(message: Message, i18n: LocalizationService) -> None:
    """A moment of suspense before the result. The prize is already saved."""
    for frame in suspense_frames(i18n):
        try:
            # No buttons while the reels turn: nothing to tap twice.
            await message.edit_text(frame, reply_markup=None)
        except TelegramAPIError:
            logger.debug("Skipping the roulette animation", exc_info=True)
            return
        await asyncio.sleep(FRAME_DELAY)


@router.message(LocalizedText("menu.roulette"))
async def open_roulette(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
) -> None:
    """A fresh screen on every tap — reading it writes nothing, so repeats are harmless."""
    await state.clear()
    ready = await ensure_onboarded_user(message, session, state)
    if ready is None:
        return
    user, i18n = ready
    await render_roulette(
        message, session=session, settings=settings, user_id=user.id, i18n=i18n, edit=False
    )


@router.callback_query(F.data == CALLBACK_ROULETTE_OPEN)
async def refresh_roulette(
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
    await render_roulette(
        message, session=session, settings=settings, user_id=user_id, i18n=localized, edit=True
    )
    await callback.answer()


@router.callback_query(F.data == CALLBACK_ROULETTE_CLOSE)
async def close_roulette(callback: CallbackQuery) -> None:
    """⬅️ Back: the roulette goes away, leaving the main menu below."""
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


@router.callback_query(F.data.startswith(CALLBACK_ROULETTE_SPIN_PREFIX))
async def spin_roulette(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    i18n: LocalizationService,
) -> None:
    """
    Play the spin this button offered.

    The payload is only that spin's id. The engine checks it is this
    customer's and unspent under the account lock, draws the prize on the
    server and books it; a spin already played comes back with its first
    result, so a replayed or crafted callback can at most be refused.
    """
    message = as_message(callback)
    if callback.from_user is None or callback.data is None or message is None:
        await callback.answer()
        return
    grant_id = parse_positive_int(callback.data.removeprefix(CALLBACK_ROULETTE_SPIN_PREFIX))
    if grant_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    # One tap at a time per customer in this process, from the first read to the
    # last frame: a second tap waits here, then finds the spin played and is shown
    # the same result. Across processes the grant's row lock and its unique spin
    # do the same.
    async with keyed_lock(f"roulette:{callback.from_user.id}"):
        ready = await _onboarded_user_id(session, callback.from_user)
        if ready is None:
            await callback.answer(i18n.t("common.not_available"), show_alert=True)
            return
        user_id, localized = ready
        engine = _engine(session, settings)
        try:
            outcome = await engine.spin(user_id, grant_id=grant_id)
            result = (
                await _result(session, settings, engine, user_id, outcome)
                if outcome is not None
                else None
            )
            # Durable before the customer is shown anything. On a replay or a
            # refusal it only ends the transaction: a request that waited while
            # another spent the same grant replays it under the account and
            # grant locks, and no lock may outlive this point into Telegram.
            await session.commit()
        except SQLAlchemyError:
            # Nothing of the spin is kept: it stays unspent. Had the commit
            # reached the database after all, the same button replays it.
            await session.rollback()
            logger.exception("Roulette spin failed user_id=%s grant_id=%s", user_id, grant_id)
            await callback.answer(
                localized.t("roulette.failed", button=localized.t("roulette.spin")),
                show_alert=True,
            )
            return

        if outcome is None or result is None:
            # No spin of this customer's behind the button. Say why, and show
            # the roulette as it is now.
            view = await _view(session, settings, engine, user_id)
            notice = "roulette.stale" if view.available else "roulette.no_spins_left"
            await callback.answer(localized.t(notice), show_alert=True)
            text = format_roulette(view, localized, currency=settings.currency_symbol)
            markup = roulette_keyboard(localized, view.next_grant_id)
            await _show(message, text, markup, edit=True)
            return

        text = format_spin_result(result, localized, currency=settings.currency_symbol)
        markup = spin_result_keyboard(
            localized,
            result.next_grant_id,
            stamps_won=result.kind == RoulettePrizeType.STAMPS,
        )
        if not outcome.created:
            await callback.answer(localized.t("roulette.already_played"))
            await _show(message, text, markup, edit=True)
            return

        await callback.answer(localized.t("roulette.good_luck"))
        await _suspense(message, localized)
        await _show(message, text, markup, edit=True)
