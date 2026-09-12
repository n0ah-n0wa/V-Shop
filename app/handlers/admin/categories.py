"""Admin categories: create, rename, delete, reorder."""

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
from app.keyboards.admin_categories import (
    CALLBACK_CATEGORY_CANCEL,
    CALLBACK_CATEGORY_CREATE,
    CALLBACK_CATEGORY_DELETE_OK_PREFIX,
    CALLBACK_CATEGORY_DELETE_PREFIX,
    CALLBACK_CATEGORY_DOWN_PREFIX,
    CALLBACK_CATEGORY_EDIT_PREFIX,
    CALLBACK_CATEGORY_LIST,
    CALLBACK_CATEGORY_NAME_PREFIX,
    CALLBACK_CATEGORY_RENAME_PREFIX,
    CALLBACK_CATEGORY_TOGGLE_PREFIX,
    CALLBACK_CATEGORY_UP_PREFIX,
    CALLBACK_CATEGORY_VIEW_PREFIX,
    categories_actions_keyboard,
    categories_admin_list_keyboard,
    category_delete_confirm_keyboard,
    category_language_keyboard,
    category_manage_keyboard,
)
from app.models.category import Category
from app.services.admin import AdminService, CategoryInUseError
from app.services.localization import LocalizationService
from app.states.admin import (
    ADMIN_WIZARD_STATES,
    CreateCategoryStates,
    RenameCategoryStates,
)
from app.utils.html import e
from app.utils.product_display import localized_category_name
from app.utils.telegram_ui import as_message, edit_or_answer
from app.utils.validators import nonempty, parse_callback_id, parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="admin_categories")

CREATE_STEPS: tuple[tuple[State, str, str], ...] = (
    (CreateCategoryStates.name_ru, "name_ru", "admin.category_ask_name_ru"),
    (CreateCategoryStates.name_en, "name_en", "admin.category_ask_name_en"),
    (CreateCategoryStates.name_de, "name_de", "admin.category_ask_name_de"),
    (CreateCategoryStates.name_uk, "name_uk", "admin.category_ask_name_uk"),
)

LANGUAGE_FIELDS = {"ru": "name_ru", "en": "name_en", "de": "name_de", "uk": "name_uk"}


async def _cancel_category_wizard(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        i18n.t("admin.category_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )


async def _show_categories_list(
    message: Message,
    i18n: LocalizationService,
    session: AsyncSession,
    *,
    edit: bool = False,
) -> None:
    categories = await AdminService(session).list_categories()
    if not categories:
        text = i18n.t("admin.category_list_empty")
        markup = categories_actions_keyboard(i18n)
    else:
        text = i18n.t("admin.category_list_title", total=len(categories))
        markup = categories_admin_list_keyboard(i18n, categories)

    await edit_or_answer(message, text, reply_markup=markup, edit=edit)


def _category_index(categories: list[Category], category_id: int) -> int | None:
    for index, category in enumerate(categories):
        if category.id == category_id:
            return index
    return None


async def _send_category_view(
    message: Message,
    i18n: LocalizationService,
    session: AsyncSession,
    category: Category,
    *,
    edit: bool = False,
) -> None:
    admin = AdminService(session)
    categories = await admin.list_categories()
    index = _category_index(categories, category.id)
    if index is None:
        await message.answer(i18n.t("admin.category_not_found"))
        return

    product_count = await admin.count_category_products(category.id)
    subcategory_count = len(await admin.list_subcategories(category.id))
    status = i18n.t(
        "admin.category_status_active" if category.is_active else "admin.category_status_inactive"
    )
    text = i18n.t(
        "admin.category_card",
        category_id=category.id,
        status=status,
        name_ru=e(category.name_ru),
        name_en=e(category.name_en),
        name_de=e(category.name_de),
        name_uk=e(category.name_uk),
        position=index + 1,
        total=len(categories),
        subcategories=subcategory_count,
        products=product_count,
    )
    markup = category_manage_keyboard(
        i18n,
        category,
        index=index,
        total=len(categories),
        subcategory_count=subcategory_count,
    )
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit category view", exc_info=True)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Section entry
# ---------------------------------------------------------------------------


@router.message(LocalizedText("admin.menu_categories"), ~StateFilter(*ADMIN_WIZARD_STATES))
async def open_categories(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.section_categories"),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.category_actions"),
        reply_markup=categories_actions_keyboard(i18n),
    )


@router.callback_query(F.data == CALLBACK_CATEGORY_LIST)
async def show_categories_list(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await state.clear()
    message = as_message(callback)
    if message is None:
        return
    await _show_categories_list(message, i18n, session, edit=True)


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_VIEW_PREFIX))
async def view_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_VIEW_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    await callback.answer()
    await state.clear()

    category = await AdminService(session).get_category(category_id)
    if category is None:
        await message.answer(i18n.t("admin.category_not_found"))
        return

    await _send_category_view(message, i18n, session, category, edit=True)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.callback_query(F.data == CALLBACK_CATEGORY_CREATE)
async def start_create_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    await state.clear()
    await state.set_state(CreateCategoryStates.name_ru)
    await message.answer(
        i18n.t("admin.category_ask_name_ru"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(CreateCategoryStates), LocalizedText("common.cancel"))
@router.message(StateFilter(RenameCategoryStates.name), LocalizedText("common.cancel"))
async def cancel_category_wizard_message(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await _cancel_category_wizard(message, i18n, state)


@router.callback_query(
    StateFilter(CreateCategoryStates, RenameCategoryStates),
    F.data == CALLBACK_CATEGORY_CANCEL,
)
async def cancel_category_wizard_callback(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    await _cancel_category_wizard(message, i18n, state)


@router.message(StateFilter(CreateCategoryStates), F.text)
async def process_create_category(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Walk the four localized names, then persist once."""
    current = await state.get_state()
    step = next((s for s in CREATE_STEPS if s[0].state == current), None)
    if step is None:
        return

    name = nonempty(message.text, min_len=1, max_len=255)
    if name is None:
        await message.answer(
            i18n.t("admin.category_name_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    _, field, _ = step
    if field == "name_ru":
        existing = await AdminService(session).get_category_by_name(name)
        if existing is not None:
            await message.answer(
                i18n.t("admin.category_name_exists"),
                reply_markup=admin_cancel_keyboard(i18n),
            )
            return

    await state.update_data({field: name})
    position = CREATE_STEPS.index(step)
    if position + 1 < len(CREATE_STEPS):
        next_state, _, prompt_key = CREATE_STEPS[position + 1]
        await state.set_state(next_state)
        await message.answer(i18n.t(prompt_key), reply_markup=admin_cancel_keyboard(i18n))
        return

    data = await state.get_data()
    category = await AdminService(session).create_category(
        str(data["name_ru"]),
        name_ru=str(data["name_ru"]),
        name_en=str(data["name_en"]),
        name_de=str(data["name_de"]),
        name_uk=str(data["name_uk"]),
    )
    await session.flush()
    await state.clear()
    logger.info("Admin created category id=%s", category.id)
    await message.answer(
        i18n.t(
            "admin.category_created",
            name=e(localized_category_name(category, i18n.language)),
            category_id=category.id,
        ),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await _send_category_view(message, i18n, session, category)


# ---------------------------------------------------------------------------
# Activate / deactivate
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_TOGGLE_PREFIX))
async def toggle_category_active(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_TOGGLE_PREFIX)
    if message is None or category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    category = await admin.get_category(category_id)
    if category is None:
        await callback.answer(i18n.t("admin.category_not_found"), show_alert=True)
        return

    new_state = not category.is_active
    await admin.set_category_active(category, new_state)
    await session.flush()
    logger.info("Admin set category id=%s active=%s", category_id, new_state)

    key = "admin.category_activated" if new_state else "admin.category_deactivated"
    await callback.answer()
    await message.answer(i18n.t(key, name=e(localized_category_name(category, i18n.language))))
    await _send_category_view(message, i18n, session, category, edit=True)


# ---------------------------------------------------------------------------
# Edit localized names
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_EDIT_PREFIX))
async def pick_category_language(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_EDIT_PREFIX)
    if message is None or category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    if await AdminService(session).get_category(category_id) is None:
        await callback.answer(i18n.t("admin.category_not_found"), show_alert=True)
        return
    await callback.answer()
    await message.edit_text(
        i18n.t("admin.category_pick_language"),
        reply_markup=category_language_keyboard(i18n, category_id),
    )


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_NAME_PREFIX))
async def start_edit_category_name(
    callback: CallbackQuery,
    state: FSMContext,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if callback.data is None or message is None:
        await callback.answer()
        return
    raw = callback.data.removeprefix(CALLBACK_CATEGORY_NAME_PREFIX).split(":")
    category_id = parse_positive_int(raw[0]) if len(raw) == 2 else None
    if category_id is None or raw[1] not in LANGUAGE_FIELDS:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    language = raw[1]

    category = await AdminService(session).get_category(category_id)
    if category is None:
        await callback.answer(i18n.t("admin.category_not_found"), show_alert=True)
        return

    await state.clear()
    await state.update_data(category_id=category_id, language=language)
    await state.set_state(RenameCategoryStates.name)
    await callback.answer()
    await message.answer(
        i18n.t(
            "admin.category_ask_name_lang",
            language=i18n.t(f"language.{language}"),
            current=e(getattr(category, LANGUAGE_FIELDS[language])),
        ),
        reply_markup=admin_cancel_keyboard(i18n),
    )


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_RENAME_PREFIX))
async def start_rename_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_RENAME_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    await callback.answer()
    category = await AdminService(session).get_category(category_id)
    if category is None:
        await message.answer(i18n.t("admin.category_not_found"))
        return

    await state.set_state(RenameCategoryStates.name)
    await state.update_data(category_id=category.id, current_name=category.name)
    await message.answer(
        i18n.t("admin.category_ask_rename", current=category.name),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(RenameCategoryStates.name), F.text)
async def process_rename_category(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    name = nonempty(message.text, min_len=1, max_len=255)
    if name is None:
        await message.answer(
            i18n.t("admin.category_name_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    data = await state.get_data()
    language = str(data.get("language") or "ru")
    admin = AdminService(session)
    category = await admin.get_category(int(data["category_id"]))
    if category is None:
        await state.clear()
        await message.answer(
            i18n.t("admin.category_not_found"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return

    existing = await admin.get_category_by_name(name)
    if language == "ru" and existing is not None and existing.id != category.id:
        await message.answer(
            i18n.t("admin.category_name_exists"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    await admin.set_category_names(category, **{LANGUAGE_FIELDS[language]: name})
    await session.flush()
    await state.clear()
    await message.answer(
        i18n.t(
            "admin.category_name_updated",
            language=i18n.t(f"language.{language}"),
        ),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await _send_category_view(message, i18n, session, category)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_DELETE_PREFIX))
async def ask_delete_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_DELETE_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    await callback.answer()
    await message.answer(
        i18n.t("admin.category_delete_ask", category_id=category_id),
        reply_markup=category_delete_confirm_keyboard(i18n, category_id),
    )


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_DELETE_OK_PREFIX))
async def confirm_delete_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_DELETE_OK_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    await callback.answer()
    admin = AdminService(session)
    category = await admin.get_category(category_id)
    if category is None:
        await message.answer(i18n.t("admin.category_not_found"))
        return

    name = category.name
    try:
        await admin.delete_category(category)
    except CategoryInUseError as exc:
        if "subcategory" in str(exc):
            await callback.answer(
                i18n.t(
                    "admin.category_delete_has_subcategories",
                    count=len(await admin.list_subcategories(category_id)),
                ),
                show_alert=True,
            )
            return
        await message.answer(i18n.t("admin.category_delete_in_use"))
        category = await admin.get_category(category_id)
        if category is not None:
            await _send_category_view(message, i18n, session, category)
        return

    await state.clear()
    await message.answer(
        i18n.t("admin.category_deleted", name=name, category_id=category_id),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await _show_categories_list(message, i18n, session)


# ---------------------------------------------------------------------------
# Change order
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_UP_PREFIX))
async def move_category_up(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_UP_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    await AdminService(session).move_category(category_id, direction=-1)
    category = await AdminService(session).get_category(category_id)
    if category is None:
        await callback.answer(i18n.t("admin.category_not_found"), show_alert=True)
        return
    await callback.answer(i18n.t("admin.category_order_updated"))
    await _send_category_view(message, i18n, session, category, edit=True)


@router.callback_query(F.data.startswith(CALLBACK_CATEGORY_DOWN_PREFIX))
async def move_category_down(
    callback: CallbackQuery,
    i18n: LocalizationService,
    session: AsyncSession,
) -> None:
    message = as_message(callback)
    if message is None or callback.data is None:
        await callback.answer()
        return

    category_id = parse_callback_id(callback.data, CALLBACK_CATEGORY_DOWN_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    await AdminService(session).move_category(category_id, direction=+1)
    category = await AdminService(session).get_category(category_id)
    if category is None:
        await callback.answer(i18n.t("admin.category_not_found"), show_alert=True)
        return
    await callback.answer(i18n.t("admin.category_order_updated"))
    await _send_category_view(message, i18n, session, category, edit=True)


@router.message(StateFilter(CreateCategoryStates))
async def process_create_category_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.category_name_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(RenameCategoryStates.name))
async def process_rename_category_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.category_name_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
