"""Checkout inline / reply keyboards."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.models.enums import CityChoice, DeliveryType, PaymentMethod
from app.services.localization import LocalizationService

CALLBACK_DELIVERY_PREFIX = "checkout:delivery:"
CALLBACK_PAYMENT_PREFIX = "checkout:pay:"
CALLBACK_CONFIRM = "checkout:confirm"
CALLBACK_CANCEL = "checkout:cancel"
# checkout:reward:{reward_id} — only the id travels: the server re-checks that it
# is one of this customer's rewards and what it is worth on this cart.
CALLBACK_REWARD_PREFIX = "checkout:reward:"
CALLBACK_REWARD_NONE = f"{CALLBACK_REWARD_PREFIX}none"


def delivery_keyboard(i18n: LocalizationService, city: CityChoice | str) -> InlineKeyboardMarkup:
    city_value = city.value if isinstance(city, CityChoice) else city
    if city_value == CityChoice.BERLIN.value:
        options = (
            (DeliveryType.PICKUP, "checkout.delivery_pickup"),
            (DeliveryType.COURIER, "checkout.delivery_courier"),
        )
    else:
        options = (
            (DeliveryType.POSTAL, "checkout.delivery_postal"),
            (DeliveryType.SERVICE, "checkout.delivery_service"),
        )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=i18n.t(label_key),
                callback_data=f"{CALLBACK_DELIVERY_PREFIX}{delivery.value}",
            )
        ]
        for delivery, label_key in options
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=i18n.t("checkout.cancel"),
                callback_data=CALLBACK_CANCEL,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkout_cancel_keyboard(i18n: LocalizationService) -> ReplyKeyboardMarkup:
    """Reply keyboard with a single Cancel action for text checkout steps."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=i18n.t("checkout.cancel"))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def contact_keyboard(i18n: LocalizationService) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=i18n.t("checkout.share_phone"),
                    request_contact=True,
                )
            ],
            [KeyboardButton(text=i18n.t("checkout.use_telegram"))],
            [KeyboardButton(text=i18n.t("checkout.cancel"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirmation_keyboard(i18n: LocalizationService) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n.t("checkout.confirm"),
                    callback_data=CALLBACK_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    text=i18n.t("checkout.cancel"),
                    callback_data=CALLBACK_CANCEL,
                )
            ],
        ]
    )


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Alias for ``remove_keyboard`` (checkout-local import compatibility)."""
    from app.keyboards.reply import remove_keyboard

    return remove_keyboard()


def reward_keyboard(
    i18n: LocalizationService, options: Sequence[tuple[int, str]]
) -> InlineKeyboardMarkup:
    """One button per usable reward — ``(reward_id, label)`` — then none, then cancel."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{CALLBACK_REWARD_PREFIX}{reward_id}")]
        for reward_id, label in options
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=i18n.t("checkout.reward_skip"), callback_data=CALLBACK_REWARD_NONE
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text=i18n.t("checkout.cancel"), callback_data=CALLBACK_CANCEL)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(i18n: LocalizationService) -> InlineKeyboardMarkup:
    """Preferred payment method: cash or card transfer."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n.t("checkout.payment_cash"),
                    callback_data=f"{CALLBACK_PAYMENT_PREFIX}{PaymentMethod.CASH.value}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=i18n.t("checkout.payment_card"),
                    callback_data=f"{CALLBACK_PAYMENT_PREFIX}{PaymentMethod.CARD.value}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=i18n.t("checkout.cancel"),
                    callback_data=CALLBACK_CANCEL,
                )
            ],
        ]
    )
