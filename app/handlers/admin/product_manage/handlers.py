"""Admin product management handlers: list, view, edit, enable/disable, delete."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.filters.localized_text import LocalizedText
from app.handlers.admin.product_manage.common import (
    EDIT_TEXT_STEPS,
    cancel_edit,
    page_from_data,
    product_snapshot,
    prompt_next_edit_step,
    send_product_view,
    show_edit_confirmation,
)
from app.handlers.admin.product_manage.common import (
    show_products_list as render_products_list,
)
from app.keyboards.admin import (
    admin_cancel_keyboard,
    admin_cancel_skip_keyboard,
    admin_menu_keyboard,
)
from app.keyboards.admin_products import (
    CALLBACK_PRODUCT_ACTIONS,
    CALLBACK_PRODUCT_CANCEL,
    CALLBACK_PRODUCT_DELETE_OK_PREFIX,
    CALLBACK_PRODUCT_DELETE_PREFIX,
    CALLBACK_PRODUCT_DESC_PREFIX,
    CALLBACK_PRODUCT_DISABLE_PREFIX,
    CALLBACK_PRODUCT_EDIT_CAT_PREFIX,
    CALLBACK_PRODUCT_EDIT_CONFIRM,
    CALLBACK_PRODUCT_EDIT_PREFIX,
    CALLBACK_PRODUCT_EDIT_SUB_PREFIX,
    CALLBACK_PRODUCT_ENABLE_PREFIX,
    CALLBACK_PRODUCT_LIST,
    CALLBACK_PRODUCT_LIST_PREFIX,
    CALLBACK_PRODUCT_PRICE_PREFIX,
    CALLBACK_PRODUCT_VIEW_PREFIX,
    admin_subcategory_pick_keyboard,
    product_delete_confirm_keyboard,
    products_actions_keyboard,
)
from app.models.product import text_limit
from app.services.admin import AdminService, ProductInUseError
from app.services.localization import LocalizationService
from app.states.admin import (
    EditDescriptionStates,
    EditPriceStates,
    EditProductStates,
)
from app.utils.confirm import confirm_once
from app.utils.html import e
from app.utils.product_display import localized_category_name
from app.utils.telegram_ui import as_message, clear_inline_markup, edit_or_answer
from app.utils.validators import (
    nonempty,
    parse_callback_id,
    parse_nonnegative_int,
    parse_positive_int,
    parse_price,
)

# (field, next state, prompt key, current-value key) for one description step.
DescriptionStep = tuple[str | None, Any | None, str | None, str | None]

logger = logging.getLogger(__name__)

router = Router(name="admin_product_manage")

# ---------------------------------------------------------------------------
# List / actions hub
# ---------------------------------------------------------------------------


@router.callback_query(F.data == CALLBACK_PRODUCT_ACTIONS)
async def show_product_actions(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    message = as_message(callback)
    if message is None:
        return
    await edit_or_answer(
        message,
        i18n.t("admin.products_actions"),
        reply_markup=products_actions_keyboard(i18n),
        edit=True,
    )


@router.callback_query(F.data == CALLBACK_PRODUCT_LIST)
@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_LIST_PREFIX))
async def on_products_list(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    page = 0
    if callback.data.startswith(CALLBACK_PRODUCT_LIST_PREFIX):
        parsed = parse_nonnegative_int(callback.data.removeprefix(CALLBACK_PRODUCT_LIST_PREFIX))
        if parsed is None:
            await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
            return
        page = parsed

    await state.set_data({"list_page": page})
    await render_products_list(
        message,
        i18n,
        session,
        page=page,
        edit=True,
    )


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_VIEW_PREFIX))
async def view_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    page = page_from_data(await state.get_data())

    product_id = parse_positive_int(callback.data.removeprefix(CALLBACK_PRODUCT_VIEW_PREFIX))
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    product = await AdminService(session).get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    await state.set_data({"list_page": page})
    await send_product_view(message, i18n, product, page=page)


# ---------------------------------------------------------------------------
# Enable / Disable / Delete
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_ENABLE_PREFIX))
async def enable_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_ENABLE_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    admin = AdminService(session)
    product = await admin.get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    product = await admin.enable_product(product)
    page = page_from_data(await state.get_data())
    await message.answer(i18n.t("admin.product_enabled", product_id=product.id))
    await send_product_view(message, i18n, product, page=page)


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_DISABLE_PREFIX))
async def disable_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_DISABLE_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    admin = AdminService(session)
    product = await admin.get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    product = await admin.disable_product(product)
    page = page_from_data(await state.get_data())
    await message.answer(i18n.t("admin.product_disabled", product_id=product.id))
    await send_product_view(message, i18n, product, page=page)


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_DELETE_PREFIX))
async def ask_delete_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return
    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_DELETE_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    page = page_from_data(await state.get_data())
    await message.answer(
        i18n.t("admin.product_delete_ask", product_id=product_id),
        reply_markup=product_delete_confirm_keyboard(i18n, product_id, page=page),
    )


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_DELETE_OK_PREFIX))
async def confirm_delete_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_DELETE_OK_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    page = page_from_data(await state.get_data())
    admin = AdminService(session)
    product = await admin.get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    try:
        await admin.delete_product(product)
    except ProductInUseError:
        await message.answer(i18n.t("admin.product_delete_in_use"))
        product = await admin.get_product(product_id)
        if product is not None:
            await send_product_view(message, i18n, product, page=page)
        return

    await state.clear()
    await message.answer(
        i18n.t("admin.product_deleted", product_id=product_id),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await render_products_list(message, i18n, session, page=page)


# ---------------------------------------------------------------------------
# Edit price
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_PRICE_PREFIX))
async def start_edit_price(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_PRICE_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    product = await AdminService(session).get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    page = page_from_data(await state.get_data())
    await state.set_state(EditPriceStates.price)
    await state.update_data(product_id=product.id, list_page=page, price=str(product.price))
    await message.answer(
        i18n.t("admin.product_ask_price_edit", current=product.price),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditPriceStates.price), LocalizedText("common.cancel"))
@router.message(StateFilter(EditDescriptionStates), LocalizedText("common.cancel"))
@router.message(StateFilter(EditProductStates), LocalizedText("common.cancel"))
async def cancel_edit_message(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await cancel_edit(message, i18n, state)


@router.callback_query(
    StateFilter(EditPriceStates, EditDescriptionStates, EditProductStates),
    F.data == CALLBACK_PRODUCT_CANCEL,
)
async def cancel_edit_callback(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None:
        return
    await cancel_edit(message, i18n, state)


@router.message(StateFilter(EditPriceStates.price), LocalizedText("common.skip"))
async def skip_edit_price(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    await state.clear()
    product = await AdminService(session).get_product(int(data["product_id"]))
    if product is None:
        await message.answer(
            i18n.t("admin.product_not_found"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return
    await message.answer(
        i18n.t("admin.product_edit_cancelled"),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await send_product_view(
        message,
        i18n,
        product,
        page=page_from_data(data),
    )


@router.message(StateFilter(EditPriceStates.price), F.text)
async def process_edit_price(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    price = parse_price(message.text or "")
    if price is None:
        await message.answer(
            i18n.t("admin.product_price_invalid"),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return

    data = await state.get_data()
    admin = AdminService(session)
    product = await admin.get_product(int(data["product_id"]))
    if product is None:
        await state.clear()
        await message.answer(
            i18n.t("admin.product_not_found"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return

    product = await admin.set_product_price(product, price)
    page = page_from_data(data)
    await state.clear()
    await message.answer(
        i18n.t("admin.product_price_updated", product_id=product.id, price=product.price),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await send_product_view(message, i18n, product, page=page)


# ---------------------------------------------------------------------------
# Edit description
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_DESC_PREFIX))
async def start_edit_description(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_DESC_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    product = await AdminService(session).get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    page = page_from_data(await state.get_data())
    await state.set_state(EditDescriptionStates.description_ru)
    await state.update_data(
        product_id=product.id,
        list_page=page,
        description_ru=product.description_ru,
        description_en=product.description_en,
        description_de=product.description_de,
    )
    await message.answer(
        i18n.t(
            "admin.product_ask_description_ru_edit",
            current=product.description_ru,
        ),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


# Description edit steps: current_state -> (field, next_state, next_prompt, next_current_key)
_DESC_FLOW: dict[Any, tuple[str | None, Any | None, str | None, str | None]] = {
    EditDescriptionStates.description_ru: (
        "description_ru",
        EditDescriptionStates.description_en,
        "admin.product_ask_description_en_edit",
        "description_en",
    ),
    EditDescriptionStates.description_en: (
        "description_en",
        EditDescriptionStates.description_de,
        "admin.product_ask_description_de_edit",
        "description_de",
    ),
    EditDescriptionStates.description_de: (
        "description_de",
        None,
        None,
        None,
    ),
}


async def _advance_description(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
    *,
    field: str | None,
    value: str | None,
    next_state: Any | None,
    next_prompt: str | None,
    next_current_key: str | None,
) -> None:
    if field is not None and value is not None:
        update: dict[str, Any] = {field: value}
        await state.update_data(**update)

    if next_state is not None and next_prompt and next_current_key:
        data = await state.get_data()
        await state.set_state(next_state)
        await message.answer(
            i18n.t(next_prompt, current=data.get(next_current_key, "")),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return

    data = await state.get_data()
    admin = AdminService(session)
    product = await admin.get_product(int(data["product_id"]))
    if product is None:
        await state.clear()
        await message.answer(
            i18n.t("admin.product_not_found"),
            reply_markup=admin_menu_keyboard(i18n),
        )
        return

    product = await admin.set_product_descriptions(
        product,
        description_ru=data["description_ru"],
        description_en=data["description_en"],
        description_de=data["description_de"],
    )
    page = page_from_data(data)
    await state.clear()
    await message.answer(
        i18n.t("admin.product_description_updated", product_id=product.id),
        reply_markup=admin_menu_keyboard(i18n),
    )
    await send_product_view(message, i18n, product, page=page)


async def _desc_flow_for_state(
    current: str | None,
) -> tuple[Any, DescriptionStep] | None:
    for state_obj, meta in _DESC_FLOW.items():
        if current == state_obj.state:
            return state_obj, meta
    return None


@router.message(StateFilter(*_DESC_FLOW.keys()), LocalizedText("common.skip"))
async def skip_description_step(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    matched = await _desc_flow_for_state(await state.get_state())
    if matched is None:
        return
    _state_obj, (_field, next_state, next_prompt, next_current_key) = matched
    await _advance_description(
        message,
        i18n,
        state,
        session,
        field=None,
        value=None,
        next_state=next_state,
        next_prompt=next_prompt,
        next_current_key=next_current_key,
    )


@router.message(StateFilter(*_DESC_FLOW.keys()), F.text)
async def process_description_step(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    matched = await _desc_flow_for_state(await state.get_state())
    if matched is None:
        return
    _state_obj, (field, next_state, next_prompt, next_current_key) = matched
    value = nonempty(message.text)
    if value is None:
        await message.answer(
            i18n.t("admin.product_text_invalid"),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return
    await _advance_description(
        message,
        i18n,
        state,
        session,
        field=field,
        value=value,
        next_state=next_state,
        next_prompt=next_prompt,
        next_current_key=next_current_key,
    )


# ---------------------------------------------------------------------------
# Edit product (full)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith(CALLBACK_PRODUCT_EDIT_PREFIX))
async def start_edit_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    product_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_EDIT_PREFIX)
    if product_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    product = await AdminService(session).get_product(product_id)
    if product is None:
        await message.answer(i18n.t("admin.product_not_found"))
        return

    page = page_from_data(await state.get_data())
    snapshot = product_snapshot(product)
    snapshot["list_page"] = page
    await state.set_state(EditProductStates.photo)
    await state.set_data(snapshot)

    has_photo = (
        i18n.t("admin.product_photo_yes")
        if product.image_file_id
        else i18n.t("admin.product_photo_no")
    )
    await message.answer(
        i18n.t("admin.product_ask_photo_edit", has_photo=has_photo),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditProductStates.photo), F.photo)
async def edit_photo(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    if not message.photo:
        return
    await state.update_data(image_file_id=message.photo[-1].file_id)
    data = await state.get_data()
    await state.set_state(EditProductStates.name_ru)
    await message.answer(
        i18n.t("admin.product_ask_name_ru_edit", current=data["name_ru"]),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditProductStates.photo), LocalizedText("common.skip"))
async def skip_edit_photo(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.set_state(EditProductStates.name_ru)
    await message.answer(
        i18n.t("admin.product_ask_name_ru_edit", current=data["name_ru"]),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditProductStates.photo))
async def edit_photo_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.product_photo_invalid"),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(*EDIT_TEXT_STEPS.keys()), LocalizedText("common.skip"))
async def skip_edit_text_step(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    current = await state.get_state()
    next_state = None
    for state_obj, meta in EDIT_TEXT_STEPS.items():
        if current == state_obj.state:
            next_state = meta[1]
            break
    if next_state is None:
        return
    await state.set_state(next_state)
    await prompt_next_edit_step(message, i18n, state, next_state, session)


@router.message(StateFilter(*EDIT_TEXT_STEPS.keys()), F.text)
async def process_edit_text_step(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    current = await state.get_state()
    matched: tuple[str, Any, str, str] | None = None
    for state_obj, meta in EDIT_TEXT_STEPS.items():
        if current == state_obj.state:
            matched = meta
            break
    if matched is None:
        return

    data_key, next_state, _prompt_key, _current_key = matched
    value = nonempty(message.text)
    if value is None:
        await message.answer(
            i18n.t("admin.product_text_invalid"),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return
    limit = text_limit(data_key)
    if limit is not None and len(value) > limit:
        await message.answer(
            i18n.t("admin.product_text_too_long", limit=limit),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return

    step_update: dict[str, Any] = {data_key: value}
    await state.update_data(**step_update)
    await state.set_state(next_state)
    await prompt_next_edit_step(message, i18n, state, next_state, session)


@router.message(StateFilter(EditProductStates.category), LocalizedText("common.skip"))
async def skip_edit_category(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.set_state(EditProductStates.flavor)
    await message.answer(
        i18n.t("admin.product_ask_flavor_edit", current=data["flavor"]),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.callback_query(
    StateFilter(EditProductStates.category),
    F.data.startswith(CALLBACK_PRODUCT_EDIT_CAT_PREFIX),
)
async def process_edit_category(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    if callback.message is None or callback.data is None:
        return

    category_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_EDIT_CAT_PREFIX)
    if category_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return
    category = await AdminService(session).get_category(category_id)
    if category is None:
        await callback.message.answer(i18n.t("admin.product_category_invalid"))
        return

    await state.update_data(
        category_id=category.id,
        category_name=localized_category_name(category, i18n.language),
    )
    message = as_message(callback)
    if message is None:
        return
    await clear_inline_markup(message)

    # A product's brand must belong to its category, so a new category means a
    # new brand. Mirrors the create wizard rather than silently keeping a brand
    # that now points somewhere else.
    await ask_edit_subcategory(message, i18n, state, session, category.id)


async def ask_edit_subcategory(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
    category_id: int,
) -> None:
    """Ask which brand of the chosen category the product belongs to."""
    admin = AdminService(session)
    subcategories = await admin.list_subcategories(category_id)
    data = await state.get_data()

    if not subcategories:
        # A category with no brands cannot hold a product in the hierarchy, so
        # keep the wizard moving and leave the brand unset rather than dead-end.
        await state.update_data(subcategory_id=None)
        await state.set_state(EditProductStates.flavor)
        await message.answer(
            i18n.t(
                "admin.product_no_subcategories",
                category=e(data.get("category_name", "")),
            )
        )
        await message.answer(
            i18n.t("admin.product_ask_flavor_edit", current=data["flavor"]),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return

    await state.set_state(EditProductStates.subcategory)
    await message.answer(
        i18n.t(
            "admin.product_pick_subcategory",
            category=e(data.get("category_name", "")),
        ),
        reply_markup=admin_subcategory_pick_keyboard(
            subcategories, prefix=CALLBACK_PRODUCT_EDIT_SUB_PREFIX
        ),
    )


@router.callback_query(
    StateFilter(EditProductStates.subcategory),
    F.data.startswith(CALLBACK_PRODUCT_EDIT_SUB_PREFIX),
)
async def process_edit_subcategory(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.data is None:
        return

    subcategory_id = parse_callback_id(callback.data, CALLBACK_PRODUCT_EDIT_SUB_PREFIX)
    if subcategory_id is None:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    subcategory = await AdminService(session).get_subcategory(subcategory_id)
    if subcategory is None:
        await message.answer(i18n.t("admin.product_subcategory_invalid"))
        return

    data = await state.get_data()
    category_id = data.get("category_id")
    if category_id is None or subcategory.category_id != int(category_id):
        # Guard: a stale keyboard must never attach a brand to the wrong category.
        await message.answer(i18n.t("admin.product_subcategory_mismatch"))
        return

    await state.update_data(subcategory_id=subcategory.id)
    await state.set_state(EditProductStates.flavor)
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear edit brand keyboard", exc_info=True)
    await message.answer(
        i18n.t("admin.product_ask_flavor_edit", current=data["flavor"]),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditProductStates.subcategory))
async def edit_subcategory_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    # Same key the create wizard uses for the same situation, rather than a new
    # near-duplicate string in four catalogs.
    await message.answer(i18n.t("admin.product_subcategory_invalid"))


@router.message(StateFilter(EditProductStates.price), LocalizedText("common.skip"))
async def skip_edit_full_price(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    await message.answer(
        i18n.t("admin.product_confirm_intro"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
    await show_edit_confirmation(message, i18n, state)


@router.message(StateFilter(EditProductStates.price), F.text)
async def process_edit_full_price(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
) -> None:
    price = parse_price(message.text or "")
    if price is None:
        await message.answer(
            i18n.t("admin.product_price_invalid"),
            reply_markup=admin_cancel_skip_keyboard(i18n),
        )
        return
    await state.update_data(price=str(price))
    await message.answer(
        i18n.t("admin.product_confirm_intro"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
    await show_edit_confirmation(message, i18n, state)


@router.callback_query(
    StateFilter(EditProductStates.confirmation),
    F.data == CALLBACK_PRODUCT_EDIT_CONFIRM,
)
async def confirm_edit_product(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is None or callback.from_user is None:
        return

    async with confirm_once(state, lock_key=f"product_edit:{callback.from_user.id}") as data:
        if data is None:
            return

        admin = AdminService(session)
        product = await admin.get_product(int(data["product_id"]))
        if product is None:
            await state.clear()
            await message.answer(
                i18n.t("admin.product_not_found"),
                reply_markup=admin_menu_keyboard(i18n),
            )
            return

        # `subcategory_id` travels with `category_id`: the wizard re-asks for a
        # brand whenever the category changes, and leaving the old one behind
        # would give the product two parents that disagree.
        fields: dict[str, Any] = {
            "category_id": int(data["category_id"]),
            "name_ru": data["name_ru"],
            "name_en": data["name_en"],
            "name_de": data["name_de"],
            "name_uk": data["name_uk"],
            "description_ru": data["description_ru"],
            "description_en": data["description_en"],
            "description_de": data["description_de"],
            "description_uk": data["description_uk"],
            "flavor": data["flavor"],
            "volume": data["volume"],
            "nicotine_strength": data["nicotine_strength"],
            "price": data["price"],
            "image_file_id": data.get("image_file_id"),
        }
        if data.get("subcategory_id") is not None:
            fields["subcategory_id"] = int(data["subcategory_id"])
        product = await admin.update_product(product, **fields)
        product = await admin.get_product(product.id) or product
        page = page_from_data(data)
        await state.clear()

        await clear_inline_markup(message)
        await message.answer(
            i18n.t("admin.product_updated", product_id=product.id),
            reply_markup=admin_menu_keyboard(i18n),
        )
        await send_product_view(message, i18n, product, page=page)


@router.message(StateFilter(EditPriceStates.price))
async def edit_price_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.product_price_invalid"),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditDescriptionStates))
async def edit_description_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.product_text_invalid"),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )


@router.message(StateFilter(EditProductStates.confirmation))
async def edit_confirmation_waiting(message: Message, i18n: LocalizationService) -> None:
    await message.answer(i18n.t("admin.product_confirm_waiting"))


@router.message(StateFilter(EditProductStates.category))
async def edit_category_invalid_message(message: Message, i18n: LocalizationService) -> None:
    await message.answer(i18n.t("admin.product_category_invalid"))


@router.message(StateFilter(EditProductStates))
async def edit_product_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.product_text_invalid"),
        reply_markup=admin_cancel_skip_keyboard(i18n),
    )
