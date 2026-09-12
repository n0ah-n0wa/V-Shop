"""
Admin-typed text reaches customers and staff through Telegram HTML — escaped, on every screen.

Parse mode is HTML for the whole bot, so a product name holding ``<`` or ``&``
must be escaped wherever it is interpolated. Telegram refuses a message it
cannot parse ("can't parse entities"), and :class:`StrictHtmlTelegram` refuses
it the same way: a screen that forgets to escape is a screen nobody sees — for
the cart, the customer never gets to Checkout; for the admin's product preview,
a product with ``<`` in its name can never be saved.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.middlewares.database
from app.handlers.admin.product_manage.common import build_edit_preview
from app.handlers.admin.products import _build_preview
from app.keyboards.admin_orders import CALLBACK_ORDER_SEARCH
from app.models.enums import LanguageCode
from app.models.user import User
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.utils.cache import invalidate_categories_cache
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, FakeTelegram, RunningBot, tree_settings
from tests.test_loyalty_journeys import admin_moves, latest_order, to_confirmation

EN = LocalizationService("en")
NAME = "Berry <3 & Ice"
ESCAPED = "Berry &lt;3 &amp; Ice"
CUSTOMER = 7_300_001
TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span",
    "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji",
}  # fmt: skip
TAG = re.compile(r"</?([a-zA-Z][\w-]*)(\s[^<>]*)?>")


def unparseable(text: str) -> str | None:
    """What Telegram would refuse: a ``<`` that opens or closes no supported tag."""
    for index, char in enumerate(text):
        if char == "<":
            match = TAG.match(text, index)
            if match is None or match.group(1).lower() not in TELEGRAM_TAGS:
                return text[index : index + 8]
    return None


class StrictHtmlTelegram(FakeTelegram):
    """Refuses HTML it cannot parse, as the Bot API does: 400 "can't parse entities"."""

    def __init__(self) -> None:
        super().__init__()
        self.refused: list[str] = []

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, SendMessage | EditMessageText) and (bad := unparseable(method.text)):
            self.refused.append(method.text)
            raise TelegramBadRequest(
                method, f"Bad Request: can't parse entities: Unsupported start tag {bad!r}"
            )
        return await super().make_request(bot, method, timeout)


@pytest_asyncio.fixture
async def sessions(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: factory)
    invalidate_categories_cache()
    yield factory
    invalidate_categories_cache()


async def test_a_product_name_with_markup_characters_reaches_every_screen(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        admin = AdminService(session)
        category = await admin.create_category("Liquids")
        brand = await admin.create_subcategory(category_id=category.id, name="Brand")
        product = await admin.create_product(
            category_id=category.id,
            subcategory_id=brand.id,
            name_ru=NAME,
            name_en=NAME,
            name_de=NAME,
            name_uk=NAME,
            description_ru="d",
            description_en="d",
            description_de="d",
            description_uk="d",
            flavor="f",
            volume="30ml",
            nicotine_strength="3mg",
            price=Decimal("5.00"),
        )
        for telegram_id in (CUSTOMER, ADMIN_ID):
            await make_user(session, telegram_id=telegram_id, language=LanguageCode.EN)
        await session.commit()
        category_id, brand_id, product_id = category.id, brand.id, product.id

    telegram = StrictHtmlTelegram()
    bot = RunningBot(sessions, tree_settings(), telegram=telegram)
    await bot.send(CUSTOMER, EN.t("menu.catalog"))
    await bot.press(CUSTOMER, f"category:{category_id}")
    await bot.press(CUSTOMER, f"subcat:{brand_id}")
    await bot.press(CUSTOMER, f"prod:{product_id}")
    await bot.press(CUSTOMER, f"cart:add:{product_id}")
    await bot.send(CUSTOMER, EN.t("menu.cart"))
    assert bot.shows(CUSTOMER, "cart:checkout"), "the cart, and its Checkout button, was shown"

    await to_confirmation(bot, CUSTOMER)  # the order summary
    await bot.press(CUSTOMER, "checkout:confirm")
    async with sessions() as session:
        customer = await session.scalar(select(User.id).where(User.telegram_id == CUSTOMER))
    assert customer is not None
    await admin_moves(bot, (await latest_order(sessions, customer)).id)  # the admin's order card
    await bot.send(ADMIN_ID, EN.t("admin.menu_orders"))
    await bot.press(ADMIN_ID, CALLBACK_ORDER_SEARCH)
    await bot.send(ADMIN_ID, NAME)  # the admin's search, echoed back

    assert telegram.refused == [], f"Telegram would refuse: {telegram.refused}"
    shown = "\n".join(bot.texts(CUSTOMER) + bot.texts(ADMIN_ID))
    assert ESCAPED in shown
    assert NAME not in shown


def test_the_admins_product_previews_escape_what_was_typed() -> None:
    fields = (
        "name_ru", "name_en", "name_de", "name_uk",
        "description_ru", "description_en", "description_de", "description_uk",
        "flavor", "volume", "nicotine_strength", "category_name", "subcategory_name",
    )  # fmt: skip
    data = dict.fromkeys(fields, NAME) | {"category_id": 1, "product_id": 1, "price": "5.00"}

    for preview in (_build_preview(EN, data), build_edit_preview(EN, data)):
        assert unparseable(preview) is None, preview
        assert ESCAPED in preview
        assert NAME not in preview
