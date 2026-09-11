"""Reply keyboard builders (localized)."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.services.localization import LocalizationService


def main_menu_keyboard(i18n: LocalizationService) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=i18n.t("menu.catalog")),
                KeyboardButton(text=i18n.t("menu.cart")),
            ],
            [KeyboardButton(text=i18n.t("menu.stamp_card"))],
            [KeyboardButton(text=i18n.t("menu.roulette"))],
            [KeyboardButton(text=i18n.t("menu.invite"))],
            [KeyboardButton(text=i18n.t("menu.info"))],
        ],
        resize_keyboard=True,
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
