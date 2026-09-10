"""Adversarial review of group-chat isolation: every route to functionality."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors.notify import notify_user_of_error
from app.middlewares import setup_middlewares
from app.middlewares.database import DatabaseMiddleware
from app.middlewares.error import ErrorHandlingMiddleware
from app.middlewares.private_chat import PrivateChatMiddleware
from app.models.enums import PaymentMethod
from app.repositories.user import UserRepository
from app.services.cart import CartService
from app.services.notification import OrderNotificationService
from app.services.order import OrderService
from app.utils.chat_scope import is_private_event
from tests.factories import make_category, make_product, make_user

ADMIN_ID = 452536082
MANAGER_GROUP = -1001234567890
RANDOM_GROUP = -1009999999999
NON_PRIVATE = [
    ("group", RANDOM_GROUP),
    ("supergroup", MANAGER_GROUP),
    ("channel", -1005555555555),
]


def _chat(kind: str, cid: int) -> Chat:
    return Chat(id=cid, type=kind)


def _user(uid: int = ADMIN_ID) -> User:
    return User(id=uid, is_bot=False, first_name="T")


def _msg(chat: Chat, body: str = "/admin") -> Message:
    return Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=_user(), text=body)


def _upd_msg(chat: Chat, body: str = "/admin") -> Update:
    return Update(update_id=1, message=_msg(chat, body))


def _upd_cb(chat: Chat, data: str) -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="c",
            from_user=_user(),
            chat_instance="i",
            data=data,
            message=_msg(chat, "menu"),
        ),
    )


class Sink:
    """Stands in for everything downstream of the gate."""

    def __init__(self) -> None:
        self.hits: list[Any] = []

    async def __call__(self, event: Any, data: dict[str, Any]) -> str:
        self.hits.append(event)
        return "reached"


# ==================== every functional route is blocked from a group


@pytest.mark.parametrize("kind,cid", NON_PRIVATE)
@pytest.mark.parametrize(
    "kind_of_update,payload",
    [
        ("message", "/start"),
        ("message", "/admin"),
        ("message", "/orders"),
        ("message", "🛍 Catalog"),
        ("message", "🛒 Cart"),
        ("message", "🪪 My Stamp Card"),
        ("message", "Some FSM text input"),
        ("callback", "catalog:open"),
        ("callback", "category:1"),
        ("callback", "subcat:1"),
        ("callback", "prod:1"),
        ("callback", "cart:add:1"),
        ("callback", "checkout:confirm"),
        ("callback", "checkout:pay:card"),
        ("callback", "stamp:open"),
        ("callback", "stamp:claim:1"),
        ("callback", "admin:ord:st:1:shipped:new:0"),
        ("callback", "admin:cat:del:1"),
        ("callback", "admin:sub:delok:1"),
        ("callback", "admin:product:delok:1"),
    ],
)
@pytest.mark.asyncio
async def test_no_route_reaches_functionality_from_a_group(
    kind: str, cid: int, kind_of_update: str, payload: str
) -> None:
    chat = _chat(kind, cid)
    update = _upd_msg(chat, payload) if kind_of_update == "message" else _upd_cb(chat, payload)

    sink = Sink()
    result = await PrivateChatMiddleware()(sink, update, {})

    assert sink.hits == [], f"{payload!r} reached handlers from a {kind}"
    assert result is None


@pytest.mark.asyncio
async def test_the_same_routes_work_in_a_private_chat() -> None:
    """The gate must not be so tight that it breaks the product."""
    private = _chat("private", ADMIN_ID)
    for update in (
        _upd_msg(private, "/start"),
        _upd_msg(private, "/admin"),
        _upd_cb(private, "cart:add:1"),
        _upd_cb(private, "admin:ord:st:1:shipped:new:0"),
    ):
        sink = Sink()
        assert await PrivateChatMiddleware()(sink, update, {}) == "reached"
        assert sink.hits


# ==================== closed bypass: error notifier answering a group


@pytest.mark.parametrize("kind,cid", NON_PRIVATE)
@pytest.mark.asyncio
async def test_error_notifier_never_answers_a_group(kind: str, cid: int) -> None:
    """Regression: a failure used to make the bot post into the group."""
    sent: list[dict[str, Any]] = []

    class SpyMessage(Message):
        async def answer(self, text: str, **kw: Any) -> None:  # type: ignore[override]
            sent.append({"chat_id": self.chat.id, "text": text})

    class FakeBot:
        async def send_message(self, **kw: Any) -> None:
            sent.append(kw)

    message = SpyMessage(
        message_id=1,
        date=datetime.now(UTC),
        chat=_chat(kind, cid),
        from_user=_user(),
        text="/admin",
    )
    await notify_user_of_error(
        Update(update_id=1, message=message),
        FakeBot(),
        "err",  # type: ignore[arg-type]
    )

    assert sent == [], f"error notification leaked into a {kind}"


@pytest.mark.asyncio
async def test_error_notifier_still_answers_a_private_chat() -> None:
    sent: list[str] = []

    class SpyMessage(Message):
        async def answer(self, text: str, **kw: Any) -> None:  # type: ignore[override]
            sent.append(text)

    message = SpyMessage(
        message_id=1,
        date=datetime.now(UTC),
        chat=_chat("private", ADMIN_ID),
        from_user=_user(),
        text="/admin",
    )
    await notify_user_of_error(Update(update_id=1, message=message), None, "boom")

    assert sent == ["boom"]


@pytest.mark.parametrize("kind,cid", NON_PRIVATE)
def test_is_private_event_rejects_every_group_shape(kind: str, cid: int) -> None:
    chat = _chat(kind, cid)
    assert not is_private_event(_upd_msg(chat))
    assert not is_private_event(_upd_cb(chat, "x"))
    assert not is_private_event(_msg(chat))


# ==================== ordering guarantees


def test_gate_precedes_both_the_error_handler_and_the_database() -> None:
    dispatcher = Dispatcher()
    setup_middlewares(dispatcher, None)  # type: ignore[arg-type]
    kinds = [type(m) for m in dispatcher.update.outer_middleware]

    gate = kinds.index(PrivateChatMiddleware)
    assert gate < kinds.index(DatabaseMiddleware), (
        "a group update could open a database session before being dropped"
    )
    assert gate < kinds.index(ErrorHandlingMiddleware), (
        "a downstream failure could answer into the group"
    )


# ==================== no state is touched by group traffic


@pytest.mark.parametrize("kind,cid", NON_PRIVATE)
@pytest.mark.asyncio
async def test_group_update_creates_no_fsm_state(kind: str, cid: int) -> None:
    """FSM context is resolved upstream; nothing may be persisted."""
    storage = MemoryStorage()
    await PrivateChatMiddleware()(Sink(), _upd_msg(_chat(kind, cid)), {})

    assert storage.storage == {}


@pytest.mark.asyncio
async def test_group_start_creates_no_user_row(session: AsyncSession) -> None:
    sink = Sink()
    await PrivateChatMiddleware()(sink, _upd_msg(_chat("supergroup", MANAGER_GROUP), "/start"), {})

    assert sink.hits == []
    assert await UserRepository(session).get_by_telegram_id(ADMIN_ID) is None


# ==================== manager group stays notification-only


@pytest.mark.asyncio
async def test_manager_notification_carries_no_keyboard(
    session: AsyncSession,
) -> None:
    """Outbound alerts are text; a group must never receive interactive buttons."""
    category = await make_category(session, name="L")
    product = await make_product(session, category, name_en="M", price="10.00")
    user = await make_user(session, telegram_id=90001)
    await session.flush()
    await CartService(session).add_product(user.id, product, quantity=1)
    await session.flush()
    order = await OrderService(session).place_order_from_cart(
        user,
        customer_name="QA",
        delivery_type="pickup",
        address="X",
        preferred_time="18:00",
        phone=None,
        payment_method=PaymentMethod.CASH,
    )
    await session.flush()

    calls: list[dict[str, Any]] = []

    class FakeBot:
        async def send_message(self, **kw: Any) -> None:
            calls.append(kw)

    settings = type("S", (), {"manager_chat_id": MANAGER_GROUP, "admin_ids": []})()
    await OrderNotificationService(FakeBot(), settings).notify_new_order(  # type: ignore[arg-type]
        order, user
    )

    assert calls, "the manager group should still be notified"
    for call in calls:
        assert call["chat_id"] == MANAGER_GROUP
        assert call.get("reply_markup") is None
    assert order.total_price == Decimal("10.00")


@pytest.mark.asyncio
async def test_broadcast_can_only_reach_individuals(session: AsyncSession) -> None:
    """Group IDs are negative; recipients are restricted to positive user IDs."""
    await make_user(session, telegram_id=555001)
    await session.flush()
    # a stray group id recorded as a user by an older build or a manual edit
    await session.execute(
        sql_text(
            "INSERT INTO users (telegram_id, language, selected_city) VALUES (:tid, 'en', 'berlin')"
        ),
        {"tid": MANAGER_GROUP},
    )
    await session.flush()

    recipients = await UserRepository(session).list_telegram_ids()

    assert 555001 in recipients
    assert MANAGER_GROUP not in recipients
    assert all(recipient > 0 for recipient in recipients)
