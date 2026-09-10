# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The virtualenv lives at `.venv` (Windows layout: `.venv\Scripts\python.exe`).

```bash
# Setup
pip install -r requirements-dev.txt && pip install -e .

# Run the bot (long polling)
python -m app.main

# Smoke-check wiring + DB + Telegram getMe without entering the polling loop.
# Exit codes: 0 ok, 1 failure, 2 bad/placeholder BOT_TOKEN, 3 Telegram API error.
python -m app.check_startup

# Read-only post-deploy check: drives the real catalog/cart/statistics services and
# prints JSON, to tell "rows missing from Postgres" apart from "rows present but nothing renders".
docker compose run --rm --no-deps bot python -m app.verify_deployment

# Tests
python -m pytest tests -q
python -m pytest tests/test_admin.py -q                              # one file
python -m pytest tests/test_admin.py::test_admin_category_move -q    # one test

# Lint / types (configured in pyproject.toml; installed via requirements-dev.txt)
ruff check .
mypy app          # strict mode

# Migrations
alembic upgrade head
alembic revision --autogenerate -m "describe change"
alembic downgrade -1

# Docker (starts Postgres, runs `alembic upgrade head`, then the bot)
docker compose up --build
```

Tests run against in-memory SQLite (`aiosqlite`) — no Postgres needed. `pytest.ini_options` sets `asyncio_mode = "auto"`, so `async def` tests need no decorator (existing tests still carry `@pytest.mark.asyncio`). [tests/conftest.py](tests/conftest.py) strips every `Settings` env var and disables the `.env` file for the whole session, so tests never see the developer's local configuration — build `Settings(...)` explicitly in tests.

Alembic reads `DATABASE_URL` from `app.config.get_settings()` (see `alembic/env.py`) — the `sqlalchemy.url` in `alembic.ini` is a placeholder and is ignored. Under Docker Compose, the `DATABASE_URL` in `.env` is also ignored: compose's `environment:` overrides it with the internal `db:5432` address.

## Architecture

Layered aiogram 3 bot; dependencies point inward:

```
Update → outer middlewares → routers → handlers → services → repositories → PostgreSQL
```

Entry: [app/main.py](app/main.py) → [app/bot.py](app/bot.py) (`create_bot` / `create_dispatcher`) → [app/lifecycle.py](app/lifecycle.py) (`on_startup` inits the DB engine, verifies connectivity, drops the webhook, calls `getMe`).

### Handler dependency injection

Handlers receive these keys as named kwargs; the names are fixed by the middleware/dispatcher wiring, not by type:

| Key | Source |
|---|---|
| `session` (`AsyncSession`) | `DatabaseMiddleware` |
| `db_user` (`User \| None`), `i18n` (`LocalizationService`), `language` | `LocalizationMiddleware` |
| `settings` (`Settings`) | `dispatcher["settings"]` workflow data |
| `is_admin` | `AdminOnlyMiddleware` (admin router only) |

### Middleware order and group-chat isolation

Outer middleware order is significant and set in [app/middlewares/__init__.py](app/middlewares/__init__.py): **Logging → PrivateChat → ErrorHandling → Database → Localization**.

- `PrivateChatMiddleware` silently drops every update not from a private chat — groups get no reply, no session, no FSM state. It sits ahead of Database (a group update never opens a session) and ahead of ErrorHandling (a failure can never produce a reply into a group). The rule lives in [app/utils/chat_scope.py](app/utils/chat_scope.py); an undeterminable chat fails closed. `notify_user_of_error` re-checks it at the send site. Outbound messages to the manager/reviews groups are unaffected.
- ErrorHandling is *outside* Database so the session has already rolled back before the user-facing message is sent.
- `tests/test_documentation.py` parses this registration order and compares it to the numbered list in `docs/architecture.md` — change both together.

### Transactions

`DatabaseMiddleware` commits on handler success and rolls back on any exception, so handlers/services normally only `flush()`. Deliberate mid-handler commits exist where a Telegram side effect must follow a durable write:

- `OrderService.place_order_from_cart` — commits so the order is durable before manager notifications go out. It takes a `SELECT … FOR UPDATE` on the cart row (`CartRepository.get_by_user_id_with_items(..., for_update=True)`).
- Admin order status change ([app/handlers/admin/orders.py](app/handlers/admin/orders.py)) — commits *before* `CustomerOrderNotificationService` tells the customer; that service swallows all Telegram failures so they can never undo the status.
- Broadcast confirm — commits to release the transaction before the long Telegram fan-out.

### Loyalty persistence

Six tables (`loyalty_accounts`, `loyalty_transactions` = stamp ledger, `roulette_spin_grants`, `roulette_spins`, `user_rewards`, `referrals`) behind `LoyaltyService`, `RouletteService`, `RewardService` and `ReferralService` in `app/services/`. Rules that are easy to break:

- Every mutation of a customer's loyalty state calls `LoyaltyService.lock_account` first — it flushes pending changes, then `SELECT … FOR UPDATE` with `populate_existing` (without the refresh, the ORM would add to a stale in-memory balance).
- Idempotency is structural: each ledger row / spin grant / reward points at its source row under a unique constraint, and replays return `created=False`. Validate before writing — never raise after a partial write, because a handler may catch the error and `DatabaseMiddleware` would commit the remainder.
- The loyalty services reach the database only through repositories. Refusals a customer can cause (`InsufficientStampsError`, `StaleCardError`, `Reward*Error`, `InvalidPrizeError`, the referral errors) derive from `LoyaltyError`, a `ValueError`; a plain `ValueError`/`LookupError` from them is a caller bug. Money computed in code goes through `to_money` in `app/utils/validators.py`.
- The CHECK constraints embed enum values; `tests/test_loyalty_schema.py` requires the model and migration CHECK texts, unique constraints, indexes and foreign keys to be identical, and that the migration touches only its own tables.
- `roulette_spins`, `user_rewards` and `loyalty_transactions` reference grant/spin/reward through composite FKs onto `(id, user_id)`, so the database refuses cross-customer references. Compare enum values with `==`, never `is` — a plain string equals a `StrEnum` member but is not the same object.
- `LoyaltyAccount` sets `eager_defaults=True` so the DB-maintained `updated_at` is fetched back on UPDATE; without it, reading the attribute after a flush raises `MissingGreenlet`.
- The loyalty migration's downgrade refuses while customer loyalty data exists; `alembic -x allow_loyalty_data_loss=true downgrade …` overrides it (dump first).
- Business rules sit above the ledger, which takes amounts as arguments. Stamp-card rules live in `StampCardService` / `StampCardPolicy` (`app/services/stamp_card.py`, fed by the `LOYALTY_*` settings). Stamps are awarded only from `AdminOrderService.set_order_status` on `Completed` — same transaction, order row locked — never from outside `app/services/`. `tests/test_stamp_card.py` pins each booking call to its single caller and requires the status-change handler to build `AdminService(session, settings=settings)`: without `settings`, completion silently applies the default rules. Only orders with `orders.loyalty_eligible` (placed after launch) earn, and `ReferralService.qualify` accepts only a Completed, paid, eligible order.
- Free bottles (stamp card or roulette — same `user_rewards` row) are redeemed only at checkout: `OrderService.place_order_from_cart(reward_id=…)` → `RewardService.plan_free_bottle` (validates and picks the dearest product within the cap, before any write) → order with that unit at €0 → `RewardService.redeem` records `discount_amount` + `redeemed_product_id`. Claims take `card_version` (latest ledger id) so one rendered card claims once.
- SQLite ignores `FOR UPDATE`. Concurrency is only proven by the opt-in `tests/test_loyalty_postgres.py` (`VSHOP_TEST_POSTGRES_URL`, database name must end in `_test` and be empty — it creates and drops the schema); `tests/test_loyalty_scenarios.py` checks on every run that each redemption path takes the account lock before its first write, alongside the owner's other end-to-end scenarios. `loyalty_health` in `app/verify_deployment.py` cross-checks the tables after a deploy.

### Router composition

- [app/handlers/user/__init__.py](app/handlers/user/__init__.py) — `start` first (so `/start` wins), then catalog/cart/checkout/info, and `admin_guard` last (the non-admin `/admin` denial must live outside the admin router's filters).
- [app/handlers/admin/__init__.py](app/handlers/admin/__init__.py) — double-gated: router-level `IsAdmin` filters *and* `AdminOnlyMiddleware` (drops unauthorized updates silently). `wizard_guard` is included before the section routers so menu taps mid-FSM are intercepted; `panel` is last.

### Catalog hierarchy and visibility

Category → Subcategory (brand) → Product ([app/models/category.py](app/models/category.py)). `products.category_id` is `NOT NULL` on every row and kept in step with the brand's category; `products.subcategory_id` is nullable because pre-hierarchy products have no brand. `categories.name` is a deprecated single-language column still kept in sync on write until a later contract migration drops it.

"Sellable" means the product, its category, *and* its subcategory (if any) are all active. That rule has exactly one definition — `only_sellable_products()` in [app/repositories/visibility.py](app/repositories/visibility.py) — used by the checkout guard and the statistics rankings. Reuse it; don't hand-write the join again.

### Orders

Status transitions are defined in `ALLOWED_TRANSITIONS` in [app/utils/order_status.py](app/utils/order_status.py): `New → Accepted → Shipped → Completed`, cancel from any active state, and `Cancelled → New` as an undo. `Completed` is terminal (revenue statistics read completed orders). Customers are notified only for the statuses in `STATUS_MESSAGE_KEYS` ([app/services/customer_notification.py](app/services/customer_notification.py)).

Manager/admin new-order alerts ([app/services/notification.py](app/services/notification.py)) go to `MANAGER_CHAT_ID` plus each `ADMIN_IDS` chat (deduplicated), and are **intentionally hardcoded English** — don't move those labels into the locale catalogs. Everything customer-facing is localized.

Statistics month boundaries use `APP_TIMEZONE` ([app/utils/periods.py](app/utils/periods.py)), never the server clock.

### Localization

- `app/locales/{en,ru,de,uk}.json`, nested JSON flattened to dotted keys (`menu.catalog`) by [app/utils/i18n.py](app/utils/i18n.py); catalogs are `lru_cache`d.
- Every user-facing string goes through `i18n.t(key, **kwargs)`. Missing keys fall back to English, then return the key itself.
- **All four locale files must have identical key sets** — `test_locales_are_in_sync` fails otherwise. Adding a key means adding it to en/ru/de/uk.
- Reply-keyboard buttons are matched with the `LocalizedText("some.key")` filter, which compares against every language's translation of that key (so a user who switches language mid-session still matches).
- Distinct from locale strings: category, subcategory and product names/descriptions are per-language **columns** (`name_ru`/`name_en`/`name_de`/`name_uk`, `description_*`) resolved by [app/utils/product_display.py](app/utils/product_display.py).

### Callback data

Namespaced colon-delimited strings declared as `CALLBACK_*` constants in `app/keyboards/*.py` and imported by handlers — never re-typed as literals. Namespaces: `lang:`, `city:`, `catalog:`, `category:`, `subcat:`, `prod:`, `cart:`, `checkout:`, `info:`, and admin `admin:product:`, `admin:cat:`, `admin:sub:`, `admin:ord:`, `admin:bc:`, `admin:st:`. Keep them short — Telegram caps callback data at 64 bytes. Page sizes (`PRODUCTS_PAGE_SIZE`, `ORDERS_PAGE_SIZE`) also live in the keyboard modules, alongside `clamp_page`/`page_count` helpers in [app/utils/telegram_ui.py](app/utils/telegram_ui.py).

### FSM and double-submit protection

`MemoryStorage` — state is process-local, so the bot is single-process by design. Two guards, both required for confirm steps:

- `confirm_once(state, lock_key=...)` ([app/utils/confirm.py](app/utils/confirm.py)) — yields FSM data only on the first submission, `None` afterwards; resets `submitted` on exception so the user can retry.
- `keyed_lock(key)` ([app/utils/concurrency.py](app/utils/concurrency.py)) — process-local async lock (checkout uses it directly; admin wizards get it through `confirm_once`).

State groups are in `app/states/`. `ADMIN_WIZARD_STATES` ([app/states/admin.py](app/states/admin.py)) aggregates every admin wizard group and drives the wizard guard — a new admin wizard must be registered there or menu taps will corrupt its state. Admin section entry handlers also filter on `~StateFilter(*ADMIN_WIZARD_STATES)`.

### Services and repositories

`BaseRepository` ([app/repositories/base.py](app/repositories/base.py)) provides generic CRUD; per-aggregate repos add queries. `AdminService` is a backwards-compatible façade over `AdminCatalogService` / `AdminOrderService` / `AdminUserService` in [app/services/admin/](app/services/admin/) — prefer the focused services in new code.

### Errors

[app/errors/classify.py](app/errors/classify.py) maps exceptions to `telegram` / `database` / `network` / `unexpected` and to a locale key (`error.*`). Users only ever see the localized generic message; stack traces stay in logs. A dispatcher-level `errors` handler is the final safety net.

### Caching

Category lists use a 60s process-local TTL cache ([app/utils/cache.py](app/utils/cache.py)). Any category mutation must call `invalidate_categories_cache()` — tests that touch categories clear it in an autouse fixture.

## Tests that pin docs and migrations to the code

Two suites fail on changes that look unrelated to them:

- [tests/test_documentation.py](tests/test_documentation.py) — every `Settings` field (as `` `UPPER_NAME` ``), enum value (order status, payment method, language, city), table, and index must appear in `docs/`/`README.md`. Every migration revision must be listed in `docs/deployment.md`, and the middleware order must match `docs/architecture.md`. **This test also scans CLAUDE.md**: any run of language codes (slash/comma/brace list) must name all four — never write a partial list.
- [tests/test_migrations.py](tests/test_migrations.py) — static checks on `alembic/versions/`: `upgrade()` may not `drop_table`/`drop_column`/`drop_constraint` or run destructive raw SQL (expand/contract: removal goes in a separate later migration). Index drops must be whitelisted in `ALLOWED_INDEX_DROPS` with a reason. `downgrade()` may not be empty, the chain must stay linear with one head, file names must start with the revision id, and every model table must be created by some migration (tests use `create_all`, so a missing migration only fails on deploy).

## Deployment safety

- Compose pins the project name (`vshop`) and the Postgres volume (`${POSTGRES_VOLUME_NAME:-vshop_pgdata}`) so data doesn't depend on the checkout directory name. Don't rename either without updating `docs/deployment.md`.
- `docker compose down -v`, `docker volume prune` and `docker system prune` destroy the database — never run them. Backup and restore (`pg_dump`/`pg_restore`) are in `docs/deployment.md`.
- Any variable `docker-compose.yml` reads must be declared (live or commented) in `.env.example`.
- `docker/ca-certificates/` lets the image build trust a TLS-intercepting proxy's CA. `*.crt` is gitignored there — never commit a certificate.

## Conventions

- Parse mode is HTML globally (`DefaultBotProperties`). Interpolate any user- or DB-supplied value through `e()` from [app/utils/html.py](app/utils/html.py).
- Enums are `StrEnum` persisted **by value** (`native_enum=False` + `enum_values()` from [app/models/types.py](app/models/types.py)) — `ru`, `berlin`, `New`, not `RU`/`BERLIN`/`NEW`.
- `Decimal` for money end to end (`Numeric(10,2)`); never floats.
- Delivery options are city-gated: Berlin → `pickup`/`courier`, other cities → `postal`/`service` (`delivery_allowed_for_city` in [app/services/order.py](app/services/order.py)); enforce it server-side, not only in the keyboard.
- FK integrity intentionally blocks deletes: a category with subcategories or products, a subcategory with products, and a product referenced by `order_items` cannot be deleted (`ON DELETE RESTRICT` → `CategoryInUseError` / `SubcategoryInUseError` / `ProductInUseError` in [app/services/admin/exceptions.py](app/services/admin/exceptions.py)).
- New ORM models must be exported from [app/models/__init__.py](app/models/__init__.py) and imported in `alembic/env.py`, or autogenerate will miss them.
- `ADMIN_IDS` fail-closes: empty means nobody has admin access. It accepts `1,2`, `[1, 2]`, or a single int.
- Extended docs live in [docs/](docs/) (installation, architecture, database schema, configuration, deployment, admin guide) and are kept current — update them alongside structural changes (several facts are enforced by the tests above).
