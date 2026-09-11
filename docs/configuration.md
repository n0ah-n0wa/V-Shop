# Configuration

Settings are loaded from environment variables and optional `.env` via Pydantic Settings (`app/config.py`).

Never commit real secrets. `.env` is gitignored; use `.env.example` as a template.

## Required variables

| Variable | Type | Description |
|---|---|---|
| `BOT_TOKEN` | string | Telegram Bot API token from BotFather |
| `DATABASE_URL` | string | Async SQLAlchemy URL, e.g. `postgresql+asyncpg://user:pass@host:5432/dbname` |
| `MANAGER_CHAT_ID` | int | Chat ID for new-order notifications (private user or group/supergroup) |

## Authorization

| Variable | Type | Default | Description |
|---|---|---|---|
| `ADMIN_IDS` | list of ints | `[]` | Telegram user IDs allowed to use `/admin` |

Formats accepted:

```env
ADMIN_IDS=123456789
ADMIN_IDS=123456789,987654321
ADMIN_IDS=[123456789, 987654321]
```

Empty `ADMIN_IDS` fail-closes: nobody gets the admin panel.

Admin IDs also receive new-order notifications in private chat (deduplicated with `MANAGER_CHAT_ID`).

## Reviews group

Both optional. With neither set, the Reviews button reports that reviews are
unavailable rather than failing.

| Variable | Type | Default | Description |
|---|---|---|---|
| `REVIEW_GROUP_CHAT_ID` | int | unset | Private reviews group. The bot creates an invite link for it on demand. |
| `REVIEW_INVITE_LINK` | string | unset | A pre-made invite link, used verbatim. Set this when the bot can invite members but cannot manage links. |

The group's chat ID is **never** sent to a customer — only the invite link, and
only inside a URL button. Links are cached in-process for an hour.

## Application

| Variable | Type | Default | Description |
|---|---|---|---|
| `APP_ENV` | string | `development` | Environment name; `development` / `dev` / `local` enable SQL echo — statements only: bound parameters (customer data) are never logged, in any environment. Set `production` for a deployment |
| `LOG_LEVEL` | string | `INFO` | Root logging level (`DEBUG`, `INFO`, `WARNING`, …) |
| `TELEGRAM_SSL_VERIFY` | bool | `true` | Verify TLS when calling `api.telegram.org` |
| `APP_TIMEZONE` | string | `Europe/Berlin` | IANA zone used for statistics month boundaries |
| `CURRENCY_SYMBOL` | string | `€` | Symbol shown beside money figures on the statistics dashboard |

Set `TELEGRAM_SSL_VERIFY=false` only if a local network intercepts HTTPS. Never disable verification in production.

`APP_TIMEZONE` decides when a reporting month starts and ends. With the default,
an order placed at 00:30 Berlin time on 1 September belongs to September even
though it is still 31 August in UTC. An unrecognised zone name does not stop the
bot: it logs and falls back to `Europe/Berlin`, then to UTC. The zone database
ships with the `tzdata` package (a pinned dependency), so the value resolves the
same way on Windows, Alpine and Debian.

`CURRENCY_SYMBOL` sets the symbol only; where it sits and how the number is
punctuated follow the reader's language — `€1,234.56` in English, `1.234,56 €`
in German, `1 234,56 €` in Russian and Ukrainian. Those conventions live in the
`format` section of each locale catalog.

## Loyalty stamp card

All optional; these are the defaults.

| Variable | Type | Default | Description |
|---|---|---|---|
| `LOYALTY_STAMP_PURCHASE_THRESHOLD` | decimal | `20.00` | Charged order total that earns one stamp, in whole multiples: €20 → 1, €39.99 → 1, €40 → 2. At least `1.00` |
| `LOYALTY_STAMPS_REQUIRED` | int | `10` | Stamps that unlock one free bottle |
| `LOYALTY_FREE_BOTTLE_MAX_PRICE` | decimal | `20.00` | Most expensive product a free bottle may cover; snapshotted onto each reward when issued |

Stamps are booked when an admin marks an order **Completed**, from the order's
charged total — after any discount, and a free bottle is a €0 line. An order
charged €0 is not a purchase. Orders that existed before migration
`8e4c1a7b2d95` never earn stamps. Changing a value affects what happens next;
stamps and rewards already booked never change.

Money values take at most two decimal places and eight whole digits, and the
threshold is at least `1.00` — a mistyped `0.2` would otherwise stamp every
order a hundredfold. An invalid value stops the bot at startup, never at the
first completed order.

The customer's 🪪 My Stamp Card shows these values. Its promo — "get your 11th
bottle free" — is worded for `LOYALTY_STAMPS_REQUIRED` between 10 and 19 in all
four languages; outside that range, review the `stamp_card.promo` strings.

## Roulette spins

All optional; these are the defaults.

| Variable | Type | Default | Description |
|---|---|---|---|
| `ROULETTE_INITIAL_FREE_SPIN` | bool | `true` | Every customer gets one welcome spin — once, ever |
| `ROULETTE_SPIN_EVERY_N_PURCHASES` | int | `5` | Every Nth qualifying completed purchase grants a spin (the 5th, 10th, …); `0` turns purchase spins off |
| `REFERRAL_SPINS` | int | `1` | Spins the referrer gets for a qualified referral: `1`, or `0` for none |

A qualifying purchase is one the stamp card books: a **Completed** order placed
after the loyalty launch and charged more than €0. The milestone is decided when
the order completes, with the interval in force then — changing it never grants
spins for past purchases. Welcome spins are granted at `/start` and, for every
customer still without one, each time the bot starts; nobody ever gets a second.
A negative value, or `REFERRAL_SPINS` above `1`, stops the bot at startup.

## Roulette prizes

All optional; these are the defaults. A prize's chance is its weight divided by
the sum of all weights — the defaults add up to 100, so they read as percentages.

| Variable | Type | Default | Prize |
|---|---|---|---|
| `ROULETTE_PRIZE_STAMP_1_WEIGHT` | int | `40` | +1 stamp |
| `ROULETTE_PRIZE_STAMP_2_WEIGHT` | int | `25` | +2 stamps |
| `ROULETTE_PRIZE_DISCOUNT_5_WEIGHT` | int | `20` | 5% off one order |
| `ROULETTE_PRIZE_DISCOUNT_10_WEIGHT` | int | `10` | 10% off one order |
| `ROULETTE_PRIZE_FREE_BOTTLE_WEIGHT` | int | `5` | One free bottle up to `LOYALTY_FREE_BOTTLE_MAX_PRICE` |

`0` takes a prize out of the roulette. At least one weight must be above `0`,
and none may be negative or above 1,000,000 — otherwise the bot stops at
startup. The prizes themselves are defined in code (`PRIZE_CATALOGUE` in
`app/services/roulette.py`), never in a handler, and a spin can record no other
prize. A won discount is a reward redeemed on one later order: its percentage of
the order total, rounded half up to the cent.

## Referrals

All optional; these are the defaults.

| Variable | Type | Default | Description |
|---|---|---|---|
| `REFERRAL_REWARD_STAMPS` | int | `2` | Stamps the referrer gets when a referral qualifies; `0` for none |
| `REFERRED_USER_START_STAMPS` | int | `2` | Stamps the referred customer gets at the same moment; `0` for none |

A referral is attributed when a brand-new customer — one who has never placed an
order — opens the bot through a friend's link (`https://t.me/<bot>?start=ref_<code>`).
It qualifies at that customer's first **Completed**, paid, post-launch order, and
both sides are paid then, once; the referrer also gets the `REFERRAL_SPINS`
roulette spin. Nothing is paid at sign-up, so a second Telegram account earns
nothing on its own. A value below `0` or above `100` stops the bot at startup.
Referral codes are random and do not expire.

## Docker Compose extras

Compose can override DB credentials via:

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_DB` | `vshop` | Database name |
| `POSTGRES_USER` | `vshop` | Database user |
| `POSTGRES_PASSWORD` | `vshop` | Database password |
| `POSTGRES_PORT` | `5432` | Host port published for Postgres |
| `POSTGRES_VOLUME_NAME` | `vshop_pgdata` | **Docker volume holding the database.** Set this to your existing volume when upgrading a deployment that predates the pinned name — see [Deployment](deployment.md#upgrading-an-existing-deployment). |

The bot service forces:

```text
DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
```

so the container always talks to the Compose `db` service by hostname.

### `DATABASE_URL` precedence

Compose's `environment:` block outranks `env_file:`, and a real environment
variable outranks the `.env` file inside Pydantic Settings. The consequence:

| Launch mode | `DATABASE_URL` in effect |
|---|---|
| `docker compose up` | `…@db:5432/…` from `docker-compose.yml` — the `.env` value is ignored |
| `python -m app.main` on the host | the `.env` value |

These are **different databases** unless the host URL happens to point at the
same Postgres. Pick one launch mode per environment. The value actually in use
is logged at startup (`Database identity: url=…`), so a mismatch is visible in
the first few log lines rather than as missing data.

## Project and volume naming

`docker-compose.yml` pins both:

```yaml
name: vshop                                        # Compose project
volumes:
  pgdata:
    name: ${POSTGRES_VOLUME_NAME:-vshop_pgdata}    # actual Docker volume
```

Without these, Compose derives both from the *directory name*, so deploying the
same code from `/opt/vshop` and `/opt/V-Shop` uses two different databases.

## Example `.env` (local Docker)

```env
BOT_TOKEN=123456:AA...
DATABASE_URL=postgresql+asyncpg://vshop:vshop@db:5432/vshop
ADMIN_IDS=123456789
MANAGER_CHAT_ID=123456789
APP_ENV=development
LOG_LEVEL=INFO
TELEGRAM_SSL_VERIFY=true
```

## Example `.env` (local Python + Compose DB)

```env
BOT_TOKEN=123456:AA...
DATABASE_URL=postgresql+asyncpg://vshop:vshop@localhost:5432/vshop
ADMIN_IDS=123456789
MANAGER_CHAT_ID=-1001234567890
APP_ENV=development
LOG_LEVEL=DEBUG
TELEGRAM_SSL_VERIFY=true
```

## Finding chat IDs

- **Private user ID**: message [@userinfobot](https://t.me/userinfobot) or similar.
- **Group ID**: add the bot to the group, send a message, inspect updates, or use a helper bot. Group IDs are typically negative (e.g. `-100…`).
