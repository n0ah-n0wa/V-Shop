"""Keyboard builders package."""

from app.keyboards.admin import (
    admin_cancel_keyboard,
    admin_cancel_skip_keyboard,
    admin_menu_keyboard,
)
from app.keyboards.admin_categories import (
    categories_actions_keyboard,
    categories_admin_list_keyboard,
    category_delete_confirm_keyboard,
    category_manage_keyboard,
)
from app.keyboards.admin_products import (
    admin_category_pick_keyboard,
    admin_product_confirm_keyboard,
    products_actions_keyboard,
)
from app.keyboards.cart import cart_keyboard
from app.keyboards.catalog import categories_keyboard, category_view_keyboard
from app.keyboards.checkout import (
    confirmation_keyboard,
    contact_keyboard,
    delivery_keyboard,
)
from app.keyboards.info import info_back_keyboard, info_menu_keyboard
from app.keyboards.inline import city_keyboard, language_keyboard
from app.keyboards.product import add_to_cart_keyboard, product_added_keyboard
from app.keyboards.reply import main_menu_keyboard, remove_keyboard
from app.keyboards.stamp_card import stamp_card_keyboard

__all__ = [
    "add_to_cart_keyboard",
    "admin_cancel_keyboard",
    "admin_cancel_skip_keyboard",
    "admin_category_pick_keyboard",
    "admin_menu_keyboard",
    "admin_product_confirm_keyboard",
    "cart_keyboard",
    "categories_actions_keyboard",
    "categories_admin_list_keyboard",
    "category_delete_confirm_keyboard",
    "category_manage_keyboard",
    "products_actions_keyboard",
    "categories_keyboard",
    "category_view_keyboard",
    "city_keyboard",
    "confirmation_keyboard",
    "contact_keyboard",
    "delivery_keyboard",
    "info_back_keyboard",
    "info_menu_keyboard",
    "language_keyboard",
    "main_menu_keyboard",
    "product_added_keyboard",
    "remove_keyboard",
    "stamp_card_keyboard",
]
