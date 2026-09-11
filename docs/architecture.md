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
| `LoyaltyService` (`app/services/loyalty.py`) | accounts, the stamp ledger, exchanging stamps for a free-bottle reward |
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

Refusals a customer can cause — `InsufficientStampsError`, `StaleCardError`,
the `Reward*Error`s, `InvalidPrizeError`, `SelfReferralError`,
`ReferralLoopError` — derive from `LoyaltyError` (a `ValueError`), so a caller
can answer them and still let a plain `ValueError`, a caller bug, surface. The
services reach the database only through repositories.

Business rules — how many stamps an order earns, prize weights — are not
decided in the ledger; callers pass the amounts in. The one rule enforced at
this level is who qualifies: `ReferralService.qualify` accepts only the
referred customer's Completed, paid, post-launch order. SQLite cannot prove the
locking (it ignores `FOR UPDATE`), so `tests/test_loyalty_postgres.py` races
real transactions on PostgreSQL when `VSHOP_TEST_POSTGRES_URL` is set, and
`tests/test_loyalty_scenarios.py` checks on every run that each redemption path
takes the account lock before its first write.

## Stamp card engine

`StampCardService` (`app/services/stamp_card.py`) applies the stamp-card rules;
`StampCardPolicy` carries them, built from the `LOYALTY_*` settings.

- **One trigger.** `AdminOrderService.set_order_status` locks the order row,
  re-reads its status and — when the new status is `Completed` — calls
  `award_for_order` in the same transaction. Status and stamps become durable
  together or not at all, and a racing cancel is refused against the real status.
- **Everything comes from the order row:** status `Completed`,
  `loyalty_eligible` (placed after launch), charged `total_price` above zero.
  Stamps = `floor(total_price / threshold)`, so €39.99 earns 1. An order below
  the threshold books a 0-stamp row — it is still a purchase.
- **Idempotent.** The ledger's unique `order_id` turns a replay into
  `already_awarded`; `Completed` is terminal, so nothing ever needs reversing.
- **No input can grant stamps.** Nothing outside `app/services/` calls the
  ledger or binds a reward, and each booking call has exactly one caller;
  `tests/test_stamp_card.py` pins both. It also requires the status-change
  handler to pass its settings — without them, completion would silently apply
  the default rules.

## Free-bottle rewards

Two steps, one reward row (`user_rewards`) whatever the source:

1. **Unlock** — `StampCardService.claim_free_bottle` spends exactly
   `LOYALTY_STAMPS_REQUIRED` stamps under the account lock and issues an
   available free-bottle reward; the ledger's redemption row names it. A roulette
   free-bottle prize issues the same kind of reward. Passing the card's
   `version` (its latest ledger id) makes one rendered card claimable once, so a
   double tap is refused with `StaleCardError`.
2. **Redeem** — at checkout, `OrderService.place_order_from_cart(reward_id=…)`
   asks `RewardService.plan` (locks and validates the reward before anything is
   written, and picks the dearest product within its price cap), creates the
   order with that unit at €0, and `RewardService.redeem` binds the reward and
   records `discount_amount` and `redeemed_product_id`. One transaction: if any
   step fails, the reward stays available and no order exists. A roulette
   percentage discount goes through the same two calls: its plan takes the
   percentage off the order total (rounded half up to the cent) instead of
   freeing a unit.

## Roulette spin entitlements

`SpinEntitlementService` (`app/services/spin_entitlement.py`) decides which
activity earns a spin; `SpinPolicy` carries the `ROULETTE_INITIAL_FREE_SPIN`,
`ROULETTE_SPIN_EVERY_N_PURCHASES` and `REFERRAL_SPINS` settings. Every grant is a
`roulette_spin_grants` row naming its reason and source — the audit trail — and
a unique constraint per source keeps each to a single grant:

| Source | Granted by | Keyed on |
|---|---|---|
| Welcome spin | `/start`, and a top-up of every customer without one at each bot start (`grant_missing_welcome_spins` in `app/lifecycle.py`) | partial unique index: one `initial_promo` grant per user |
| Every Nth purchase | `AdminOrderService.set_order_status` on `Completed`, right after the stamp award, same transaction | `order_id` |
| Referral | `grant_for_referral`, to the referrer, once the referral qualifies | `(referral_id, user_id)` |

A qualifying purchase is a purchase row in the stamp ledger (Completed, placed
after launch, charged more than €0), numbered among the customer's purchase
rows; the interval in force when the order completes decides, so changing it
never grants for past purchases. A replayed completion, a restart or a race can
never grant twice. `SpinEntitlementService.balance` reports available and used
spins by reason. The referral payout that will call `grant_for_referral` is not
wired yet.

## Roulette prize engine

`RouletteEngine` (`app/services/roulette_engine.py`) draws the prize;
`RouletteService.spin` spends the spin.

- **The server decides.** `PRIZE_CATALOGUE` defines the prizes (+1 and +2
  stamps, 5% and 10% discounts, a free bottle); `RoulettePolicy` weighs them from
  the `ROULETTE_PRIZE_*_WEIGHT` settings; the draw is one integer ticket from
  `secrets.randbelow`, so every chance is exact. `RouletteEngine` requires its
  policy, so the configured odds can never be skipped. A client only asks to
  spend the grant its screen offered (`next_grant_id`) and never supplies a
  prize, type, value or balance; `RouletteService.spin` refuses any prize
  outside the catalogue, and only the engine calls it.
- **One transaction.** Locking the account and the grant, marking it consumed,
  recording the spin (a snapshot of the prize) and applying the prize — ledger
  stamps, or a `user_rewards` row pointing back at the spin — happen together.
  A failure rolls all of it back; the spin stays available.
- **Once per grant.** The grant is locked and `roulette_spins.grant_id` is
  unique. `RouletteEngine.spin` requires the `grant_id`, so every request is
  idempotent: a double tap, or the same Telegram update processed again after a
  restart, finds the grant spent and replays its result (`created=False`). A
  refused spin — no grant, someone else's grant — writes nothing.
- **True values.** `RewardService.use_reward` re-checks what it records: a free
  bottle at its product's price, a discount at exactly its percentage of the
  order's lines and already taken off the total. `loyalty_health` reports any
  spin whose prize is missing or does not match what was won.
- **Real rewards.** A discount is a redeemable `user_rewards` row, used once at
  checkout; a free bottle is the same kind of row the stamp card issues. Prize
  display names are the locale keys `roulette.prize.<code>`.

## Routing

```text
root
 ├── user router
│   ├── /start onboarding
│   ├── catalog / cart
│   ├── my stamp card
│   ├── lucky roulette
│   ├── checkout
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

`/start` → ensure user row → choose language → choose city → main reply keyboard (Catalog / Cart / My Stamp Card / Lucky Roulette / Info).

### Catalog → cart

Catalog → categories → product cards → add to cart → cart (± quantity, remove) → checkout.

### Checkout (FSM)

Name → delivery type (city-dependent) → address → preferred time → phone (contact share or typed) → confirmation → `OrderService.place_order_from_cart` → notify `MANAGER_CHAT_ID` + `ADMIN_IDS`.

Cart row is locked with `SELECT … FOR UPDATE` during placement; FSM `submitted` + process lock reduce double-taps.

### My Stamp Card

🪪 My Stamp Card (`app/handlers/user/stamp_card.py`) draws the card from
`StampCardService.card`, in the order a customer reads it on a phone: the
progress bar toward `LOYALTY_STAMPS_REQUIRED`; the promo directly beneath it;
what to do next (stamps still needed, or a ready card naming the button to tap),
extra stamps and free bottles already saved; and one italic line on how stamps
are earned (`LOYALTY_STAMP_PURCHASE_THRESHOLD`, in the reader's money format,
without zero cents). Every figure is a `StampCard` property — the screen
computes nothing. Opening it is read-only, so repeats are harmless. Below the
card, 🛍 Catalog is the next step and 🔄 Refresh redraws it in place, answering
"up to date" when nothing changed.

On a full card the backend enables 🎁 Claim Free Bottle. Its callback carries
the card's version (latest ledger id); `StampCardService.claim_free_bottle`
decides under the account lock, and the claim is committed — inside a
per-customer `keyed_lock` — before the customer is told. A double tap is
answered "already claimed" (`AlreadyClaimedError`), a card that changed
meanwhile is redrawn (`StaleCardError`), and a malformed payload is refused
before the database is touched. The checkout screens do not offer a saved bottle
yet; `place_order_from_cart(reward_id=…)` supports it at the service level.

### Lucky Roulette

🎰 Lucky Roulette (`app/handlers/user/roulette.py`) shows what the backend
reports: the spins available, the completed orders still needed for the next one
(`SpinEntitlementService.purchases_to_next_spin`), the prizes that can be won
(weight above 0), one line per kind. Opening it is read-only. With a spin,
a full-width 🎰 Spin! button carries the id of the spin on offer
(`RouletteEngine.next_grant_id`) and nothing else — no prize, value or balance
ever travels in a callback. Without one, 🛍 Catalog is the next step. ⬅️ Back
closes the screen, leaving the main menu.

A tap runs `RouletteEngine.spin(user_id, grant_id=…)` inside a per-customer
`keyed_lock`: the server draws, spends the spin and books the prize in one
transaction, committed before anything is shown. Only then comes the suspense —
turning reels, then a drumroll, 0.8 s each, with no buttons to tap — and the
result, read back from the saved spin and reward: the prize (a free bottle is
the jackpot), stamps drawn on the card's own progress bar or a discount or
bottle saved as a reward, and the spins left with 🎰 Spin again — or, with none
left, the countdown to the next spin and 🛍 Catalog. A double tap, a stale screen or the same update delivered twice
finds the spin played and is shown its result; an id that is not the
customer's spends nothing and the roulette is redrawn; a malformed payload never
reaches the database; a database failure is rolled back and the customer told
nothing was lost — the same button retries safely. Won discounts and free
bottles are saved rewards; the checkout screens do not offer them yet.

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
