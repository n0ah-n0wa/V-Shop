"""Render 👥 Invite a Friend for Telegram: the screen, the shared message, the news.

Every figure comes from the backend — :class:`Invitation` for the screen and the
shared message, :class:`ReferralPayout` for the payout news — and this module
only lays it out. Stamp counts go through :meth:`LocalizationService.plural`:
the amounts are configurable, and Russian and Ukrainian inflect them. An amount
configured as 0 is left out rather than promised.
"""

from __future__ import annotations

from app.services.localization import LocalizationService
from app.services.referral_program import Invitation, ReferralPayout
from app.utils.html import e


def format_invite(invitation: Invitation, i18n: LocalizationService) -> str:
    """
    The screen, in the order a customer reads it on a phone.

    1. the offer — what the friend gets, and what the customer gets;
    2. how it works, in three steps: who qualifies, and when rewards arrive;
    3. the link, which a tap copies;
    4. the friends it has brought in so far;
    5. a pointer to the 📤 button right below.
    """
    blocks = [
        [i18n.t("invite.title")],
        _offer(invitation, i18n),
        _steps(invitation, i18n),
        [i18n.t("invite.link", link=e(invitation.link))],
        _progress(invitation, i18n),
        [i18n.t("invite.cta")],
    ]
    return "\n\n".join("\n".join(lines) for lines in blocks)


def share_text(invitation: Invitation, i18n: LocalizationService) -> str:
    """What Telegram's share sheet sends under the link: the friend's side of the offer."""
    if invitation.referred_stamps:
        return i18n.t("invite.share_text", stamps=_stamps(i18n, invitation.referred_stamps))
    return i18n.t("invite.share_text_plain")


def own_link_note(i18n: LocalizationService) -> str:
    """For a customer who opened their own link: it works, and here is where to share it."""
    return i18n.t("invite.own_link", menu=i18n.t("menu.invite"))


def format_friend_joined(invitation: Invitation, i18n: LocalizationService) -> str:
    """News for the referrer: a friend joined, and what their first order will pay."""
    lines = [i18n.t("invite.news.joined")]
    stamps, spin = invitation.referrer_stamps, invitation.referrer_spins > 0
    if stamps and spin:
        lines.append(i18n.t("invite.news.joined_stamps_spin", stamps=_stamps(i18n, stamps)))
    elif stamps:
        lines.append(i18n.t("invite.news.joined_stamps", stamps=_stamps(i18n, stamps)))
    elif spin:
        lines.append(i18n.t("invite.news.joined_spin"))
    return "\n".join(lines)


def format_payout_for_referrer(payout: ReferralPayout, i18n: LocalizationService) -> str:
    """News for the referrer: the friend's first order completed, and what it paid them."""
    lines = [i18n.t("invite.news.paid")]
    if payout.referrer_stamps:
        stamps = _stamps(i18n, payout.referrer_stamps)
        lines.append(i18n.t("invite.news.stamps_added", stamps=stamps))
    if payout.spin_granted:
        lines.append(i18n.t("invite.news.spin_added"))
    return "\n".join([*lines, "", i18n.t("invite.news.more")])


def format_payout_for_friend(payout: ReferralPayout, i18n: LocalizationService) -> str:
    """News for the invited friend: their welcome stamps, and a nudge to invite in turn."""
    return "\n".join(
        [
            i18n.t("invite.news.welcome_bonus"),
            i18n.t("invite.news.stamps_added", stamps=_stamps(i18n, payout.referred_stamps)),
            "",
            i18n.t("invite.news.your_turn", menu=i18n.t("menu.invite")),
        ]
    )


def _offer(invitation: Invitation, i18n: LocalizationService) -> list[str]:
    friend, you = invitation.referred_stamps, invitation.referrer_stamps
    if friend and you:
        offer = i18n.t("invite.offer", friend=_stamps(i18n, friend), you=_stamps(i18n, you))
    elif friend:
        offer = i18n.t("invite.offer_friend", friend=_stamps(i18n, friend))
    elif you:
        offer = i18n.t("invite.offer_you", you=_stamps(i18n, you))
    else:  # nothing is configured to pay out: the link is still worth sharing
        offer = i18n.t("invite.offer_plain")
    if not invitation.referrer_spins:
        return [offer]
    return [offer, i18n.t("invite.spin_bonus" if you else "invite.spin")]


def _steps(invitation: Invitation, i18n: LocalizationService) -> list[str]:
    """Who qualifies, what they do, and when the rewards arrive — no manual needed."""
    steps = [i18n.t("invite.how_title"), i18n.t("invite.step_send"), i18n.t("invite.step_order")]
    friend_gets = invitation.referred_stamps > 0
    you_get = invitation.referrer_stamps > 0 or invitation.referrer_spins > 0
    if friend_gets and you_get:
        steps.append(i18n.t("invite.step_both"))
    elif friend_gets:
        steps.append(i18n.t("invite.step_friend"))
    elif you_get:
        steps.append(i18n.t("invite.step_you"))
    return steps


def _progress(invitation: Invitation, i18n: LocalizationService) -> list[str]:
    if not invitation.invited:
        return [i18n.t("invite.no_friends")]
    lines = [i18n.t("invite.stats", invited=invitation.invited, rewarded=invitation.rewarded)]
    if invitation.waiting:
        lines.append(i18n.t("invite.waiting", waiting=invitation.waiting))
    return lines


def _stamps(i18n: LocalizationService, count: int) -> str:
    return i18n.plural("invite.stamps", count)
