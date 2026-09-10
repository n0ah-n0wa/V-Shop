"""LoyaltyService — the stamp ledger: idempotent, auditable, never overdrawn."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    RewardSource,
    RewardStatus,
    RewardType,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.reward import UserReward
from app.models.user import User
from app.services.loyalty import InsufficientStampsError, LoyaltyService
from app.services.referral import ReferralService
from tests.factories import make_order, make_user

CEILING = Decimal("20.00")


async def ledger(session: AsyncSession, user_id: int) -> list[LoyaltyTransaction]:
    """Oldest first."""
    result = await session.scalars(
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.user_id == user_id)
        .order_by(LoyaltyTransaction.id)
    )
    return list(result.all())


async def count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def qualified_referral(session: AsyncSession, *, referrer: User, referred: User) -> int:
    """A referral whose referred customer has completed an order — it may pay out."""
    referrals = ReferralService(session)
    attribution = await referrals.attribute(
        referrer_user_id=referrer.id, referred_user_id=referred.id
    )
    order = await make_order(session, referred, status=OrderStatus.COMPLETED)
    assert await referrals.qualify(attribution.referral.id, order_id=order.id)
    return attribution.referral.id


async def test_an_account_is_created_once(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7201)
    loyalty = LoyaltyService(session)

    first = await loyalty.get_or_create_account(user.id)
    second = await loyalty.get_or_create_account(user.id)

    assert first.id == second.id
    assert (first.stamp_balance, first.qualifying_purchase_count) == (0, 0)
    assert await count(session, LoyaltyAccount) == 1


async def test_a_purchase_is_booked_once_however_often_it_is_replayed(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=7202)
    order = await make_order(session, user)
    loyalty = LoyaltyService(session)

    first = await loyalty.record_purchase(user.id, order_id=order.id, stamps=2)
    replay = await loyalty.record_purchase(user.id, order_id=order.id, stamps=2)

    assert (first.created, replay.created) == (True, False)
    assert replay.transaction.id == first.transaction.id
    account = await loyalty.get_or_create_account(user.id)
    assert (account.stamp_balance, account.qualifying_purchase_count) == (2, 1)
    assert len(await ledger(session, user.id)) == 1


async def test_a_purchase_below_the_threshold_still_counts(session: AsyncSession) -> None:
    """A €15 order earns no stamp but is still a purchase towards the next spin."""
    user = await make_user(session, telegram_id=7203)
    order = await make_order(session, user)
    loyalty = LoyaltyService(session)

    posting = await loyalty.record_purchase(user.id, order_id=order.id, stamps=0)

    account = await loyalty.get_or_create_account(user.id)
    assert (account.stamp_balance, account.qualifying_purchase_count) == (0, 1)
    assert (posting.transaction.amount, posting.transaction.balance_after) == (0, 0)


async def test_stamps_only_go_to_the_orders_owner(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7204)
    bob = await make_user(session, telegram_id=7205)
    order = await make_order(session, alice)

    with pytest.raises(ValueError):
        await LoyaltyService(session).record_purchase(bob.id, order_id=order.id, stamps=2)
    assert await count(session, LoyaltyTransaction) == 0


async def test_a_purchase_cannot_earn_negative_stamps(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7206)
    order = await make_order(session, user)
    with pytest.raises(ValueError):
        await LoyaltyService(session).record_purchase(user.id, order_id=order.id, stamps=-1)


async def test_each_side_of_a_referral_is_credited_once(session: AsyncSession) -> None:
    alice = await make_user(session, telegram_id=7207)
    bob = await make_user(session, telegram_id=7208)
    carol = await make_user(session, telegram_id=7209)
    referral_id = await qualified_referral(session, referrer=alice, referred=bob)
    loyalty = LoyaltyService(session)

    for user in (alice, bob):
        first = await loyalty.credit_referral(user.id, referral_id=referral_id, stamps=2)
        replay = await loyalty.credit_referral(user.id, referral_id=referral_id, stamps=2)
        assert (first.created, replay.created) == (True, False)
        assert await loyalty.balance(user.id) == 2

    with pytest.raises(ValueError):
        await loyalty.credit_referral(carol.id, referral_id=referral_id, stamps=2)
    assert await loyalty.balance(carol.id) == 0


async def test_a_pending_referral_pays_nothing(session: AsyncSession) -> None:
    """Owner decision: the bonus waits for the referred customer's first paid order."""
    alice = await make_user(session, telegram_id=7220)
    bob = await make_user(session, telegram_id=7221)
    attribution = await ReferralService(session).attribute(
        referrer_user_id=alice.id, referred_user_id=bob.id
    )

    for user in (alice, bob):
        with pytest.raises(ValueError):
            await LoyaltyService(session).credit_referral(
                user.id, referral_id=attribution.referral.id, stamps=2
            )
    assert await count(session, LoyaltyTransaction) == 0


@pytest.mark.parametrize(
    "ceiling",
    [20.0, Decimal("NaN"), Decimal("0"), Decimal("-1"), Decimal("100000000"), "abc"],
    ids=["float", "nan", "zero", "negative", "too-large", "not-a-number"],
)
async def test_a_price_ceiling_must_be_real_money(session: AsyncSession, ceiling: object) -> None:
    user = await make_user(session, telegram_id=7222)
    loyalty = LoyaltyService(session)
    await loyalty.adjust(user.id, amount=10, note="opening balance")

    with pytest.raises((TypeError, ValueError)):
        await loyalty.claim_free_bottle(
            user.id,
            stamps_required=10,
            max_item_price=ceiling,  # type: ignore[arg-type]
        )

    assert await count(session, UserReward) == 0
    assert await loyalty.balance(user.id) == 10


async def test_the_account_timestamps_stay_readable_after_a_change(
    session: AsyncSession,
) -> None:
    """
    Regression: updated_at is written by the database on every UPDATE.

    It was expired after the flush, so reading it needed implicit IO and raised
    MissingGreenlet under AsyncSession. It is now fetched back with RETURNING.
    """
    user = await make_user(session, telegram_id=7223)
    loyalty = LoyaltyService(session)
    account = await loyalty.lock_account(user.id)

    await loyalty.adjust(user.id, amount=2, note="probe")

    assert account.stamp_balance == 2
    assert account.updated_at is not None
    assert account.created_at is not None


async def test_claiming_a_free_bottle_spends_stamps_and_issues_the_reward(
    session: AsyncSession,
) -> None:
    user = await make_user(session, telegram_id=7210)
    loyalty = LoyaltyService(session)
    await loyalty.adjust(user.id, amount=12, note="opening balance")

    reward = await loyalty.claim_free_bottle(
        user.id, stamps_required=10, max_item_price=Decimal("20")
    )

    assert (reward.kind, reward.value, reward.max_item_price) == (
        RewardType.FREE_BOTTLE,
        1,
        CEILING,
    )
    assert (reward.source, reward.status) == (RewardSource.STAMP_CARD, RewardStatus.AVAILABLE)
    assert await loyalty.balance(user.id) == 2

    debit = (await ledger(session, user.id))[-1]
    assert debit.kind is LoyaltyTransactionType.REDEMPTION
    assert (debit.amount, debit.balance_after, debit.reward_id) == (-10, 2, reward.id)


async def test_a_refused_claim_leaves_nothing_behind(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7211)
    loyalty = LoyaltyService(session)
    await loyalty.adjust(user.id, amount=9, note="opening balance")

    with pytest.raises(InsufficientStampsError):
        await loyalty.claim_free_bottle(user.id, stamps_required=10, max_item_price=CEILING)

    assert await loyalty.balance(user.id) == 9
    assert await count(session, UserReward) == 0
    assert len(await ledger(session, user.id)) == 1


async def test_twenty_stamps_buy_exactly_two_bottles(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7212)
    loyalty = LoyaltyService(session)
    await loyalty.adjust(user.id, amount=20, note="opening balance")

    for _ in range(2):
        await loyalty.claim_free_bottle(user.id, stamps_required=10, max_item_price=CEILING)
    with pytest.raises(InsufficientStampsError):
        await loyalty.claim_free_bottle(user.id, stamps_required=10, max_item_price=CEILING)

    assert await loyalty.balance(user.id) == 0
    assert await count(session, UserReward) == 2


async def test_an_adjustment_needs_a_reason_and_cannot_overdraw(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=7213)
    loyalty = LoyaltyService(session)

    with pytest.raises(ValueError):
        await loyalty.adjust(user.id, amount=3, note="   ")
    with pytest.raises(ValueError):
        await loyalty.adjust(user.id, amount=0, note="nothing")
    with pytest.raises(InsufficientStampsError):
        await loyalty.adjust(user.id, amount=-1, note="correction")

    row = await loyalty.adjust(user.id, amount=3, note="  goodwill  ")
    assert (row.note, row.balance_after) == ("goodwill", 3)


async def test_the_ledger_explains_the_balance(session: AsyncSession) -> None:
    """Every balance can be traced row by row, and the cache agrees with it."""
    user = await make_user(session, telegram_id=7214)
    friend = await make_user(session, telegram_id=7215)
    first_order = await make_order(session, user)
    second_order = await make_order(session, user)
    referral_id = await qualified_referral(session, referrer=user, referred=friend)
    loyalty = LoyaltyService(session)

    await loyalty.record_purchase(user.id, order_id=first_order.id, stamps=2)
    await loyalty.record_purchase(user.id, order_id=second_order.id, stamps=1)
    await loyalty.credit_referral(user.id, referral_id=referral_id, stamps=2)
    await loyalty.adjust(user.id, amount=10, note="support gesture")
    await loyalty.claim_free_bottle(user.id, stamps_required=10, max_item_price=CEILING)
    await loyalty.adjust(user.id, amount=-1, note="correction")

    rows = await ledger(session, user.id)
    running = 0
    for row in rows:
        running += row.amount
        assert row.balance_after == running, row
    assert running == await loyalty.balance(user.id) == await loyalty.ledger_balance(user.id) == 4
    assert [row.kind for row in rows] == [
        LoyaltyTransactionType.PURCHASE,
        LoyaltyTransactionType.PURCHASE,
        LoyaltyTransactionType.REFERRAL,
        LoyaltyTransactionType.ADJUSTMENT,
        LoyaltyTransactionType.REDEMPTION,
        LoyaltyTransactionType.ADJUSTMENT,
    ]


async def test_a_stale_in_memory_account_cannot_lose_an_update(session: AsyncSession) -> None:
    """
    The locking read refreshes the account from the database.

    Without that refresh the ORM would add to whatever balance an earlier query
    left in the identity map — 0 here — and write 2 over another transaction's 5.
    """
    user = await make_user(session, telegram_id=7216)
    order = await make_order(session, user)
    loyalty = LoyaltyService(session)
    account = await loyalty.get_or_create_account(user.id)
    assert account.stamp_balance == 0

    # Another transaction moves the balance behind this session's back.
    await session.execute(
        update(LoyaltyAccount)
        .where(LoyaltyAccount.id == account.id)
        .values(stamp_balance=5)
        .execution_options(synchronize_session=False)
    )

    await loyalty.record_purchase(user.id, order_id=order.id, stamps=2)

    assert account.stamp_balance == 7
    stored = await session.scalar(
        select(LoyaltyAccount.stamp_balance).where(LoyaltyAccount.id == account.id)
    )
    assert stored == 7
