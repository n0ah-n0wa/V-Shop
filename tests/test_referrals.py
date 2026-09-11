"""
The referral programme — personal links, attribution at /start, and the payout.

Attribution runs through the real /start handler with a real ``CommandObject``;
the payout through the admin's real order completion. Owner decision: a
brand-new customer is attributed when they open a friend's link, and both sides
are paid at that customer's first completed, paid order — never at sign-up.
Concurrent attempts are raced on PostgreSQL in ``tests/test_loyalty_postgres.py``.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.user.start import cmd_start
from app.models.enums import (
    LoyaltyTransactionType,
    OrderStatus,
    ReferralStatus,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.order import Order
from app.models.referral import Referral
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
from app.repositories.referral import ReferralRepository
from app.repositories.user import UserRepository
from app.services import referral as referral_module
from app.services.admin import AdminService
from app.services.loyalty import LoyaltyService
from app.services.referral import (
    ReferralService,
    generate_referral_code,
    parse_referral_payload,
    referral_link,
    referral_payload,
)
from app.services.referral_program import (
    ReferralOutcome,
    ReferralPolicy,
    ReferralProgramService,
)
from app.services.roulette import RouletteService
from app.services.spin_entitlement import SpinEntitlementService
from tests.factories import make_order, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
CODE = "Abc-def_1234"

# Everything a client can type after /start that is not a referral.
MALFORMED = [
    "ref_",
    "ref_short",
    "REF_Abcdefgh1234",
    "Ref_Abcdefgh1234",
    "Abcdefgh1234",  # a code without its prefix
    "promo_Abcdefgh1234",
    " ref_Abcdefgh1234",
    "ref_Abcdefgh1234 ",
    "ref_Abc def1234",
    "ref_Abcdefgh1234\n",
    "ref_Abcdefgh1234\x00",
    "ref_абвгдежзий",
    "ref_١٢٣٤٥٦٧٨٩",
    "ref_' OR 1=1 --",
    "ref_%00%00%00%00",
    "ref_../../etc/passwd",
    "ref_" + "a" * 33,
    "ref_" + "a" * 61,
]


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        **overrides,
    )


class Chatting(Message):
    """A /start message; records what the bot answered."""

    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        self.sent.append(text)
        return self

    @property
    def sent(self) -> list[str]:
        return self.__dict__.setdefault("answers", [])


async def send_start(
    session: AsyncSession,
    telegram_id: int,
    payload: str | None = None,
    *,
    settings: Settings | None = None,
) -> Chatting:
    """The customer sends /start — through a link when ``payload`` is given."""
    message = Chatting(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=TgUser(id=telegram_id, is_bot=False, first_name="Friend"),
        text="/start" if payload is None else f"/start {payload}",
    )
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
    )
    await cmd_start(
        message=message,
        state=state,
        session=session,
        settings=settings or configured(),
        command=CommandObject(prefix="/", command="start", args=payload),
    )
    return message


async def user_by_telegram(session: AsyncSession, telegram_id: int) -> User:
    user = await UserRepository(session).get_by_telegram_id(telegram_id)
    assert user is not None
    return user


async def referrer_with_link(session: AsyncSession, telegram_id: int) -> tuple[User, str]:
    """A customer, and the /start payload of their personal link."""
    user = await make_user(session, telegram_id=telegram_id)
    return user, referral_payload(await ReferralProgramService(session).referral_code(user.id))


async def attributed_pair(session: AsyncSession, telegram_id: int) -> tuple[User, User]:
    """A referrer, and a brand-new friend who arrived through their link."""
    referrer, payload = await referrer_with_link(session, telegram_id)
    await send_start(session, telegram_id + 1, payload)
    return referrer, await user_by_telegram(session, telegram_id + 1)


async def referral_of(session: AsyncSession, user_id: int) -> Referral | None:
    return await ReferralService(session).get_for_referred_user(user_id)


async def complete(
    session: AsyncSession,
    user: User,
    *,
    total: str = "20.00",
    eligible: bool = True,
    settings: Settings | None = None,
) -> Order:
    """An order the admin takes all the way to Completed."""
    order = await make_order(session, user)
    order.total_price = Decimal(total)
    order.loyalty_eligible = eligible
    admin = AdminService(session, settings=settings or configured())
    for status in TO_COMPLETED:
        order = await admin.set_order_status(order, status)
    return order


async def bonuses(session: AsyncSession, user_id: int) -> list[int]:
    """The referral bonuses in the customer's stamp ledger."""
    rows = await session.scalars(
        select(LoyaltyTransaction.amount)
        .where(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.kind == LoyaltyTransactionType.REFERRAL,
        )
        .order_by(LoyaltyTransaction.id)
    )
    return list(rows.all())


async def referral_spins(session: AsyncSession, user_id: int | None = None) -> int:
    statement = (
        select(func.count())
        .select_from(RouletteSpinGrant)
        .where(RouletteSpinGrant.reason == SpinGrantReason.REFERRAL)
    )
    if user_id is not None:
        statement = statement.where(RouletteSpinGrant.user_id == user_id)
    return int(await session.scalar(statement) or 0)


async def count_rows(session: AsyncSession, model: type[Any]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ============================================================== personal link


async def test_every_customer_gets_one_stable_personal_code(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=900000001)
    programme = ReferralProgramService(session)

    first = await programme.referral_code(user.id)
    again = await programme.referral_code(user.id)

    assert first == again
    assert re.fullmatch(r"[A-Za-z0-9_-]{12}", first), "12 URL-safe characters"
    account = (await session.scalars(select(LoyaltyAccount))).one()
    assert account.referral_code == first, "stored with the customer, once"


async def test_codes_are_unique_and_reveal_nothing(session: AsyncSession) -> None:
    users = [await make_user(session, telegram_id=900000100 + i) for i in range(25)]
    programme = ReferralProgramService(session)

    codes = [await programme.referral_code(user.id) for user in users]

    assert len(set(codes)) == len(codes)
    for user, code in zip(users, codes, strict=True):
        assert str(user.telegram_id) not in code, "no Telegram id inside"
        assert code != str(user.id), "no database id either"


def test_codes_come_from_the_operating_systems_randomness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn: list[int] = []

    def token_urlsafe(nbytes: int) -> str:
        drawn.append(nbytes)
        return "Abcdefgh1234"

    monkeypatch.setattr(referral_module.secrets, "token_urlsafe", token_urlsafe)

    assert generate_referral_code() == "Abcdefgh1234"
    assert drawn == [9], "72 random bits"


def test_the_link_is_a_telegram_deep_link() -> None:
    assert referral_link("VShopBot", CODE) == f"https://t.me/VShopBot?start=ref_{CODE}"
    assert referral_link("@VShopBot", CODE) == referral_link("VShopBot", CODE)
    payload = referral_payload(CODE)
    assert len(payload) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", payload), (
        "a valid Telegram start parameter"
    )


@pytest.mark.parametrize(
    "username",
    ["", "bot", "V Shop Bot", "vshop.bot", "x" * 33, "https://evil.example", "1VShopBot"],
)
def test_a_link_needs_a_real_bot_username(username: str) -> None:
    with pytest.raises(ValueError):
        referral_link(username, CODE)


@pytest.mark.parametrize("code", ["", "short", "has space12", "x" * 33, "a/b/c/d/e/f"])
def test_a_link_needs_a_real_code(code: str) -> None:
    with pytest.raises(ValueError):
        referral_link("VShopBot", code)


async def test_the_programme_builds_the_customers_own_link(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=900000002)
    programme = ReferralProgramService(session)

    link = await programme.referral_link(user.id, bot_username="VShopBot")

    assert link == f"https://t.me/VShopBot?start=ref_{await programme.referral_code(user.id)}"


# ================================================================ deep links


def test_a_referral_payload_parses_to_its_code() -> None:
    assert parse_referral_payload(f"ref_{CODE}") == CODE
    assert parse_referral_payload(referral_payload(CODE)) == CODE


@pytest.mark.parametrize("payload", [None, "", *MALFORMED])
def test_a_malformed_payload_carries_no_referral(payload: str | None) -> None:
    assert parse_referral_payload(payload) is None


# =============================================================== attribution


async def test_a_new_customer_arriving_through_a_link_is_attributed(session: AsyncSession) -> None:
    referrer, payload = await referrer_with_link(session, 900000010)

    message = await send_start(session, 900000011, payload)

    friend = await user_by_telegram(session, 900000011)
    referral = await referral_of(session, friend.id)
    assert referral is not None
    assert (referral.referrer_user_id, referral.status) == (referrer.id, ReferralStatus.PENDING)
    assert message.sent, "onboarding carries on as usual"


async def test_nothing_is_paid_at_sign_up(session: AsyncSession) -> None:
    """Owner decision: bonuses follow the friend's first paid order, not the link."""
    _, friend = await attributed_pair(session, 900000012)

    assert (await referral_of(session, friend.id)) is not None
    assert await count_rows(session, LoyaltyTransaction) == 0
    assert await referral_spins(session) == 0


async def test_repeated_start_through_the_same_link_attributes_once(
    session: AsyncSession,
) -> None:
    referrer, payload = await referrer_with_link(session, 900000014)

    for _ in range(3):
        await send_start(session, 900000015, payload)

    assert await count_rows(session, Referral) == 1


async def test_a_later_link_never_changes_the_referrer(session: AsyncSession) -> None:
    first, first_payload = await referrer_with_link(session, 900000016)
    _, second_payload = await referrer_with_link(session, 900000017)
    await send_start(session, 900000018, first_payload)
    friend = await user_by_telegram(session, 900000018)

    attempt = await ReferralProgramService(session).attribute_from_start(friend.id, second_payload)
    await send_start(session, 900000018, second_payload)

    assert attempt.outcome == ReferralOutcome.ALREADY_REFERRED
    referral = await referral_of(session, friend.id)
    assert referral is not None and referral.referrer_user_id == first.id
    assert await count_rows(session, Referral) == 1


async def test_a_customer_cannot_refer_themselves(session: AsyncSession) -> None:
    user, payload = await referrer_with_link(session, 900000019)

    await send_start(session, 900000019, payload)
    attempt = await ReferralProgramService(session).attribute_from_start(user.id, payload)

    assert attempt.outcome == ReferralOutcome.SELF_REFERRAL
    assert await count_rows(session, Referral) == 0


async def test_an_unknown_code_is_ignored(session: AsyncSession) -> None:
    message = await send_start(session, 900000020, "ref_Unknown-1234")

    friend = await user_by_telegram(session, 900000020)
    attempt = await ReferralProgramService(session).attribute_from_start(
        friend.id, "ref_Unknown-1234"
    )
    assert attempt.outcome == ReferralOutcome.UNKNOWN_CODE
    assert await count_rows(session, Referral) == 0
    assert message.sent


@pytest.mark.parametrize("payload", MALFORMED)
async def test_start_never_fails_on_a_hostile_payload(session: AsyncSession, payload: str) -> None:
    await referrer_with_link(session, 900000021)

    message = await send_start(session, 900000022, payload)

    assert message.sent, "onboarding goes on"
    assert await count_rows(session, Referral) == 0


async def test_start_without_a_payload_attributes_nothing(session: AsyncSession) -> None:
    await referrer_with_link(session, 900000023)

    await send_start(session, 900000024)

    assert await count_rows(session, Referral) == 0


async def test_a_customer_who_has_ordered_is_not_a_new_referral(session: AsyncSession) -> None:
    """Owner decision: referrals are for brand-new customers — nobody who ever ordered."""
    _, payload = await referrer_with_link(session, 900000025)
    customer = await make_user(session, telegram_id=900000026)
    await make_order(session, customer)  # any status counts

    await send_start(session, 900000026, payload)
    attempt = await ReferralProgramService(session).attribute_from_start(customer.id, payload)

    assert attempt.outcome == ReferralOutcome.NOT_NEW_CUSTOMER
    assert await count_rows(session, Referral) == 0


async def test_referral_loops_are_refused_all_the_way_up(session: AsyncSession) -> None:
    alice, alice_payload = await referrer_with_link(session, 900000030)
    await send_start(session, 900000031, alice_payload)
    bob = await user_by_telegram(session, 900000031)
    programme = ReferralProgramService(session)
    bob_payload = referral_payload(await programme.referral_code(bob.id))
    await send_start(session, 900000032, bob_payload)
    carol = await user_by_telegram(session, 900000032)
    carol_payload = referral_payload(await programme.referral_code(carol.id))

    direct = await programme.attribute_from_start(alice.id, bob_payload)
    through_the_chain = await programme.attribute_from_start(alice.id, carol_payload)

    assert (direct.outcome, through_the_chain.outcome) == (ReferralOutcome.LOOP,) * 2
    assert await referral_of(session, alice.id) is None
    assert await count_rows(session, Referral) == 2


# ==================================================================== payout


async def test_the_first_paid_order_pays_both_sides_once(session: AsyncSession) -> None:
    referrer, friend = await attributed_pair(session, 900000050)

    order = await complete(session, friend)

    referral = await referral_of(session, friend.id)
    assert referral is not None
    assert (referral.status, referral.qualifying_order_id) == (ReferralStatus.QUALIFIED, order.id)
    assert referral.qualified_at is not None
    assert await bonuses(session, friend.id) == [2]
    assert await bonuses(session, referrer.id) == [2]
    loyalty = LoyaltyService(session)
    assert await loyalty.balance(friend.id) == 1 + 2, "the order's own stamp and the bonus"
    assert await loyalty.balance(referrer.id) == 2
    assert await referral_spins(session, referrer.id) == 1
    assert await referral_spins(session, friend.id) == 0, "the spin is the referrer's"


async def test_later_orders_and_replays_pay_nothing_more(session: AsyncSession) -> None:
    referrer, friend = await attributed_pair(session, 900000052)
    first = await complete(session, friend)

    await complete(session, friend)
    replay = await ReferralProgramService(session).settle_for_completed_order(first.id)
    await AdminService(session, settings=configured()).set_order_status(
        first, OrderStatus.COMPLETED
    )  # a second tap on "Complete"

    assert replay is None
    assert await bonuses(session, friend.id) == [2]
    assert await bonuses(session, referrer.id) == [2]
    assert await referral_spins(session) == 1


@pytest.mark.parametrize(
    ("total", "eligible"),
    [("0.00", True), ("20.00", False)],
    ids=["charged-nothing", "placed-before-launch"],
)
async def test_an_order_that_does_not_qualify_leaves_the_referral_pending(
    session: AsyncSession, total: str, eligible: bool
) -> None:
    referrer, friend = await attributed_pair(session, 900000054)

    await complete(session, friend, total=total, eligible=eligible)

    referral = await referral_of(session, friend.id)
    assert referral is not None and referral.status == ReferralStatus.PENDING
    assert await bonuses(session, friend.id) == [] and await bonuses(session, referrer.id) == []

    paid = await complete(session, friend)

    referral = await referral_of(session, friend.id)
    assert referral is not None and referral.qualifying_order_id == paid.id
    assert await bonuses(session, referrer.id) == [2]


async def test_a_cancelled_order_pays_nothing(session: AsyncSession) -> None:
    referrer, friend = await attributed_pair(session, 900000056)
    order = await make_order(session, friend)
    order.total_price = Decimal("20.00")

    await AdminService(session, settings=configured()).set_order_status(
        order, OrderStatus.CANCELLED
    )

    referral = await referral_of(session, friend.id)
    assert referral is not None and referral.status == ReferralStatus.PENDING
    assert await bonuses(session, referrer.id) == []


async def test_the_payout_follows_the_configuration(session: AsyncSession) -> None:
    settings = configured(referral_reward_stamps=3, referred_user_start_stamps=1, referral_spins=0)
    referrer, friend = await attributed_pair(session, 900000058)

    await complete(session, friend, settings=settings)

    assert await bonuses(session, referrer.id) == [3]
    assert await bonuses(session, friend.id) == [1]
    assert await referral_spins(session) == 0


async def test_a_side_set_to_zero_gets_no_stamps(session: AsyncSession) -> None:
    referrer, friend = await attributed_pair(session, 900000060)

    await complete(session, friend, settings=configured(referral_reward_stamps=0))

    assert await bonuses(session, referrer.id) == []
    assert await bonuses(session, friend.id) == [2]
    assert await referral_spins(session, referrer.id) == 1
    referral = await referral_of(session, friend.id)
    assert referral is not None and referral.status == ReferralStatus.QUALIFIED


async def test_each_link_in_a_chain_pays_only_its_own_pair(session: AsyncSession) -> None:
    alice, bob = await attributed_pair(session, 900000070)
    bob_payload = referral_payload(await ReferralProgramService(session).referral_code(bob.id))
    await send_start(session, 900000072, bob_payload)
    carol = await user_by_telegram(session, 900000072)

    await complete(session, carol)

    assert (
        await bonuses(session, carol.id),
        await bonuses(session, bob.id),
        await bonuses(session, alice.id),
    ) == ([2], [2], [])

    await complete(session, bob)

    assert await bonuses(session, bob.id) == [2, 2], "as a referrer, then as a referred friend"
    assert await bonuses(session, alice.id) == [2]


async def test_a_customer_nobody_referred_triggers_no_payout(session: AsyncSession) -> None:
    customer = await make_user(session, telegram_id=900000080)

    order = await complete(session, customer)

    assert await ReferralProgramService(session).settle_for_completed_order(order.id) is None
    assert await count_rows(session, Referral) == 0
    assert await bonuses(session, customer.id) == []


async def test_the_payout_and_the_completion_are_one_transaction(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a bonus cannot be booked, the completion is undone with it."""
    _, friend = await attributed_pair(session, 900000082)
    order = await make_order(session, friend)
    order.total_price = Decimal("20.00")
    admin = AdminService(session, settings=configured())
    for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED):
        order = await admin.set_order_status(order, status)
    await session.commit()
    friend_id, order_id = friend.id, order.id

    async def broken(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(LoyaltyService, "credit_referral", broken)
    with pytest.raises(RuntimeError):
        await admin.set_order_status(order, OrderStatus.COMPLETED)
    await session.rollback()  # what DatabaseMiddleware does with any exception
    monkeypatch.undo()

    status = await session.scalar(select(Order.status).where(Order.id == order_id))
    referral_status = await session.scalar(
        select(Referral.status).where(Referral.referred_user_id == friend_id)
    )
    assert (status, referral_status) == (OrderStatus.SHIPPED, ReferralStatus.PENDING)
    assert await count_rows(session, LoyaltyTransaction) == 0


# ============================================================= configuration


def test_the_policy_defaults_are_the_configuration_defaults() -> None:
    assert ReferralPolicy.defaults() == ReferralPolicy.from_settings(configured())
    assert ReferralPolicy.defaults() == ReferralPolicy(referrer_stamps=2, referred_stamps=2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"referral_reward_stamps": -1},
        {"referred_user_start_stamps": -1},
        {"referral_reward_stamps": 101},
        {"referred_user_start_stamps": 101},
    ],
    ids=["negative-referrer", "negative-referred", "absurd-referrer", "absurd-referred"],
)
def test_unusable_referral_settings_stop_the_bot_at_startup(overrides: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        configured(**overrides)


def test_a_policy_refuses_negative_stamps() -> None:
    with pytest.raises(ValueError):
        ReferralPolicy(referrer_stamps=-1, referred_stamps=0)


# ===================================================================== guards


def callers_of(call: str) -> list[str]:
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if call in path.read_text(encoding="utf-8")
    )


def test_attribution_and_payout_each_have_one_entry_point() -> None:
    """/start attributes; order completion pays. Nothing else touches either."""
    assert callers_of(".attribute_from_start(") == ["app/handlers/user/start.py"]
    assert callers_of(".settle_for_completed_order(") == ["app/services/admin/orders.py"]
    assert callers_of(".attribute(") == ["app/services/referral_program.py"]
    assert callers_of(".qualify(") == ["app/services/referral_program.py"]
    assert callers_of(".credit_referral(") == ["app/services/referral_program.py"]
    assert callers_of(".grant_for_referral(") == ["app/services/referral_program.py"]
    assert callers_of(".grant_referral_spin(") == ["app/services/spin_entitlement.py"]


def test_no_button_or_callback_touches_referrals() -> None:
    """
    The only way in is /start: no callback can name a referrer or trigger a payout.

    The 👥 Invite a Friend screen reads the customer's own link and nothing more,
    and the admin's order status change only sends the news of a payout its
    completion already made; ``tests/test_invite_ui.py`` pins both.
    """
    touching = sorted(
        path.relative_to(ROOT).as_posix()
        for folder in ("handlers", "keyboards", "middlewares", "filters")
        for path in (ROOT / "app" / folder).rglob("*.py")
        if "referral" in path.read_text(encoding="utf-8").lower()
    )
    assert touching == [
        "app/handlers/admin/orders.py",
        "app/handlers/user/invite.py",
        "app/handlers/user/start.py",
    ]


# ==================================================================== exploits


async def test_start_answers_the_same_whatever_the_code(session: AsyncSession) -> None:
    """No oracle: nothing tells a guesser that a code was real."""
    _, payload = await referrer_with_link(session, 900000200)

    real = await send_start(session, 900000201, payload)
    fake = await send_start(session, 900000202, "ref_Fake-code-12")
    plain = await send_start(session, 900000203)

    assert real.sent == fake.sent == plain.sent


async def test_the_referred_customer_is_whoever_sent_the_update(session: AsyncSession) -> None:
    """A payload cannot name who is referred, or by whom: only the Telegram sender is."""
    referrer, payload = await referrer_with_link(session, 900000210)
    victim = await make_user(session, telegram_id=900000211)  # brand-new, never ordered

    await send_start(session, 900000212, payload)
    for forged in (
        f"ref_{victim.telegram_id}",
        f"ref_{victim.id:08d}",
        f"ref_{referrer.telegram_id}",
        f"ref_{referrer.id:08d}",
    ):
        await send_start(session, 900000212, forged)

    attacker = await user_by_telegram(session, 900000212)
    referral = await referral_of(session, attacker.id)
    assert referral is not None and referral.referrer_user_id == referrer.id
    assert await referral_of(session, victim.id) is None, "the victim is untouched"
    assert await count_rows(session, Referral) == 1


@pytest.mark.parametrize(
    "guess",
    [
        "00000001",
        "12345678",
        "AAAAAAAAAAAA",
        "aaaaaaaaaaaa",
        "000000000001",
        "________",
        "--------",
    ],
)
async def test_guessable_codes_resolve_to_nobody(session: AsyncSession, guess: str) -> None:
    """Codes are 72 random bits: ids, sequences and patterns name nobody."""
    programme = ReferralProgramService(session)
    for index in range(5):
        user = await make_user(session, telegram_id=900000220 + index)
        await programme.referral_code(user.id)

    assert await ReferralService(session).find_referrer_user_id(guess) is None


async def test_a_referral_never_changes_hands(session: AsyncSession) -> None:
    """Its parties and its qualification are fixed — in the ORM and in the schema."""
    referrer, friend = await attributed_pair(session, 900000230)
    other = await make_user(session, telegram_id=900000232)
    referral = await referral_of(session, friend.id)
    assert referral is not None

    with pytest.raises(ValueError):
        referral.referrer_user_id = other.id
    with pytest.raises(ValueError):
        referral.referred_user_id = other.id
    await complete(session, friend)
    with pytest.raises(ValueError):
        referral.status = ReferralStatus.PENDING
    with pytest.raises(ValueError):
        referral.qualifying_order_id = 999_999
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await ReferralRepository(session).create_and_add(
                referrer_user_id=other.id,
                referred_user_id=friend.id,
                status=ReferralStatus.PENDING,
            )

    kept = await referral_of(session, friend.id)
    assert kept is not None
    assert (kept.referrer_user_id, kept.status) == (referrer.id, ReferralStatus.QUALIFIED)


async def test_rewards_cannot_be_booked_twice_or_to_strangers(session: AsyncSession) -> None:
    """The ledger and the spin grants refuse a second payout, whoever calls them."""
    referrer, friend = await attributed_pair(session, 900000240)
    stranger = await make_user(session, telegram_id=900000242)
    await complete(session, friend)
    referral = await referral_of(session, friend.id)
    assert referral is not None
    loyalty = LoyaltyService(session)

    again = await loyalty.credit_referral(friend.id, referral_id=referral.id, stamps=2)
    with pytest.raises(ValueError):
        await loyalty.credit_referral(stranger.id, referral_id=referral.id, stamps=2)
    spin = await RouletteService(session).grant_referral_spin(referrer.id, referral_id=referral.id)
    replayed = await SpinEntitlementService(session).grant_for_referral(referral.id)

    assert again.created is False
    assert spin.created is False
    assert replayed is not None and replayed.created is False
    assert await bonuses(session, friend.id) == [2]
    assert await bonuses(session, referrer.id) == [2]
    assert await bonuses(session, stranger.id) == []
    assert await referral_spins(session) == 1


async def test_nothing_can_be_booked_before_the_referral_qualifies(
    session: AsyncSession,
) -> None:
    referrer, friend = await attributed_pair(session, 900000250)
    referral = await referral_of(session, friend.id)
    assert referral is not None

    with pytest.raises(ValueError):
        await LoyaltyService(session).credit_referral(
            referrer.id, referral_id=referral.id, stamps=2
        )
    with pytest.raises(ValueError):
        await SpinEntitlementService(session).grant_for_referral(referral.id)

    assert await count_rows(session, LoyaltyTransaction) == 0
    assert await referral_spins(session) == 0
