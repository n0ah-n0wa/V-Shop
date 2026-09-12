"""Shared helpers for admin product management handlers."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.admin import admin_cancel_skip_keyboard, admin_menu_keyboard
from app.keyboards.admin_products import (
    CALLBACK_PRODUCT_EDIT_CAT_PREFIX,
    CALLBACK_PRODUCT_EDIT_CONFIRM,
    PRODUCTS_PAGE_SIZE,
    admin_category_pick_keyboard,
    admin_product_confirm_keyboard,
    product_manage_keyboard,
    products_actions_keyboard,
    products_list_keyboard,
)
from app.models.product import Product
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.states.admin import EditProductStates
from app.utils.html import e
from app.utils.product_display import format_admin_product_card
from app.utils.telegram_ui import clamp_page, edit_or_answer, int_from_state, page_count

logger = logging.getLogger(__name__)

# Edit-product text steps: (data_key, next_state, prompt_key, current_key_for_prompt)
EDIT_TEXT_STEPS: dict[Any, tuple[str, Any, str, str]] = {
    EditProductStates.name_ru: (
        "name_ru",
        EditProductStates.name_en,
        "admin.product_ask_name_ru_edit",
        "name_ru",
    ),
    EditProductStates.name_en: (
        "name_en",
        EditProductStates.name_de,
        "admin.product_ask_name_en_edit",
        "name_en",
    ),
    EditProductStates.name_de: (
        "name_de",
        EditProductStates.name_uk,
        "admin.product_ask_name_de_edit",
        "name_de",
    ),
    EditProductStates.name_uk: (
        "name_uk",
        EditProductStates.description_ru,
        "admin.product_ask_name_uk_edit",
        "name_uk",
    ),
    EditProductStates.description_ru: (
        "description_ru",
        EditProductStates.description_en,
        "admin.product_ask_description_ru_edit",
        "description_ru",
    ),
    EditProductStates.description_en: (
        "description_en",
        EditProductStates.description_de,
        "admin.product_ask_description_en_edit",
        "description_en",
    ),
    EditProductStates.description_de: (
        "description_de",
        EditProductStates.description_uk,
        "admin.product_ask_description_de_edit",
        "description_de",
    ),
    EditProductStates.description_uk: (
        "description_uk",
        EditProductStates.category,
        "admin.product_ask_description_uk_edit",
        "description_uk",
    ),
    EditProductStates.flavor: (
        "flavor",
        EditProductStates.volume,
        "admin.product_ask_flavor_edit",
        "flavor",
    ),
    EditProductStates.volume: (
        "volume",
        EditProductStates.nicotine_strength,
        "admin.product_ask_volume_edit",
        "volume",
    ),
    EditProductStates.nicotine_strength: (
        "nicotine_strength",
        EditProductStates.price,
        "admin.product_ask_nicotine_edit",
        "nicotine_strength",
    ),
}

# After saving a step, which prompt to show for the *next* state
EDIT_NEXT_PROMPTS: dict[Any, tuple[str, str]] = {
    EditProductStates.name_en: ("admin.product_ask_name_en_edit", "name_en"),
    EditProductStates.name_de: ("admin.product_ask_name_de_edit", "name_de"),
    EditProductStates.name_uk: ("admin.product_ask_name_uk_edit", "name_uk"),
    EditProductStates.description_ru: (
        "admin.product_ask_description_ru_edit",
        "description_ru",
    ),
    EditProductStates.description_en: (
        "admin.product_ask_description_en_edit",
        "description_en",
    ),
    EditProductStates.description_de: (
        "admin.product_ask_description_de_edit",
        "description_de",
    ),
    EditProductStates.description_uk: (
        "admin.product_ask_description_uk_edit",
        "description_uk",
    ),
    EditProductStates.flavor: ("admin.product_ask_flavor_edit", "flavor"),
    EditProductStates.volume: ("admin.product_ask_volume_edit", "volume"),
    EditProductStates.nicotine_strength: (
        "admin.product_ask_nicotine_edit",
        "nicotine_strength",
    ),
    EditProductStates.price: ("admin.product_ask_price_edit", "price"),
}


def page_from_data(data: dict[str, Any]) -> int:
    return int_from_state(data, "list_page", 0)


async def cancel_edit(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await state.clear()
    await message.answer(
        i18n.t("admin.product_edit_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )


async def send_product_view(
    message: Message,
    i18n: LocalizationService,
    product: Product,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    text = format_admin_product_card(product, i18n)
    markup = product_manage_keyboard(i18n, product, page=page)
    if edit:
        try:
            if message.photo:
                await message.edit_caption(caption=text[:1024], reply_markup=markup)
            else:
                await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit product view message", exc_info=True)

    file_id = product.image_file_id
    if file_id:
        try:
            await message.answer_photo(
                photo=file_id,
                caption=text[:1024],
                reply_markup=markup,
            )
            if len(text) > 1024:
                await message.answer(text)
            return
        except Exception:
            logger.debug("Could not send product photo", exc_info=True)
    await message.answer(text, reply_markup=markup)


async def show_products_list(
    message: Message,
    i18n: LocalizationService,
    session: AsyncSession,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    admin = AdminService(session)
    requested_page = page
    total, products = await admin.page_products(
        offset=requested_page * PRODUCTS_PAGE_SIZE,
        limit=PRODUCTS_PAGE_SIZE,
    )
    if total == 0:
        await edit_or_answer(
            message,
            i18n.t("admin.products_empty"),
            reply_markup=products_actions_keyboard(i18n),
            edit=edit,
        )
        return

    page = clamp_page(requested_page, total, PRODUCTS_PAGE_SIZE)
    if page != requested_page:
        products = await admin.list_products(
            offset=page * PRODUCTS_PAGE_SIZE,
            limit=PRODUCTS_PAGE_SIZE,
        )
    text = i18n.t(
        "admin.products_list_title",
        page=page + 1,
        pages=page_count(total, PRODUCTS_PAGE_SIZE),
        total=total,
    )
    markup = products_list_keyboard(i18n, products, page=page, total=total)
    await edit_or_answer(message, text, reply_markup=markup, edit=edit)


def product_snapshot(product: Product) -> dict[str, Any]:
    category_name = product.category.name if product.category is not None else ""
    return {
        "product_id": product.id,
        "category_id": product.category_id,
        "category_name": category_name,
        "name_ru": product.name_ru,
        "name_en": product.name_en,
        "name_de": product.name_de,
        "description_ru": product.description_ru,
        "description_en": product.description_en,
        "description_de": product.description_de,
        "name_uk": product.name_uk,
        "description_uk": product.description_uk,
        "subcategory_id": product.subcategory_id,
        "subcategory_name": (
            product.subcategory.name_ru if product.subcategory is not None else "—"
        ),
        "flavor": product.flavor,
        "volume": product.volume,
        "nicotine_strength": product.nicotine_strength,
        "price": str(product.price),
        "image_file_id": product.image_file_id,
        "is_active": product.is_active,
    }


def build_edit_preview(i18n: LocalizationService, data: dict[str, Any]) -> str:
    return i18n.t(
        "admin.product_preview_edit",
        product_id=data["product_id"],
        name_ru=e(data["name_ru"]),
        name_en=e(data["name_en"]),
        name_de=e(data["name_de"]),
        description_ru=e(data["description_ru"]),
        description_en=e(data["description_en"]),
        name_uk=e(data["name_uk"]),
        description_uk=e(data["description_uk"]),
        subcategory=e(data.get("subcategory_name", "—")),
        description_de=e(data["description_de"]),
        category=e(data.get("category_name", data["category_id"])),
        flavor=e(data["flavor"]),
        volume=e(data["volume"]),
        nicotine=e(data["nicotine_strength"]),
        price=e(data["price"]),
    )


async def prompt_next_edit_step(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    next_state: Any,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    if next_state == EditProductStates.category:
        await ask_edit_category(message, i18n, state, session)
        return

    prompt_meta = EDIT_NEXT_PROMPTS.get(next_state)
    if prompt_meta is None:
        return
    prompt_key, current_key = prompt_meta
    await message.answer(
        i18n.t(prompt_key, current=data.get(current_key, "")),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


async def ask_edit_category(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    categories = await AdminService(session).list_categories()
    data = await state.get_data()
    if not categories:
        await state.clear()
        await message.answer(
            i18n.t("admin.product_no_categories"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return

    await state.set_state(EditProductStates.category)
    await message.answer(
        i18n.t(
            "admin.product_ask_category_edit",
            current=data.get("category_name", ""),
        ),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )
    await message.answer(
        i18n.t("admin.product_pick_category"),
        reply_markup=admin_category_pick_keyboard(
            categories,
            prefix=CALLBACK_PRODUCT_EDIT_CAT_PREFIX,
        ),
    )


async def show_edit_confirmation(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.set_state(EditProductStates.confirmation)
    preview = build_edit_preview(i18n, data)
    markup = admin_product_confirm_keyboard(
        i18n,
        confirm_callback=CALLBACK_PRODUCT_EDIT_CONFIRM,
    )
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
            logger.debug("Could not send edit preview photo", exc_info=True)
    await message.answer(preview, reply_markup=markup)
