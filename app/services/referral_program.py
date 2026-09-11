"""The referral programme — bringing a friend in, and paying both sides once.

Owner decision: a referral is attributed when a brand-new customer opens the bot
through a friend's link (``/start ref_<code>``), and pays out when that
customer's first completed, paid order completes — they get
``REFERRED_USER_START_STAMPS``, the referrer ``REFERRAL_REWARD_STAMPS`` and, if
``REFERRAL_SPINS`` allows, a roulette spin. Paying at the first real purchase
rather than at sign-up means a bonus is never earned by opening the bot from a
second Telegram account.

* **Attribution** (:meth:`ReferralProgramService.attribute_from_start`) takes
  whatever followed ``/start`` and never raises for anything a client can send:
  a malformed or unknown code, the customer's own code, a customer who already
  has a referrer, one who has ordered before, a referral that would close a
  loop — each is an outcome, and nothing is written. "Brand-new" means the
  customer has never placed an order.
* **Payout** (:meth:`ReferralProgramService.settle_for_completed_order`) runs in
  the transaction that completes the order. Every part is keyed on the referral
  — its qualifying order, one ledger row per side, one spin — so a replayed
  completion, a second order or a race pays nothing more.

Codes are random (``secrets.token_urlsafe``), stored once per customer, and say
nothing about who owns them. They do not expire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.enums import OrderStatus, ReferralStatus
from app.models.referral import Referral
from app.repositories.order import OrderRepository
from app.services.loyalty import LoyaltyService
from app.services.referral import (
    ReferralLoopError,
    ReferralService,
    parse_referral_payload,
    referral_link,
)
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReferralPolicy:
    """What a qualified referral pays each side, as configured."""

    referrer_stamps: int
    referred_stamps: int

    def __post_init__(self) -> None:
        if self.referrer_stamps < 0 or self.referred_stamps < 0:
            raise ValueError("Referral stamps cannot be negative")

    @classmethod
    def from_settings(cls, settings: Settings) -> ReferralPolicy:
        return cls(
            referrer_stamps=settings.referral_reward_stamps,
            referred_stamps=settings.referred_user_start_stamps,
        )

    @classmethod
    def defaults(cls) -> ReferralPolicy:
        """The configuration defaults — one source of truth, no environment read."""
        fields = Settings.model_fields
        return cls(
            referrer_stamps=fields["referral_reward_stamps"].default,
            referred_stamps=fields["referred_user_start_stamps"].default,
        )


class ReferralOutcome(StrEnum):
    """What a ``/start`` payload did."""

    ATTRIBUTED = "attributed"
    ALREADY_REFERRED = "already_referred"  # the first referrer is kept, forever
    NOT_NEW_CUSTOMER = "not_new_customer"
    SELF_REFERRAL = "self_referral"
    LOOP = "loop"
    UNKNOWN_CODE = "unknown_code"
    NOT_A_REFERRAL = "not_a_referral"  # no payload, a malformed one, or another kind


@dataclass(frozen=True, slots=True)
class ReferralAttempt:
    outcome: ReferralOutcome
    referral: Referral | None = None  # the customer's referral, when they have one


@dataclass(frozen=True, slots=True)
class ReferralPayout:
    referral: Referral
    referred_stamps: int
    referrer_stamps: int
    spin_granted: bool


@dataclass(frozen=True, slots=True)
class Invitation:
    """What a customer's invite screen shows — every figure from the backend."""

    link: str  # https://t.me/<bot>?start=ref_<code>: a random code, never an id
    referrer_stamps: int
    referred_stamps: int
    referrer_spins: int
    invited: int  # friends who joined through the link
    rewarded: int  # of those, the ones whose first order has paid out

    @property
    def waiting(self) -> int:
        """Friends who joined but whose first order has not completed yet."""
        return self.invited - self.rewarded


class ReferralProgramService:
    def __init__(
        self,
        session: AsyncSession,
        policy: ReferralPolicy | None = None,
        *,
        spin_policy: SpinPolicy | None = None,
    ) -> None:
        self.session = session
        self.policy = policy or ReferralPolicy.defaults()
        self.referrals = ReferralService(session)
        self.loyalty = LoyaltyService(session)
        self.orders = OrderRepository(session)
        self.spins = SpinEntitlementService(session, spin_policy)

    # --- the customer's link ------------------------------------------------------

    async def referral_code(self, user_id: int) -> str:
        """The customer's personal code: created on first request, then stable."""
        return await self.referrals.get_or_create_referral_code(user_id)

    async def referral_link(self, user_id: int, *, bot_username: str) -> str:
        """The customer's personal deep link into the bot."""
        return referral_link(bot_username, await self.referral_code(user_id))

    async def invitation(self, user_id: int, *, bot_username: str) -> Invitation:
        """
        The customer's link, what it pays each side, and the friends it brought in.

        Creates the customer's code on first use — under their account lock, so
        the caller commits before showing the link — and is read-only after.
        """
        code = await self.referral_code(user_id)
        return await self._invitation(user_id, code, bot_username=bot_username)

    async def existing_invitation(self, user_id: int, *, bot_username: str) -> Invitation | None:
        """
        Read-only: the customer's invitation, if they already have a code.

        Takes no lock and creates nothing — safe to use while talking to
        Telegram, as referral news does. ``None`` for a customer without a code.
        """
        code = await self.referrals.referral_code(user_id)
        if code is None:
            return None
        return await self._invitation(user_id, code, bot_username=bot_username)

    async def _invitation(self, user_id: int, code: str, *, bot_username: str) -> Invitation:
        invited, rewarded = await self.referrals.counts_for_referrer(user_id)
        return Invitation(
            link=referral_link(bot_username, code),
            referrer_stamps=self.policy.referrer_stamps,
            referred_stamps=self.policy.referred_stamps,
            referrer_spins=self.spins.policy.referral_spins,
            invited=invited,
            rewarded=rewarded,
        )

    async def payout_for_order(self, order_id: int) -> ReferralPayout | None:
        """
        Read-only: what the referral ``order_id`` qualified paid each side.

        ``None`` unless the order qualified one. The stamps and the spin are read
        back from the ledger and the spin grants — what was booked, whatever the
        settings say now.
        """
        referral = await self.referrals.get_qualified_by_order(order_id)
        if referral is None:
            return None
        return ReferralPayout(
            referral=referral,
            referred_stamps=await self.loyalty.referral_bonus(
                referral.referred_user_id, referral_id=referral.id
            ),
            referrer_stamps=await self.loyalty.referral_bonus(
                referral.referrer_user_id, referral_id=referral.id
            ),
            spin_granted=await self.spins.has_referral_spin(
                referral.id, user_id=referral.referrer_user_id
            ),
        )

    # --- attribution --------------------------------------------------------------

    async def attribute_from_start(self, user_id: int, payload: str | None) -> ReferralAttempt:
        """
        Attribute a brand-new customer to the friend whose link they opened.

        ``payload`` is whatever followed ``/start``, untrusted. Only a pending
        referral is written — nothing is paid here — and a customer keeps their
        first referrer for good.
        """
        code = parse_referral_payload(payload)
        if code is None:
            return ReferralAttempt(ReferralOutcome.NOT_A_REFERRAL)
        referrer_id = await self.referrals.find_referrer_user_id(code)
        if referrer_id is None:
            return ReferralAttempt(ReferralOutcome.UNKNOWN_CODE)
        if referrer_id == user_id:
            return ReferralAttempt(ReferralOutcome.SELF_REFERRAL)
        # Placing an order takes this lock too: an order being placed right now
        # has either committed before the check below, or waits for this
        # attribution — a customer is never attributed on top of their first order.
        await self.loyalty.lock_account(user_id)
        existing = await self.referrals.get_for_referred_user(user_id)
        if existing is not None:
            return ReferralAttempt(ReferralOutcome.ALREADY_REFERRED, existing)
        if await self.orders.has_any_for_user(user_id):
            return ReferralAttempt(ReferralOutcome.NOT_NEW_CUSTOMER)
        try:
            attribution = await self.referrals.attribute(
                referrer_user_id=referrer_id, referred_user_id=user_id
            )
        except ReferralLoopError:
            return ReferralAttempt(ReferralOutcome.LOOP)
        if not attribution.created:  # a concurrent /start attributed them first
            return ReferralAttempt(ReferralOutcome.ALREADY_REFERRED, attribution.referral)
        logger.info(
            "Referral attributed referral_id=%s referrer_user_id=%s referred_user_id=%s",
            attribution.referral.id,
            referrer_id,
            user_id,
        )
        return ReferralAttempt(ReferralOutcome.ATTRIBUTED, attribution.referral)

    # --- payout -------------------------------------------------------------------

    async def settle_for_completed_order(self, order_id: int) -> ReferralPayout | None:
        """
        Pay a referral out on the referred customer's first qualifying order.

        Called when an order completes, inside that transaction. A Completed,
        paid, post-launch order of a customer with a pending referral qualifies
        it — under the referral's row lock, so exactly one order ever does —
        and then each side gets its stamps and the referrer the configured spin.
        Anything else returns ``None`` and writes nothing.
        """
        order = await self.orders.get_by_id(order_id)
        if order is None:
            raise LookupError(f"Order {order_id} does not exist")
        if (
            order.status != OrderStatus.COMPLETED
            or not order.loyalty_eligible
            or order.total_price <= 0
        ):
            return None
        referral = await self.referrals.get_for_referred_user(order.user_id)
        if referral is None or referral.status != ReferralStatus.PENDING:
            return None
        if not await self.referrals.qualify(referral.id, order_id=order.id):
            return None  # another completion qualified it first

        policy = self.policy
        if policy.referred_stamps:
            await self.loyalty.credit_referral(
                referral.referred_user_id, referral_id=referral.id, stamps=policy.referred_stamps
            )
        if policy.referrer_stamps:
            await self.loyalty.credit_referral(
                referral.referrer_user_id, referral_id=referral.id, stamps=policy.referrer_stamps
            )
        spin = await self.spins.grant_for_referral(referral.id)
        logger.info(
            "Referral paid out referral_id=%s order_id=%s referred_stamps=%s "
            "referrer_stamps=%s spin=%s",
            referral.id,
            order.id,
            policy.referred_stamps,
            policy.referrer_stamps,
            spin is not None,
        )
        return ReferralPayout(
            referral=referral,
            referred_stamps=policy.referred_stamps,
            referrer_stamps=policy.referrer_stamps,
            spin_granted=spin is not None,
        )
