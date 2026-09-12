"""Admin order management: new, completed, search, change status."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.keyboards.admin_orders import (
    CALLBACK_ORDER_ACTIONS,
    CALLBACK_ORDER_CANCEL,
    CALLBACK_ORDER_DONE,
    CALLBACK_ORDER_DONE_PREFIX,
    CALLBACK_ORDER_NEW,
    CALLBACK_ORDER_NEW_PREFIX,
    CALLBACK_ORDER_SEARCH,
    CALLBACK_ORDER_STATUS_PREFIX,
    CALLBACK_ORDER_VIEW_PREFIX,
    ORDERS_PAGE_SIZE,
    order_manage_keyboard,
    orders_actions_keyboard,
    orders_list_keyboard,
    search_results_keyboard,
    status_from_code,
    status_label,
)
from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.admin import AdminService, InvalidStatusTransitionError
from app.services.customer_notification import CustomerOrderNotificationService
from app.services.localization import LocalizationService
from app.services.referral_notification import ReferralNotificationService
from app.states.admin import ADMIN_WIZARD_STATES, SearchOrderStates
from app.utils.admin_order import format_admin_order_card
from app.utils.html import e
from app.utils.telegram_ui import as_message, clamp_page, edit_or_answer, page_count
from app.utils.validators import allowlist, nonempty, parse_nonnegative_int, parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="admin_orders")

_ORDER_LIST_KINDS = frozenset({"new", "done", "search"})


async def _show_order_list(
    message: Message,
    i18n: LocalizationService,
    session: AsyncSession,
    *,
    status: OrderStatus,
    list_kind: str,
    page: int = 0,
    edit: bool = False,
) -> None:
    admin = AdminService(session)
    requested_page = page
    total, orders = await admin.page_orders_by_status(
        status,
        offset=requested_page * ORDERS_PAGE_SIZE,
        limit=ORDERS_PAGE_SIZE,
    )
    title_key = "admin.orders_new_title" if list_kind == "new" else "admin.orders_completed_title"

    if total == 0:
        text = i18n.t(
            "admin.orders_empty_new" if list_kind == "new" else "admin.orders_empty_completed"
        )
        await edit_or_answer(
            message,
            text,
            reply_markup=orders_actions_keyboard(i18n),
            edit=edit,
        )
        return

    page = clamp_page(requested_page, total, ORDERS_PAGE_SIZE)
    if page != requested_page:
        orders = await admin.list_orders_by_status(
            status,
            offset=page * ORDERS_PAGE_SIZE,
            limit=ORDERS_PAGE_SIZE,
        )
    text = i18n.t(
        title_key,
        page=page + 1,
        pages=page_count(total, ORDERS_PAGE_SIZE),
        total=total,
    )
    markup = orders_list_keyboard(
        i18n,
        orders,
        page=page,
        total=total,
        list_kind=list_kind,
    )
    await edit_or_answer(message, text, reply_markup=markup, edit=edit)


async def _send_order_view(
    message: Message,
    i18n: LocalizationService,
    order: Order,
    *,
    list_kind: str = "new",
    page: int = 0,
    edit: bool = False,
) -> None:
    text = format_admin_order_card(order, i18n)
    markup = order_manage_keyboard(i18n, order, list_kind=list_kind, page=page)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit order view", exc_info=True)
    await message.answer(text, reply_markup=markup)


def _parse_view_callback(data: str) -> tuple[int, str, int] | None:
    raw = data.removeprefix(CALLBACK_ORDER_VIEW_PREFIX)
    parts = raw.split(":")
    if not parts:
        return None
    order_id = parse_positive_int(parts[0])
    if order_id is None:
        return None
    list_kind = allowlist(parts[1] if len(parts) > 1 else "new", _ORDER_LIST_KINDS)
    if list_kind is None:
        return None
    page = 0
    if len(parts) > 2:
        parsed_page = parse_nonnegative_int(parts[2])
        if parsed_page is None:
            return None
        page = parsed_page
    return order_id, list_kind, page


def _parse_status_callback(data: str) -> tuple[int, OrderStatus, str, int] | None:
    raw = data.removeprefix(CALLBACK_ORDER_STATUS_PREFIX)
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    order_id = parse_positive_int(parts[0])
    if order_id is None:
        return None
    status = status_from_code(parts[1])
    if status is None:
        return None
    list_kind = allowlist(parts[2] if len(parts) > 2 else "new", _ORDER_LIST_KINDS)
    if list_kind is None:
        return None
    page = 0
    if len(parts) > 3:
        parsed_page = parse_nonnegative_int(parts[3])
        if parsed_page is None:
            return None
        page = parsed_page
    return order_id, status, list_kind, page


# ---------------------------------------------------------------------------
# Section entry
# ---------------------------------------------------------------------------


@router.message(LocalizedText("admin.menu_orders"), ~StateFilter(*ADMIN_WIZARD_STATES))
async def open_orders(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.section_orders"),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.orders_actions"),
        reply_markup=orders_actions_keyboard(i18n),
    )


@router.callback_query(F.data == CALLBACK_ORDER_ACTIONS)
async def show_order_actions(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    message = as_message(callback)
    if message is None:
        return
    try:
        await message.edit_text(
            i18n.t("admin.orders_actions"),
            reply_markup=orders_actions_keyboard(i18n),
        )
    except Exception:
        await message.answer(
            i18n.t("admin.orders_actions"),
            reply_markup=orders_actions_keyboard(i18n),
        )


# ---------------------------------------------------------------------------
# View new / completed
# ---------------------------------------------------------------------------


@router.callback_query(F.data == CALLBACK_ORDER_NEW)
@router.callback_query(F.data.startswith(CALLBACK_ORDER_NEW_PREFIX))
async def view_new_orders(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    page = 0
    if callback.data.startswith(CALLBACK_ORDER_NEW_PREFIX):
        parsed = parse_nonnegative_int(callback.data.removeprefix(CALLBACK_ORDER_NEW_PREFIX))
        if parsed is None:
            await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
            return
        page = parsed

    # Answered once, after parsing: Telegram accepts one answer per tap.
    await callback.answer()
    await _show_order_list(
        message,
        i18n,
        session,
        status=OrderStatus.NEW,
        list_kind="new",
        page=page,
        edit=True,
    )


@router.callback_query(F.data == CALLBACK_ORDER_DONE)
@router.callback_query(F.data.startswith(CALLBACK_ORDER_DONE_PREFIX))
async def view_completed_orders(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    page = 0
    if callback.data.startswith(CALLBACK_ORDER_DONE_PREFIX):
        parsed = parse_nonnegative_int(callback.data.removeprefix(CALLBACK_ORDER_DONE_PREFIX))
        if parsed is None:
            await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
            return
        page = parsed

    # Answered once, after parsing: Telegram accepts one answer per tap.
    await callback.answer()
    await _show_order_list(
        message,
        i18n,
        session,
        status=OrderStatus.COMPLETED,
        list_kind="done",
        page=page,
        edit=True,
    )


@router.callback_query(F.data.startswith(CALLBACK_ORDER_VIEW_PREFIX))
async def view_order(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await state.clear()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    parsed = _parse_view_callback(callback.data)
    if parsed is None:
        await message.answer(i18n.t("error.invalid_callback"))
        return

    order_id, list_kind, page = parsed
    order = await AdminService(session).get_order(order_id)
    if order is None:
        await message.answer(i18n.t("admin.order_not_found"))
        return

    await _send_order_view(
        message,
        i18n,
        order,
        list_kind=list_kind,
        page=page,
        edit=True,
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.callback_query(F.data == CALLBACK_ORDER_SEARCH)
async def start_search_orders(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    await state.set_state(SearchOrderStates.query)
    await message.answer(
        i18n.t("admin.orders_ask_search"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(SearchOrderStates.query), LocalizedText("common.cancel"))
async def cancel_search(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        i18n.t("admin.orders_search_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.orders_actions"),
        reply_markup=orders_actions_keyboard(i18n),
    )


@router.callback_query(StateFilter(SearchOrderStates), F.data == CALLBACK_ORDER_CANCEL)
async def cancel_search_callback(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    message = as_message(callback)
    if message is None:
        return
    await message.answer(
        i18n.t("admin.orders_search_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )


@router.message(StateFilter(SearchOrderStates.query), F.text)
async def process_search(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    query = nonempty(message.text)
    if query is None:
        await message.answer(
            i18n.t("admin.orders_search_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    orders = await AdminService(session).search_orders(query, limit=20)
    await state.clear()

    if not orders:
        await message.answer(
            i18n.t("admin.orders_search_empty", query=e(query)),
            reply_markup=admin_menu_keyboard(i18n),
        )
        await message.answer(
            i18n.t("admin.orders_actions"),
            reply_markup=orders_actions_keyboard(i18n),
        )
        return

    if len(orders) == 1:
        await message.answer(
            i18n.t("admin.orders_search_one"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        await _send_order_view(message, i18n, orders[0], list_kind="search", page=0)
        return

    await message.answer(
        i18n.t("admin.orders_search_results", count=len(orders), query=e(query)),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.orders_search_pick"),
        reply_markup=search_results_keyboard(i18n, orders),
    )


@router.message(StateFilter(SearchOrderStates.query))
async def process_search_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.orders_search_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


# ---------------------------------------------------------------------------
# Change status
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_ORDER_STATUS_PREFIX))
async def change_order_status(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    message = callback.message if isinstance(callback.message, Message) else None
    parsed = _parse_status_callback(callback.data)
    if message is None or parsed is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    order_id, new_status, list_kind, page = parsed
    # Settings carry the configured stamp-card rules to order completion.
    admin = AdminService(session, settings=settings)
    order = await admin.get_order(order_id)
    if order is None:
        await callback.answer(i18n.t("admin.order_not_found"), show_alert=True)
        return

    if order.status == new_status:
        await callback.answer()
        return

    try:
        change = await admin.order_admin.change_order_status(order, new_status)
    except InvalidStatusTransitionError:
        # Stale keyboard or crafted callback: refuse and re-render the truth.
        # Nothing was written — end the transaction, and the order row lock it
        # took, before answering, then read the order afresh.
        await session.rollback()
        current = await admin.get_order(order_id)
        if current is None:
            await callback.answer(i18n.t("admin.order_not_found"), show_alert=True)
            return
        await callback.answer(
            i18n.t(
                "admin.order_invalid_transition",
                current=status_label(i18n, current.status),
                target=status_label(i18n, new_status),
            ),
            show_alert=True,
        )
        await _send_order_view(message, i18n, current, list_kind=list_kind, page=page, edit=True)
        return
    # Commit before telling the customer: they must never hear about a change
    # that is not durable, and a Telegram failure must not undo it.
    await session.commit()

    order = await admin.get_order(change.order.id) or change.order
    # Two admins tapping at once, or one update delivered twice: only the
    # request that actually moved the order tells anyone about it.
    if change.changed and order.user is not None:
        await CustomerOrderNotificationService(bot).notify_status_change(order, order.user)

    try:
        await callback.answer(
            i18n.t(
                "admin.order_status_changed",
                order_id=order.id,
                status=status_label(i18n, order.status),
            )
        )
        view_target = as_message(callback)
        if view_target is not None:
            await _send_order_view(
                view_target,
                i18n,
                order,
                list_kind=list_kind,
                page=page,
                edit=True,
            )
    except TelegramAPIError:
        # The admin's own screen is best effort: the change is committed, and
        # the referral news below must go out whatever happened to it.
        logger.warning(
            "Could not refresh the admin's order screen order_id=%s", order.id, exc_info=True
        )
    if change.changed and order.status == OrderStatus.COMPLETED:
        # Last, after the commit: tell both sides of a referral this completion
        # paid out, if it paid one. Read-only, and it swallows its own failures.
        await ReferralNotificationService(session, bot, settings=settings).rewards_paid(order.id)
