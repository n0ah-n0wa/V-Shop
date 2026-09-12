# V-Shop — production-readiness audit, 2026-09-12

**Verdict: approved for release, on one condition.** The owner must set `POSTGRES_VOLUME_NAME=v-shop_pgdata` in `.env` before the next deploy. Compose now refuses to start without it; that refusal is the P0 fix. Every P0 and P1 finding is fixed and covered by a test that fails on the pre-fix code (HEAD `22de079`). The complete CI is green. Nothing is committed.

Scope: architecture, database, loyalty, roulette, referrals, security, localization (RU, EN, DE, UK), testing, deployment and persistence. Specialist sub-audits covered architecture, database, roulette/referrals, and security/tests/localization. Every finding they reported was re-checked against the code before it was classified.

## Findings

### P0 — release blockers (fixed)

| ID | Area | Finding | Fix | Proof |
|---|---|---|---|---|
| D-1 | Persistence | Compose named the database volume `${POSTGRES_VOLUME_NAME:-vshop_pgdata}` and created it if missing. On this host the data lives in `v-shop_pgdata`, so a deploy from an `.env` without that name started the bot on a **new, empty** volume — the "catalog disappears after an update" incident. | The volume is `external: true` and its name is required: `${POSTGRES_VOLUME_NAME:?…}`, with no default. Compose never creates, renames or deletes it, and a missing or unknown name stops `docker compose up`. Runbook, README, `.env.example`, configuration and installation docs updated; `tests/test_documentation.py` pins the compose shape. | Compose refuses when the name is unset or the volume is missing. Nine-step rehearsal below. |

### P1 — must fix before release (fixed)

| ID | Area | Finding | Fix | Proof |
|---|---|---|---|---|
| SEC-1 | Security / HTML | The cart screen put product names into HTML unescaped. A name containing `<` or `&` made Telegram reject the cart, so the customer never reached Checkout and was told "Could not reach Telegram". | `e(line.name)` in `format_cart_text`. The admin product previews and the order-search echo are escaped too. | `tests/test_html_escaping.py` refuses HTML the way the Bot API does, across catalog → cart → summary → order → admin card → search. Fails on HEAD. |
| L-1 | Localization / admin | Four admin templates use a `{language}` placeholder, which collided with `translate()`'s own `language` parameter (`TypeError`). Renaming a category or brand in one language failed for every admin, in all four languages, and nothing was saved. | `translate`, `t`, `get` and `plural` take their key (and language or count) positional-only. | `test_every_text_renders_with_its_placeholders_filled` renders every key in every catalog. `tests/test_admin_names_per_language.py` renames through the dispatcher in RU, EN, DE and UK. Both fail on HEAD. |
| T-1 | Testing / concurrency | Lock correctness is proven only by the PostgreSQL suites, which skip by default. With the roulette account/grant locks or the attribution lock removed, the default run stayed green. | The PostgreSQL suites ran in this CI with 0 skipped. `VSHOP_TEST_NO_SKIPS=1` now fails a session on any skip (`tests/conftest.py`). `tests/test_loyalty_guards.py` checks the lock order of a spin and of an attribution on every run. `alembic check` runs against the migrated schema in the PostgreSQL suite. The release checklist in `docs/deployment.md` requires the PostgreSQL run. | The switch fails a skipping run with exit 1. PostgreSQL run: 1936 passed, 0 skipped. |

### P2 — should fix (fixed)

- **Checkout:** the unexpected-failure branch read an expired `user` (A2).
- **Qualifying purchase:** "what counts as a qualifying purchase" was restated in three places. There is now one rule, `purchase_disqualification` / `is_qualifying_purchase` (A3).
- **Ledger invariant:** it raised a customer-refusal type; it now raises `LedgerInvariantError`, a bug (A4).
- **Roulette handler:** it now commits unconditionally (R-3).
- **Lock registry:** pruning could split a `keyed_lock` mid-handover; a reference count fixes it (R-5).
- **Referral news:** a failed admin screen held back the referral payout news; it no longer does (R-6).
- **`verify_deployment`:** five new integrity cross-checks between orders and loyalty rows (DB-1).
- **Admin product wizards:** no length limit, so PostgreSQL would refuse VARCHAR(255)/(64) values at the last step. `text_limit` now refuses them, with a localized message in all four languages.
- **Tests added where nothing would have caught a removed guard:**
  - checkout double-tap: one order, and the second tap is told;
  - notify-once on a repeated status tap;
  - `confirm_once`;
  - alert chats and de-duplication;
  - cache invalidation;
  - rollback before the answer when a reward was used meanwhile;
  - configured card rules at the claim;
  - milestone numbering;
  - the reward price-cap re-check;
  - migration-guard bypasses (`batch_op.drop_*`, `sa.text`, `bind.execute`, helper functions, SQL constants, import aliases, unreadable SQL);
  - `alembic check` against the migrated schema.

### P3 — optional (fixed)

- The admin order card showed a literal `\n` in all four languages; fixed, and a test forbids escaped newlines in catalogs.
- Admin callback parsers used `isdigit()`; they now use `parse_positive_int`.
- `.env.example` now ships `APP_ENV=production`.
- Weak test assertions tightened: checkout money, the middleware doc list (now compared in full), and an always-true localization assertion.
- A malformed order-list page answered twice; now once (A14).

### Deferred (left open deliberately)

| ID | P | Item |
|---|---|---|
| S1 / A1 | P2 | `place_order_from_cart` commits, then re-reads the order. If the re-read fails, the order exists but the customer is told it failed, the manager is not alerted, and a retry says the cart is empty. This needs a database failure in the milliseconds after the commit. Fix idea: after the commit, never take the failure branch. |
| L2 | P2 | Shop screens (product card, cart, summary, admin card) show a bare Decimal: no currency symbol and an English decimal point. Loyalty screens use `format_amount`. This is cosmetic and predates the audit; changing it affects `test_shop_lifecycle` pins. |
| R-1 / O1 | Owner decision | A brand-new customer's welcome spin can win a free bottle (5% weight) and order it for €0. Options: keep it; refuse a €0 roulette-only order without a prior paid order; a separate welcome prize table; or weight 0 for the welcome spin. |
| — | P3 | Remaining P3 items:<br>• A price change between the summary and Confirm is charged without showing the new total.<br>• `verify_deployment` prints ids unbounded.<br>• The image runs as root.<br>• There is no update-concurrency limit or throttle.<br>• Broadcasts drop Telegram entities.<br>• German admin texts mix du and Sie.<br>• de/ru/uk use a plain space before €.<br>• PostgreSQL DETAIL text can reach the logs.<br>• Minor naming and refactor notes from the architecture and database passes. |

## Persistence: can the catalog still disappear?

**Static review.** The application never recreates the database, drops tables, truncates, reseeds or re-points itself:

- There is no `create_all`, `drop_all`, `TRUNCATE` or seeding on any startup path.
- `DROP` appears only in migration downgrades, and the loyalty downgrades refuse while customer data exists.
- The start-up loyalty backfill is `INSERT … SELECT … ON CONFLICT DO NOTHING`: it never updates an existing row and never touches orders.
- Compose overrides `DATABASE_URL` to `db:5432`.
- The volume is external and must be named, so Compose cannot swap it for a new one.

**Rehearsal.** A scratch Compose project, run from its own directory with a placeholder token. It started from the pre-loyalty schema and a populated shop, then ran the new code on the same volume:

| Step | Revision | Categories / brands / products | Seeded orders / items / cart items | Loyalty rows | Postgres system id |
|---|---|---|---|---|---|
| 1. Populate (old code) | f6b1d4e8a207 | 3 / 6 / 30 — digests A | 120 / 235 / 20 — digests B | — | 7684586194773921826 |
| 2–4. Run, restart, verify | f6b1d4e8a207 | identical (A) | identical (B) | — | same |
| 5–6. Apply migrations (new image), verify | c5d2e8f1a6b3 | identical (A) | identical (B) | 50 accounts, 50 welcome spins, 0 seeded orders eligible | same |
| Customer journey (3 customers: 2 seeded, 1 referred newcomer) | c5d2e8f1a6b3 | identical (A) | identical (B); 7 new orders | 51 accounts, 11 ledger rows, 53 grants, 2 spins, 1 reward, 1 referral — digests C | same |
| 7–9. Rebuild image, down/up, restart, verify | c5d2e8f1a6b3 | identical (A) | identical (B) | identical (C) | same |

The digests are MD5s over each table's original columns (`checksum_catalog.sql`, `checksum_loyalty.sql`). The seeded-users digest changed only at the journey step: two seeded customers used the bot, and `UserService.ensure_user` refreshes their Telegram `username` and `first_name`. The count stayed 50, and the digest was identical across the redeploy. `verify_deployment` reported every loyalty integrity counter as 0.

## CI verification (final tree)

| Step | Result |
|---|---|
| `ruff format --check --no-cache .` | 241 files formatted |
| `ruff check --no-cache .` | clean |
| `mypy app` (strict, fresh cache) | 151 files, no issues |
| pytest, SQLite | 1897 passed, 39 skipped (the PostgreSQL opt-ins) |
| pytest, PostgreSQL, `VSHOP_TEST_NO_SKIPS=1` | **1936 passed, 0 failed, 0 skipped** |
| — by category | concurrency 39, migrations/schema 95, localization 145, security 138, E2E 58, roulette 188, referral 220, loyalty 265, integration 500, unit 288; four-language journey in ru, en, de and uk |
| PostgreSQL concurrency suites ×3 | 39/39, 39/39, 39/39 |
| Alembic round trip | one head `c5d2e8f1a6b3`; upgrade → downgrade base → upgrade; `alembic check`: no new operations |
| Package build (commit-equivalent export) | wheel and sdist |
| `docker build --no-cache` | ok, 211 MB |
| Image checks | heads `c5d2e8f1a6b3`; `pip check` clean; no `.env`, dump, `reports/` or spec in the image; fixes present; 4 catalogs × 486 identical keys; rename prompt renders in all four languages |
| Placeholder-token startup in the image | migrations ran, then `check_startup` exited 2 (by design) |
| In-image full suite (PostgreSQL) | 1935 passed, 1 skipped (git is absent from the image; that test passed on the host) |
| Red proof on HEAD `22de079` | new behaviour tests fail: 14 failures plus a missing `text_limit`; 6 more with limits hard-coded. All pass on the fixed tree |
| Cleanup | scratch container, network and image removed; `vshop-ci:local`, `vshop-baseline:local`, `v-shop_pgdata` and `vshop_cleanroom_pgdata` untouched |

## Owner actions before deploying

1. **Add `POSTGRES_VOLUME_NAME=v-shop_pgdata` to `.env`.** This is required: without it, `docker compose up` refuses to start. Check `docker compose config` shows that volume before `up`.
2. Set `APP_ENV=production` in `.env`.
3. Change the default `POSTGRES_PASSWORD`: run `ALTER USER` inside the container, then set the same value in `.env`. The steps are under "`password authentication failed for user "vshop"`" in `docs/deployment.md`.
4. Set `TELEGRAM_SSL_VERIFY=true` on a host without TLS interception.
5. Decide R-1, the €0 welcome-spin free bottle.
