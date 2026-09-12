"""
Loyalty guards the other SQLite suites would let slip.

SQLite ignores ``FOR UPDATE``, so a lock taken out of the code leaves every
SQLite test green; the PostgreSQL suites prove the locks hold. These check, on
every run, that each path still asks for its locks, in order, before it writes:
a spin (the account, then its grant) and a referral attribution (the advisory
lock before the insert). The redemption paths are checked the same way in
``tests/test_loyalty_scenarios.py``.

Then three rules nothing else pins: a claim uses the configured card, only
purchases number the milestone spin, and the reward service's own re-check of
a free bottle's price cap.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.handlers.user.stamp_card import claim_free_bottle
from app.keyboards.stamp_card import CALLBACK_STAMP_CLAIM_PREFIX
from app.models.enums import RewardStatus
from app.models.reward import UserReward
from app.repositories.referral import ReferralRepository
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.referral import ReferralService
from app.services.reward import RewardNotApplicableError, RewardService
from app.services.roulette_engine import RouletteEngine, RoulettePolicy
from app.services.stamp_card import StampCardPolicy, StampCardService
from tests.factories import add_order_item, make_category, make_order, make_product, make_user
from tests.test_loyalty_scenarios import first_loyalty_write, locks_and_writes
from tests.test_rewards_and_referrals import free_bottle
from tests.test_roulette_engine import landing_on, with_spins
from tests.test_spin_entitlements import complete, milestone_orders
from tests.test_stamp_card_ui import configured, tap

EN = LocalizationService("en")


@pytest.mark.parametrize("code", ["stamp_2", "discount_10"])
async def test_a_spin_locks_the_account_then_the_grant_before_writing(
    engine: AsyncEngine, session: AsyncSession, code: str
) -> None:
    user = await with_spins(session, 9990)
    roulette = RouletteEngine(session, RoulettePolicy.defaults(), randbelow=landing_on(code))
    grant_id = await roulette.next_grant_id(user.id)
    assert grant_id is not None

    with locks_and_writes(engine, session) as log:
        outcome = await roulette.spin(user.id, grant_id=grant_id)

    assert outcome is not None and outcome.created
    assert "lock loyalty_accounts" in log, log
    assert log.index("lock loyalty_accounts") < first_loyalty_write(log), log
    assert "lock roulette_spin_grants" in log, log
    assert log.index("lock roulette_spin_grants") < log.index("update roulette_spin_grants"), log


async def test_an_attribution_takes_the_attribution_lock_before_writing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_lock = ReferralRepository.lock_attributions
    real_create = ReferralRepository.create_and_add

    async def lock(self: ReferralRepository, *args: Any, **kwargs: Any) -> Any:
        events.append("attribution lock")
        return await real_lock(self, *args, **kwargs)

    async def create(self: ReferralRepository, **fields: Any) -> Any:
        events.append("insert referral")
        return await real_create(self, **fields)

    monkeypatch.setattr(ReferralRepository, "lock_attributions", lock)
    monkeypatch.setattr(ReferralRepository, "create_and_add", create)
    alice = await make_user(session, telegram_id=9991)
    bob = await make_user(session, telegram_id=9992)

    await ReferralService(session).attribute(referrer_user_id=alice.id, referred_user_id=bob.id)

    assert events == ["attribution lock", "insert referral"]


async def test_the_claim_button_claims_under_the_configured_card(session: AsyncSession) -> None:
    """LOYALTY_STAMPS_REQUIRED=8, LOYALTY_FREE_BOTTLE_MAX_PRICE=18.50: 8 stamps claim it."""
    user = await make_user(session, telegram_id=9994)
    await LoyaltyService(session).adjust(user.id, amount=8, note="setup")
    settings = configured(loyalty_stamps_required=8, loyalty_free_bottle_max_price=Decimal("18.50"))
    card = await StampCardService(session, StampCardPolicy.from_settings(settings)).card(user.id)
    assert card.can_claim
    callback = tap(user, f"{CALLBACK_STAMP_CLAIM_PREFIX}{card.version}")

    await claim_free_bottle(callback=callback, session=session, settings=settings, i18n=EN)

    assert callback.alerts == [(EN.t("stamp_card.claimed"), True)]
    reward = (await session.scalars(select(UserReward))).one()
    assert reward.max_item_price == Decimal("18.50")
    assert await LoyaltyService(session).balance(user.id) == 0


async def test_other_ledger_rows_never_shift_the_purchase_milestone(session: AsyncSession) -> None:
    """A goodwill adjustment before the first order: the 5th purchase still earns the spin."""
    user = await make_user(session, telegram_id=9995)
    await LoyaltyService(session).adjust(user.id, amount=3, note="goodwill")

    orders = [await complete(session, user) for _ in range(5)]

    assert await milestone_orders(session, user.id) == [orders[4].id]


async def test_a_free_bottle_cannot_be_bound_to_a_product_above_its_cap(
    session: AsyncSession,
) -> None:
    """Checkout plans within the cap; the reward service refuses a record above it anyway."""
    user = await make_user(session, telegram_id=9993)
    category = await make_category(session, name="Liquids")
    dear = await make_product(session, category, name_en="Dear", price="22.00")
    order = await make_order(session, user)
    await add_order_item(session, order, dear, price="0.00")
    reward_id = await free_bottle(session, user.id)  # a stamp-card bottle, capped at 20.00

    with pytest.raises(RewardNotApplicableError):
        await RewardService(session).use_reward(
            reward_id,
            user_id=user.id,
            order_id=order.id,
            discount_amount=Decimal("22.00"),  # the product's real price, above the cap
            redeemed_product_id=dear.id,
        )

    reward = await RewardService(session).get_for_user(reward_id, user.id)
    assert reward is not None and reward.status == RewardStatus.AVAILABLE
