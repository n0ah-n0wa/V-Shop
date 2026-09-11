"""Order notification service — alert managers/admins about new orders.

LOCALIZATION POLICY — INTENTIONALLY NOT LOCALIZED.

The field labels in :meth:`OrderNotificationService.format_new_order_message`
are deliberately hardcoded English and must NOT be moved into the locale
catalogs. Reasons:

* The primary destination is ``MANAGER_CHAT_ID`` — a single shared operations
  chat. One message is delivered to every member, so there is no per-recipient
  language to resolve; picking one stable language is the only coherent option.
* Staff parse these alerts at speed. A fixed format is more valuable
  operationally than translation, and avoids the same order appearing in two
  languages depending on which admin reads it.
* Companion helpers ``city_label_en`` / ``delivery_label_en`` in
  ``app.utils.labels`` exist solely to serve this policy.

Everything the CUSTOMER sees is localized and must stay that way — see
``docs/architecture.md`` (Localization policy). If per-admin localization of
the private-chat copies is ever wanted, localize there only and leave the
group message in the operations language.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings
from app.models.enums import RewardType
from app.models.order import Order
from app.models.user import User
from app.utils.labels import city_label_en, delivery_label_en, payment_label_en
from app.utils.product_display import localized_product_name
from app.utils.reward_display import order_reward, redeemed_product_name
from app.utils.timefmt import format_timestamp

logger = logging.getLogger(__name__)


def _reward_line(order: Order) -> str:
    """The reward the order redeemed, so staff charge what the customer was promised."""
    reward = order_reward(order)
    if reward is None or reward.discount_amount is None:
        return ""
    if reward.kind == RewardType.FREE_BOTTLE:
        what = f"free bottle — {escape(redeemed_product_name(order, reward, 'en'))}"
    else:
        what = f"{reward.value}% discount"
    return f"🎁 <b>Reward used:</b> {what} (−<code>{reward.discount_amount}</code>)\n"


class OrderNotificationService:
    """
    Forwards new orders to the configured manager group and/or admin chats.

    Destinations come from environment:
    - ``MANAGER_CHAT_ID`` — group or private admin chat (always notified)
    - ``ADMIN_IDS`` — optional additional private chats (deduplicated)
    """

    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    def notification_chat_ids(self) -> list[int]:
        """Unique chat IDs that should receive order alerts."""
        targets: list[int] = [self.settings.manager_chat_id]
        for admin_id in self.settings.admin_ids:
            if admin_id not in targets:
                targets.append(admin_id)
        return targets

    def format_new_order_message(
        self,
        order: Order,
        user: User,
        *,
        telegram_username: str | None = None,
    ) -> str:
        """
        Build a readable HTML notification for managers.

        Staff-facing and intentionally English — see the module docstring.
        """
        username = (
            f"@{escape(telegram_username)}"
            if telegram_username
            else (f"@{escape(user.username)}" if user.username else "—")
        )
        phone = escape(order.phone) if order.phone else "Telegram account"
        customer = escape(order.customer_name)
        address = escape(order.address)
        preferred_time = escape(order.preferred_time or "—")

        item_lines: list[str] = []
        for item in order.items:
            product = item.product
            if product is not None:
                name = escape(localized_product_name(product, "en"))
            else:
                name = f"Product #{item.product_id}"
            line_total = item.price * item.quantity
            item_lines.append(f"• {name} × <b>{item.quantity}</b> — <code>{line_total}</code>")
        items_block = "\n".join(item_lines) if item_lines else "• —"

        return (
            f"🆕 <b>New order #{order.id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Customer:</b> {customer}\n"
            f"🔗 <b>Telegram:</b> {username}\n"
            f"🆔 <b>User ID:</b> <code>{user.telegram_id}</code>\n"
            f"📍 <b>City:</b> {escape(city_label_en(order.city))}\n"
            f"🚚 <b>Delivery:</b> {escape(delivery_label_en(order.delivery_type))}\n"
            f"🏠 <b>Address:</b> {address}\n"
            f"🕒 <b>Preferred time:</b> {preferred_time}\n"
            f"📞 <b>Phone:</b> {phone}\n"
            f"💳 <b>Payment:</b> {escape(payment_label_en(order.payment_method))}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛍 <b>Ordered products</b>\n"
            f"{items_block}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{_reward_line(order)}"
            f"💰 <b>Total:</b> <code>{order.total_price}</code>\n"
            f"📌 <b>Status:</b> {escape(str(order.status))}\n"
            f"🕐 <i>{escape(format_timestamp(order.created_at, with_seconds=True))}</i>"
        )

    async def notify_new_order(
        self,
        order: Order,
        user: User,
        *,
        telegram_username: str | None = None,
    ) -> dict[int, bool]:
        """
        Send the new-order alert immediately to all configured chats.

        Returns a mapping of chat_id → success.
        """
        text = self.format_new_order_message(
            order,
            user,
            telegram_username=telegram_username,
        )
        chat_ids = self.notification_chat_ids()

        async def _send_one(chat_id: int) -> tuple[int, bool]:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                logger.info(
                    "Order notification sent order_id=%s chat_id=%s",
                    order.id,
                    chat_id,
                )
                return chat_id, True
            except TelegramAPIError:
                logger.exception(
                    "Failed to send order notification order_id=%s chat_id=%s",
                    order.id,
                    chat_id,
                )
                return chat_id, False
            except Exception:
                logger.exception(
                    "Unexpected error sending order notification order_id=%s chat_id=%s",
                    order.id,
                    chat_id,
                )
                return chat_id, False

        pairs = await asyncio.gather(*(_send_one(chat_id) for chat_id in chat_ids))
        return dict(pairs)
