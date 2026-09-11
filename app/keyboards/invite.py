"""👥 Invite a Friend inline keyboards."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.localization import LocalizationService

# The screen's only callback: it closes the screen. The link travels in a URL
# button and a copy button, both handled by the Telegram client — nothing a
# customer taps here comes back to the bot carrying a code or an id.
CALLBACK_INVITE_CLOSE = "invite:close"
# Telegram's own share sheet: the chat picker, with the link and a message.
TELEGRAM_SHARE_URL = "https://t.me/share/url"


def share_url(link: str, text: str) -> str:
    return f"{TELEGRAM_SHARE_URL}?url={quote(link, safe='')}&text={quote(text, safe='')}"


def _send_button(i18n: LocalizationService, link: str, share_text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=i18n.t("invite.share"), url=share_url(link, share_text))


def invite_keyboard(
    i18n: LocalizationService, *, link: str, share_text: str
) -> InlineKeyboardMarkup:
    """📤 Send to a friend first — the main action — then 📋 Copy link, then ⬅️ Back."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_send_button(i18n, link, share_text)],
            [InlineKeyboardButton(text=i18n.t("invite.copy"), copy_text=CopyTextButton(text=link))],
            [InlineKeyboardButton(text=i18n.t("common.back"), callback_data=CALLBACK_INVITE_CLOSE)],
        ]
    )


def share_keyboard(
    i18n: LocalizationService, *, link: str, share_text: str
) -> InlineKeyboardMarkup:
    """Just 📤 Send to a friend: news about a friend offers the next invite in one tap."""
    return InlineKeyboardMarkup(inline_keyboard=[[_send_button(i18n, link, share_text)]])
