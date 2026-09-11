"""
Deploy verification: can this build still read and display the shop's data?

Read-only. Run it right after a deploy — it answers the question the startup log
can only hint at, by driving the same services the handlers drive
(:class:`CatalogService`, :class:`CartService`, :class:`StatisticsService`) and
the same renderers, then printing what came back as JSON.

That distinction is the point. Two very different incidents look identical to a
user ("everything is gone"):

* rows missing from PostgreSQL          -> a deployment or volume problem;
* rows present but nothing renders      -> an application, query or schema bug.

Usage::

    docker compose run --rm --no-deps bot python -m app.verify_deployment

See docs/deployment.md, "Verifying a deploy".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, case, exists, func, inspect, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.cart import Cart, CartItem
from app.models.category import Category, Subcategory
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    RewardSource,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.models.user import User
from app.services.cart import CartService
from app.services.catalog import CatalogService
from app.services.localization import LocalizationService
from app.services.statistics import StatisticsService
from app.utils.product_display import format_product_card, localized_category_name
from app.utils.statistics_display import format_statistics

LANGUAGE = "en"


async def _has_table(session: AsyncSession, name: str) -> bool:
    return bool(await session.run_sync(lambda sync: inspect(sync.connection()).has_table(name)))


async def loyalty_health(session: AsyncSession) -> dict[str, dict[str, int]]:
    """
    The loyalty tables checked against each other.

    ``integrity`` counts states the services never produce, so every value must
    be 0 — anything else means rows were changed behind their back, and the
    ledger is the record to trust. ``coverage`` counts customers without their
    loyalty rows: welcome spins are granted at ``/start`` and topped up on every
    bot start, so that count is 0 after one; accounts are created on first use.
    """

    async def count(statement: Select[Any]) -> int:
        return int(await session.scalar(statement) or 0)

    per_user = (
        select(
            LoyaltyTransaction.user_id.label("user_id"),
            func.sum(LoyaltyTransaction.amount).label("total"),
            func.sum(
                case((LoyaltyTransaction.kind == LoyaltyTransactionType.PURCHASE, 1), else_=0)
            ).label("purchases"),
        )
        .group_by(LoyaltyTransaction.user_id)
        .subquery()
    )
    accounts = (
        select(func.count())
        .select_from(LoyaltyAccount)
        .outerjoin(per_user, per_user.c.user_id == LoyaltyAccount.user_id)
    )
    running = select(
        LoyaltyTransaction.balance_after.label("balance_after"),
        func.sum(LoyaltyTransaction.amount)
        .over(partition_by=LoyaltyTransaction.user_id, order_by=LoyaltyTransaction.id)
        .label("running"),
    ).subquery()
    spun = exists().where(RouletteSpin.grant_id == RouletteSpinGrant.id)
    # What each spin won must exist exactly as won: stamps booked at the prize's
    # value, or a reward of the prize's kind and value pointing back at it.
    stamp_prize = RouletteSpin.prize_type == RoulettePrizeType.STAMPS
    stamps_booked = exists().where(
        LoyaltyTransaction.spin_id == RouletteSpin.id,
        LoyaltyTransaction.amount == RouletteSpin.prize_value,
    )
    reward_issued = exists().where(
        UserReward.spin_id == RouletteSpin.id,
        UserReward.kind == RouletteSpin.prize_type,
        UserReward.value == RouletteSpin.prize_value,
    )

    return {
        "integrity": {
            # Ledger rows of a customer with no account count too: nothing
            # explains them either.
            "balances_disagreeing_with_ledger": await count(
                accounts.where(LoyaltyAccount.stamp_balance != func.coalesce(per_user.c.total, 0))
            )
            + await count(
                select(func.count())
                .select_from(per_user)
                .where(~exists().where(LoyaltyAccount.user_id == per_user.c.user_id))
            ),
            "purchase_counts_disagreeing_with_ledger": await count(
                accounts.where(
                    LoyaltyAccount.qualifying_purchase_count
                    != func.coalesce(per_user.c.purchases, 0)
                )
            ),
            "ledger_rows_with_wrong_running_balance": await count(
                select(func.count())
                .select_from(running)
                .where(running.c.balance_after != running.c.running)
            ),
            "stamp_card_rewards_without_debit": await count(
                select(func.count())
                .select_from(UserReward)
                .where(
                    UserReward.source == RewardSource.STAMP_CARD,
                    ~exists().where(LoyaltyTransaction.reward_id == UserReward.id),
                )
            ),
            "spin_grants_out_of_step_with_spins": await count(
                select(func.count())
                .select_from(RouletteSpinGrant)
                .where(
                    or_(
                        and_(RouletteSpinGrant.consumed_at.is_not(None), ~spun),
                        and_(RouletteSpinGrant.consumed_at.is_(None), spun),
                    )
                )
            ),
            "spins_without_their_prize": await count(
                select(func.count())
                .select_from(RouletteSpin)
                .where(
                    or_(
                        and_(stamp_prize, ~stamps_booked),
                        and_(~stamp_prize, ~reward_issued),
                    )
                )
            ),
        },
        "coverage": {
            "users_without_account": await count(
                select(func.count())
                .select_from(User)
                .where(~exists().where(LoyaltyAccount.user_id == User.id))
            ),
            "users_without_welcome_spin": await count(
                select(func.count())
                .select_from(User)
                .where(
                    ~exists().where(
                        RouletteSpinGrant.user_id == User.id,
                        RouletteSpinGrant.reason == SpinGrantReason.INITIAL_PROMO,
                    )
                )
            ),
        },
    }


async def collect(session: AsyncSession) -> dict[str, object]:
    settings = get_settings()
    report: dict[str, object] = {}

    # ---------------------------------------- A. what PostgreSQL holds
    async def count(model: type) -> int:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)

    db: dict[str, int] = {
        "users": await count(User),
        "categories": await count(Category),
        "subcategories": await count(Subcategory),
        "products": await count(Product),
        "carts": await count(Cart),
        "cart_items": await count(CartItem),
        "orders": await count(Order),
        "order_items": await count(OrderItem),
    }
    # A deploy whose loyalty migration did not run must still get the rest of
    # the report, so a missing schema is reported, not raised.
    if await _has_table(session, LoyaltyAccount.__tablename__):
        db.update(
            {
                "loyalty_accounts": await count(LoyaltyAccount),
                "loyalty_transactions": await count(LoyaltyTransaction),
                "roulette_spin_grants": await count(RouletteSpinGrant),
                "roulette_spins": await count(RouletteSpin),
                "user_rewards": await count(UserReward),
                "referrals": await count(Referral),
            }
        )
        report["loyalty"] = await loyalty_health(session)
    else:
        report["loyalty"] = {"schema": "missing: migration 3b9d6f2a8c14 is not applied"}
    report["db"] = db
    report["order_status_counts"] = {
        status.value: int(
            await session.scalar(
                select(func.count()).select_from(Order).where(Order.status == status)
            )
            or 0
        )
        for status in OrderStatus
    }
    report["historical_total"] = str(
        await session.scalar(select(func.coalesce(func.sum(Order.total_price), 0)))
    )
    report["completed_revenue"] = str(
        await session.scalar(
            select(func.coalesce(func.sum(Order.total_price), 0)).where(
                Order.status == OrderStatus.COMPLETED
            )
        )
    )
    # identifies the PostgreSQL cluster itself: a new value means new storage
    report["system_identifier"] = str(
        await session.scalar(text("SELECT system_identifier FROM pg_control_system()"))
    )

    # ------------------------------------- B. what the bot would render
    i18n = LocalizationService(LANGUAGE)
    catalog = CatalogService(session)
    rendered: dict[str, object] = {}

    tree: dict[str, dict[str, list[str]]] = {}
    cards_ok, cards_broken = 0, []
    for category in await catalog.list_categories():
        category_name = localized_category_name(category, LANGUAGE)
        tree[category_name] = {}
        for subcategory in await catalog.list_subcategories(category.id):
            sub_name = localized_category_name(subcategory, LANGUAGE)
            names = []
            for product in await catalog.list_subcategory_products(subcategory.id):
                card = format_product_card(product, i18n)
                names.append(product.name_en)
                # a card that cannot show its own name and price is a render failure
                if product.name_en in card and str(product.price) in card:
                    cards_ok += 1
                else:
                    cards_broken.append(product.id)
            tree[category_name][sub_name] = names
    rendered["catalog_tree"] = tree
    rendered["product_cards_rendered"] = cards_ok
    rendered["product_cards_broken"] = cards_broken

    cart_service = CartService(session)
    carts = []
    for user in (await session.scalars(select(User).order_by(User.id))).all():
        view = await cart_service.get_view(user.id, language=LANGUAGE)
        if view is not None and view.lines:
            carts.append(
                {
                    "telegram_id": user.telegram_id,
                    "lines": len(view.lines),
                    "total": str(view.total),
                }
            )
    rendered["carts"] = carts

    orders = (await session.scalars(select(Order).order_by(Order.id))).all()
    rendered["orders"] = [
        {"id": o.id, "status": o.status.value, "total": str(o.total_price)} for o in orders
    ]

    stats = await StatisticsService(session, settings.app_timezone).collect(datetime.now(UTC))
    dashboard = format_statistics(stats, i18n, settings.currency_symbol)
    rendered["dashboard"] = dashboard
    rendered["dashboard_general"] = {
        "users": stats.general.users,
        "categories": stats.general.categories,
        "subcategories": stats.general.subcategories,
        "products": stats.general.products,
        "orders": stats.general.orders,
    }
    rendered["dashboard_all_time_revenue"] = str(stats.all_time.revenue)
    rendered["dashboard_most_ordered"] = [(p.name_en, p.order_count) for p in stats.most_ordered]

    report["rendered"] = rendered
    return report


async def main() -> int:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            report = await collect(session)
    finally:
        await engine.dispose()
    print("---VERIFY-JSON-START---")
    print(json.dumps(report, ensure_ascii=False))
    print("---VERIFY-JSON-END---")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
