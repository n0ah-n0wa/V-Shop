"""Cart navigation and cart mutation actions (DB-persisted)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.filters.localized_text import LocalizedText
from app.handlers.user.navigation import answer_with_menu, ensure_onboarded_user
from app.keyboards.cart import (
    CALLBACK_CART_DEC_PREFIX,
    CALLBACK_CART_INC_PREFIX,
    CALLBACK_CART_NOOP,
    CALLBACK_CART_OPEN,
    CALLBACK_CART_RM_PREFIX,
    cart_keyboard,
)
from app.keyboards.product import CALLBACK_CART_ADD_PREFIX, product_added_keyboard
from app.models.user import User
from app.services.cart import CartService, CartView
from app.services.catalog import CatalogService
from app.services.localization import LocalizationService
from app.services.user import UserService
from app.utils.html import e
from app.utils.telegram_ui import as_message
from app.utils.validators import parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="user_cart")


def format_cart_text(i18n: LocalizationService, view: CartView) -> str:
    lines = [i18n.t("cart.title"), ""]
    for line in view.lines:
        lines.append(
            i18n.t(
                "cart.item_line",
                name=e(line.name),
                quantity=line.quantity,
                price=line.line_total,
            )
        )
    lines.append("")
    lines.append(i18n.t("cart.total", total=view.total))
    return "\n".join(lines)


async def _load_onboarded_user(
    session: AsyncSession,
    tg_user: TgUser,
    i18n: LocalizationService,
) -> tuple[User | None, LocalizationService]:
    service = UserService(session)
    user = await service.ensure_user(tg_user)
    if not UserService.is_onboarded(user):
        return None, i18n
    return user, LocalizationService.from_user(user)


async def render_cart(
    *,
    target: Message,
    session: AsyncSession,
    user_id: int,
    i18n: LocalizationService,
    edit: bool = False,
) -> None:
    view = await CartService(session).get_view(user_id, language=i18n.language)
    if view is None or view.is_empty:
        text = i18n.t("cart.empty")
        if edit:
            try:
                await target.edit_text(text)
                return
            except TelegramBadRequest:
                pass
            await answer_with_menu(target, text, i18n)
            return
        await answer_with_menu(target, text, i18n)
        return

    text = format_cart_text(i18n, view)
    markup = cart_keyboard(i18n, view)
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            logger.debug("Could not edit cart message; sending a new one", exc_info=True)
    await target.answer(text, reply_markup=markup)


@router.message(LocalizedText("menu.cart"))
async def open_cart(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    ready = await ensure_onboarded_user(message, session, state)
    if ready is None:
        return
    user, i18n = ready
    await render_cart(target=message, session=session, user_id=user.id, i18n=i18n, edit=False)


@router.callback_query(F.data == CALLBACK_CART_OPEN)
async def open_cart_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or message is None:
        await callback.answer()
        return

    user, localized = await _load_onboarded_user(session, callback.from_user, i18n)
    if user is None:
        await callback.answer(i18n.t("common.not_available"), show_alert=True)
        return

    await callback.answer()
    await render_cart(
        target=message,
        session=session,
        user_id=user.id,
        i18n=localized,
        edit=False,
    )


@router.callback_query(F.data == CALLBACK_CART_NOOP)
async def cart_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith(CALLBACK_CART_ADD_PREFIX))
async def add_to_cart(
    callback: CallbackQuery,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or callback.data is None or message is None:
        await callback.answer()
        return

    raw_id = callback.data.removeprefix(CALLBACK_CART_ADD_PREFIX)
    product_id = parse_positive_int(raw_id)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    user, localized = await _load_onboarded_user(session, callback.from_user, i18n)
    if user is None:
        await callback.answer(i18n.t("common.not_available"), show_alert=True)
        return

    product = await CatalogService(session).get_purchasable_product(product_id)
    if product is None:
        await callback.answer(localized.t("product.not_found"), show_alert=True)
        return

    await CartService(session).add_product(user.id, product, quantity=1)
    await callback.answer(localized.t("product.added"))
    await message.answer(
        localized.t("product.added"),
        reply_markup=product_added_keyboard(localized, subcategory_id=product.subcategory_id),
    )


async def _mutate_and_refresh(
    callback: CallbackQuery,
    session: AsyncSession,
    i18n: LocalizationService,
    *,
    action: str,
    item_id: int,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or message is None:
        await callback.answer()
        return

    user, localized = await _load_onboarded_user(session, callback.from_user, i18n)
    if user is None:
        await callback.answer(i18n.t("common.not_available"), show_alert=True)
        return

    cart = CartService(session)
    if action == "inc":
        ok = await cart.increase_item(user.id, item_id)
        notice = localized.t("cart.updated")
    elif action == "dec":
        ok = await cart.decrease_item(user.id, item_id)
        notice = localized.t("cart.updated")
    elif action == "rm":
        ok = await cart.remove_item(user.id, item_id)
        notice = localized.t("cart.item_removed")
    else:
        await callback.answer(localized.t("error.invalid_callback"), show_alert=True)
        return

    if not ok:
        await callback.answer(localized.t("error.invalid_callback"), show_alert=True)
        await render_cart(
            target=message,
            session=session,
            user_id=user.id,
            i18n=localized,
            edit=True,
        )
        return

    await callback.answer(notice)
    await render_cart(
        target=message,
        session=session,
        user_id=user.id,
        i18n=localized,
        edit=True,
    )


@router.callback_query(F.data.startswith(CALLBACK_CART_INC_PREFIX))
async def increase_quantity(
    callback: CallbackQuery,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    raw = (callback.data or "").removeprefix(CALLBACK_CART_INC_PREFIX)
    item_id = parse_positive_int(raw)
    if item_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    await _mutate_and_refresh(callback, session, i18n, action="inc", item_id=item_id)


@router.callback_query(F.data.startswith(CALLBACK_CART_DEC_PREFIX))
async def decrease_quantity(
    callback: CallbackQuery,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    raw = (callback.data or "").removeprefix(CALLBACK_CART_DEC_PREFIX)
    item_id = parse_positive_int(raw)
    if item_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    await _mutate_and_refresh(callback, session, i18n, action="dec", item_id=item_id)


@router.callback_query(F.data.startswith(CALLBACK_CART_RM_PREFIX))
async def remove_item(
    callback: CallbackQuery,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    raw = (callback.data or "").removeprefix(CALLBACK_CART_RM_PREFIX)
    item_id = parse_positive_int(raw)
    if item_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    await _mutate_and_refresh(callback, session, i18n, action="rm", item_id=item_id)
