"""Checkout conversation (FSM): name → delivery → address → time → phone → payment →
reward (only for customers holding one that fits the cart) → confirm.

Every amount comes from :class:`~app.services.order.OrderService`: what a reward
would take off (``reward_options`` / ``quote``) is decided by the same planning
that places the order, which re-checks the chosen reward under lock. The
reward step's callback carries only a reward id."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.keyboards.checkout import (
    CALLBACK_CANCEL,
    CALLBACK_CONFIRM,
    CALLBACK_DELIVERY_PREFIX,
    CALLBACK_PAYMENT_PREFIX,
    CALLBACK_REWARD_NONE,
    CALLBACK_REWARD_PREFIX,
    checkout_cancel_keyboard,
    confirmation_keyboard,
    contact_keyboard,
    delivery_keyboard,
    payment_keyboard,
    reward_keyboard,
)
from app.keyboards.reply import main_menu_keyboard, remove_keyboard
from app.models.enums import CityChoice, DeliveryType, PaymentMethod
from app.models.user import User
from app.services.cart import CartService, CartView
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyError
from app.services.notification import OrderNotificationService
from app.services.order import (
    CheckoutQuote,
    EmptyCartError,
    InactiveProductError,
    InvalidDeliveryError,
    OrderService,
    delivery_allowed_for_city,
)
from app.services.user import UserService
from app.states.checkout import CheckoutStates
from app.utils.concurrency import keyed_lock
from app.utils.html import e
from app.utils.labels import city_label, delivery_label, payment_label
from app.utils.reward_display import reward_option_label, reward_summary_line
from app.utils.telegram_ui import as_message, clear_inline_markup
from app.utils.validators import nonempty, normalize_phone, parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="user_checkout")

_CHECKOUT_STATES = (
    CheckoutStates.customer_name,
    CheckoutStates.delivery_type,
    CheckoutStates.address,
    CheckoutStates.preferred_time,
    CheckoutStates.contact,
    CheckoutStates.payment_method,
    CheckoutStates.reward,
    CheckoutStates.confirmation,
)


def _phone_label(i18n: LocalizationService, phone: str | None) -> str:
    if phone:
        return phone
    return i18n.t("checkout.phone_via_telegram")


def build_checkout_summary(
    i18n: LocalizationService,
    *,
    data: dict[str, Any],
    user: User,
    view: CartView,
    quote: CheckoutQuote | None = None,
) -> str:
    """
    The order summary; with a ``quote`` carrying a reward, what it takes off —
    as the order service computed it, never here.
    """
    lines = [
        i18n.t("checkout.summary_title"),
        "",
        i18n.t("checkout.summary_name", name=e(data["customer_name"])),
        i18n.t("checkout.summary_city", city=e(city_label(i18n, user.selected_city))),
        i18n.t(
            "checkout.summary_delivery",
            delivery=e(delivery_label(i18n, data["delivery_type"])),
        ),
        i18n.t("checkout.summary_address", address=e(data["address"])),
        i18n.t("checkout.summary_time", time=e(data["preferred_time"])),
        i18n.t(
            "checkout.summary_phone",
            phone=e(_phone_label(i18n, data.get("phone"))),
        ),
        i18n.t(
            "checkout.summary_payment",
            payment=e(payment_label(i18n, data.get("payment_method"))),
        ),
        "",
        i18n.t("checkout.summary_items"),
    ]
    for line in view.lines:
        lines.append(
            i18n.t(
                "checkout.summary_item",
                name=e(line.name),
                quantity=line.quantity,
                price=line.line_total,
            )
        )
    lines.append("")
    if quote is not None and quote.reward is not None:
        names = {line.product_id: line.name for line in view.lines}
        lines.append(i18n.t("checkout.summary_subtotal", subtotal=quote.subtotal))
        lines.append(reward_summary_line(quote.reward, i18n, names))
        lines.append(i18n.t("checkout.summary_total", total=quote.total))
    else:
        lines.append(i18n.t("checkout.summary_total", total=view.total))
    return "\n".join(lines)


async def _abort_checkout(
    *,
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
    notice_key: str = "checkout.cancelled",
) -> None:
    await state.clear()
    await message.answer(
        i18n.t(notice_key),
        reply_markup=main_menu_keyboard(i18n),
    )


async def start_checkout(
    *,
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    i18n: LocalizationService,
) -> None:
    """Begin checkout from an existing message target (cart callback/message)."""
    view = await CartService(session).get_view(user.id, language=i18n.language)
    if view is None or view.is_empty:
        await message.answer(
            i18n.t("checkout.empty_cart"),
            reply_markup=main_menu_keyboard(i18n),
        )
        await state.clear()
        return

    await state.clear()
    await state.set_state(CheckoutStates.customer_name)
    await message.answer(
        i18n.t("checkout.ask_name"),
        reply_markup=checkout_cancel_keyboard(i18n),
    )


@router.callback_query(F.data == "cart:checkout")
async def checkout_from_cart(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or message is None:
        await callback.answer()
        return

    service = UserService(session)
    user = await service.ensure_user(callback.from_user)
    localized = LocalizationService.from_user(user)
    if not UserService.is_onboarded(user):
        await callback.answer(localized.t("common.not_available"), show_alert=True)
        return

    view = await CartService(session).get_view(user.id, language=localized.language)
    if view is None or view.is_empty:
        await callback.answer(localized.t("checkout.empty_cart"), show_alert=True)
        return

    await callback.answer()
    await clear_inline_markup(message)
    await start_checkout(
        message=message,
        state=state,
        session=session,
        user=user,
        i18n=localized,
    )


@router.message(StateFilter(*_CHECKOUT_STATES), LocalizedText("checkout.cancel"))
async def checkout_cancel_text(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
) -> None:
    await _abort_checkout(message=message, state=state, i18n=i18n)


@router.callback_query(StateFilter(*_CHECKOUT_STATES), F.data == CALLBACK_CANCEL)
async def checkout_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or message is None:
        await callback.answer()
        return

    user = await UserService(session).ensure_user(callback.from_user)
    localized = LocalizationService.from_user(user)
    await callback.answer()
    await clear_inline_markup(message)
    await _abort_checkout(message=message, state=state, i18n=localized)


@router.message(StateFilter(CheckoutStates.customer_name), F.text)
async def checkout_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    if message.from_user is None:
        return

    name = nonempty(message.text, min_len=2, max_len=255)
    if name is None:
        await message.answer(
            i18n.t("checkout.invalid_input"),
            reply_markup=checkout_cancel_keyboard(i18n),
        )
        return

    user = await UserService(session).ensure_user(message.from_user)
    localized = LocalizationService.from_user(user)
    city = user.selected_city or CityChoice.DELIVERY

    await state.update_data(customer_name=name)
    await state.set_state(CheckoutStates.delivery_type)
    await message.answer(
        localized.t("checkout.ask_delivery"),
        reply_markup=remove_keyboard(),
    )
    await message.answer(
        localized.t("checkout.use_buttons"),
        reply_markup=delivery_keyboard(localized, city),
    )


@router.message(StateFilter(CheckoutStates.customer_name))
async def checkout_name_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("checkout.invalid_input"),
        reply_markup=checkout_cancel_keyboard(i18n),
    )


@router.callback_query(
    StateFilter(CheckoutStates.delivery_type),
    F.data.startswith(CALLBACK_DELIVERY_PREFIX),
)
async def checkout_delivery(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.data is None or message is None or callback.from_user is None:
        await callback.answer()
        return

    raw = callback.data.removeprefix(CALLBACK_DELIVERY_PREFIX)
    try:
        delivery = DeliveryType(raw)
    except ValueError:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    user = await UserService(session).ensure_user(callback.from_user)
    localized = LocalizationService.from_user(user)
    city = user.selected_city or CityChoice.DELIVERY
    if not delivery_allowed_for_city(city, delivery.value):
        await callback.answer(localized.t("checkout.invalid_delivery"), show_alert=True)
        return

    await state.update_data(delivery_type=delivery.value)
    await state.set_state(CheckoutStates.address)
    await callback.answer()
    await clear_inline_markup(message)
    await message.answer(
        localized.t("checkout.ask_address"),
        reply_markup=checkout_cancel_keyboard(localized),
    )


@router.message(StateFilter(CheckoutStates.delivery_type))
async def checkout_delivery_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(i18n.t("checkout.use_buttons"))


@router.message(StateFilter(CheckoutStates.address), F.text)
async def checkout_address(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
) -> None:
    address = nonempty(message.text, min_len=3, max_len=1000)
    if address is None:
        await message.answer(
            i18n.t("checkout.invalid_input"),
            reply_markup=checkout_cancel_keyboard(i18n),
        )
        return

    await state.update_data(address=address)
    await state.set_state(CheckoutStates.preferred_time)
    await message.answer(
        i18n.t("checkout.ask_time"),
        reply_markup=checkout_cancel_keyboard(i18n),
    )


@router.message(StateFilter(CheckoutStates.address))
async def checkout_address_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("checkout.invalid_input"),
        reply_markup=checkout_cancel_keyboard(i18n),
    )


@router.message(StateFilter(CheckoutStates.preferred_time), F.text)
async def checkout_time(
    message: Message,
    state: FSMContext,
    i18n: LocalizationService,
) -> None:
    preferred_time = nonempty(message.text, min_len=1, max_len=255)
    if preferred_time is None:
        await message.answer(
            i18n.t("checkout.invalid_input"),
            reply_markup=checkout_cancel_keyboard(i18n),
        )
        return

    await state.update_data(preferred_time=preferred_time)
    await state.set_state(CheckoutStates.contact)
    await message.answer(
        i18n.t("checkout.ask_contact"),
        reply_markup=contact_keyboard(i18n),
    )


@router.message(StateFilter(CheckoutStates.preferred_time))
async def checkout_time_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("checkout.invalid_input"),
        reply_markup=checkout_cancel_keyboard(i18n),
    )


@router.message(StateFilter(CheckoutStates.contact), F.contact)
async def checkout_contact_phone(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    if message.from_user is None or message.contact is None:
        return

    contact = message.contact
    if contact.user_id is not None and contact.user_id != message.from_user.id:
        await message.answer(
            i18n.t("checkout.invalid_phone_owner"),
            reply_markup=contact_keyboard(i18n),
        )
        return

    phone = normalize_phone(contact.phone_number)
    if phone is None:
        await message.answer(
            i18n.t("checkout.invalid_phone"),
            reply_markup=contact_keyboard(i18n),
        )
        return

    await state.update_data(phone=phone)
    await _ask_payment(message, state, session, i18n)


@router.message(StateFilter(CheckoutStates.contact), LocalizedText("checkout.use_telegram"))
async def checkout_contact_telegram(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    await state.update_data(phone=None)
    await _ask_payment(message, state, session, i18n)


@router.message(StateFilter(CheckoutStates.contact), F.text)
async def checkout_contact_typed_phone(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    phone = normalize_phone(message.text)
    if phone is None:
        await message.answer(
            i18n.t("checkout.invalid_phone"),
            reply_markup=contact_keyboard(i18n),
        )
        return
    await state.update_data(phone=phone)
    await _ask_payment(message, state, session, i18n)


@router.message(StateFilter(CheckoutStates.contact))
async def checkout_contact_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(
        i18n.t("checkout.invalid_phone"),
        reply_markup=contact_keyboard(i18n),
    )


@router.callback_query(
    StateFilter(CheckoutStates.payment_method),
    F.data.startswith(CALLBACK_PAYMENT_PREFIX),
)
async def checkout_payment(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    message = callback.message if isinstance(callback.message, Message) else None
    if callback.data is None or message is None or callback.from_user is None:
        await callback.answer()
        return

    raw = callback.data.removeprefix(CALLBACK_PAYMENT_PREFIX)
    try:
        payment = PaymentMethod(raw)
    except ValueError:
        await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
        return

    # Resolved from callback.from_user — the customer who tapped — and passed
    # down, because `message` here is the bot's own message.
    user = await UserService(session).ensure_user(callback.from_user)

    await state.update_data(payment_method=payment.value)
    await callback.answer()
    await clear_inline_markup(message)
    await _offer_rewards(message, state, session, user)


@router.message(StateFilter(CheckoutStates.payment_method))
async def checkout_payment_invalid(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(i18n.t("checkout.invalid_payment"))


@router.callback_query(
    StateFilter(CheckoutStates.reward),
    F.data.startswith(CALLBACK_REWARD_PREFIX),
)
async def checkout_reward(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    """The customer picks one reward for this order, or none."""
    message = as_message(callback)
    if callback.data is None or message is None or callback.from_user is None:
        await callback.answer()
        return

    user = await UserService(session).ensure_user(callback.from_user)
    localized = LocalizationService.from_user(user)
    reward_id: int | None = None
    if callback.data != CALLBACK_REWARD_NONE:
        reward_id = parse_positive_int(callback.data.removeprefix(CALLBACK_REWARD_PREFIX))
        # Only a reward offered to this customer for this cart, checked afresh:
        # a stale button or a crafted id is refused, and the list offered again.
        offered = await OrderService(session).reward_options(user)
        if reward_id not in {option.reward_id for option in offered}:
            await callback.answer(localized.t("checkout.reward_unavailable"), show_alert=True)
            await clear_inline_markup(message)
            await _offer_rewards(message, state, session, user)
            return

    await state.update_data(reward_id=reward_id)
    await callback.answer()
    await clear_inline_markup(message)
    await _show_confirmation(message, state, session, user)


@router.message(StateFilter(CheckoutStates.reward))
async def checkout_reward_invalid(message: Message, i18n: LocalizationService) -> None:
    await message.answer(i18n.t("checkout.use_buttons"))


@router.message(StateFilter(CheckoutStates.confirmation))
async def checkout_confirmation_waiting(
    message: Message,
    i18n: LocalizationService,
) -> None:
    await message.answer(i18n.t("checkout.use_buttons"))


async def _ask_payment(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    i18n: LocalizationService,
) -> None:
    """Ask for the preferred payment method before the summary."""
    if message.from_user is None:
        return
    user = await UserService(session).ensure_user(message.from_user)
    localized = LocalizationService.from_user(user)

    await state.set_state(CheckoutStates.payment_method)
    await message.answer(
        localized.t("checkout.ask_payment"),
        reply_markup=remove_keyboard(),
    )
    await message.answer(
        localized.t("checkout.use_buttons"),
        reply_markup=payment_keyboard(localized),
    )


async def _offer_rewards(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
) -> None:
    """
    Offer the customer's rewards that fit this cart — or go straight to the summary.

    Customers without one never see this step. One reward per order; the rest
    stay saved. What each would take off is the order service's figure.
    """
    localized = LocalizationService.from_user(user)
    options = await OrderService(session).reward_options(user)
    if not options:
        await state.update_data(reward_id=None)
        await _show_confirmation(message, state, session, user)
        return

    view = await CartService(session).get_view(user.id, language=localized.language)
    names = {line.product_id: line.name for line in view.lines} if view is not None else {}
    await state.set_state(CheckoutStates.reward)
    await message.answer(
        localized.t("checkout.ask_reward"),
        reply_markup=reward_keyboard(
            localized,
            [
                (option.reward_id, reward_option_label(option, localized, names))
                for option in options
            ],
        ),
    )


async def _show_confirmation(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
) -> None:
    """
    Render the order summary for ``user``.

    The customer is passed in rather than read from ``message.from_user``: the
    callers reach here from callback queries and hand us
    ``callback.message`` — a message the *bot* sent, whose ``from_user`` is the
    bot. Resolving the user from it loaded the bot's (empty) cart and told the
    customer their cart was empty at the last step of checkout.
    """
    localized = LocalizationService.from_user(user)
    data = await state.get_data()
    view = await CartService(session).get_view(user.id, language=localized.language)
    if view is None or view.is_empty:
        await state.clear()
        await message.answer(
            localized.t("checkout.empty_cart"),
            reply_markup=main_menu_keyboard(localized),
        )
        return

    chosen = data.get("reward_id")
    reward_id = chosen if isinstance(chosen, int) else None
    quote = await OrderService(session).quote(user, reward_id=reward_id)
    if reward_id is not None and (quote is None or quote.reward is None):
        # Used meanwhile, or the cart no longer fits it: go on without it.
        await state.update_data(reward_id=None)
        await message.answer(localized.t("checkout.reward_unavailable"))

    await state.set_state(CheckoutStates.confirmation)
    await message.answer(
        build_checkout_summary(localized, data=data, user=user, view=view, quote=quote),
        reply_markup=remove_keyboard(),
    )
    await message.answer(
        localized.t("checkout.confirm_prompt"),
        reply_markup=confirmation_keyboard(localized),
    )


@router.callback_query(StateFilter(CheckoutStates.confirmation), F.data == CALLBACK_CONFIRM)
async def checkout_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    i18n: LocalizationService,
) -> None:
    message = as_message(callback)
    if callback.from_user is None or message is None:
        await callback.answer()
        return

    order = None
    user = None
    localized = i18n

    async with keyed_lock(f"checkout:{callback.from_user.id}"):
        data = await state.get_data()
        # A tap that waited here while another placed the order finds checkout
        # closed: that order is already in, which is no error.
        if data.get("submitted") or await state.get_state() != CheckoutStates.confirmation.state:
            await callback.answer(i18n.t("checkout.already_submitted"), show_alert=True)
            return

        user = await UserService(session).ensure_user(callback.from_user)
        localized = LocalizationService.from_user(user)

        required = (
            "customer_name",
            "delivery_type",
            "address",
            "preferred_time",
            "payment_method",
        )
        if any(not data.get(key) for key in required):
            await state.clear()
            await clear_inline_markup(message)
            await callback.answer(localized.t("error.generic"), show_alert=True)
            await message.answer(
                localized.t("error.generic"),
                reply_markup=main_menu_keyboard(localized),
            )
            return

        try:
            delivery = DeliveryType(str(data["delivery_type"]))
            payment = PaymentMethod(str(data["payment_method"]))
        except ValueError:
            await state.clear()
            await clear_inline_markup(message)
            await callback.answer(localized.t("error.invalid_callback"), show_alert=True)
            await message.answer(
                localized.t("error.generic"),
                reply_markup=main_menu_keyboard(localized),
            )
            return

        phone = data.get("phone")
        if phone is not None:
            phone = normalize_phone(str(phone))
            if phone is None:
                await callback.answer(localized.t("checkout.invalid_phone"), show_alert=True)
                await state.set_state(CheckoutStates.contact)
                await clear_inline_markup(message)
                await message.answer(
                    localized.t("checkout.ask_contact"),
                    reply_markup=contact_keyboard(localized),
                )
                return

        await state.update_data(submitted=True)
        chosen = data.get("reward_id")
        # Read while the user row is loaded: a rollback below expires it, and an
        # async session cannot load it back lazily.
        customer_id = user.id

        try:
            order = await OrderService(session).place_order_from_cart(
                user,
                customer_name=str(data["customer_name"]),
                delivery_type=delivery.value,
                address=str(data["address"]),
                preferred_time=str(data["preferred_time"]),
                phone=phone,
                payment_method=payment,
                reward_id=chosen if isinstance(chosen, int) else None,
            )
        except EmptyCartError:
            # Nothing was written. End the transaction — and the cart and loyalty
            # locks it holds — before the customer is told; the same below.
            await session.rollback()
            await state.clear()
            await clear_inline_markup(message)
            await callback.answer(localized.t("checkout.empty_cart"), show_alert=True)
            await message.answer(
                localized.t("checkout.empty_cart"),
                reply_markup=main_menu_keyboard(localized),
            )
            return
        except InactiveProductError:
            await session.rollback()
            await state.clear()
            await clear_inline_markup(message)
            await callback.answer(localized.t("checkout.inactive_product"), show_alert=True)
            await message.answer(
                localized.t("checkout.inactive_product"),
                reply_markup=main_menu_keyboard(localized),
            )
            return
        except InvalidDeliveryError:
            await session.rollback()
            await state.clear()
            await clear_inline_markup(message)
            await callback.answer(localized.t("checkout.invalid_delivery"), show_alert=True)
            await message.answer(
                localized.t("checkout.invalid_delivery"),
                reply_markup=main_menu_keyboard(localized),
            )
            return
        except LoyaltyError:
            # The chosen reward was used meanwhile or no longer fits the cart.
            # Nothing is kept: show the order again without it, to confirm or cancel.
            await session.rollback()
            await state.update_data(submitted=False, reward_id=None)
            await callback.answer(localized.t("checkout.reward_unavailable"), show_alert=True)
            await clear_inline_markup(message)
            user = await UserService(session).ensure_user(callback.from_user)
            await _show_confirmation(message, state, session, user)
            return
        except Exception:
            await state.update_data(submitted=False)
            await session.rollback()
            logger.exception("Failed to place order for user_id=%s", customer_id)
            await callback.answer(localized.t("error.generic"), show_alert=True)
            await message.answer(
                localized.t("error.generic"),
                reply_markup=main_menu_keyboard(localized),
            )
            return

        # Drop FSM before releasing the lock so a waiter sees a cleared state.
        await state.clear()

    if order is None or user is None:
        return

    await callback.answer()
    await clear_inline_markup(message)
    await message.answer(
        localized.t("checkout.success", order_id=order.id),
        reply_markup=main_menu_keyboard(localized),
    )

    await OrderNotificationService(bot, settings).notify_new_order(
        order,
        user,
        telegram_username=callback.from_user.username,
    )
