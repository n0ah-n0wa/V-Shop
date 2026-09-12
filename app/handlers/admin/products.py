"""Admin products section and Add Product FSM wizard."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.filters.localized_text import LocalizedText
from app.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.keyboards.admin_products import (
    CALLBACK_PRODUCT_ADD,
    CALLBACK_PRODUCT_CANCEL,
    CALLBACK_PRODUCT_CAT_PREFIX,
    CALLBACK_PRODUCT_CONFIRM,
    CALLBACK_PRODUCT_SUB_PREFIX,
    admin_category_pick_keyboard,
    admin_product_confirm_keyboard,
    admin_subcategory_pick_keyboard,
    products_actions_keyboard,
)
from app.models.category import Category
from app.models.product import text_limit
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.states.admin import ADMIN_WIZARD_STATES, AddProductStates
from app.utils.confirm import confirm_once
from app.utils.html import e
from app.utils.product_display import localized_category_name
from app.utils.telegram_ui import as_message, clear_inline_markup
from app.utils.validators import nonempty, parse_positive_int, parse_price

logger = logging.getLogger(__name__)

router = Router(name="admin_products")

# Current state → (FSM data key, next state, prompt key for the next step)
_TEXT_STEPS: dict[Any, tuple[str, Any, str | None]] = {
    AddProductStates.name_ru: ("name_ru", AddProductStates.name_en, "admin.product_ask_name_en"),
    AddProductStates.name_en: ("name_en", AddProductStates.name_de, "admin.product_ask_name_de"),
    AddProductStates.name_de: (
        "name_de",
        AddProductStates.name_uk,
        "admin.product_ask_name_uk",
    ),
    AddProductStates.name_uk: (
        "name_uk",
        AddProductStates.description_ru,
        "admin.product_ask_description_ru",
    ),
    AddProductStates.description_ru: (
        "description_ru",
        AddProductStates.description_en,
        "admin.product_ask_description_en",
    ),
    AddProductStates.description_en: (
        "description_en",
        AddProductStates.description_de,
        "admin.product_ask_description_de",
    ),
    AddProductStates.description_de: (
        "description_de",
        AddProductStates.description_uk,
        "admin.product_ask_description_uk",
    ),
    AddProductStates.description_uk: ("description_uk", AddProductStates.category, None),
    AddProductStates.flavor: ("flavor", AddProductStates.volume, "admin.product_ask_volume"),
    AddProductStates.volume: (
        "volume",
        AddProductStates.nicotine_strength,
        "admin.product_ask_nicotine",
    ),
    AddProductStates.nicotine_strength: (
        "nicotine_strength",
        AddProductStates.price,
        "admin.product_ask_price",
    ),
}


def _build_preview(i18n: LocalizationService, data: dict[str, Any]) -> str:
    return i18n.t(
        "admin.product_preview",
        name_ru=e(data["name_ru"]),
        name_en=e(data["name_en"]),
        name_de=e(data["name_de"]),
        description_ru=e(data["description_ru"]),
        description_en=e(data["description_en"]),
        description_de=e(data["description_de"]),
        name_uk=e(data["name_uk"]),
        description_uk=e(data["description_uk"]),
        category=e(data.get("category_name", data["category_id"])),
        subcategory=e(data.get("subcategory_name", "—")),
        flavor=e(data["flavor"]),
        volume=e(data["volume"]),
        nicotine=e(data["nicotine_strength"]),
        price=e(data["price"]),
    )


async def _cancel_wizard(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        i18n.t("admin.product_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )


async def _ask_photo(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await state.set_state(AddProductStates.photo)
    await message.answer(
        i18n.t("admin.product_ask_photo"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


async def _ask_subcategory(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
    category: Category,
) -> None:
    """Offer only the brands of the chosen category — the assignment guard."""
    subcategories = await AdminService(session).list_subcategories(category.id)
    if not subcategories:
        await state.clear()
        await message.answer(
            i18n.t(
                "admin.product_no_subcategories",
                category=e(localized_category_name(category, i18n.language)),
            ),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return

    await state.set_state(AddProductStates.subcategory)
    await message.answer(
        i18n.t("admin.product_ask_subcategory"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
    await message.answer(
        i18n.t(
            "admin.product_pick_subcategory",
            category=e(localized_category_name(category, i18n.language)),
        ),
        reply_markup=admin_subcategory_pick_keyboard(subcategories),
    )


async def _ask_category(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    categories = await AdminService(session).list_categories()
    if not categories:
        await state.clear()
        await message.answer(
            i18n.t("admin.product_no_categories"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return

    await state.set_state(AddProductStates.category)
    await message.answer(
        i18n.t("admin.product_ask_category"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.product_pick_category"),
        reply_markup=admin_category_pick_keyboard(categories),
    )


async def _show_confirmation(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.set_state(AddProductStates.confirmation)
    preview = _build_preview(i18n, data)
    markup = admin_product_confirm_keyboard(i18n)
    file_id = data.get("image_file_id")
    if file_id:
        try:
            await message.answer_photo(
                photo=file_id,
                caption=preview[:1024],
                reply_markup=markup,
            )
            if len(preview) > 1024:
                await message.answer(preview)
            return
        except Exception:
            logger.debug("Could not send product preview photo", exc_info=True)
    await message.answer(preview, reply_markup=markup)


# ---------------------------------------------------------------------------
# Section entry
# ---------------------------------------------------------------------------


@router.message(LocalizedText("admin.menu_products"), ~StateFilter(*ADMIN_WIZARD_STATES))
async def open_products(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.section_products"),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.products_actions"),
        reply_markup=products_actions_keyboard(i18n),
    )


@router.callback_query(F.data == CALLBACK_PRODUCT_ADD)
async def start_add_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    await state.clear()
    await _ask_photo(message, i18n, state)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@router.message(StateFilter(AddProductStates), LocalizedText("common.cancel"))
async def cancel_add_product_message(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await _cancel_wizard(message, i18n, state)


@router.callback_query(StateFilter(AddProductStates), F.data == CALLBACK_PRODUCT_CANCEL)
async def cancel_add_product_callback(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    await _cancel_wizard(message, i18n, state)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@router.message(StateFilter(AddProductStates.photo), F.photo)
async def process_photo(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    if not message.photo:
        return
    photo = message.photo[-1]
    await state.update_data(image_file_id=photo.file_id)
    await state.set_state(AddProductStates.name_ru)
    await message.answer(
        i18n.t("admin.product_ask_name_ru"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(AddProductStates.photo))
async def process_photo_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.product_photo_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(*_TEXT_STEPS.keys()), F.text)
async def process_text_step(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    current = await state.get_state()
    matched: tuple[str, Any, str | None] | None = None
    for state_obj, meta in _TEXT_STEPS.items():
        if current == state_obj.state:
            matched = meta
            break
    if matched is None:
        return

    data_key, next_state, next_prompt = matched
    value = nonempty(message.text)
    if value is None:
        await message.answer(
            i18n.t("admin.product_text_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return
    limit = text_limit(data_key)
    if limit is not None and len(value) > limit:
        await message.answer(
            i18n.t("admin.product_text_too_long", limit=limit),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    update: dict[str, Any] = {data_key: value}
    await state.update_data(**update)
    await state.set_state(next_state)

    if next_state == AddProductStates.category:
        await _ask_category(message, i18n, state, session)
        return

    if next_prompt:
        await message.answer(
            i18n.t(next_prompt),
            reply_markup=admin_cancel_keyboard(i18n),
        )


@router.message(StateFilter(*_TEXT_STEPS.keys()))
async def process_text_step_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.product_text_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.callback_query(
    StateFilter(AddProductStates.category),
    F.data.startswith(CALLBACK_PRODUCT_CAT_PREFIX),
)
async def process_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    raw_id = callback.data.removeprefix(CALLBACK_PRODUCT_CAT_PREFIX)
    category_id = parse_positive_int(raw_id)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    category = await AdminService(session).get_category(category_id)
    if category is None:
        await message.answer(i18n.t("admin.product_category_invalid"))
        return

    await state.update_data(
        category_id=category.id,
        category_name=localized_category_name(category, i18n.language),
    )
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear category keyboard", exc_info=True)
    await _ask_subcategory(message, i18n, state, session, category)


@router.message(StateFilter(AddProductStates.category))
async def process_category_invalid(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await message.answer(i18n.t("admin.product_category_invalid"))
    await _ask_category(message, i18n, state, session)


@router.callback_query(
    StateFilter(AddProductStates.subcategory),
    F.data.startswith(CALLBACK_PRODUCT_SUB_PREFIX),
)
async def process_subcategory(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    subcategory_id = parse_positive_int(callback.data.removeprefix(CALLBACK_PRODUCT_SUB_PREFIX))
    if subcategory_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    admin = AdminService(session)
    subcategory = await admin.get_subcategory(subcategory_id)
    if subcategory is None:
        await message.answer(i18n.t("admin.product_subcategory_invalid"))
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    if category_id is None or subcategory.category_id != int(category_id):
        # Guard: a stale keyboard must never attach a brand to the wrong category.
        await message.answer(i18n.t("admin.product_subcategory_mismatch"))
        return

    await state.update_data(
        subcategory_id=subcategory.id,
        subcategory_name=localized_category_name(subcategory, i18n.language),
    )
    await state.set_state(AddProductStates.flavor)
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear brand keyboard", exc_info=True)
    await message.answer(
        i18n.t("admin.product_ask_flavor"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(AddProductStates.subcategory))
async def process_subcategory_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(i18n.t("admin.product_subcategory_invalid"))


@router.message(StateFilter(AddProductStates.price), F.text)
async def process_price(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    price = parse_price(message.text or "")
    if price is None:
        await message.answer(
            i18n.t("admin.product_price_invalid"),
            reply_markup=admin_cancel_keyboard(i18n),
        )
        return

    await state.update_data(price=str(price))
    await message.answer(
        i18n.t("admin.product_confirm_intro"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
    await _show_confirmation(message, i18n, state)


@router.message(StateFilter(AddProductStates.price))
async def process_price_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.product_price_invalid"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.callback_query(
    StateFilter(AddProductStates.confirmation),
    F.data == CALLBACK_PRODUCT_CONFIRM,
)
async def confirm_add_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.from_user is None:
        return

    async with confirm_once(state, lock_key=f"product_create:{callback.from_user.id}") as data:
        if data is None:
            return

        required = (
            "category_id",
            "name_ru",
            "name_en",
            "name_de",
            "name_uk",
            "description_ru",
            "description_en",
            "description_de",
            "description_uk",
            "flavor",
            "volume",
            "nicotine_strength",
            "price",
        )
        if any(key not in data for key in required):
            await state.clear()
            await message.answer(
                i18n.t("admin.product_incomplete"),
                reply_markup=admin_menu_keyboard(i18n),
            )
            return

        product = await AdminService(session).create_product(
            category_id=int(data["category_id"]),
            subcategory_id=int(data["subcategory_id"]),
            name_ru=data["name_ru"],
            name_en=data["name_en"],
            name_de=data["name_de"],
            name_uk=data["name_uk"],
            description_ru=data["description_ru"],
            description_en=data["description_en"],
            description_de=data["description_de"],
            description_uk=data["description_uk"],
            flavor=data["flavor"],
            volume=data["volume"],
            nicotine_strength=data["nicotine_strength"],
            price=data["price"],
            image_file_id=data.get("image_file_id"),
            is_active=True,
        )
        await state.clear()

        await clear_inline_markup(message)
        await message.answer(
            i18n.t("admin.product_created", product_id=product.id),
            reply_markup=admin_menu_keyboard(i18n),
        )


@router.message(StateFilter(AddProductStates.confirmation))
async def confirmation_waiting(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("admin.product_confirm_waiting"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
