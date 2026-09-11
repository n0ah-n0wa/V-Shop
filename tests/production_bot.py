"""
The production bot, for tests that feed real Telegram updates end to end.

``Bot`` talks to :class:`FakeTelegram` instead of the Bot API: every request is
recorded and answered the way Telegram would answer it (a sent message comes
back as a message with an id). Updates go through a real dispatcher — every
middleware, router, filter and FSM state, one database session per update —
just as polling delivers them.

Handler modules create their routers at import time and aiogram lets a router
have one parent, so the production tree is composed once per process
(:func:`production_router`) and moved to whichever dispatcher needs it
(:func:`mount`).
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import UnionType
from typing import Any, get_args

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.handlers import setup_routers
from app.middlewares import setup_middlewares

ADMIN_ID = 452536082
BOT_TOKEN = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
BOT_USERNAME = "VShopTestBot"
MANAGER_CHAT_ID = -1001234567890


def tree_settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "bot_token": BOT_TOKEN,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "admin_ids": [ADMIN_ID],
        "manager_chat_id": MANAGER_CHAT_ID,
    }
    return Settings(**(fields | overrides))


@functools.cache
def production_router() -> Router:
    """The real root router, composed once per process (admin: :data:`ADMIN_ID`)."""
    return setup_routers(tree_settings())


def mount(dispatcher: Dispatcher) -> None:
    """Attach the production tree to ``dispatcher``, detaching it from any earlier one."""
    root = production_router()
    parent = root.parent_router
    if parent is dispatcher:
        return
    if parent is not None:
        parent.sub_routers.remove(root)
        root._parent_router = None  # aiogram has no public way to detach a router
    dispatcher.include_router(root)


class FakeTelegram(BaseSession):
    """Answers Bot API calls the way Telegram would, and records every one."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[TelegramMethod[Any], Any]] = []
        self._message_ids = itertools.count(10_000)

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        result = self._answer(bot, method)
        self.calls.append((method, result))
        return result

    def _answer(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, GetMe):
            return TgUser(id=bot.id, is_bot=True, first_name="V-Shop", username=BOT_USERNAME)
        returning = method.__returning__
        if returning is bool:
            return True
        if returning is Message:
            markup = getattr(method, "reply_markup", None)
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),  # type: ignore[attr-defined]
                from_user=TgUser(id=bot.id, is_bot=True, first_name="V-Shop"),
                text=getattr(method, "text", None) or getattr(method, "caption", None),
                reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None,
            ).as_(bot)
        if isinstance(returning, UnionType) and bool in get_args(returning):
            return True  # an edit: Telegram answers True or the message
        raise NotImplementedError(f"FakeTelegram does not answer {type(method).__name__}")

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
        raise NotImplementedError
        yield b""  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


def _has_button(markup: InlineKeyboardMarkup | None, data: str) -> bool:
    return markup is not None and any(
        button.callback_data == data for row in markup.inline_keyboard for button in row
    )


class RunningBot:
    """
    One running bot: the production dispatcher on a database, talking to a fake Telegram.

    :meth:`restart` is a process restart — a new dispatcher with empty FSM
    storage — on the same database, the same Telegram and the same bot.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        telegram: FakeTelegram | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.telegram = telegram or FakeTelegram()
        self.bot = Bot(
            token=BOT_TOKEN,
            session=self.telegram,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._ids = itertools.count(1)
        self.dispatcher = self._dispatcher()

    def _dispatcher(self) -> Dispatcher:
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher["settings"] = self.settings
        setup_middlewares(dispatcher, self.settings)
        mount(dispatcher)
        return dispatcher

    def restart(self) -> None:
        self.dispatcher = self._dispatcher()

    # --- updates ------------------------------------------------------------------

    def message(self, telegram_id: int, text: str, *, first_name: str = "Test") -> Update:
        """An update: the customer sends ``text`` (or taps a reply-keyboard button)."""
        number = next(self._ids)
        return Update(
            update_id=number,
            message=Message(
                message_id=number,
                date=datetime.now(UTC),
                chat=Chat(id=telegram_id, type="private"),
                from_user=TgUser(id=telegram_id, is_bot=False, first_name=first_name),
                text=text,
            ),
        )

    def tap(self, telegram_id: int, data: str, *, on: Message | None = None) -> Update:
        """An update: the customer taps the button carrying ``data`` on a screen they see."""
        screen = on if on is not None else self.showing(telegram_id, data)
        number = next(self._ids)
        return Update(
            update_id=number,
            callback_query=CallbackQuery(
                id=f"{telegram_id}:{number}",
                from_user=TgUser(id=telegram_id, is_bot=False, first_name="Test"),
                chat_instance=str(telegram_id),
                data=data,
                message=screen,
            ),
        )

    async def feed(self, update: Update) -> list[tuple[TelegramMethod[Any], Any]]:
        """Deliver one update, as polling does; returns the Bot API calls it caused."""
        before = len(self.telegram.calls)
        await self.dispatcher.feed_update(self.bot, update)
        return self.telegram.calls[before:]

    async def send(self, telegram_id: int, text: str, **kwargs: Any) -> list[Any]:
        return await self.feed(self.message(telegram_id, text, **kwargs))

    async def press(self, telegram_id: int, data: str, *, on: Message | None = None) -> list[Any]:
        return await self.feed(self.tap(telegram_id, data, on=on))

    # --- what the customer sees ---------------------------------------------------

    def screens(self, chat_id: int) -> dict[int, Message]:
        """Every message the bot left in ``chat_id``, as it looks now (edits applied)."""
        shown: dict[int, Message] = {}
        for method, result in self.telegram.calls:
            if getattr(method, "chat_id", None) != chat_id:
                continue
            if isinstance(result, Message):
                shown[result.message_id] = result
            elif isinstance(method, EditMessageText | EditMessageReplyMarkup):
                current = shown.get(method.message_id or 0)
                if current is None:
                    continue
                markup = method.reply_markup
                text = method.text if isinstance(method, EditMessageText) else current.text
                shown[current.message_id] = current.model_copy(
                    update={
                        "text": text,
                        "reply_markup": markup
                        if isinstance(markup, InlineKeyboardMarkup)
                        else None,
                    }
                ).as_(self.bot)
            elif isinstance(method, DeleteMessage):
                shown.pop(method.message_id, None)
        return shown

    def shows(self, chat_id: int, data: str) -> bool:
        """Whether any screen in ``chat_id`` currently has a button carrying ``data``."""
        return any(_has_button(m.reply_markup, data) for m in self.screens(chat_id).values())

    def showing(self, chat_id: int, data: str) -> Message:
        """The latest screen in ``chat_id`` with a button carrying ``data``."""
        found = [m for m in self.screens(chat_id).values() if _has_button(m.reply_markup, data)]
        assert found, f"no button {data!r} on any screen in chat {chat_id}"
        return max(found, key=lambda message: message.message_id)

    def button(self, chat_id: int, prefix: str) -> str:
        """The data of the latest button in ``chat_id`` whose data starts with ``prefix``."""
        for message in sorted(self.screens(chat_id).values(), key=lambda m: -m.message_id):
            if message.reply_markup is None:
                continue
            for row in message.reply_markup.inline_keyboard:
                for button in row:
                    if (button.callback_data or "").startswith(prefix):
                        return button.callback_data or ""
        raise AssertionError(f"no button starting {prefix!r} in chat {chat_id}")

    def texts(self, chat_id: int) -> list[str]:
        """Everything sent to or edited into ``chat_id``, in order."""
        return [
            method.text
            for method, _ in self.telegram.calls
            if getattr(method, "chat_id", None) == chat_id
            and isinstance(getattr(method, "text", None), str)
        ]

    def alerts(self, telegram_id: int) -> list[tuple[str | None, bool]]:
        """How ``telegram_id``'s button taps were answered: (text, shown as an alert)."""
        prefix = f"{telegram_id}:"
        return [
            (method.text, bool(method.show_alert))
            for method, _ in self.telegram.calls
            if isinstance(method, AnswerCallbackQuery)
            and method.callback_query_id.startswith(prefix)
        ]
