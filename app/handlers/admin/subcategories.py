"""Admin subcategory (brand) management: CRUD, visibility, order, reassignment."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.filters.localized_text import LocalizedText
from app.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.keyboards.admin_subcategories import (
    CALLBACK_SUB_ASSIGN_PREFIX,
    CALLBACK_SUB_ASSIGN_TO_PREFIX,
    CALLBACK_SUB_CREATE_PREFIX,
    CALLBACK_SUB_DELETE_OK_PREFIX,
    CALLBACK_SUB_DELETE_PREFIX,
    CALLBACK_SUB_DOWN_PREFIX,
    CALLBACK_SUB_EDIT_PREFIX,
    CALLBACK_SUB_LIST_PREFIX,
    CALLBACK_SUB_NAME_PREFIX,
    CALLBACK_SUB_TOGGLE_PREFIX,
    CALLBACK_SUB_UP_PREFIX,
    CALLBACK_SUB_VIEW_PREFIX,
    subcategories_list_keyboard,
    subcategory_assign_keyboard,
    subcategory_delete_confirm_keyboard,
    subcategory_language_keyboard,
    subcategory_manage_keyboard,
)
from app.models.category import Subcategory
from app.services.admin import AdminService, SubcategoryInUseError
from app.services.localization import LocalizationService
from app.states.admin import CreateSubcategoryStates, RenameSubcategoryStates
from app.utils.confirm import confirm_once
from app.utils.html import e
from app.utils.telegram_ui import as_message
from app.utils.validators import nonempty, parse_callback_id, parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="admin_subcategories")

_CREATE_STEPS: tuple[tuple[State, str, str], ...] = (
    (CreateSubcategoryStates.name_ru, "name_ru", "admin.subcategory_ask_name_ru"),
    (CreateSubcategoryStates.name_en, "name_en", "admin.subcategory_ask_name_en"),
    (CreateSubcategoryStates.name_de, "name_de", "admin.subcategory_ask_name_de"),
    (CreateSubcategoryStates.name_uk, "name_uk", "admin.subcategory_ask_name_uk"),
)

_LANGUAGE_FIELDS = {"ru": "name_ru", "en": "name_en", "de": "name_de", "uk": "name_uk"}


def _status(i18n: LocalizationService, subcategory: Subcategory) -> str:
    key = (
        "admin.subcategory_status_active"
        if subcategory.is_active
        else "admin.subcategory_status_inactive"
    )
    return i18n.t(key)


def _parse_id_and_lang(data: str, prefix: str) -> tuple[int, str] | None:
    """Parse ``prefix{id}:{lang}`` callbacks."""
    raw = data.removeprefix(prefix)
    parts = raw.split(":")
    row_id = parse_positive_int(parts[0]) if len(parts) == 2 else None
    if row_id is None or parts[1] not in _LANGUAGE_FIELDS:
        return None
    return row_id, parts[1]


async def _render_list(
    message: Message,
    i18n: LocalizationService,
    session: AsyncSession,
    category_id: int,
    *,
    edit: bool = False,
) -> None:
    admin = AdminService(session)
    category = await admin.get_category(category_id)
    if category is None:
        await message.answer(i18n.t("admin.category_not_found"))
        return

    subcategories = await admin.list_subcategories(category_id)
    name = e(category.name_ru)
    if subcategories:
        text = i18n.t("admin.subcategory_list_title", category=name, total=len(subcategories))
    else:
        text = i18n.t("admin.subcategory_list_empty", category=name)
    markup = subcategories_list_keyboard(i18n, category_id, subcategories)

    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit brand list", exc_info=True)
    await message.answer(text, reply_markup=markup)


async def _render_card(
    message: Message,
    i18n: LocalizationService,
    session: AsyncSession,
    subcategory: Subcategory,
    *,
    edit: bool = False,
) -> None:
    admin = AdminService(session)
    siblings = await admin.list_subcategories(subcategory.category_id)
    index = next((i for i, s in enumerate(siblings) if s.id == subcategory.id), None)
    if index is None:
        await message.answer(i18n.t("admin.subcategory_not_found"))
        return

    category = await admin.get_category(subcategory.category_id)
    products = await admin.count_subcategory_products(subcategory.id)
    text = i18n.t(
        "admin.subcategory_card",
        subcategory_id=subcategory.id,
        status=_status(i18n, subcategory),
        category=e(category.name_ru if category else "—"),
        name_ru=e(subcategory.name_ru),
        name_en=e(subcategory.name_en),
        name_de=e(subcategory.name_de),
        name_uk=e(subcategory.name_uk),
        position=index + 1,
        total=len(siblings),
        products=products,
    )
    markup = subcategory_manage_keyboard(i18n, subcategory, index=index, total=len(siblings))
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit brand card", exc_info=True)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Browse
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_SUB_LIST_PREFIX))
async def show_subcategories(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    category_id = parse_callback_id(callback.data, CALLBACK_SUB_LIST_PREFIX)
    if message is None or category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    await callback.answer()
    await _render_list(message, i18n, session, category_id, edit=True)


@router.callback_query(F.data.startswith(CALLBACK_SUB_VIEW_PREFIX))
async def view_subcategory(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, CALLBACK_SUB_VIEW_PREFIX)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    subcategory = await AdminService(session).get_subcategory(sub_id)
    if subcategory is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return
    await callback.answer()
    await _render_card(message, i18n, session, subcategory, edit=True)


# ---------------------------------------------------------------------------
# Create (four languages)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_SUB_CREATE_PREFIX))
async def start_create_subcategory(
    callback: CallbackQuery,
    state: FSMContext,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    category_id = parse_callback_id(callback.data, CALLBACK_SUB_CREATE_PREFIX)
    if message is None or category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    if await AdminService(session).get_category(category_id) is None:
        await callback.answer(i18n.t("admin.category_not_found"), show_alert=True)
        return

    await state.clear()
    await state.update_data(category_id=category_id)
    await state.set_state(CreateSubcategoryStates.name_ru)
    await callback.answer()
    await message.answer(
        i18n.t("admin.subcategory_ask_name_ru"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(CreateSubcategoryStates), LocalizedText("common.cancel"))
async def cancel_create_subcategory(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
) -> None:
    await state.clear()
    await message.answer(
        i18n.t("admin.subcategory_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )


@router.message(StateFilter(RenameSubcategoryStates.name), LocalizedText("common.cancel"))
async def cancel_rename_subcategory(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
) -> None:
    await state.clear()
    await message.answer(
        i18n.t("admin.subcategory_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )


@router.message(StateFilter(CreateSubcategoryStates), F.text)
async def process_create_step(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    current = await state.get_state()
    step = next((s for s in _CREATE_STEPS if s[0].state == current), None)
    if step is None:
        return

    value = nonempty(message.text, min_len=1, max_len=255)
    if value is None:
        await message.answer(
            i18n.t("admin.subcategory_name_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    _, field, _ = step
    await state.update_data({field: value})

    position = _CREATE_STEPS.index(step)
    if position + 1 < len(_CREATE_STEPS):
        next_state, _, prompt_key = _CREATE_STEPS[position + 1]
        await state.set_state(next_state)
        await message.answer(i18n.t(prompt_key), reply_markup=admin_cancel_keyboard(i18n))
        return

    async with confirm_once(state, lock_key=f"sub:create:{message.chat.id}") as data:
        if data is None:
            return
        category_id = int(data["category_id"])
        subcategory = await AdminService(session).create_subcategory(
            category_id=category_id,
            name=str(data["name_ru"]),
            name_ru=str(data["name_ru"]),
            name_en=str(data["name_en"]),
            name_de=str(data["name_de"]),
            name_uk=str(data["name_uk"]),
        )
        await session.flush()

    await state.clear()
    logger.info(
        "Admin created subcategory id=%s category_id=%s",
        subcategory.id,
        subcategory.category_id,
    )
    await message.answer(
        i18n.t(
            "admin.subcategory_created",
            name=e(subcategory.name_ru),
            subcategory_id=subcategory.id,
        ),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await _render_list(message, i18n, session, subcategory.category_id)


@router.message(StateFilter(CreateSubcategoryStates))
async def process_create_step_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.subcategory_name_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


# ---------------------------------------------------------------------------
# Edit localized names
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_SUB_EDIT_PREFIX))
async def pick_language(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, CALLBACK_SUB_EDIT_PREFIX)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    if await AdminService(session).get_subcategory(sub_id) is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return
    await callback.answer()
    await message.edit_text(
        i18n.t("admin.subcategory_pick_language"),
        reply_markup=subcategory_language_keyboard(i18n, sub_id),
    )


@router.callback_query(F.data.startswith(CALLBACK_SUB_NAME_PREFIX))
async def start_rename(
    callback: CallbackQuery,
    state: FSMContext,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if callback.data is None or message is None:
        await callback.answer()
        return
    parsed = _parse_id_and_lang(callback.data, CALLBACK_SUB_NAME_PREFIX)
    if parsed is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    sub_id, language = parsed

    subcategory = await AdminService(session).get_subcategory(sub_id)
    if subcategory is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return

    await state.clear()
    await state.update_data(subcategory_id=sub_id, language=language)
    await state.set_state(RenameSubcategoryStates.name)
    await callback.answer()
    await message.answer(
        i18n.t(
            "admin.subcategory_ask_name_lang",
            language=i18n.t(f"language.{language}"),
            current=e(getattr(subcategory, _LANGUAGE_FIELDS[language])),
        ),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(RenameSubcategoryStates.name), F.text)
async def process_rename(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    value = nonempty(message.text, min_len=1, max_len=255)
    if value is None:
        await message.answer(
            i18n.t("admin.subcategory_name_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    data = await state.get_data()
    sub_id = int(data["subcategory_id"])
    language = str(data["language"])

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(sub_id)
    if subcategory is None:
        await state.clear()
        await message.answer(i18n.t("admin.subcategory_not_found"))
        return

    await admin.set_subcategory_names(subcategory, **{_LANGUAGE_FIELDS[language]: value})
    await session.flush()
    await state.clear()

    await message.answer(
        i18n.t(
            "admin.subcategory_name_updated",
            language=i18n.t(f"language.{language}"),
        ),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await _render_card(message, i18n, session, subcategory)


@router.message(StateFilter(RenameSubcategoryStates.name))
async def process_rename_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.subcategory_name_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


# ---------------------------------------------------------------------------
# Visibility, ordering, reassignment
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_SUB_TOGGLE_PREFIX))
async def toggle_active(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, CALLBACK_SUB_TOGGLE_PREFIX)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(sub_id)
    if subcategory is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return

    new_state = not subcategory.is_active
    await admin.set_subcategory_active(subcategory, new_state)
    await session.flush()

    key = "admin.subcategory_activated" if new_state else "admin.subcategory_deactivated"
    logger.info("Admin set subcategory id=%s active=%s", sub_id, new_state)
    await callback.answer()
    await message.answer(i18n.t(key, name=e(subcategory.name_ru)))
    await _render_card(message, i18n, session, subcategory, edit=True)


async def _move(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
    prefix: str,
    direction: int,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, prefix)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(sub_id)
    if subcategory is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return

    await admin.move_subcategory(subcategory.category_id, sub_id, direction=direction)
    await session.flush()
    await callback.answer(i18n.t("admin.subcategory_order_updated"))
    await _render_card(message, i18n, session, subcategory, edit=True)


@router.callback_query(F.data.startswith(CALLBACK_SUB_UP_PREFIX))
async def move_up(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    await _move(callback, i18n, session, CALLBACK_SUB_UP_PREFIX, -1)


@router.callback_query(F.data.startswith(CALLBACK_SUB_DOWN_PREFIX))
async def move_down(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    await _move(callback, i18n, session, CALLBACK_SUB_DOWN_PREFIX, 1)


@router.callback_query(F.data.startswith(CALLBACK_SUB_ASSIGN_PREFIX))
async def ask_assign(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, CALLBACK_SUB_ASSIGN_PREFIX)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(sub_id)
    if subcategory is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return

    categories = await admin.list_categories()
    await callback.answer()
    await message.edit_text(
        i18n.t("admin.subcategory_assign_pick"),
        reply_markup=subcategory_assign_keyboard(i18n, subcategory, categories),
    )


@router.callback_query(F.data.startswith(CALLBACK_SUB_ASSIGN_TO_PREFIX))
async def confirm_assign(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if callback.data is None or message is None:
        await callback.answer()
        return
    raw = callback.data.removeprefix(CALLBACK_SUB_ASSIGN_TO_PREFIX).split(":")
    sub_id = parse_positive_int(raw[0]) if len(raw) == 2 else None
    category_id = parse_positive_int(raw[1]) if len(raw) == 2 else None
    if sub_id is None or category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(sub_id)
    category = await admin.get_category(category_id)
    if subcategory is None or category is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return

    await admin.reassign_subcategory(subcategory, category_id)
    await session.flush()
    logger.info("Admin moved subcategory id=%s to category_id=%s", sub_id, category_id)

    await callback.answer()
    await message.answer(i18n.t("admin.subcategory_assigned", category=e(category.name_ru)))
    await _render_card(message, i18n, session, subcategory, edit=True)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_SUB_DELETE_OK_PREFIX))
async def confirm_delete(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, CALLBACK_SUB_DELETE_OK_PREFIX)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(sub_id)
    if subcategory is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return

    name = subcategory.name_ru
    category_id = subcategory.category_id
    try:
        await admin.delete_subcategory(subcategory)
        await session.flush()
    except SubcategoryInUseError:
        await callback.answer(i18n.t("admin.subcategory_delete_in_use"), show_alert=True)
        return

    logger.info("Admin deleted subcategory id=%s", sub_id)
    await callback.answer()
    await message.answer(i18n.t("admin.subcategory_deleted", name=e(name), subcategory_id=sub_id))
    await _render_list(message, i18n, session, category_id)


@router.callback_query(F.data.startswith(CALLBACK_SUB_DELETE_PREFIX))
async def ask_delete(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    sub_id = parse_callback_id(callback.data, CALLBACK_SUB_DELETE_PREFIX)
    if message is None or sub_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    if await AdminService(session).get_subcategory(sub_id) is None:
        await callback.answer(i18n.t("admin.subcategory_not_found"), show_alert=True)
        return
    await callback.answer()
    await message.edit_text(
        i18n.t("admin.subcategory_delete_ask", subcategory_id=sub_id),
        reply_markup=subcategory_delete_confirm_keyboard(i18n, sub_id),
    )
