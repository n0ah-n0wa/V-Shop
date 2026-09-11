"""Referral persistence — codes, deep links, attribution, qualification.

Rewarding the two sides is not done here: when a referral qualifies, the
programme (:mod:`app.services.referral_program`) books the bonuses through
:meth:`LoyaltyService.credit_referral` and the spin through the entitlement
service, each idempotent per referral. Deciding that the referred customer is
genuinely *new* is also the programme's job, at ``/start`` time — this module
enforces what the database can know.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, ReferralStatus
from app.models.referral import Referral
from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.repositories.order import OrderRepository
from app.repositories.referral import ReferralRepository
from app.services.loyalty import LoyaltyError, LoyaltyService

# 9 random bytes -> 12 URL-safe characters: fits Telegram's 64-character
# /start payload with room to spare, and is not guessable like a user id.
REFERRAL_CODE_BYTES = 9
REFERRAL_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,32}")
_CODE_ATTEMPTS = 5
# A referral link opens the bot with ``/start ref_<code>``. Telegram allows a
# start parameter of 1-64 characters from [A-Za-z0-9_-].
REFERRAL_PAYLOAD_PREFIX = "ref_"
START_PAYLOAD_MAX_LENGTH = 64
# A bot's username: 5-32 characters, letters, digits and underscores.
BOT_USERNAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
# How far up a referral chain attribution looks for a loop. A longer chain is
# refused rather than trusted; none exists in practice.
MAX_CHAIN = 100


class SelfReferralError(LoyaltyError):
    """A customer tried to refer themselves."""


class ReferralLoopError(LoyaltyError):
    """The would-be referrer was themselves referred by this customer."""


@dataclass(frozen=True, slots=True)
class ReferralAttribution:
    referral: Referral
    created: bool  # False: the customer already had a referrer, which is kept


def generate_referral_code() -> str:
    return secrets.token_urlsafe(REFERRAL_CODE_BYTES)


def is_valid_referral_code(code: str | None) -> bool:
    return code is not None and REFERRAL_CODE_PATTERN.fullmatch(code) is not None


def referral_payload(code: str) -> str:
    """The ``/start`` payload that carries ``code``."""
    if not is_valid_referral_code(code):
        raise ValueError("Not a referral code")
    return f"{REFERRAL_PAYLOAD_PREFIX}{code}"


def parse_referral_payload(payload: str | None) -> str | None:
    """
    The referral code in a ``/start`` payload, or ``None`` for anything else.

    Never raises: a missing, overlong, foreign or malformed payload — anything
    a client can type after /start — simply carries no referral, and none of it
    reaches SQL.
    """
    if not payload or len(payload) > START_PAYLOAD_MAX_LENGTH:
        return None
    if not payload.startswith(REFERRAL_PAYLOAD_PREFIX):
        return None
    code = payload.removeprefix(REFERRAL_PAYLOAD_PREFIX)
    return code if is_valid_referral_code(code) else None


def referral_link(bot_username: str, code: str) -> str:
    """Telegram's deep link into the bot: ``https://t.me/<bot>?start=ref_<code>``."""
    username = bot_username.removeprefix("@")
    if not BOT_USERNAME_PATTERN.fullmatch(username):
        raise ValueError(f"Not a bot username: {bot_username!r}")
    return f"https://t.me/{username}?start={referral_payload(code)}"


class ReferralService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.referrals = ReferralRepository(session)
        self.accounts = LoyaltyAccountRepository(session)
        self.orders = OrderRepository(session)
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

        # One attribution at a time, to the end of the transaction. The loop
        # check below must see every referral — including one a concurrent
        # /start committed a moment ago, or two brand-new customers opening each
        # other's links at once would each become the other's referrer.
        await self.referrals.lock_attributions()
        existing = await self.referrals.get_by_referred_user_id(referred_user_id)
        if existing is not None:
            return ReferralAttribution(existing, created=False)

        if await self._leads_back_to(referrer_user_id, referred_user_id):
            raise ReferralLoopError(
                f"User {referred_user_id} is up user {referrer_user_id}'s referral chain; "
                "the referral would form a loop"
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

    async def _leads_back_to(self, referrer_user_id: int, target: int) -> bool:
        """
        Whether ``target`` is up ``referrer_user_id``'s referral chain.

        Refusing it keeps the referral graph free of cycles, of any length —
        which also keeps the account locks a payout takes free of cycles.
        """
        seen = {referrer_user_id}
        current = referrer_user_id
        for _ in range(MAX_CHAIN):
            referral = await self.referrals.get_by_referred_user_id(current)
            if referral is None:
                return False
            current = referral.referrer_user_id
            if current == target or current in seen:
                return True
            seen.add(current)
        return True

    async def qualify(self, referral_id: int, *, order_id: int) -> bool:
        """
        Mark a pending referral qualified by the referred customer's order.

        Owner decision: the order that qualifies a referral is a Completed one,
        charged more than €0, placed after the programme launched. Anything else
        is refused, so no bonus can ever be paid on an order that may still be
        cancelled. Returns ``True`` only for the call that made the change; an
        already qualified referral returns ``False`` and is left as it is.
        """
        referral = await self.referrals.get_for_update(referral_id)
        if referral is None:
            raise ValueError(f"Referral {referral_id} does not exist")
        order = await self.orders.get_by_id(order_id)
        if order is None or order.user_id != referral.referred_user_id:
            raise ValueError(
                f"Order {order_id} is not an order of referred user {referral.referred_user_id}"
            )
        if (
            order.status != OrderStatus.COMPLETED
            or not order.loyalty_eligible
            or order.total_price <= 0
        ):
            raise ValueError(f"Order {order_id} is not a completed, paid order placed after launch")
        if referral.status == ReferralStatus.QUALIFIED:
            return False

        referral.status = ReferralStatus.QUALIFIED
        referral.qualifying_order_id = order_id
        referral.qualified_at = datetime.now(UTC)
        await self.session.flush()
        return True
