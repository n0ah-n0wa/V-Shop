"""Roulette spin entitlements — which activity earns a spin, and when.

Three sources, each configurable (``ROULETTE_*`` and ``REFERRAL_SPINS``):

* **the welcome spin** — one per customer, ever. Granted at ``/start``, and
  topped up for every customer still without one each time the bot starts, so
  existing customers, new ones and anyone missed in between all end up with
  exactly one;
* **purchases** — the order that is a customer's Nth, 2Nth, … qualifying
  purchase earns a spin, decided when that order completes. A qualifying
  purchase is a purchase row in the stamp ledger: a Completed order, placed
  after the loyalty launch, charged more than €0;
* **referrals** — the referrer's spin once a referral qualifies.

Every grant is a ``roulette_spin_grants`` row naming its reason and its source
(an order or a referral) — the audit trail — and a unique constraint per source
lets each grant at most once, so replays, restarts and races cannot duplicate
one. Spending grants is :class:`~app.services.roulette.RouletteService`'s job.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.enums import SpinGrantReason
from app.models.roulette import RouletteSpinGrant
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.repositories.referral import ReferralRepository
from app.repositories.roulette_spin_grant import RouletteSpinGrantRepository
from app.services.loyalty import LoyaltyService
from app.services.roulette import RouletteService, SpinGrantResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpinPolicy:
    """Which activity earns a roulette spin, as configured. Validated on construction."""

    initial_free_spin: bool
    every_n_purchases: int  # 0: purchases earn no spins
    referral_spins: int  # 0 or 1

    def __post_init__(self) -> None:
        if self.every_n_purchases < 0:
            raise ValueError("every_n_purchases cannot be negative")
        if self.referral_spins not in (0, 1):
            raise ValueError("referral_spins must be 0 or 1")

    @classmethod
    def from_settings(cls, settings: Settings) -> SpinPolicy:
        return cls(
            initial_free_spin=settings.roulette_initial_free_spin,
            every_n_purchases=settings.roulette_spin_every_n_purchases,
            referral_spins=settings.referral_spins,
        )

    @classmethod
    def defaults(cls) -> SpinPolicy:
        """The configuration defaults — one source of truth, no environment read."""
        fields = Settings.model_fields
        return cls(
            initial_free_spin=fields["roulette_initial_free_spin"].default,
            every_n_purchases=fields["roulette_spin_every_n_purchases"].default,
            referral_spins=fields["referral_spins"].default,
        )

    def is_purchase_milestone(self, purchase_number: int) -> bool:
        """The 5th, 10th, 15th … qualifying purchase, with the default interval."""
        every = self.every_n_purchases
        return every > 0 and purchase_number > 0 and purchase_number % every == 0

    def purchases_to_next_spin(self, purchases: int) -> int | None:
        """Qualifying purchases still needed for the next spin; ``None`` if purchases earn none."""
        every = self.every_n_purchases
        if every == 0:
            return None
        return every - max(purchases, 0) % every


@dataclass(frozen=True, slots=True)
class SpinBalance:
    """A customer's spins: what is left, what was spent, and where each came from."""

    available: int
    used: int
    granted_by_reason: Mapping[SpinGrantReason, int]

    @property
    def granted(self) -> int:
        return self.available + self.used


class SpinEntitlementService:
    def __init__(self, session: AsyncSession, policy: SpinPolicy | None = None) -> None:
        self.session = session
        self.policy = policy or SpinPolicy.defaults()
        self.roulette = RouletteService(session)
        self.grants = RouletteSpinGrantRepository(session)
        self.transactions = LoyaltyTransactionRepository(session)
        self.referrals = ReferralRepository(session)
        self.loyalty = LoyaltyService(session)

    # --- the welcome spin ---------------------------------------------------------

    async def grant_welcome_spin(self, user_id: int) -> SpinGrantResult | None:
        """This customer's welcome spin; every later call returns the same grant."""
        if not self.policy.initial_free_spin:
            return None
        return _logged(await self.roulette.grant_initial_spin(user_id))

    async def grant_missing_welcome_spins(self) -> int:
        """
        Give the welcome spin to every customer who has none. Returns how many.

        One statement, safe on every start: a customer who has one — spent or
        not — is skipped, and the unique index absorbs a grant ``/start`` makes
        at the same moment, so nobody ever gets a second.
        """
        if not self.policy.initial_free_spin:
            return 0
        created = await self.grants.insert_missing_initial_grants()
        if created:
            logger.info("Welcome spins granted to %s customers who had none", created)
        return created

    # --- purchases ----------------------------------------------------------------

    async def grant_for_completed_order(self, order_id: int) -> SpinGrantResult | None:
        """
        The spin a milestone purchase earned, or ``None`` if the order earned none.

        Only an order the stamp ledger booked as a qualifying purchase counts,
        numbered among that customer's purchases; with the default interval the
        5th, 10th, … earns a spin. Keyed on the order, so a replayed completion
        returns the existing grant instead of adding one. The interval in force
        when the order completes decides — changing it never grants for the past.
        """
        if self.policy.every_n_purchases == 0:
            return None
        purchase = await self.transactions.get_purchase_for_order(order_id)
        if purchase is None:
            return None
        number = await self.transactions.purchase_number(purchase.user_id, purchase.id)
        if not self.policy.is_purchase_milestone(number):
            return None
        return _logged(
            await self.roulette.grant_purchase_milestone_spin(purchase.user_id, order_id=order_id)
        )

    # --- referrals ----------------------------------------------------------------

    async def grant_for_referral(self, referral_id: int) -> SpinGrantResult | None:
        """
        The referrer's spin for a qualified referral (owner decision), if configured.

        Once per referral: a replay returns the existing grant. A referral that
        has not qualified is refused with ``ValueError``.
        """
        if self.policy.referral_spins == 0:
            return None
        referral = await self.referrals.get_by_id(referral_id)
        if referral is None:
            raise ValueError(f"Referral {referral_id} does not exist")
        return _logged(
            await self.roulette.grant_referral_spin(
                referral.referrer_user_id, referral_id=referral_id
            )
        )

    # --- reading ------------------------------------------------------------------

    async def balance(self, user_id: int) -> SpinBalance:
        """Read-only: counting spins never creates one."""
        granted: dict[SpinGrantReason, int] = {}
        used = 0
        for reason, total, spent in await self.grants.tally_for_user(user_id):
            granted[SpinGrantReason(reason)] = total
            used += spent
        return SpinBalance(
            available=sum(granted.values()) - used,
            used=used,
            granted_by_reason=MappingProxyType(granted),
        )

    async def history(self, user_id: int) -> list[RouletteSpinGrant]:
        """Every grant, oldest first: its reason, its source, when granted and spent."""
        return await self.grants.list_for_user(user_id)

    async def purchases_to_next_spin(self, user_id: int) -> int | None:
        """
        Read-only: qualifying purchases this customer still needs for their next spin.

        ``None`` when purchases earn no spins. Counted from the purchases the
        stamp ledger booked — the same count milestones are numbered from.
        """
        if self.policy.every_n_purchases == 0:
            return None
        return self.policy.purchases_to_next_spin(await self.loyalty.purchase_count(user_id))


def _logged(result: SpinGrantResult) -> SpinGrantResult:
    if result.created:
        grant = result.grant
        logger.info(
            "Spin granted user_id=%s reason=%s order_id=%s referral_id=%s grant_id=%s",
            grant.user_id,
            grant.reason,
            grant.order_id,
            grant.referral_id,
            grant.id,
        )
    return result
