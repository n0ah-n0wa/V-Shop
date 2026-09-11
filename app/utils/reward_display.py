"""Render a reward on an order: offered at checkout, and recorded on the order.

Every figure comes from the backend — a :class:`~app.services.reward.RewardOption`
(what a reward would do to the cart, decided by the planning that places the
order) or the order's used reward row (what it did). Amounts print the way the
checkout prints them. Nothing here decides what a reward is worth.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import inspect

from app.models.order import Order
from app.models.reward import UserReward
from app.services.localization import LocalizationService
from app.services.reward import FreeBottlePlan, RewardOption
from app.utils.html import e
from app.utils.product_display import localized_product_name

UNKNOWN_PRODUCT = "—"


def reward_option_label(
    option: RewardOption, i18n: LocalizationService, names: Mapping[int, str]
) -> str:
    """A reward's button at checkout: what it is, and what it takes off this cart."""
    plan = option.plan
    if isinstance(plan, FreeBottlePlan):
        name = names.get(plan.product_id, UNKNOWN_PRODUCT)
        return i18n.t("checkout.reward_option_free_bottle", name=name, amount=plan.discount)
    return i18n.t("checkout.reward_option_discount", percent=plan.percent, amount=plan.discount)


def reward_summary_line(
    option: RewardOption, i18n: LocalizationService, names: Mapping[int, str]
) -> str:
    """The reward's line in the order summary, between the subtotal and the total."""
    plan = option.plan
    if isinstance(plan, FreeBottlePlan):
        name = e(names.get(plan.product_id, UNKNOWN_PRODUCT))
        return i18n.t("checkout.summary_reward_free_bottle", name=name, amount=plan.discount)
    return i18n.t("checkout.summary_reward_discount", percent=plan.percent, amount=plan.discount)


def order_reward(order: Order) -> UserReward | None:
    """
    The reward redeemed on ``order``, as loaded with it — never a query.

    An order read from the database carries it (``Order.reward`` loads with the
    order); one only built in memory has none to show.
    """
    if "reward" in inspect(order).unloaded:
        return None
    return order.reward


def redeemed_product_name(order: Order, reward: UserReward, language: str) -> str:
    """The product a free bottle made free, named from the order's own lines."""
    for item in order.items:
        if item.product_id == reward.redeemed_product_id and item.product is not None:
            return localized_product_name(item.product, language)
    return f"#{reward.redeemed_product_id}"
