"""
An admin renames a category and a brand in one language — in every admin language.

The prompt names the language being edited through a ``{language}`` placeholder,
the same word as ``translate``'s own parameter, and the rename once failed on
it for every admin: the generic error, and the name never saved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.middlewares.database
from app.keyboards.admin_categories import CALLBACK_CATEGORY_NAME_PREFIX
from app.keyboards.admin_subcategories import CALLBACK_SUB_NAME_PREFIX
from app.models.category import Category, Subcategory
from app.models.enums import LanguageCode
from app.services.localization import LocalizationService
from app.utils.cache import invalidate_categories_cache
from app.utils.i18n import SUPPORTED_LANGUAGES
from tests.factories import make_category, make_user
from tests.production_bot import ADMIN_ID, RunningBot, tree_settings


@pytest_asyncio.fixture
async def sessions(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: factory)
    invalidate_categories_cache()
    yield factory
    invalidate_categories_cache()


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_an_admin_renames_a_category_and_a_brand_in_one_language(
    sessions: async_sessionmaker[AsyncSession], language: str
) -> None:
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID, language=LanguageCode(language))
        category = await make_category(session, name="Liquids")
        brand = Subcategory(
            category_id=category.id, name_ru="B", name_en="B", name_de="B", name_uk="B"
        )
        session.add(brand)
        await session.commit()
        category_id, brand_id = category.id, brand.id

    i18n = LocalizationService(language)
    bot = RunningBot(sessions, tree_settings())
    await bot.send(ADMIN_ID, i18n.t("admin.menu_categories"))
    screen = max(bot.screens(ADMIN_ID).values(), key=lambda m: m.message_id)
    for data, typed in (
        (f"{CALLBACK_CATEGORY_NAME_PREFIX}{category_id}:de", "Liquids NEU"),
        (f"{CALLBACK_SUB_NAME_PREFIX}{brand_id}:de", "Marke NEU"),
    ):
        await bot.press(ADMIN_ID, data, on=screen)
        await bot.send(ADMIN_ID, typed)

    assert i18n.t("error.generic") not in bot.texts(ADMIN_ID)
    async with sessions() as session:
        renamed_category = await session.get(Category, category_id)
        renamed_brand = await session.get(Subcategory, brand_id)
    assert renamed_category is not None and renamed_brand is not None
    assert (renamed_category.name_de, renamed_brand.name_de) == ("Liquids NEU", "Marke NEU")
    assert (renamed_category.name_ru, renamed_brand.name_ru) != ("Liquids NEU", "Marke NEU")
