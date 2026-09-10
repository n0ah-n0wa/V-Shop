"""Referral persistence — codes, attribution, qualification.

Rewarding the two sides is not done here: when a referral qualifies, the caller
books the bonuses through :meth:`LoyaltyService.credit_referral` and
:meth:`RouletteService.grant_referral_spin`, both idempotent per referral.
Deciding that the referred customer is genuinely *new* is also the caller's
job, at ``/start`` time — this module enforces what the database can know.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ReferralStatus
from app.models.order import Order
from app.models.referral import Referral
from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.repositories.referral import ReferralRepository
from app.services.loyalty import LoyaltyService

# 9 random bytes -> 12 URL-safe characters: fits Telegram's 64-character
# /start payload with room to spare, and is not guessable like a user id.
REFERRAL_CODE_BYTES = 9
REFERRAL_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,32}")
_CODE_ATTEMPTS = 5


class SelfReferralError(ValueError):
    """A customer tried to refer themselves."""


class ReferralLoopError(ValueError):
    """The would-be referrer was themselves referred by this customer."""


@dataclass(frozen=True, slots=True)
class ReferralAttribution:
    referral: Referral
    created: bool  # False: the customer already had a referrer, which is kept


def generate_referral_code() -> str:
    return secrets.token_urlsafe(REFERRAL_CODE_BYTES)


def is_valid_referral_code(code: str | None) -> bool:
    return code is not None and REFERRAL_CODE_PATTERN.fullmatch(code) is not None


class ReferralService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.referrals = ReferralRepository(session)
        self.accounts = LoyaltyAccountRepository(session)
        self.loyalty = LoyaltyService(session)

    async def get_or_create_referral_code(self, user_id: int) -> str:
        """The customer's personal code; assigned once, then stable."""
        account = await self.loyalty.lock_account(user_id)
        if account.referral_code:
            return account.referral_code
        for _ in range(_CODE_ATTEMPTS):
            code = generate_referral_code()
            if await self.accounts.get_by_referral_code(code) is None:
                account.referral_code = code
                await self.session.flush()
                return code
        raise RuntimeError("Could not allocate a unique referral code")

    async def find_referrer_user_id(self, code: str | None) -> int | None:
        """Resolve a code from a deep link; malformed input never reaches SQL."""
        if code is None or not is_valid_referral_code(code):
            return None
        account = await self.accounts.get_by_referral_code(code)
        return account.user_id if account is not None else None

    async def attribute(
        self, *, referrer_user_id: int, referred_user_id: int
    ) -> ReferralAttribution:
        """
        Record that ``referrer_user_id`` invited ``referred_user_id`` (pending).

        A customer has at most one referrer, ever: a second attribution — to the
        same or another referrer — returns the original with ``created=False``.
        """
        if referrer_user_id == referred_user_id:
            raise SelfReferralError(f"User {referred_user_id} cannot refer themselves")

        existing = await self.referrals.get_by_referred_user_id(referred_user_id)
        if existing is not None:
            return ReferralAttribution(existing, created=False)

        reverse = await self.referrals.get_by_referred_user_id(referrer_user_id)
        if reverse is not None and reverse.referrer_user_id == referred_user_id:
            raise ReferralLoopError(
                f"User {referrer_user_id} was referred by {referred_user_id}; "
                "the reverse referral would form a loop"
            )

        try:
            async with self.session.begin_nested():
                referral = await self.referrals.create_and_add(
                    referrer_user_id=referrer_user_id,
                    referred_user_id=referred_user_id,
                    status=ReferralStatus.PENDING,
                )
            return ReferralAttribution(referral, created=True)
        except IntegrityError:
            existing = await self.referrals.get_by_referred_user_id(referred_user_id)
            if existing is None:
                raise
            return ReferralAttribution(existing, created=False)

    async def get_for_referred_user(self, user_id: int) -> Referral | None:
        return await self.referrals.get_by_referred_user_id(user_id)

    async def qualify(self, referral_id: int, *, order_id: int) -> bool:
        """
        Mark a pending referral qualified by the referred customer's order.

        Returns ``True`` only for the call that made the change; an already
        qualified referral returns ``False`` and is left as it is.
        """
        referral = await self.referrals.get_for_update(referral_id)
        if referral is None:
            raise ValueError(f"Referral {referral_id} does not exist")
        order = await self.session.get(Order, order_id)
        if order is None or order.user_id != referral.referred_user_id:
            raise ValueError(
                f"Order {order_id} is not an order of referred user {referral.referred_user_id}"
            )
        if referral.status == ReferralStatus.QUALIFIED:
            return False

        referral.status = ReferralStatus.QUALIFIED
        referral.qualifying_order_id = order_id
        referral.qualified_at = datetime.now(UTC)
        await self.session.flush()
        return True
