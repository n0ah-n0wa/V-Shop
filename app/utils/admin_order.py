"""Admin order display helpers."""

from __future__ import annotations

from html import escape

from app.models.enums import RewardType
from app.models.order import Order
from app.services.localization import LocalizationService
from app.utils.html import e
from app.utils.labels import city_label, delivery_label, payment_label
from app.utils.order_status import status_label
from app.utils.product_display import localized_product_name
from app.utils.reward_display import order_reward, redeemed_product_name
from app.utils.timefmt import format_timestamp


def format_admin_order_card(order: Order, i18n: LocalizationService) -> str:
    """Localized HTML card for admin order management."""
    city = city_label(i18n, order.city) or order.city
    delivery = delivery_label(i18n, order.delivery_type) or order.delivery_type

    user = order.user
    username = f"@{user.username}" if user is not None and user.username else "—"
    telegram_id = user.telegram_id if user is not None else "—"
    phone = order.phone or i18n.t("checkout.phone_via_telegram")

    item_lines: list[str] = []
    for item in order.items:
        product = item.product
        if product is not None:
            name = localized_product_name(product, i18n.language)
        else:
            name = f"#{item.product_id}"
        line_total = item.price * item.quantity
        item_lines.append(
            i18n.t(
                "admin.order_item",
                name=escape(name),
                quantity=item.quantity,
                price=line_total,
            )
        )
    items_block = "\n".join(item_lines) if item_lines else "—"
    reward_line = _reward_line(order, i18n)
    if reward_line is not None:
        items_block = f"{items_block}\n{reward_line}"

    return i18n.t(
        "admin.order_card",
        payment=e(payment_label(i18n, order.payment_method)),
        order_id=order.id,
        status=status_label(i18n, order.status),
        customer=escape(order.customer_name),
        username=escape(username),
        telegram_id=telegram_id,
        city=escape(city),
        delivery=escape(delivery),
        address=escape(order.address),
        time=escape(order.preferred_time or "—"),
        phone=escape(phone),
        items=items_block,
        total=order.total_price,
        created=format_timestamp(order.created_at),
    )


def _reward_line(order: Order, i18n: LocalizationService) -> str | None:
    """The reward the order redeemed — what it was, and what it took off the total."""
    reward = order_reward(order)
    if reward is None or reward.discount_amount is None:
        return None
    if reward.kind == RewardType.FREE_BOTTLE:
        name = escape(redeemed_product_name(order, reward, i18n.language))
        return i18n.t("admin.order_reward_free_bottle", name=name, amount=reward.discount_amount)
    return i18n.t(
        "admin.order_reward_discount", percent=reward.value, amount=reward.discount_amount
    )
