"""
The shop's lifecycle with the loyalty programme wired in — complete flows.

Every flow runs through the handlers customers and admins actually use: the
checkout conversation (payment → reward → summary → confirm) and the admin's
order-status change, which commits and then notifies. Stamps, spins, referral
payouts and reward redemptions hang off those two lifecycle events — placing an
order and completing it — and nowhere else: there is no second pipeline.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handlers.admin.orders import change_order_status
from app.handlers.user.checkout import checkout_confirm, checkout_payment, checkout_reward
from app.handlers.user.start import cmd_start
from app.keyboards.admin_orders import CALLBACK_ORDER_STATUS_PREFIX
from app.keyboards.checkout import CALLBACK_CONFIRM, CALLBACK_REWARD_NONE, CALLBACK_REWARD_PREFIX
from app.models.enums import (
    CityChoice,
    LanguageCode,
    LoyaltyTransactionType,
    OrderStatus,
    PaymentMethod,
    RewardSource,
    RewardStatus,
    RewardType,
    RoulettePrizeType,
    SpinGrantReason,
)
from app.models.loyalty import LoyaltyTransaction
from app.models.order import Order
from app.models.product import Product
from app.models.reward import UserReward
from app.models.roulette import RouletteSpinGrant
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.admin import AdminService
from app.services.cart import CartService
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.order import OrderService
from app.services.referral_program import ReferralProgramService
from app.services.roulette import PRIZE_CATALOGUE, RouletteService, SpinOutcome
from app.services.roulette_engine import RouletteEngine, RoulettePolicy
from app.services.stamp_card import StampCardService
from app.states.checkout import CheckoutStates
from app.utils.admin_order import format_admin_order_card
from tests.factories import make_category, make_product, make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
EN = LocalizationService("en")
BOT = "VShopTestBot"
BOT_ID = 1
ADMIN_ID = 424242
MANAGER_CHAT = -1001234567890
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)
PURCHASE = LoyaltyTransactionType.PURCHASE
REFERRAL = LoyaltyTransactionType.REFERRAL
MILESTONE = SpinGrantReason.PURCHASE_MILESTONE
PRIZES = {prize.code: prize for prize in PRIZE_CATALOGUE}
REWARD_FOR = {
    RoulettePrizeType.DISCOUNT_PERCENT: RewardType.DISCOUNT_PERCENT,
    RoulettePrizeType.FREE_BOTTLE: RewardType.FREE_BOTTLE,
}


def configured(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=MANAGER_CHAT,
        **overrides,
    )


class FakeBot:
    """The bot: ``getMe`` knows its username, and every message it sends is recorded."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []

    async def me(self) -> TgUser:
        return TgUser(id=BOT_ID, is_bot=True, first_name="V-Shop", username=BOT)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))
        return True

    def to(self, chat_id: int) -> list[str]:
        return [text for chat, text, _ in self.sent if chat == chat_id]


class Screen(Message):
    """A chat message: records everything the handlers answer or edit into it."""

    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        self.shown.append((text, kwargs.get("reply_markup")))
        return self

    async def edit_text(self, text: str, **kwargs: Any) -> Any:
        self.shown.append((text, kwargs.get("reply_markup")))
        return self

    async def edit_reply_markup(self, **kwargs: Any) -> Any:
        return self

    async def delete(self, **kwargs: Any) -> Any:
        return True

    @property
    def shown(self) -> list[tuple[str, Any]]:
        return self.__dict__.setdefault("seen", [])


class Tap(CallbackQuery):
    """A button tap: records the toast or alert it is answered with."""

    model_config = {"extra": "allow"}

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None, **kwargs: Any
    ) -> Any:
        self.alerts.append((text, bool(show_alert)))
        return True

    @property
    def alerts(self) -> list[tuple[str | None, bool]]:
        return self.__dict__.setdefault("seen", [])


def screen(chat_id: int, *, sender: TgUser | None = None, text: str = "…") -> Screen:
    return Screen(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=sender or TgUser(id=BOT_ID, is_bot=True, first_name="V-Shop"),
        text=text,
    )


def tap(telegram_id: int, data: str) -> Tap:
    """A button tap by the customer, on a message the bot sent them."""
    return Tap(
        id="t",
        from_user=TgUser(id=telegram_id, is_bot=False, first_name="Clara"),
        chat_instance="ci",
        data=data,
        message=screen(telegram_id),
    )


def shown_texts(message: Screen | None) -> list[str]:
    assert isinstance(message, Screen)
    return [text for text, _ in message.shown]


def summary_of(message: Screen | None) -> str:
    """The order summary checkout showed last."""
    return next(t for t in reversed(shown_texts(message)) if t.startswith("📋"))


def buttons_of(message: Screen | None) -> list[tuple[str, str | None]]:
    """The buttons of the last inline keyboard shown."""
    assert isinstance(message, Screen)
    markup = next(m for _, m in reversed(message.shown) if isinstance(m, InlineKeyboardMarkup))
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


class Shop:
    """The shop as its customers and its admin use it — through the real handlers."""

    def __init__(self, session: AsyncSession, **settings: Any) -> None:
        self.session = session
        self.settings = configured(**settings)
        self.bot = FakeBot()
        self._states: dict[int, FSMContext] = {}
        self._products = 0

    async def product(self, name: str, price: str) -> Product:
        category = await make_category(self.session, name=f"Liquids {self._products}")
        self._products += 1
        return await make_product(self.session, category, name_en=name, price=price)

    async def fill_cart(self, user: User, *lines: tuple[Product, int]) -> None:
        for product, quantity in lines:
            await CartService(self.session).add_product(user.id, product, quantity=quantity)

    def state(self, user: User) -> FSMContext:
        telegram_id = user.telegram_id
        if telegram_id not in self._states:
            self._states[telegram_id] = FSMContext(
                storage=MemoryStorage(),
                key=StorageKey(bot_id=BOT_ID, chat_id=telegram_id, user_id=telegram_id),
            )
        return self._states[telegram_id]

    async def pay(self, user: User) -> Screen:
        """The customer has filled in checkout and taps 💵 Cash."""
        state = self.state(user)
        await state.set_state(CheckoutStates.payment_method)
        await state.update_data(
            customer_name="Clara Schmidt",
            delivery_type="pickup",
            address="Alexanderplatz 1",
            preferred_time="18:00",
            phone="+4915112345678",
        )
        callback = tap(user.telegram_id, f"checkout:pay:{PaymentMethod.CASH.value}")
        await checkout_payment(callback, state, self.session, EN)
        assert isinstance(callback.message, Screen)
        return callback.message

    async def pick(self, user: User, reward_id: int | None) -> Tap:
        """The customer taps a reward — or ➡️ Continue without a reward."""
        data = CALLBACK_REWARD_NONE if reward_id is None else f"{CALLBACK_REWARD_PREFIX}{reward_id}"
        callback = tap(user.telegram_id, data)
        await checkout_reward(callback, self.state(user), self.session, EN)
        return callback

    async def confirm(self, user: User) -> Tap:
        callback = tap(user.telegram_id, CALLBACK_CONFIRM)
        await checkout_confirm(  # type: ignore[arg-type]
            callback, self.state(user), self.session, self.bot, self.settings, EN
        )
        return callback

    async def buy(
        self, user: User, *lines: tuple[Product, int], reward_id: int | None = None
    ) -> Order:
        """Cart → checkout, choosing ``reward_id`` if checkout offers rewards → the order."""
        await self.fill_cart(user, *lines)
        await self.pay(user)
        if await self.state(user).get_state() == CheckoutStates.reward.state:
            await self.pick(user, reward_id)
        else:
            assert reward_id is None, "a reward was chosen, but checkout offered none"
        await self.confirm(user)
        return await self.latest_order(user.id)

    async def latest_order(self, user_id: int) -> Order:
        order = await AdminService(self.session).get_order(
            await self.session.scalar(select(func.max(Order.id)).where(Order.user_id == user_id))
            or 0
        )
        assert order is not None
        return order

    async def admin_sets(self, order_id: int, *statuses: OrderStatus) -> None:
        """The admin taps status buttons on the order card: the real handler, commit and all."""
        for status in statuses:
            callback = Tap(
                id="a",
                from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
                chat_instance="ci",
                data=f"{CALLBACK_ORDER_STATUS_PREFIX}{order_id}:{status.name.lower()}:new:0",
                message=screen(ADMIN_ID),
            )
            await change_order_status(
                callback=callback,
                i18n=EN,
                session=self.session,
                bot=self.bot,  # type: ignore[arg-type]
                settings=self.settings,
            )

    async def complete(self, order_id: int) -> None:
        await self.admin_sets(order_id, *TO_COMPLETED)

    def staff_alerts(self) -> list[str]:
        return self.bot.to(MANAGER_CHAT)


def landing_on(code: str, policy: RoulettePolicy) -> Callable[[int], int]:
    """A ticket source whose draw lands on ``code``."""
    ticket = 0
    for entry in policy.table.entries:
        if entry.prize.code == code:
            break
        ticket += entry.weight
    return lambda total: ticket


async def win(session: AsyncSession, user: User, code: str) -> SpinOutcome:
    """The customer's welcome spin, played by the real engine, lands on ``code``."""
    await RouletteService(session).grant_initial_spin(user.id)
    policy = RoulettePolicy.defaults()
    engine = RouletteEngine(session, policy, randbelow=landing_on(code, policy))
    grant_id = await engine.next_grant_id(user.id)
    assert grant_id is not None
    outcome = await engine.spin(user.id, grant_id=grant_id)
    assert outcome is not None and outcome.spin.prize_code == code
    return outcome


async def stamp_card_bottle(session: AsyncSession, user: User) -> UserReward:
    """Ten stamps, then 🎁 Claim Free Bottle."""
    await LoyaltyService(session).adjust(user.id, amount=10, note="test setup")
    return await StampCardService(session).claim_free_bottle(user.id)


async def ledger(session: AsyncSession, user_id: int, kind: LoyaltyTransactionType) -> list[int]:
    rows = await session.scalars(
        select(LoyaltyTransaction.amount)
        .where(LoyaltyTransaction.user_id == user_id, LoyaltyTransaction.kind == kind)
        .order_by(LoyaltyTransaction.id)
    )
    return list(rows)


async def spins(session: AsyncSession, user_id: int, reason: SpinGrantReason) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(RouletteSpinGrant)
        .where(RouletteSpinGrant.user_id == user_id, RouletteSpinGrant.reason == reason)
    )
    return int(count or 0)


async def reward_row(session: AsyncSession, reward_id: int) -> tuple[Any, ...]:
    """Straight from the table: status, order, value given, product made free."""
    row = (
        await session.execute(
            select(
                UserReward.status,
                UserReward.order_id,
                UserReward.discount_amount,
                UserReward.redeemed_product_id,
            ).where(UserReward.id == reward_id)
        )
    ).one()
    return tuple(row)


async def orders_of(session: AsyncSession, user_id: int) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == user_id)
    )
    return int(count or 0)


# ======================================================= the unchanged shop


async def test_a_customer_without_rewards_checks_out_exactly_as_before(
    session: AsyncSession,
) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9101)
    mango = await shop.product("Mango Ice", "12.50")
    await shop.fill_cart(user, (mango, 2))

    after_paying = await shop.pay(user)

    assert await shop.state(user).get_state() == CheckoutStates.confirmation.state
    texts = shown_texts(after_paying)
    assert EN.t("checkout.ask_reward") not in texts
    summary = summary_of(after_paying)
    assert summary.endswith(EN.t("checkout.summary_total", total=Decimal("25.00")))
    assert "Subtotal" not in summary

    await shop.confirm(user)
    order = await shop.latest_order(user.id)
    assert order.total_price == Decimal("25.00")
    (alert,) = shop.staff_alerts()
    assert "Reward used" not in alert
    assert "Reward used" not in format_admin_order_card(order, EN)


# ======================================================= completed purchase → stamps


async def test_a_completed_purchase_books_its_stamps_once(session: AsyncSession) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9102)
    mango = await shop.product("Mango Ice", "12.50")
    order = await shop.buy(user, (mango, 4))  # €50.00 → 2 stamps at €20 each

    await shop.admin_sets(order.id, OrderStatus.ACCEPTED, OrderStatus.SHIPPED)
    assert await ledger(session, user.id, PURCHASE) == []  # nothing before completion

    await shop.admin_sets(order.id, OrderStatus.COMPLETED)
    await shop.admin_sets(order.id, OrderStatus.COMPLETED)  # a second tap on a stale card

    assert await ledger(session, user.id, PURCHASE) == [2]
    card = await StampCardService(session).card(user.id)
    assert card.stamps == 2
    assert await LoyaltyService(session).ledger_balance(user.id) == 2
    # the customer still hears about each step, as before
    told = shop.bot.to(user.telegram_id)
    assert EN.t("notification.status_completed", order_id=order.id) in told


async def test_a_cancelled_order_earns_nothing(session: AsyncSession) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9103)
    order = await shop.buy(user, (await shop.product("Bottle", "20.00"), 3))

    await shop.admin_sets(order.id, OrderStatus.ACCEPTED, OrderStatus.CANCELLED)

    assert await ledger(session, user.id, PURCHASE) == []
    assert await spins(session, user.id, MILESTONE) == 0


# ======================================================= every 5th purchase → a spin


async def test_every_fifth_qualifying_purchase_earns_one_spin(session: AsyncSession) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9104)
    bottle = await shop.product("Bottle", "20.00")

    earned = []
    for _ in range(10):
        order = await shop.buy(user, (bottle, 1))
        await shop.complete(order.id)
        earned.append(await spins(session, user.id, MILESTONE))

    assert earned == [0, 0, 0, 0, 1, 1, 1, 1, 1, 2]
    assert await ledger(session, user.id, PURCHASE) == [1] * 10


async def test_an_order_paid_entirely_by_a_free_bottle_is_not_a_qualifying_purchase(
    session: AsyncSession,
) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9105)
    bottle = await shop.product("Bottle", "20.00")
    for _ in range(4):
        await shop.complete((await shop.buy(user, (bottle, 1))).id)
    reward = await stamp_card_bottle(session, user)

    free = await shop.buy(user, (bottle, 1), reward_id=reward.id)
    await shop.complete(free.id)

    assert free.total_price == Decimal("0.00")
    assert await ledger(session, user.id, PURCHASE) == [1, 1, 1, 1]  # €0 earns no stamp
    assert await spins(session, user.id, MILESTONE) == 0  # …and is not the 5th purchase

    await shop.complete((await shop.buy(user, (bottle, 1))).id)
    assert await spins(session, user.id, MILESTONE) == 1


# ======================================================= referral


@pytest.mark.parametrize("referral_spins", [1, 0])
async def test_a_referral_pays_both_sides_at_the_first_completed_order(
    session: AsyncSession, referral_spins: int
) -> None:
    shop = Shop(session, referral_spins=referral_spins)
    base = 9110 + 10 * referral_spins
    referrer = await make_user(session, telegram_id=base)
    link = await ReferralProgramService(session).referral_link(referrer.id, bot_username=BOT)
    friend_id = base + 1
    await cmd_start(
        message=screen(
            friend_id,
            sender=TgUser(id=friend_id, is_bot=False, first_name="Friend"),
            text="/start",
        ),
        state=FSMContext(
            storage=MemoryStorage(),
            key=StorageKey(bot_id=BOT_ID, chat_id=friend_id, user_id=friend_id),
        ),
        session=session,
        settings=shop.settings,
        command=CommandObject(prefix="/", command="start", args=link.split("?start=")[1]),
        bot=shop.bot,  # type: ignore[arg-type]
    )
    friend = await UserRepository(session).get_by_telegram_id(friend_id)
    assert friend is not None
    friend.language, friend.selected_city = LanguageCode.EN, CityChoice.BERLIN  # onboarded
    bottle = await shop.product("Bottle", "20.00")

    await shop.complete((await shop.buy(friend, (bottle, 1))).id)
    await shop.complete((await shop.buy(friend, (bottle, 1))).id)  # a second order: no more

    assert await ledger(session, friend.id, REFERRAL) == [2]
    assert await ledger(session, referrer.id, REFERRAL) == [2]
    assert await spins(session, referrer.id, SpinGrantReason.REFERRAL) == referral_spins
    assert await ledger(session, friend.id, PURCHASE) == [1, 1]  # and the purchases themselves
    told_referrer = shop.bot.to(referrer.telegram_id)
    assert any(text.startswith(EN.t("invite.news.joined")) for text in told_referrer)
    assert sum(text.startswith(EN.t("invite.news.paid")) for text in told_referrer) == 1
    told_friend = shop.bot.to(friend.telegram_id)
    assert sum(text.startswith(EN.t("invite.news.welcome_bonus")) for text in told_friend) == 1


# ======================================================= roulette prizes


@pytest.mark.parametrize("code", list(PRIZES))
async def test_every_roulette_prize_lands_where_it_belongs(
    session: AsyncSession, code: str
) -> None:
    user = await make_user(session, telegram_id=9140 + list(PRIZES).index(code))

    outcome = await win(session, user, code)

    prize = PRIZES[code]
    if prize.kind == RoulettePrizeType.STAMPS:
        assert outcome.reward is None
        assert outcome.transaction is not None and outcome.transaction.amount == prize.value
        assert await LoyaltyService(session).ledger_balance(user.id) == prize.value
    else:
        reward = outcome.reward
        assert reward is not None and outcome.transaction is None
        assert (reward.kind, reward.value, reward.source, reward.status) == (
            REWARD_FOR[prize.kind],
            prize.value,
            RewardSource.ROULETTE,
            RewardStatus.AVAILABLE,
        )
        cap = RoulettePolicy.defaults().free_bottle_max_price
        assert reward.max_item_price == (
            cap if prize.kind == RoulettePrizeType.FREE_BOTTLE else None
        )
        assert await LoyaltyService(session).ledger_balance(user.id) == 0


@pytest.mark.parametrize(
    ("code", "percent", "saving"),
    [("discount_5", 5, Decimal("1.88")), ("discount_10", 10, Decimal("3.75"))],
)
async def test_a_roulette_discount_is_offered_and_redeemed_at_checkout(
    session: AsyncSession, code: str, percent: int, saving: Decimal
) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9150 + percent)
    reward = (await win(session, user, code)).reward
    assert reward is not None
    mango = await shop.product("Mango Ice", "12.50")
    await shop.fill_cart(user, (mango, 3))  # €37.50
    subtotal, total = Decimal("37.50"), Decimal("37.50") - saving

    offer = await shop.pay(user)

    assert await shop.state(user).get_state() == CheckoutStates.reward.state
    assert shown_texts(offer)[-1] == EN.t("checkout.ask_reward")
    assert buttons_of(offer) == [
        (
            EN.t("checkout.reward_option_discount", percent=percent, amount=saving),
            f"{CALLBACK_REWARD_PREFIX}{reward.id}",
        ),
        (EN.t("checkout.reward_skip"), CALLBACK_REWARD_NONE),
        (EN.t("checkout.cancel"), "checkout:cancel"),
    ]

    summary = summary_of((await shop.pick(user, reward.id)).message)
    assert summary.endswith(
        "\n".join(
            [
                EN.t("checkout.summary_subtotal", subtotal=subtotal),
                EN.t("checkout.summary_reward_discount", percent=percent, amount=saving),
                EN.t("checkout.summary_total", total=total),
            ]
        )
    )

    await shop.confirm(user)
    order = await shop.latest_order(user.id)
    assert order.total_price == total  # what the summary promised
    assert await reward_row(session, reward.id) == (RewardStatus.USED, order.id, saving, None)
    (alert,) = shop.staff_alerts()
    assert f"Reward used:</b> {percent}% discount (−<code>{saving}</code>)" in alert
    assert f"<b>Total:</b> <code>{total}</code>" in alert

    await shop.complete(order.id)
    assert await ledger(session, user.id, PURCHASE) == [int(total // 20)]  # on what was charged


# ======================================================= free bottles


@pytest.mark.parametrize("source", ["stamp card", "roulette"])
async def test_a_free_bottle_is_redeemed_through_the_one_shared_mechanism(
    session: AsyncSession, source: str
) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9160 if source == "roulette" else 9161)
    if source == "roulette":
        won = (await win(session, user, "free_bottle")).reward
        assert won is not None
        reward_id = won.id
    else:
        reward_id = (await stamp_card_bottle(session, user)).id
    cheap = await shop.product("Cheap", "8.00")
    dear = await shop.product("Dear", "18.50")
    over = await shop.product("Over", "24.00")  # above the €20 cap
    await shop.fill_cart(user, (cheap, 1), (dear, 2), (over, 1))  # €69.00

    offer = await shop.pay(user)
    assert buttons_of(offer)[0] == (
        EN.t("checkout.reward_option_free_bottle", name="Dear", amount=Decimal("18.50")),
        f"{CALLBACK_REWARD_PREFIX}{reward_id}",
    )
    summary = summary_of((await shop.pick(user, reward_id)).message)
    assert EN.t("checkout.summary_reward_free_bottle", name="Dear", amount=Decimal("18.50")) in (
        summary
    )
    assert summary.endswith(EN.t("checkout.summary_total", total=Decimal("50.50")))
    await shop.confirm(user)

    order = await shop.latest_order(user.id)
    assert order.total_price == Decimal("50.50")
    assert sorted((i.product_id, i.quantity, i.price) for i in order.items) == sorted(
        [
            (cheap.id, 1, Decimal("8.00")),
            (dear.id, 1, Decimal("18.50")),
            (dear.id, 1, Decimal("0.00")),  # the free one
            (over.id, 1, Decimal("24.00")),
        ]
    )
    assert await reward_row(session, reward_id) == (
        RewardStatus.USED,
        order.id,
        Decimal("18.50"),
        dear.id,
    )
    card = format_admin_order_card(order, EN)
    assert EN.t("admin.order_reward_free_bottle", name="Dear", amount=Decimal("18.50")) in card
    (alert,) = shop.staff_alerts()
    assert "Reward used:</b> free bottle — Dear (−<code>18.50</code>)" in alert

    await shop.complete(order.id)
    assert await ledger(session, user.id, PURCHASE) == [2]  # €50.50 charged → 2 stamps


# ======================================================= one reward per order


async def test_one_reward_per_order_and_the_others_stay_saved(session: AsyncSession) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9170)
    discount = (await win(session, user, "discount_10")).reward
    assert discount is not None
    bottle = await stamp_card_bottle(session, user)
    mango = await shop.product("Mango Ice", "12.50")
    await shop.fill_cart(user, (mango, 2))  # €25.00: the bottle saves 12.50, 10% saves 2.50

    offer = await shop.pay(user)
    offered = [data for _, data in buttons_of(offer)][:2]
    assert offered == [
        f"{CALLBACK_REWARD_PREFIX}{bottle.id}",
        f"{CALLBACK_REWARD_PREFIX}{discount.id}",
    ]
    await shop.pick(user, discount.id)
    await shop.confirm(user)

    assert (await reward_row(session, discount.id))[0] == RewardStatus.USED
    assert (await reward_row(session, bottle.id))[0] == RewardStatus.AVAILABLE
    await shop.fill_cart(user, (mango, 1))
    next_offer = await shop.pay(user)
    assert [data for _, data in buttons_of(next_offer)][0] == f"{CALLBACK_REWARD_PREFIX}{bottle.id}"
    assert f"{CALLBACK_REWARD_PREFIX}{discount.id}" not in [d for _, d in buttons_of(next_offer)]


async def test_continuing_without_a_reward_keeps_it(session: AsyncSession) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9171)
    reward = (await win(session, user, "discount_5")).reward
    assert reward is not None

    order = await shop.buy(user, (await shop.product("Mango Ice", "12.50"), 2), reward_id=None)

    assert order.total_price == Decimal("25.00")
    assert await reward_row(session, reward.id) == (RewardStatus.AVAILABLE, None, None, None)


# ======================================================= stale and crafted choices


async def test_a_reward_spent_meanwhile_is_dropped_before_anything_is_charged(
    session: AsyncSession,
) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9180)
    user_id = user.id
    won = (await win(session, user, "discount_10")).reward
    assert won is not None
    reward_id = won.id
    mango = await shop.product("Mango Ice", "12.50")
    await shop.fill_cart(user, (mango, 3))
    await shop.pay(user)
    await shop.pick(user, reward_id)  # the summary shows the discount…
    # …and meanwhile the same reward pays for an order placed from elsewhere.
    elsewhere = await OrderService(session).place_order_from_cart(
        user,
        customer_name="Clara",
        delivery_type="pickup",
        address="Alexanderplatz 1",
        preferred_time="now",
        phone=None,
        payment_method=PaymentMethod.CASH,
        reward_id=reward_id,
    )
    elsewhere_id = elsewhere.id
    await shop.fill_cart(user, (mango, 3))

    refused = await shop.confirm(user)  # rolls back — which expires every loaded object

    assert refused.alerts == [(EN.t("checkout.reward_unavailable"), True)]
    assert await orders_of(session, user_id) == 1  # nothing was placed
    await session.refresh(user)
    assert await shop.state(user).get_state() == CheckoutStates.confirmation.state
    summary = summary_of(refused.message)
    assert "Subtotal" not in summary
    assert summary.endswith(EN.t("checkout.summary_total", total=Decimal("37.50")))

    await shop.confirm(user)  # confirming again places it at the full price
    order = await shop.latest_order(user_id)
    assert order.total_price == Decimal("37.50")
    assert await orders_of(session, user_id) == 2
    assert (await reward_row(session, reward_id))[1] == elsewhere_id  # still where it was spent


async def test_a_crafted_reward_tap_spends_nothing(session: AsyncSession) -> None:
    shop = Shop(session)
    alice = await make_user(session, telegram_id=9181)
    mallory = await make_user(session, telegram_id=9182)
    alices = (await win(session, alice, "discount_10")).reward
    mallorys = (await win(session, mallory, "discount_5")).reward
    assert alices is not None and mallorys is not None
    await shop.fill_cart(mallory, (await shop.product("Mango Ice", "12.50"), 2))
    await shop.pay(mallory)

    for forged in (alices.id, 0):
        refused = await shop.pick(mallory, forged)
        assert refused.alerts == [(EN.t("checkout.reward_unavailable"), True)]
        assert await shop.state(mallory).get_state() == CheckoutStates.reward.state

    assert (await shop.state(mallory).get_data()).get("reward_id") is None
    assert await reward_row(session, alices.id) == (RewardStatus.AVAILABLE, None, None, None)


async def test_a_confirmed_checkout_closes_so_a_second_tap_spends_nothing(
    session: AsyncSession,
) -> None:
    shop = Shop(session)
    user = await make_user(session, telegram_id=9183)
    reward = (await win(session, user, "discount_10")).reward
    assert reward is not None
    await shop.buy(user, (await shop.product("Mango Ice", "12.50"), 2), reward_id=reward.id)

    assert await shop.state(user).get_state() is None  # the router drops a second tap
    await shop.confirm(user)  # …and even delivered directly, it places nothing

    assert await orders_of(session, user.id) == 1
    assert (await reward_row(session, reward.id))[0] == RewardStatus.USED


# ======================================================= cancellation


async def test_a_reward_on_a_cancelled_order_stays_used(session: AsyncSession) -> None:
    """Owner decision: cancelling never hands a reward back."""
    shop = Shop(session)
    user = await make_user(session, telegram_id=9190)
    reward = (await win(session, user, "discount_10")).reward
    assert reward is not None
    order = await shop.buy(user, (await shop.product("Mango Ice", "12.50"), 4), reward_id=reward.id)

    await shop.admin_sets(order.id, OrderStatus.CANCELLED)
    assert await reward_row(session, reward.id) == (
        RewardStatus.USED,
        order.id,
        Decimal("5.00"),
        None,
    )
    assert await ledger(session, user.id, PURCHASE) == []

    await shop.admin_sets(order.id, OrderStatus.NEW)  # reopened: still the same reward
    await shop.complete(order.id)
    assert (await reward_row(session, reward.id))[1] == order.id
    assert await ledger(session, user.id, PURCHASE) == [2]  # €45.00 charged


# ======================================================= no second pipeline


REWARD_AND_STAMP_ARITHMETIC = {
    "percentage_off",
    "choose_free_bottle",
    "stamps_for",
    "record_purchase",
    "credit_referral",
    "credit_roulette",
    "award_for_order",
    "grant_for_completed_order",
    "grant_purchase_milestone_spin",
    "settle_for_completed_order",
    "use_reward",
    "redeem",
    "plan_free_bottle",
}


def test_no_telegram_handler_calculates_or_books_a_reward() -> None:
    """Handlers name rewards by id and show what the services decided — nothing more."""
    for path in sorted((ROOT / "app" / "handlers").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        named |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not named & REWARD_AND_STAMP_ARITHMETIC, (
            f"{path.relative_to(ROOT)} reaches into loyalty rules: "
            f"{sorted(named & REWARD_AND_STAMP_ARITHMETIC)}"
        )


def test_every_loyalty_effect_hangs_off_the_existing_order_events() -> None:
    """Placing an order redeems; completing one earns. There is no other pipeline."""

    def callers_of(call: str) -> list[str]:
        return sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "app").rglob("*.py")
            if call in path.read_text(encoding="utf-8")
        )

    assert callers_of(".redeem(") == ["app/services/order.py"]
    assert callers_of(".award_for_order(") == ["app/services/admin/orders.py"]
    assert callers_of(".grant_for_completed_order(") == ["app/services/admin/orders.py"]
    assert callers_of(".settle_for_completed_order(") == ["app/services/admin/orders.py"]
    assert callers_of(".place_order_from_cart(") == ["app/handlers/user/checkout.py"]
