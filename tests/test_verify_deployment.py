"""The deploy check's loyalty section: quiet on a consistent state, loud on drift."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, ReferralStatus
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.referral import Referral
from app.models.reward import UserReward
from app.models.roulette import RouletteSpin, RouletteSpinGrant
from app.services.admin import AdminService
from app.services.loyalty import LoyaltyService
from app.services.referral import ReferralService
from app.services.roulette import PRIZE_CATALOGUE, RouletteService
from app.services.stamp_card import StampCardService
from app.verify_deployment import loyalty_health
from tests.factories import make_order, make_user

TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
PRIZE = {prize.code: prize for prize in PRIZE_CATALOGUE}


async def active_customer(session: AsyncSession) -> int:
    """A customer who has done a bit of everything, all through the services."""
    user = await make_user(session, telegram_id=8801)
    admin = AdminService(session)
    order_ids = []
    for _ in range(3):  # 3 x €40 = 6 stamps
        order = await make_order(session, user)
        order.total_price = Decimal("40.00")
        for status in TO_COMPLETED:
            order = await admin.set_order_status(order, status)
        order_ids.append(order.id)
    await LoyaltyService(session).adjust(user.id, amount=4, note="goodwill")
    await StampCardService(session).claim_free_bottle(user.id)
    roulette = RouletteService(session)
    await roulette.grant_initial_spin(user.id)
    await roulette.spin(user.id, PRIZE["stamp_1"])  # won stamps
    await roulette.grant_purchase_milestone_spin(user.id, order_id=order_ids[0])
    await roulette.spin(user.id, PRIZE["discount_10"])  # won a reward
    await roulette.grant_purchase_milestone_spin(user.id, order_id=order_ids[-1])  # unspun
    return user.id


async def test_a_consistent_state_has_no_integrity_problem(session: AsyncSession) -> None:
    await active_customer(session)

    health = await loyalty_health(session)

    assert health["integrity"] == {
        "balances_disagreeing_with_ledger": 0,
        "purchase_counts_disagreeing_with_ledger": 0,
        "ledger_rows_with_wrong_running_balance": 0,
        "stamp_card_rewards_without_debit": 0,
        "spin_grants_out_of_step_with_spins": 0,
        "spins_without_their_prize": 0,
        "referral_bonuses_before_qualification": 0,
        "purchase_stamps_on_orders_that_do_not_qualify": 0,
        "milestone_spins_without_their_purchase": 0,
        "used_rewards_on_another_customers_order": 0,
        "free_bottles_without_their_free_line": 0,
        "referrals_qualified_by_an_order_that_does_not": 0,
    }
    assert health["coverage"] == {"users_without_account": 0, "users_without_welcome_spin": 0}


async def test_coverage_counts_customers_not_yet_given_their_rows(session: AsyncSession) -> None:
    await active_customer(session)
    await make_user(session, telegram_id=8802)  # signed up, never touched the programme

    health = await loyalty_health(session)

    assert health["coverage"] == {"users_without_account": 1, "users_without_welcome_spin": 1}
    assert set(health["integrity"].values()) == {0}, "a new customer is not an inconsistency"


def _first_ledger_row() -> Any:
    return select(func.min(LoyaltyTransaction.id)).scalar_subquery()


# Each writes behind the services' back what they would never write themselves.
DRIFT: list[tuple[str, Callable[[int], Any]]] = [
    (
        "balances_disagreeing_with_ledger",
        lambda user_id: (
            update(LoyaltyAccount)
            .where(LoyaltyAccount.user_id == user_id)
            .values(stamp_balance=LoyaltyAccount.stamp_balance + 1)
        ),
    ),
    (
        "purchase_counts_disagreeing_with_ledger",
        lambda user_id: (
            update(LoyaltyAccount)
            .where(LoyaltyAccount.user_id == user_id)
            .values(qualifying_purchase_count=LoyaltyAccount.qualifying_purchase_count + 1)
        ),
    ),
    (
        "ledger_rows_with_wrong_running_balance",
        lambda _: (
            update(LoyaltyTransaction)
            .where(LoyaltyTransaction.id == _first_ledger_row())
            .values(balance_after=LoyaltyTransaction.balance_after + 1)
        ),
    ),
    (
        "stamp_card_rewards_without_debit",
        lambda _: delete(LoyaltyTransaction).where(LoyaltyTransaction.reward_id.is_not(None)),
    ),
    (
        "spin_grants_out_of_step_with_spins",
        lambda _: (
            update(RouletteSpinGrant)
            .where(RouletteSpinGrant.consumed_at.is_(None))
            .values(consumed_at=datetime.now(UTC))
        ),
    ),
]


@pytest.mark.parametrize(("check", "drift"), DRIFT, ids=[check for check, _ in DRIFT])
async def test_drift_behind_the_services_is_reported(
    session: AsyncSession, check: str, drift: Callable[[int], Any]
) -> None:
    user_id = await active_customer(session)

    await session.execute(drift(user_id).execution_options(synchronize_session=False))

    assert (await loyalty_health(session))["integrity"][check] >= 1


# What a spin won, changed after the fact — each no longer matches the spin.
PRIZE_DRIFT: dict[str, Callable[[], Any]] = {
    "stamps-rebooked": lambda: (
        update(LoyaltyTransaction)
        .where(LoyaltyTransaction.spin_id.is_not(None))
        .values(amount=LoyaltyTransaction.amount + 1)
    ),
    "reward-inflated": lambda: (
        update(UserReward).where(UserReward.spin_id.is_not(None)).values(value=50)
    ),
    "reward-removed": lambda: delete(UserReward).where(UserReward.spin_id.is_not(None)),
    "prize-rewritten": lambda: update(RouletteSpin).values(
        prize_value=RouletteSpin.prize_value + 1
    ),
}


@pytest.mark.parametrize("drift", list(PRIZE_DRIFT.values()), ids=list(PRIZE_DRIFT))
async def test_a_spin_whose_prize_no_longer_matches_is_reported(
    session: AsyncSession, drift: Callable[[], Any]
) -> None:
    await active_customer(session)

    await session.execute(drift().execution_options(synchronize_session=False))

    assert (await loyalty_health(session))["integrity"]["spins_without_their_prize"] >= 1


async def test_a_referral_bonus_paid_before_qualifying_is_reported(session: AsyncSession) -> None:
    referrer = await make_user(session, telegram_id=8803)
    referred = await make_user(session, telegram_id=8804)
    referral = (
        await ReferralService(session).attribute(
            referrer_user_id=referrer.id, referred_user_id=referred.id
        )
    ).referral
    order = await make_order(session, referred)
    order.total_price = Decimal("20.00")
    admin = AdminService(session)
    for status in TO_COMPLETED:
        order = await admin.set_order_status(order, status)  # pays both sides and a spin
    check = "referral_bonuses_before_qualification"
    assert (await loyalty_health(session))["integrity"][check] == 0

    await session.execute(
        update(Referral)
        .where(Referral.id == referral.id)
        .values(status=ReferralStatus.PENDING, qualifying_order_id=None, qualified_at=None)
        .execution_options(synchronize_session=False)
    )

    assert (await loyalty_health(session))["integrity"][check] == 3, "two bonuses and a spin"
