"""Loyalty ledger — the only code that moves a stamp balance.

Locking rule: every mutation of a customer's loyalty state begins by locking
that customer's ``loyalty_accounts`` row (:meth:`LoyaltyService.lock_account`).
That one rule serialises a customer's stamp, spin and reward operations inside
PostgreSQL, so a double tap, a replayed callback or two admins completing the
same order are applied one after another, each against fresh values.

The database is the backstop for anything the lock does not cover: unique
constraints make every earning event bookable at most once, and CHECK
constraints keep balances non-negative and every ledger row consistent with its
source.

Operations validate before they write, so a rejected one leaves nothing behind
even if the caller catches the error and commits. Nothing here commits: the
caller owns the unit of work (``DatabaseMiddleware`` in a handler).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    LoyaltyTransactionType,
    ReferralStatus,
    RewardSource,
    RewardStatus,
    RewardType,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.reward import UserReward
from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.repositories.order import OrderRepository
from app.repositories.referral import ReferralRepository
from app.repositories.roulette_spin import RouletteSpinRepository
from app.repositories.user_reward import UserRewardRepository
from app.utils.validators import to_money

NOTE_MAX_LENGTH = 255


class LoyaltyError(ValueError):
    """
    A loyalty operation refused by a business rule, before anything was written.

    Every refusal a customer can cause — too few stamps, a stale card, a reward
    already used — derives from it, so a caller can answer those and still let a
    plain ``ValueError`` (a caller bug, such as another customer's order) surface.
    """


class InsufficientStampsError(LoyaltyError):
    """Raised when an operation would take a stamp balance below zero."""


class StaleCardError(LoyaltyError):
    """The card a claim was made from no longer matches the ledger — e.g. a double tap."""


class AlreadyClaimedError(StaleCardError):
    """The card a claim was made from has already been used to claim a free bottle."""


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """A ledger row, and whether this call created it (``False``: already booked)."""

    transaction: LoyaltyTransaction
    created: bool


class LoyaltyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.accounts = LoyaltyAccountRepository(session)
        self.transactions = LoyaltyTransactionRepository(session)
        self.rewards = UserRewardRepository(session)
        self.orders = OrderRepository(session)
        self.referrals = ReferralRepository(session)
        self.spins = RouletteSpinRepository(session)

    # --- accounts -----------------------------------------------------------------

    async def get_or_create_account(self, user_id: int) -> LoyaltyAccount:
        account, _created = await self.accounts.get_or_create_for_user(user_id)
        return account

    async def lock_account(self, user_id: int) -> LoyaltyAccount:
        """
        Create the account if needed, then lock it until the transaction ends.

        Pending changes are flushed first: the locking read refreshes the
        instance from the database, which would otherwise discard them.
        """
        await self.session.flush()
        await self.accounts.get_or_create_for_user(user_id)
        account = await self.accounts.get_by_user_id(user_id, for_update=True)
        if account is None:  # pragma: no cover - created just above
            raise RuntimeError(f"Loyalty account for user {user_id} vanished")
        return account

    async def balance(self, user_id: int) -> int:
        """Cached balance; 0 for a customer who has never had an account."""
        account = await self.accounts.get_by_user_id(user_id)
        return account.stamp_balance if account is not None else 0

    async def purchase_count(self, user_id: int) -> int:
        """Qualifying purchases booked so far; 0 for a customer who has never had an account."""
        account = await self.accounts.get_by_user_id(user_id)
        return account.qualifying_purchase_count if account is not None else 0

    async def ledger_balance(self, user_id: int) -> int:
        """The balance as the ledger explains it. Must equal :meth:`balance`."""
        return await self.transactions.sum_for_user(user_id)

    async def ledger_version(self, user_id: int) -> int:
        """Id of the customer's latest ledger row (0 if none) — changes on every movement."""
        return await self.transactions.latest_id_for_user(user_id)

    async def history(self, user_id: int, *, limit: int = 50) -> list[LoyaltyTransaction]:
        return await self.transactions.list_for_user(user_id, limit=limit)

    # --- earning ------------------------------------------------------------------

    async def record_purchase(self, user_id: int, *, order_id: int, stamps: int) -> LedgerPosting:
        """
        Book a qualifying purchase: its stamps (possibly 0) and one purchase count.

        Idempotent per order — replaying it changes nothing and returns the
        original row with ``created=False``. Deciding whether an order qualifies,
        and how many stamps it earns, is the caller's business rule.
        """
        if stamps < 0:
            raise ValueError("A purchase cannot earn a negative number of stamps")
        order = await self.orders.get_by_id(order_id)
        if order is None or order.user_id != user_id:
            raise ValueError(f"Order {order_id} does not belong to user {user_id}")

        account = await self.lock_account(user_id)
        existing = await self.transactions.get_purchase_for_order(order_id)
        if existing is not None:
            return LedgerPosting(existing, created=False)

        account.qualifying_purchase_count += 1
        row = await self._append(
            account,
            kind=LoyaltyTransactionType.PURCHASE,
            amount=stamps,
            order_id=order_id,
        )
        return LedgerPosting(row, created=True)

    async def credit_referral(
        self,
        user_id: int,
        *,
        referral_id: int,
        stamps: int,
    ) -> LedgerPosting:
        """Book a referral bonus for either side of the referral. Once per side."""
        if stamps <= 0:
            raise ValueError("A referral bonus must be positive")
        referral = await self.referrals.get_by_id(referral_id)
        if referral is None or user_id not in (
            referral.referrer_user_id,
            referral.referred_user_id,
        ):
            raise ValueError(f"User {user_id} is not a party to referral {referral_id}")
        if referral.status != ReferralStatus.QUALIFIED:
            # Owner decision: referral bonuses are paid at the referred
            # customer's first completed paid order, never at sign-up.
            raise ValueError(f"Referral {referral_id} has not qualified yet")

        account = await self.lock_account(user_id)
        existing = await self.transactions.get_for_referral(referral_id, user_id)
        if existing is not None:
            return LedgerPosting(existing, created=False)

        row = await self._append(
            account,
            kind=LoyaltyTransactionType.REFERRAL,
            amount=stamps,
            referral_id=referral_id,
        )
        return LedgerPosting(row, created=True)

    async def credit_roulette(self, user_id: int, *, spin_id: int, stamps: int) -> LedgerPosting:
        """Book the stamps a roulette spin won. Once per spin."""
        if stamps <= 0:
            raise ValueError("A roulette stamp prize must be positive")
        spin = await self.spins.get_by_id(spin_id)
        if spin is None or spin.user_id != user_id:
            raise ValueError(f"Spin {spin_id} does not belong to user {user_id}")

        account = await self.lock_account(user_id)
        existing = await self.transactions.get_for_spin(spin_id)
        if existing is not None:
            return LedgerPosting(existing, created=False)

        row = await self._append(
            account,
            kind=LoyaltyTransactionType.ROULETTE,
            amount=stamps,
            spin_id=spin_id,
        )
        return LedgerPosting(row, created=True)

    async def adjust(self, user_id: int, *, amount: int, note: str) -> LoyaltyTransaction:
        """
        Manual correction, always with a reason.

        Not idempotent: each call is a separate correction, so an admin UI must
        guard its confirm step (``confirm_once``). It still cannot overdraw.
        """
        if amount == 0:
            raise ValueError("An adjustment must change the balance")
        reason = note.strip()
        if not reason:
            raise ValueError("An adjustment needs a note explaining it")

        account = await self.lock_account(user_id)
        if account.stamp_balance + amount < 0:
            raise InsufficientStampsError(
                f"User {user_id} has {account.stamp_balance} stamps; cannot apply {amount}"
            )
        return await self._append(
            account,
            kind=LoyaltyTransactionType.ADJUSTMENT,
            amount=amount,
            note=reason[:NOTE_MAX_LENGTH],
        )

    # --- spending -----------------------------------------------------------------

    async def claim_free_bottle(
        self,
        user_id: int,
        *,
        stamps_required: int,
        max_item_price: Decimal,
        expected_version: int | None = None,
    ) -> UserReward:
        """
        Exchange stamps for a free-bottle reward, debit and reward together.

        The balance is checked under the account lock, so no concurrency can
        overdraw it. A customer holding 20 stamps may claim twice; to make one
        *card* claimable once, pass ``expected_version`` — the
        :meth:`ledger_version` the card was rendered from. Any movement since
        raises :class:`StaleCardError`; when that movement was itself a claim
        (the first tap of a double tap), :class:`AlreadyClaimedError`.
        """
        if stamps_required <= 0:
            raise ValueError("A redemption must cost at least one stamp")
        price = to_money(max_item_price)

        account = await self.lock_account(user_id)
        if expected_version is not None:
            current = await self.transactions.latest_id_for_user(user_id)
            if current != expected_version:
                if await self.transactions.has_redemption_after(user_id, expected_version):
                    raise AlreadyClaimedError(
                        f"User {user_id} already claimed a reward from the card "
                        f"at version {expected_version}"
                    )
                raise StaleCardError(
                    f"User {user_id}'s card changed since it was shown "
                    f"(version {expected_version}, now {current})"
                )
        if account.stamp_balance < stamps_required:
            raise InsufficientStampsError(
                f"User {user_id} has {account.stamp_balance} stamps; {stamps_required} are required"
            )

        reward = await self.rewards.create_and_add(
            user_id=user_id,
            kind=RewardType.FREE_BOTTLE,
            value=1,
            max_item_price=price,
            source=RewardSource.STAMP_CARD,
            status=RewardStatus.AVAILABLE,
        )
        await self._append(
            account,
            kind=LoyaltyTransactionType.REDEMPTION,
            amount=-stamps_required,
            reward_id=reward.id,
        )
        return reward

    # --- internals ----------------------------------------------------------------

    async def _append(
        self,
        account: LoyaltyAccount,
        *,
        kind: LoyaltyTransactionType,
        amount: int,
        order_id: int | None = None,
        referral_id: int | None = None,
        spin_id: int | None = None,
        reward_id: int | None = None,
        note: str | None = None,
    ) -> LoyaltyTransaction:
        """
        Move the cached balance and write its ledger row in a single flush.

        The caller holds the account lock and has validated the operation; the
        check here is the last line before the database's own CHECK.
        """
        new_balance = account.stamp_balance + amount
        if new_balance < 0:
            raise InsufficientStampsError(
                f"User {account.user_id} has {account.stamp_balance} stamps; cannot apply {amount}"
            )
        account.stamp_balance = new_balance
        return await self.transactions.create_and_add(
            user_id=account.user_id,
            kind=kind,
            amount=amount,
            balance_after=new_balance,
            order_id=order_id,
            referral_id=referral_id,
            spin_id=spin_id,
            reward_id=reward_id,
            note=note,
        )
