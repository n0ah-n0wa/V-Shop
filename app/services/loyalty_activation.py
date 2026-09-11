"""Loyalty activation — every customer has an account and their one welcome spin.

Two paths lead there, and both are safe to repeat:

* **new customers** — ``UserService.ensure_user`` opens the account when it
  registers the user (beside the cart), and ``/start`` grants the welcome spin;
* **existing customers** — :meth:`LoyaltyActivationService.activate_everyone`
  backfills anyone still missing either, at every bot start (``app.lifecycle``).
  The loyalty migration (``3b9d6f2a8c14``) ran the same backfill once, at
  launch; this catches up anyone registered or left behind since.

Each step is one ``INSERT … SELECT … ON CONFLICT DO NOTHING`` over the users
missing the row, behind a unique constraint (``loyalty_accounts.user_id``; one
``initial_promo`` grant per user) that makes a second row impossible — so
repeated runs, restarts, redeploys and two instances starting at once never
duplicate anything. Existing rows are never updated: balances, referral codes
and welcome spins spent long ago stay exactly as they are.

Orders are never read or written. Orders placed before launch carry
``loyalty_eligible = false`` (migration ``8e4c1a7b2d95``) and so never earn
purchase stamps; nothing here books any.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.loyalty_account import LoyaltyAccountRepository
from app.services.spin_entitlement import SpinEntitlementService, SpinPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActivationReport:
    """What one activation added: nothing, once every customer is in."""

    accounts_opened: int
    welcome_spins_granted: int


class LoyaltyActivationService:
    def __init__(self, session: AsyncSession, spin_policy: SpinPolicy | None = None) -> None:
        self.session = session
        self.accounts = LoyaltyAccountRepository(session)
        self.spins = SpinEntitlementService(session, spin_policy)

    async def activate_everyone(self) -> ActivationReport:
        """
        Open every missing account, then grant every missing welcome spin.

        Repeatable: a second run adds nothing. Welcome spins follow
        ``ROULETTE_INITIAL_FREE_SPIN``; accounts are opened regardless.
        """
        accounts = await self.accounts.insert_missing_accounts()
        if accounts:
            logger.info("Loyalty accounts opened for %s customers who had none", accounts)
        spins = await self.spins.grant_missing_welcome_spins()
        return ActivationReport(accounts_opened=accounts, welcome_spins_granted=spins)
