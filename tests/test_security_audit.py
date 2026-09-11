"""
Standing security guards.

Each test here corresponds to an attack that was attempted against the running
bot during the security audit. They exist so the property survives refactoring,
not because the code looked wrong.
"""

from __future__ import annotations

import ast
import pathlib
import re
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, _parse_admin_ids
from app.security.admin import is_admin_id, is_admin_user
from app.services.cart import CartService
from app.utils.validators import (
    MAX_DB_INT,
    parse_callback_id,
    parse_nonnegative_int,
    parse_positive_int,
)
from tests.factories import make_category, make_product, make_user

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Telegram allows 64 bytes of callback_data, so this is what an attacker can
# actually put on the wire — not a theoretical value.
TELEGRAM_CALLBACK_LIMIT = 64


def settings_with(admin_ids: list[int]) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        admin_ids=admin_ids,
        manager_chat_id=-1001234567890,
    )


# ------------------------------------------------------- callback id bounds


@pytest.mark.parametrize(
    "raw",
    [
        "9" * 20,
        "9" * (TELEGRAM_CALLBACK_LIMIT - len("cart:add:")),
        str(MAX_DB_INT + 1),
        str(2**63),
        "12345678901",  # 11 digits
    ],
)
def test_out_of_range_ids_are_rejected_before_the_database(raw: str) -> None:
    """
    Regression: these reached the database and raised.

    An id larger than a PostgreSQL ``integer`` cannot name a row, but binding it
    raises — DataError on PostgreSQL, OverflowError on SQLite — which lands in
    the "unexpected" error bucket and writes a full traceback. Any customer could
    trigger that on demand by editing one callback, turning the error log into
    noise and hiding real incidents behind it.
    """
    assert parse_positive_int(raw) is None
    assert parse_nonnegative_int(raw) is None
    assert parse_callback_id(f"cart:add:{raw}", "cart:add:") is None


@pytest.mark.parametrize("raw", ["1", "42", str(MAX_DB_INT)])
def test_ids_inside_the_range_still_parse(raw: str) -> None:
    """The bound must not break ordinary ids."""
    assert parse_positive_int(raw) == int(raw)
    assert parse_callback_id(f"prod:{raw}", "prod:") == int(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "abc",
        "-1",
        "0",
        "1.5",
        "1e9",
        "'; DROP TABLE users;--",
        "1 OR 1=1",
        "../../etc/passwd",
        "%00",
        "NaN",
        "null",
    ],
)
def test_hostile_callback_ids_parse_to_nothing(raw: str) -> None:
    assert parse_positive_int(raw) is None


def test_zero_is_rejected_as_an_id_but_allowed_as_a_page() -> None:
    assert parse_positive_int("0") is None
    assert parse_nonnegative_int("0") == 0


# ------------------------------------------------------------- admin access


def test_admin_ids_fail_closed_when_unset() -> None:
    """An empty ADMIN_IDS must mean nobody, never everybody."""
    cfg = settings_with([])

    assert is_admin_id(12345, cfg) is False
    assert is_admin_id(None, cfg) is False
    assert is_admin_user(None, cfg) is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", []),
        (" ", []),
        ("1,2", [1, 2]),
        ("[1, 2]", [1, 2]),
        ("1,,2", [1, 2]),
        (" 7 , 8 ", [7, 8]),
        (7, [7]),
        ([7, 8], [7, 8]),
    ],
)
def test_admin_ids_accept_the_documented_shapes(value: object, expected: list) -> None:
    assert _parse_admin_ids(value) == expected


@pytest.mark.parametrize("value", ["abc", "true", "1;DROP TABLE", "1.5", '["a"]', '{"a":1}'])
def test_admin_ids_reject_anything_unparseable(value: object) -> None:
    """
    Garbage must stop the process, not silently become an empty allowlist.

    Failing to boot is loud; quietly granting nobody access looks identical to a
    working deployment until an admin tries to use the panel.
    """
    with pytest.raises((ValueError, TypeError)):
        _parse_admin_ids(value)


def test_a_negative_id_can_never_match_a_telegram_user() -> None:
    """Group ids are negative; user ids never are, so a stray group id is inert."""
    cfg = settings_with([-1001234567890])

    assert is_admin_id(-1001234567890, cfg) is True  # the value round-trips
    assert is_admin_id(1001234567890, cfg) is False  # but no user id matches it


# --------------------------------------------------------------- raw SQL


# The startup identity probe is the only place SQL is not a bare literal: it
# takes its statement as a parameter, and interpolates a table name. Both are
# safe because every value comes from a module-level constant — which
# test_identity_probe_tables_are_hardcoded pins, so the exemption cannot quietly
# become unsafe.
RAW_SQL_EXEMPT = {
    "app/database/session.py": "startup identity probe; see the test below",
}


def _relative(path: pathlib.Path) -> str:
    return path.relative_to(APP.parent).as_posix()


def test_no_raw_sql_is_built_from_anything_but_a_literal() -> None:
    """
    The one place injection could enter is ``text()``.

    Every other query goes through the ORM with bound parameters. This fails if
    anyone ever writes ``text(f"... {value}")`` or concatenates into one.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if _relative(path) in RAW_SQL_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "text" or not node.args:
                continue
            argument = node.args[0]
            literal = isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            if not literal:
                offenders.append(
                    f"{_relative(path)}:{node.lineno}: text({ast.unparse(argument)[:60]})"
                )
    assert offenders == [], f"raw SQL built from a non-literal: {offenders}"


def test_identity_probe_tables_are_hardcoded() -> None:
    """
    What the one exemption rests on.

    ``read_database_identity`` interpolates a table name into its SQL. That is
    only safe while the names are compile-time constants — if the tuple ever
    became a parameter, a variable, or anything derived from input, the
    exemption above would be hiding a real injection point.
    """
    tree = ast.parse((APP / "database" / "session.py").read_text(encoding="utf-8"))

    literal_tables: list[str] | None = None
    for node in tree.body:
        targets = getattr(node, "targets", None) or (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_IDENTITY_TABLES":
                value = getattr(node, "value", None)
                if isinstance(value, ast.Tuple | ast.List):
                    literal_tables = [
                        element.value
                        for element in value.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    ]
                    assert len(literal_tables) == len(value.elts), (
                        "_IDENTITY_TABLES contains a non-literal entry"
                    )

    assert literal_tables, "_IDENTITY_TABLES is no longer a module-level literal"
    assert all(name.isidentifier() for name in literal_tables), (
        f"a probe table name is not a plain identifier: {literal_tables}"
    )


def test_no_sql_keyword_is_string_formatted() -> None:
    formatted = re.compile(
        r"""(f["'][^"']*\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b"""
        r"""|["'][^"']*\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b[^"']*["']\s*(\+|%|\.format\())""",
        re.IGNORECASE,
    )
    offenders = [
        f"{_relative(path)}:{index}"
        for path in sorted(APP.rglob("*.py"))
        if _relative(path) not in RAW_SQL_EXEMPT
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if formatted.search(line)
    ]
    assert offenders == [], f"SQL assembled by string formatting: {offenders}"


# ------------------------------------------------------------------- logs


# Fake, but shaped like what the database is sent: a customer's phone number.
CUSTOMER_PHONE = "+49 151 0000 FAKE"
ENGINE_FACTORIES = frozenset(
    {"create_engine", "create_async_engine", "engine_from_config", "async_engine_from_config"}
)


@pytest.mark.asyncio
async def test_sql_logging_never_carries_customer_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    Regression (P1): customer data was written to the logs.

    ``APP_ENV`` defaults to ``development`` — and the deployed ``.env`` said so
    too — which turns SQL echo on, and the echo logged every statement with its
    bound parameters: names, phone numbers, addresses, referral codes. In any
    environment, a ``DBAPIError`` carried them into the traceback of a handled
    error. The bot's engine now hides them, whatever the environment.
    """
    import io
    import logging

    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import DBAPIError

    from app.database import session as database

    development = Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        manager_chat_id=-1001234567890,
        app_env="development",
    )
    monkeypatch.setattr(database, "get_settings", lambda: development)
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    logged = io.StringIO()
    handler = logging.StreamHandler(logged)
    sql_log = logging.getLogger("sqlalchemy.engine")
    sql_log.addHandler(handler)
    engine = database.get_engine()
    try:
        assert engine.sync_engine.echo, "the premise: development echoes SQL"
        async with engine.connect() as connection:
            await connection.execute(sql_text("SELECT :phone"), {"phone": CUSTOMER_PHONE})
            with pytest.raises(DBAPIError) as failure:
                await connection.execute(
                    sql_text("SELECT * FROM no_such_table WHERE phone = :phone"),
                    {"phone": CUSTOMER_PHONE},
                )
    finally:
        sql_log.removeHandler(handler)
        await engine.dispose()

    written = logged.getvalue() + capsys.readouterr().out
    assert "SELECT" in written, "the statements are still echoed"
    assert CUSTOMER_PHONE not in written
    assert CUSTOMER_PHONE not in str(failure.value)


def test_every_engine_hides_bound_parameters() -> None:
    """
    What keeps the regression above fixed: no engine can log customer data.

    The bot, the deployment check and Alembic each build their own engine, and
    every one must pass ``hide_parameters=True``.
    """
    sources = [*sorted(APP.rglob("*.py")), APP.parent / "alembic" / "env.py"]
    engines: list[str] = []
    offenders: list[str] = []
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name not in ENGINE_FACTORIES:
                continue
            where = f"{_relative(path)}:{node.lineno}"
            engines.append(where)
            if not any(
                keyword.arg == "hide_parameters"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                offenders.append(where)
    assert len(engines) >= 3, f"the bot's, the check's and Alembic's engines: {engines}"
    assert offenders == [], f"an engine that can log customer data: {offenders}"


# ------------------------------------------------- cross-customer isolation


@pytest.mark.asyncio
async def test_one_customer_cannot_touch_another_customers_cart(
    session: AsyncSession,
) -> None:
    """
    Cart line ids are sequential and appear in callback data.

    Guessing a neighbour's id must not let an attacker empty their basket.
    """
    category = await make_category(session, name="Liquids")
    product = await make_product(session, category, name_en="Mango", price="10.00")
    victim = await make_user(session, telegram_id=880001)
    attacker = await make_user(session, telegram_id=880002)
    await session.flush()

    cart = CartService(session)
    await cart.add_product(victim.id, product, quantity=3)
    await session.flush()

    view = await cart.get_view(victim.id, language="en")
    assert view is not None and view.lines
    victim_line = view.lines[0].item_id

    assert await cart.remove_item(attacker.id, victim_line) is False
    assert await cart.increase_item(attacker.id, victim_line) is False
    assert await cart.decrease_item(attacker.id, victim_line) is False

    after = await cart.get_view(victim.id, language="en")
    assert after is not None
    assert [line.quantity for line in after.lines] == [3], "the victim's cart moved"


# ------------------------------------------------------------ HTML escaping


@pytest.mark.asyncio
async def test_customer_supplied_text_is_escaped_in_the_manager_notification(
    session: AsyncSession,
) -> None:
    """
    The customer controls their name, address and preferred time.

    Those land in an HTML message sent to the manager group. Unescaped, a
    customer could forge markup in the manager's view of the order — or break
    the send outright with an unbalanced tag.
    """
    from app.models.enums import PaymentMethod
    from app.services.notification import OrderNotificationService
    from app.services.order import OrderService

    category = await make_category(session, name="Liquids")
    product = await make_product(session, category, name_en="Mango", price="10.00")
    user = await make_user(session, telegram_id=880003)
    user.username = "<b>eviluser</b>"
    await session.flush()
    await CartService(session).add_product(user.id, product, quantity=1)
    await session.flush()

    hostile = "<b>Boss</b> <a href='http://evil'>click</a>"
    order = await OrderService(session).place_order_from_cart(
        user,
        customer_name=hostile,
        delivery_type="pickup",
        address="<i>Nowhere</i> & Co",
        preferred_time="<script>alert(1)</script>",
        phone=None,
        payment_method=PaymentMethod.CASH,
    )
    await session.flush()

    settings = settings_with([1])
    text = OrderNotificationService(None, settings).format_new_order_message(  # type: ignore[arg-type]
        order, user
    )

    for injected in ("<b>Boss</b>", "<a href=", "<i>Nowhere</i>", "<script>"):
        assert injected not in text, f"{injected!r} reached the manager unescaped"
    assert "&lt;b&gt;Boss&lt;/b&gt;" in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text, "a bare ampersand would break Telegram's HTML parser"


@pytest.mark.asyncio
async def test_product_names_are_escaped_in_the_manager_notification(
    session: AsyncSession,
) -> None:
    """An admin-authored product name also ends up inside HTML."""
    from app.models.enums import PaymentMethod
    from app.services.notification import OrderNotificationService
    from app.services.order import OrderService

    category = await make_category(session, name="Liquids")
    product = await make_product(session, category, name_en="<b>Mango</b> & Ice", price="10.00")
    user = await make_user(session, telegram_id=880004)
    await session.flush()
    await CartService(session).add_product(user.id, product, quantity=1)
    await session.flush()

    order = await OrderService(session).place_order_from_cart(
        user,
        customer_name="Clara",
        delivery_type="pickup",
        address="Street 1",
        preferred_time="18:00",
        phone=None,
        payment_method=PaymentMethod.CASH,
    )
    await session.flush()

    text = OrderNotificationService(None, settings_with([1])).format_new_order_message(  # type: ignore[arg-type]
        order, user
    )

    assert "<b>Mango</b>" not in text
    assert "&lt;b&gt;Mango&lt;/b&gt;" in text


def test_prices_are_decimals_not_floats() -> None:
    """Money arriving as a float would round in the customer's favour or ours."""
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    for model, column in (
        (Product, "price"),
        (Order, "total_price"),
        (OrderItem, "price"),
    ):
        python_type = model.__table__.c[column].type.python_type
        assert python_type is Decimal, f"{model.__name__}.{column} is {python_type}"
