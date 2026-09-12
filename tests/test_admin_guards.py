"""
Admin-side guards that no other test would miss.

* The product wizards refuse a text longer than its column. SQLite, which the
  suite runs on, stores it; PostgreSQL refuses the write, and the admin would
  meet a generic error at the very last step of the wizard.
* ``confirm_once`` lets one confirmation through, so a double-tapped broadcast
  or product save happens once.
* New-order alerts reach the manager chat and every admin, each once.
* A category change shows at once, without waiting out the list cache.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.admin.product_manage.handlers import process_edit_text_step
from app.handlers.admin.products import process_text_step
from app.models.product import text_limit
from app.services.admin import AdminService
from app.services.catalog import CatalogService
from app.services.localization import LocalizationService
from app.services.notification import OrderNotificationService
from app.states.admin import AddProductStates, EditProductStates
from app.utils.cache import invalidate_categories_cache
from app.utils.confirm import confirm_once
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, tree_settings

EN = LocalizationService("en")
PRODUCT_FIELDS = (
    "name_ru", "name_en", "name_de", "name_uk",
    "description_ru", "description_en", "description_de", "description_uk",
    "flavor", "volume", "nicotine_strength", "price",
)  # fmt: skip


class Typed(Message):
    """A message the admin typed; records the bot's answers."""

    model_config = {"extra": "allow"}

    async def answer(self, text: str, **kwargs: Any) -> Any:
        self.replies.append(text)
        return self

    @property
    def replies(self) -> list[str]:
        return self.__dict__.setdefault("seen", [])


def typed(text: str) -> Typed:
    return Typed(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
        text=text,
    )


def wizard() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID))


# ======================================================= product text limits


def test_the_limits_are_the_columns() -> None:
    fields = ("name_en", "flavor", "volume", "nicotine_strength", "description_en")
    assert {field: text_limit(field) for field in fields} == {
        "name_en": 255,
        "flavor": 255,
        "volume": 64,
        "nicotine_strength": 64,
        "description_en": None,
    }


@pytest.mark.parametrize(
    ("step", "field", "following"),
    [
        (AddProductStates.name_en, "name_en", AddProductStates.name_de),
        (AddProductStates.flavor, "flavor", AddProductStates.volume),
        (AddProductStates.volume, "volume", AddProductStates.nicotine_strength),
        (AddProductStates.nicotine_strength, "nicotine_strength", AddProductStates.price),
    ],
    ids=["name", "flavor", "volume", "nicotine"],
)
async def test_the_add_wizard_takes_a_text_up_to_its_column_and_no_further(
    session: AsyncSession, step: State, field: str, following: State
) -> None:
    limit = text_limit(field)
    assert limit is not None
    state = wizard()
    await state.set_state(step)

    too_long = typed("x" * (limit + 1))
    await process_text_step(too_long, EN, state, session)
    assert too_long.replies == [EN.t("admin.product_text_too_long", limit=limit)]
    assert await state.get_state() == step.state
    assert field not in await state.get_data()

    await process_text_step(typed("x" * limit), EN, state, session)
    assert await state.get_state() == following.state
    assert (await state.get_data())[field] == "x" * limit


@pytest.mark.parametrize(
    ("step", "field", "following"),
    [
        (EditProductStates.name_en, "name_en", EditProductStates.name_de),
        (EditProductStates.volume, "volume", EditProductStates.nicotine_strength),
    ],
    ids=["name", "volume"],
)
async def test_the_edit_wizard_takes_a_text_up_to_its_column_and_no_further(
    session: AsyncSession, step: State, field: str, following: State
) -> None:
    limit = text_limit(field)
    assert limit is not None
    state = wizard()
    await state.set_state(step)
    await state.set_data(dict.fromkeys(PRODUCT_FIELDS, "x"))

    too_long = typed("y" * (limit + 1))
    await process_edit_text_step(too_long, EN, state, session)
    assert too_long.replies == [EN.t("admin.product_text_too_long", limit=limit)]
    assert await state.get_state() == step.state
    assert (await state.get_data())[field] == "x"

    await process_edit_text_step(typed("y" * limit), EN, state, session)
    assert await state.get_state() == following.state
    assert (await state.get_data())[field] == "y" * limit


# ======================================================= confirm once


async def test_confirm_once_lets_one_confirmation_through() -> None:
    state = wizard()
    await state.update_data(text="Sale")
    for lock_key in ("broadcast:1", None):
        await state.update_data(submitted=False)
        async with confirm_once(state, lock_key=lock_key) as first:
            assert first is not None and first["text"] == "Sale"
        async with confirm_once(state, lock_key=lock_key) as second:
            assert second is None


async def test_two_confirmations_at_once_let_one_through() -> None:
    state = wizard()
    through: list[bool] = []

    async def tap() -> None:
        async with confirm_once(state, lock_key="broadcast:1") as data:
            through.append(data is not None)
            await asyncio.sleep(0)  # still at work when the second tap arrives

    await asyncio.gather(tap(), tap())
    assert sorted(through) == [False, True]


async def test_a_failed_confirmation_can_be_retried() -> None:
    state = wizard()
    with pytest.raises(RuntimeError):
        async with confirm_once(state, lock_key="broadcast:1") as data:
            assert data is not None
            raise RuntimeError("Telegram unreachable")
    async with confirm_once(state, lock_key="broadcast:1") as retry:
        assert retry is not None


# ======================================================= new-order alerts


@pytest.mark.parametrize(
    ("admins", "expected"),
    [
        ([], [MANAGER_CHAT_ID]),
        ([ADMIN_ID], [MANAGER_CHAT_ID, ADMIN_ID]),
        ([ADMIN_ID, 7001, ADMIN_ID, MANAGER_CHAT_ID], [MANAGER_CHAT_ID, ADMIN_ID, 7001]),
    ],
    ids=["no admins", "one admin", "repeats"],
)
def test_new_order_alerts_reach_the_manager_chat_and_each_admin_once(
    admins: list[int], expected: list[int]
) -> None:
    alerts = OrderNotificationService(None, tree_settings(admin_ids=admins))  # type: ignore[arg-type]
    assert alerts.notification_chat_ids() == expected


# ======================================================= the category list cache


@pytest.fixture
def cold_cache() -> Iterator[None]:
    invalidate_categories_cache()
    yield
    invalidate_categories_cache()


@pytest.mark.usefixtures("cold_cache")
async def test_a_category_change_shows_at_once(session: AsyncSession) -> None:
    """Nothing here clears the cache by hand: each admin change has to."""
    admin = AdminService(session)
    catalog = CatalogService(session)
    liquids = await admin.create_category("Liquids")
    assert [c.id for c in await catalog.list_categories()] == [liquids.id]  # cached now

    pods = await admin.create_category("Pods")
    assert {c.id for c in await catalog.list_categories()} == {liquids.id, pods.id}

    await admin.set_category_active(pods, False)
    assert [c.id for c in await catalog.list_categories()] == [liquids.id]
