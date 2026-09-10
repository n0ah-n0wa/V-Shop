# Architecture

## Overview

V-Shop is a layered Telegram bot:

```text
Telegram update
    → Middlewares (log → errors → DB session → i18n)
    → Routers (user | admin)
    → Handlers
    → Services
    → Repositories
    → PostgreSQL
```

Dependencies point inward. Handlers do not talk to SQLAlchemy sessions for business rules beyond injecting `session` into services/repositories.

## Package map

| Package | Responsibility |
|---|---|
| `app/handlers/` | Aiogram routers: parse updates, drive FSM, call services |
| `app/keyboards/` | Reply / inline keyboard builders + callback prefixes |
| `app/middlewares/` | Cross-cutting: logging, errors, session lifecycle, localization |
| `app/filters/` | `IsAdmin`, `LocalizedText` (menu button matching) |
| `app/services/` | Use-cases: cart, catalog, order, admin façade, broadcast, notifications |
| `app/repositories/` | CRUD and query helpers per aggregate |
| `app/models/` | SQLAlchemy ORM + domain enums |
| `app/states/` | FSM `StatesGroup` definitions |
| `app/locales/` | Nested JSON catalogs (flattened to dotted keys) |
| `app/utils/` | Validators, labels, cache, concurrency, Telegram UI helpers |
| `app/errors/` | Classify exceptions → safe localized user messages |
| `app/security/` | Admin ID checks |
| `app/config.py` | Settings |
| `app/bot.py` | Bot + dispatcher factory |
| `app/main.py` | Process entrypoint |

## Middleware order

Registered as outer update middlewares in `app/middlewares/__init__.py`
(first = outermost). **The order is load-bearing, not cosmetic.**

1. **Logging** — timing / request log (`update_id`, kind, `user_id`, duration only;
   never message text or payloads)
2. **PrivateChat** — drops anything that is not a private chat
3. **Error handling** — catches handler failures, answers with safe localized text
4. **Database** — opens `AsyncSession`, commits on success, rolls back on error, closes
5. **Localization** — loads `db_user`, injects `i18n` / `language`

Why 2 sits where it does:

- **Ahead of Database**, so a group update never opens a session or starts a
  transaction.
- **Ahead of Error handling**, so a failure further down can never produce a
  reply *into* a group.

Dispatcher-level `errors` handlers act as a final safety net.

## Group-chat isolation

The bot processes user and admin interactions **only in private chats**. Manager
and review groups are notification destinations, never interfaces.

Enforced centrally, in three places that each close a different route:

| Layer | What it stops |
|---|---|
| `PrivateChatMiddleware` (outer, position 2) | every message and callback from a group, supergroup or channel |
| `notify_user_of_error` | the error notifier answering into a group when aiogram handles an update itself |
| `UserRepository.list_telegram_ids()` | broadcasts reaching a chat recorded with a negative (group) ID |

Outbound notifications still go to `MANAGER_CHAT_ID`, and carry **no inline
keyboards** — a group must never be given buttons to press.

## Customer order status notifications

When an admin changes an order's status, the customer is messaged in the language
stored on their user row — not the admin's.

- Notified on `Accepted`, `Shipped`, `Completed`, `Cancelled`.
- **Not** notified on `Cancelled → New` (the undo), and never when a status is
  re-applied unchanged.
- Delivery is best-effort and isolated: `notify_status_change` never raises, so a
  blocked user or a Telegram outage **cannot roll back the status change**. A
  blocked or deleted user logs at INFO; anything unexpected logs at ERROR.

Implementation: `app/services/customer_notification.py`.

## Statistics

`StatisticsService` assembles the admin dashboard from **nine aggregate queries**
whose count does not grow with order history — no order rows are loaded into the
process. Month boundaries are cut in `APP_TIMEZONE` (default `Europe/Berlin`),
so an order placed at 00:30 local on the 1st belongs to the new month even though
it is still the previous month in UTC.

Product rankings count **distinct completed orders** containing a product, not
units sold, and cover only products that are on sale. See
[admin-guide.md](admin-guide.md#statistics).

## Reviews group

Customers reach the private reviews group through an invite link the bot resolves
on demand (or `REVIEW_INVITE_LINK` verbatim). The group's chat ID never appears
in anything sent to a user. Links are cached in-process for an hour.

## Loyalty persistence

The stamp card, the roulette and the referral programme share one persistence
layer; the tables are described in [database-schema.md](database-schema.md#loyalty).

| Service | Owns |
|---|---|
| `LoyaltyService` (`app/services/loyalty.py`) | accounts, the stamp ledger, free-bottle redemption |
| `RouletteService` (`app/services/roulette.py`) | spin grants, and spending a grant on a prize |
| `RewardService` (`app/services/reward.py`) | listing rewards, binding one to an order |
| `ReferralService` (`app/services/referral.py`) | referral codes, attribution, qualification |

Three rules hold it together:

1. **Lock the customer's account first.** Every mutation starts with
   `LoyaltyService.lock_account` — `SELECT … FOR UPDATE` on the customer's
   `loyalty_accounts` row, refreshing the ORM instance — which serialises one
   customer's loyalty operations inside PostgreSQL.
2. **Idempotency comes from the schema.** Each earning event is tied to its
   source row (order, referral, spin, reward) by a unique constraint. Replaying
   it returns the original row with `created=False` instead of booking it twice.
3. **Validate before writing; never commit.** A refused operation leaves nothing
   behind, and the caller's transaction decides when the work becomes durable.

Business rules — how many stamps an order earns, prize weights, who qualifies —
are not decided in this layer; callers pass the amounts in. SQLite cannot prove
the locking (it ignores `FOR UPDATE`), so `tests/test_loyalty_postgres.py`
races real transactions on PostgreSQL when `VSHOP_TEST_POSTGRES_URL` is set.

## Routing

```text
root
 ├── user router
│   ├── /start onboarding
│   ├── catalog / cart / checkout
│   ├── information
│   └── /admin access-denied for non-admins
└── admin router  (IsAdmin filter + AdminOnlyMiddleware)
    ├── wizard guard (block menu jumps mid-FSM)
    ├── products (add wizard)
    ├── product_manage (list / edit / delete)
    ├── categories
    ├── orders
    ├── broadcast
    ├── settings
    └── panel (/admin menu)
```

## Main user flows

### Onboarding

`/start` → ensure user row → choose language → choose city → main reply keyboard (Catalog / Cart / Info).

### Catalog → cart

Catalog → categories → product cards → add to cart → cart (± quantity, remove) → checkout.

### Checkout (FSM)

Name → delivery type (city-dependent) → address → preferred time → phone (contact share or typed) → confirmation → `OrderService.place_order_from_cart` → notify `MANAGER_CHAT_ID` + `ADMIN_IDS`.

Cart row is locked with `SELECT … FOR UPDATE` during placement; FSM `submitted` + process lock reduce double-taps.

## Admin services (SOLID split)

`AdminService` is a façade over:

- `AdminCatalogService` — categories & products
- `AdminOrderService` — order queries & status
- `AdminUserService` — broadcast recipient IDs

Handlers may use the façade or focused services.

## Localization

- Files: `app/locales/{en,ru,de,uk}.json` — four languages, identical key sets
- Keys flattened to dotted paths (`menu.catalog`)
- `LocalizationService.t(key, **kwargs)` formats strings
- Menu buttons matched via `LocalizedText` against all language variants
- Product names/descriptions are per-language **columns**, resolved by
  `app/utils/product_display.py` — distinct from the locale catalogs

### Localization policy

Every **customer-facing** string goes through `i18n.t()`. Enforced by
`tests/test_localization_audit.py`, which fails the build if a referenced key is
missing, if the catalogs drift apart, or if a handler/keyboard passes a literal
string to Telegram.

**Documented exception — the manager/ops order alert.**
`app/services/notification.py` builds its field labels in English on purpose, and
`app/utils/labels.py` provides `city_label_en` / `delivery_label_en` for it. The
alert's primary destination is `MANAGER_CHAT_ID`, a single shared chat delivered
to every member at once, so there is no per-recipient language to resolve; a
fixed format also lets staff parse alerts at speed. Both modules carry an
`INTENTIONALLY NOT LOCALIZED` marker, and the audit test asserts that marker is
present. Customer-facing city/delivery labels use the localized
`city_label()` / `delivery_label()` in the same module.

## Concurrency & caching

- Process-local `keyed_lock` for confirm actions (checkout, broadcast, product create/edit)
- Category list TTL cache (`app/utils/cache.py`), invalidated on category mutations
- FSM: `MemoryStorage` (single process)

## Error UX

Exceptions are classified (`telegram` / `database` / `network` / `unexpected`). Users only see localized generic messages — never stack traces or raw DB errors.

## Testing

`tests/` uses pytest-asyncio and in-memory SQLite. Factories seed users/products/orders. See `tests/conftest.py`.
