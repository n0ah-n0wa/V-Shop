"""Last resort for button taps nothing else handles.

A callback reaches here when its button outlived its purpose: a checkout step
after a restart wiped the conversation state, or a keyboard from a screen that
has since moved on. Telegram keeps a spinner on the button until the callback
is answered, so it is answered — "this button is no longer valid" — and the
stale keyboard taken away. Admin buttons are left alone: a non-admin's tap on
one is dropped silently, as the admin router promises.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.services.localization import LocalizationService
from app.utils.telegram_ui import as_message, clear_inline_markup

ADMIN_CALLBACK_PREFIX = "admin:"

router = Router(name="fallback")


@router.callback_query(~F.data.startswith(ADMIN_CALLBACK_PREFIX))
async def stale_button(callback: CallbackQuery, i18n: LocalizationService) -> None:
    await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
    message = as_message(callback)
    if message is not None:
        await clear_inline_markup(message)
