"""News for both sides of a referral: a friend joined, and the rewards landed.

Like order-status notifications, the news follows the write it reports and never
reaches back into it: callers commit first and call this last, and every failure
— Telegram's, or the read behind a message — is logged and swallowed here, so
news can neither undo nor hold up what it reports.

No message names the other side: the referrer is never told who joined or who
ordered, and nothing typed after /start is echoed to anyone. Nothing here
messages the newcomer at /start either — their replies stay exactly those of a
plain /start (see ``cmd_start``), so nothing tells a guesser a code was real.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keyboards.invite import share_keyboard
from app.models.referral import Referral
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.localization import LocalizationService
from app.services.referral_program import Invitation, ReferralPolicy, ReferralProgramService
from app.services.spin_entitlement import SpinPolicy
from app.utils.invite_display import (
    format_friend_joined,
    format_payout_for_friend,
    format_payout_for_referrer,
    share_text,
)

logger = logging.getLogger(__name__)

# Who to tell, what, and the button that comes with it.
News = tuple[User, str, InlineKeyboardMarkup | None]


class ReferralNotificationService:
    def __init__(self, session: AsyncSession, bot: Bot, *, settings: Settings) -> None:
        self.session = session
        self.bot = bot
        self.users = UserRepository(session)
        self.programme = ReferralProgramService(
            session,
            ReferralPolicy.from_settings(settings),
            spin_policy=SpinPolicy.from_settings(settings),
        )

    async def friend_joined(self, referral: Referral) -> bool:
        """
        Tell the referrer a friend arrived through their link, and what it will pay.

        Call once the attribution is committed. Returns whether it was delivered.
        """
        try:
            news = await self._joined(referral)
        except Exception:
            await self._give_up(f"friend joined referral_id={referral.id}")
            return False
        return news is not None and await self._deliver(news)

    async def rewards_paid(self, order_id: int) -> int:
        """
        Tell each side of the referral ``order_id`` qualified what it was paid.

        Call once the completion is committed. The amounts are read back from the
        ledger; an order that qualified nothing sends nothing, and a side that
        earned nothing is not messaged. Returns how many messages were delivered.
        """
        try:
            news = await self._paid(order_id)
        except Exception:
            await self._give_up(f"rewards paid order_id={order_id}")
            return 0
        delivered = 0
        for item in news:
            delivered += await self._deliver(item)
        return delivered

    async def _joined(self, referral: Referral) -> News | None:
        referrer = await self.users.get_by_id(referral.referrer_user_id)
        if referrer is None:
            return None
        invitation = await self._invitation(referrer.id)
        if invitation is None:  # cannot happen: the friend came in through the referrer's code
            return None
        i18n = LocalizationService.from_user(referrer)
        return referrer, format_friend_joined(invitation, i18n), self._send(invitation, i18n)

    async def _paid(self, order_id: int) -> list[News]:
        payout = await self.programme.payout_for_order(order_id)
        if payout is None:
            return []
        news: list[News] = []
        friend = await self.users.get_by_id(payout.referral.referred_user_id)
        if friend is not None and payout.referred_stamps:
            i18n = LocalizationService.from_user(friend)
            news.append((friend, format_payout_for_friend(payout, i18n), None))
        referrer = await self.users.get_by_id(payout.referral.referrer_user_id)
        if referrer is not None and (payout.referrer_stamps or payout.spin_granted):
            i18n = LocalizationService.from_user(referrer)
            invitation = await self._invitation(referrer.id)
            text = format_payout_for_referrer(payout, i18n)
            news.append((referrer, text, self._send(invitation, i18n)))
        return news

    async def _invitation(self, user_id: int) -> Invitation | None:
        """The referrer's invitation, read without a lock: none is held while news goes out."""
        me = await self.bot.me()  # cached getMe: the link needs the bot's username
        return await self.programme.existing_invitation(user_id, bot_username=me.username or "")

    @staticmethod
    def _send(
        invitation: Invitation | None, i18n: LocalizationService
    ) -> InlineKeyboardMarkup | None:
        """📤 Send to a friend, with the referrer's own link: the next invite in one tap."""
        if invitation is None:
            return None
        return share_keyboard(i18n, link=invitation.link, share_text=share_text(invitation, i18n))

    async def _give_up(self, what: str) -> None:
        logger.exception("Referral news not prepared: %s", what)
        # The caller committed what the news was about; only the failed read goes.
        await self.session.rollback()

    async def _deliver(self, news: News) -> bool:
        user, text, markup = news
        try:
            await self.bot.send_message(chat_id=user.telegram_id, text=text, reply_markup=markup)
        except TelegramForbiddenError:
            logger.info("Referral news skipped: bot blocked or account gone user_id=%s", user.id)
            return False
        except TelegramAPIError:
            logger.warning("Referral news not delivered user_id=%s", user.id, exc_info=True)
            return False
        except Exception:
            logger.exception("Unexpected error sending referral news user_id=%s", user.id)
            return False
        logger.info("Referral news delivered user_id=%s", user.id)
        return True
