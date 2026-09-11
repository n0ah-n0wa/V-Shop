# Deployment

## Recommended: Docker Compose

The repository ships a production-oriented Compose stack:

| Service | Role |
|---|---|
| `db` | PostgreSQL 16 Alpine, named volume `pgdata`, healthcheck |
| `bot` | App image; migrates then runs `python -m app.main` |

### Deploy steps

1. Provision a host with Docker and Docker Compose.
2. Copy the project (or pull from git).
3. Create `.env` from `.env.example` and fill in **production** secrets: a
   real `BOT_TOKEN`, locked-down `ADMIN_IDS`, and a strong
   `POSTGRES_PASSWORD` (it defaults to `vshop` if you leave it unset — see
   [Configuration](configuration.md#docker-compose-extras)).
4. Postgres is published on `127.0.0.1` only by default (see `docker-compose.yml`). For stricter production, omit the `db` ports mapping entirely so only the bot container can reach Postgres on the internal network.
5. Start:

```bash
docker compose up -d --build
```

6. Follow logs:

```bash
docker compose logs -f bot
```

## Upgrading an existing deployment

**Do this once, before the first deploy of a version that pins the volume name.**

Earlier revisions of `docker-compose.yml` did not pin the Compose project or the
volume name, so both were derived from the deployment directory. Your database
may therefore live in a volume named after that directory (`v-shop_pgdata`,
`vshop-2026_pgdata`, …) rather than the new default `vshop_pgdata`. Deploying
without checking would start the stack against a **new, empty** database.

1. Find the volume currently in use:

```bash
docker inspect vshop-db --format '{{ (index .Mounts 0).Name }}'
docker volume ls
```

2. If the name is **not** `vshop_pgdata`, record it in `.env`:

```env
POSTGRES_VOLUME_NAME=v-shop_pgdata
```

3. Take a backup (see below) and note the cluster identity:

```bash
docker exec vshop-db psql -U vshop -d vshop -tAc \
  "SELECT system_identifier FROM pg_control_system();"
```

4. Stop the old stack **from its original directory** so the pinned
   `container_name` values are released, then deploy normally:

```bash
docker compose down          # in the OLD directory
docker compose up -d --build # in the new one
```

If you forget step 4 the deploy fails loudly with a container-name conflict —
that is intentional, and preferable to starting a second stack silently.

5. Confirm the data came with you (see *Verifying a deploy* below).

### Updates — the safe production update procedure

This is the whole procedure. Run it in order; steps 1 and 5 are the ones people
skip and regret.

```bash
# 1. Back up, and record what you are about to change
docker exec vshop-db pg_dump -U vshop -Fc vshop > vshop-$(date +%F).dump
docker exec vshop-db psql -U vshop -d vshop -tAc   "SELECT system_identifier FROM pg_control_system();"          # write this down
docker inspect vshop-db --format '{{ (index .Mounts 0).Name }}' # and this

# 2. Get the new code
git pull

# 3. Deploy. Builds the image, recreates the bot container, keeps the volume.
docker compose up -d --build

# 4. Migrations run automatically: the container's command is
#    `alembic upgrade head && python -m app.main`

# 5. Verify before walking away
docker compose logs bot | grep "Database identity"
docker compose run --rm --no-deps bot python -m app.verify_deployment
```

Step 5 is the point of the procedure. The identity line reports the cluster and
its row counts:

```text
Database identity: url=postgresql+asyncpg://vshop:***@db:5432/vshop
  system_identifier=7675364240903147554 categories=12 products=84 users=430 orders=1180
```

**If `system_identifier` differs from step 1, stop.** The stack is attached to
different storage. The old volume is almost certainly still on disk — see
[Recovery](#recovery). Do not add data first; that makes the two datasets
diverge.

This procedure is safe to run from any directory, including a fresh clone under a
different name, because `docker-compose.yml` pins both the Compose project name
(`name: vshop`) and the volume name (`POSTGRES_VOLUME_NAME`, default
`vshop_pgdata`). Earlier revisions derived both from the deployment directory,
which is what caused the catalog to "disappear" after a redeploy — the stack
silently created a new, empty volume, migrations applied cleanly to it, and the
bot started healthy with nothing in it.

### Verifying a deploy

Every startup logs which database it attached to:

```text
Database identity: url=postgresql+asyncpg://vshop:***@db:5432/vshop
  system_identifier=7675364240903147554 categories=12 products=84 users=430 orders=1180
```

Check two things in that line:

- **`system_identifier` is unchanged from the previous deploy.** It is assigned
  once at `initdb` and uniquely identifies a PostgreSQL cluster. A new value
  means the stack is attached to different storage — the connection string,
  database name and OID all stay identical in that case, so this is the only
  cheap way to notice.
- **The row counts match what you expect.** An unexpectedly empty catalog also
  raises an explicit `WARNING` on the next line.

If either looks wrong, **stop and investigate before adding any data** — the
original volume is very likely still on disk and recoverable (`docker volume ls`).

For a stronger check than the log line, run the read-only deploy verifier. It
drives the same services and renderers the handlers use and prints what came
back:

```bash
docker compose run --rm --no-deps bot python -m app.verify_deployment
```

It exists to separate two incidents that look identical to a user:

- **rows missing from PostgreSQL** — a deployment or volume problem; stop, and
  check `docker volume ls` before writing anything;
- **rows present but nothing renders** — an application, query or schema bug;
  the data is safe, the deploy is not.

The report covers users, categories, subcategories, products, carts, orders and
their statuses, historical totals, and the rendered statistics dashboard. Its
`loyalty` section has two parts:

- `integrity` — cached balances or purchase counts the stamp ledger does not
  explain, ledger rows whose running balance is wrong, stamp-card rewards
  without their debit, spin grants out of step with their spins, spins whose
  prize is missing or does not match what was won. The bot never
  produces any of these, so every value must be `0`; anything else means rows
  were changed outside it, and the ledger is the record to trust.
- `coverage` — users without a loyalty account or welcome spin. The bot grants
  the welcome spin at `/start` and tops it up for every customer still without
  one each time it starts, so after a start that count is `0`. Accounts are
  created on first use, so users without one are normal.

## Safe operations

These preserve all production data and are the only commands needed for routine
operation.

```bash
docker compose up -d --build   # deploy / update
docker compose restart bot     # restart the app only
docker compose down            # stop the stack; the database volume is KEPT
docker compose logs -f bot     # follow logs
docker compose ps              # status
```

### What preserves data, and what destroys it

Every row below was exercised against a running stack holding seeded data, and
the data was re-verified afterwards both in PostgreSQL and through the bot's own
rendering (`python -m app.verify_deployment`).

| Operation | Data |
|---|---|
| Bot process restart (`kill 1`, crash, `restart: unless-stopped`) | **Preserved** |
| `docker compose restart bot` | **Preserved** |
| `docker compose restart` (whole stack) | **Preserved** |
| `docker compose down` then `up -d` (containers recreated) | **Preserved** |
| `docker compose build --no-cache` + `up -d` | **Preserved** |
| New application image (code change) deployed | **Preserved** |
| `alembic upgrade head` | **Preserved** |
| Redeploy from a **different directory** or a fresh clone | **Preserved** — the project and volume names are pinned in `docker-compose.yml` |
| `docker compose down -v` | **DESTROYED** |
| `docker volume rm` / `docker volume prune` | **DESTROYED** |
| `docker system prune -a --volumes` | **DESTROYED** |
| `alembic downgrade` past an index-only migration | **Columns dropped** — see [Migrations](#migrations) |

The distinction that matters: containers and images are disposable, **the named
volume is not**. Everything that only replaces containers or images is safe.
Only commands that name a volume — or `prune`, which names them implicitly — are
destructive.

## Backups

There is no automatic backup. Take one before every deploy and before any schema
change:

```bash
docker exec vshop-db pg_dump -U vshop -Fc vshop > vshop-$(date +%F).dump
```

Verify the dump restores before trusting it — an untested dump is not a backup:

```bash
docker exec -i vshop-db createdb -U vshop restore_check
docker exec -i vshop-db pg_restore -U vshop -d restore_check < vshop-YYYY-MM-DD.dump
docker exec vshop-db psql -U vshop -d restore_check -c "SELECT count(*) FROM products;"
docker exec vshop-db dropdb -U vshop restore_check
```

## Destructive operations

> **Never run these as part of a normal update.** Each one permanently deletes
> production data. Take and verify a backup first, and only run them when a
> complete environment reset is explicitly intended.

| Command | Effect |
|---|---|
| `docker compose down -v` | **Deletes the database volume.** All users, catalog, carts and order history are gone. |
| `docker volume rm <name>` | Same, targeted at one volume. |
| `docker volume prune` | Deletes **every** unreferenced volume on the host. `docker compose down` leaves the database volume unreferenced, so this destroys it. |
| `docker system prune -a --volumes` | As above, plus images. |
| `alembic downgrade …` | **Depends on the target — see [Migrations](#migrations) below.** Three of the nine (`b2c4d5e6f7a8`, `e5a3c7d21f04`, `f6b1d4e8a207`) only touch indexes and are safe; the others drop columns or tables. |

Because `docker compose down` leaves `pgdata` dangling, do not schedule any
Docker cleanup job on this host. If disk reclamation is genuinely needed, scope
it — `docker image prune` is safe; volume pruning is not.

## Migrations

The bot container runs `alembic upgrade head` before it starts polling, so a
deploy always brings the schema forward. Nine migrations exist; the chain is
linear and single-headed.

| Revision | What `upgrade()` does | Is `downgrade()` data-safe? |
|---|---|---|
| `a9b389353e68` | creates every table (initial schema) | **No** — drops all tables |
| `b2c4d5e6f7a8` | adds four performance indexes | Yes — indexes only |
| `c7e1f4a9d3b6` | catalog hierarchy: adds `subcategories`, localized name columns, `uk` support; backfills existing rows | **No** — drops the new columns and their data |
| `d4f2a8c1b9e3` | adds `orders.payment_method` (nullable) | **No** — drops the column |
| `e5a3c7d21f04` | adds `(product_id, order_id)` on `order_items`, drops the superseded single-column index | Yes — indexes only |
| `f6b1d4e8a207` | drops two indexes made redundant by composites | Yes — indexes only |
| `3b9d6f2a8c14` | loyalty foundation: six new tables (accounts, stamp ledger, roulette grants and spins, rewards, referrals); backfills one account and one welcome spin per existing user; touches no existing table | **Guarded.** With only the backfill present it loses nothing a re-upgrade would not recreate. Once customers have loyalty activity it **refuses**; `alembic -x allow_loyalty_data_loss=true downgrade f6b1d4e8a207` then drops every stamp, spin, reward and referral — take a `pg_dump` first |
| `8e4c1a7b2d95` | adds `orders.loyalty_eligible` (`NOT NULL DEFAULT false`, instant on PostgreSQL 11+); every existing order reads `false` and never earns stamps, orders placed afterwards are eligible | **Guarded.** Refuses while eligible orders exist — a re-upgrade would mark them all ineligible; `-x allow_loyalty_data_loss=true` overrides |
| `c5d2e8f1a6b3` | adds `user_rewards.discount_amount` and `redeemed_product_id` (both nullable, plus a consistency CHECK); touches no catalog, order or user table | **Guarded.** Refuses while used rewards exist — their redemption record would be lost; `-x allow_loyalty_data_loss=true` overrides |

**No `upgrade()` in this project drops a table, drops a column, truncates, or
deletes rows.** Backfills are `INSERT`/`UPDATE` only. That rule is enforced by
`tests/test_migrations.py`, which fails the build if a future migration breaks it.

Useful commands:

```bash
docker compose run --rm --no-deps bot alembic current    # where is this database?
docker compose run --rm --no-deps bot alembic history    # the chain
docker compose run --rm --no-deps bot alembic check      # does the schema match the models?
```

`alembic check` reporting "No new upgrade operations detected" means the
migrations and the ORM models agree. It does **not** compare foreign-key names,
so it cannot catch every kind of drift on its own.

## Recovery

### The catalog looks empty after a deploy

Almost always the stack attached to different storage rather than losing data.
**Do not add any data yet** — writing to the new volume makes the two datasets
diverge and complicates the merge.

```bash
# 1. which volume is mounted now, and which exist?
docker inspect vshop-db --format '{{ (index .Mounts 0).Name }}'
docker volume ls | grep pgdata

# 2. is the old one still there with the data in it?
docker run --rm -v <old-volume>:/var/lib/postgresql/data -e POSTGRES_PASSWORD=x   --name vshop-inspect -d postgres:16-alpine
docker exec vshop-inspect psql -U vshop -d vshop   -c "SELECT count(*) FROM products;"
docker rm -f vshop-inspect

# 3. point the stack back at it and redeploy
echo "POSTGRES_VOLUME_NAME=<old-volume>" >> .env
docker compose up -d
```

Confirm the `system_identifier` in the startup log matches what you recorded
before the deploy.

### Restoring from a dump

```bash
docker compose stop bot                      # stop writes first
docker exec -i vshop-db dropdb -U vshop vshop
docker exec -i vshop-db createdb -U vshop vshop
docker exec -i vshop-db pg_restore -U vshop -d vshop < vshop-YYYY-MM-DD.dump
docker compose start bot                     # migrations run again on start
```

`dropdb` destroys the current database. Take a dump of the *current* state first,
even if you believe it is empty — it costs seconds and it is the only way back
if the restore turns out to be the wrong file.

## Image build

`Dockerfile` uses `python:3.13-slim`, installs `requirements.txt`, copies `app/` + Alembic, then `pip install -e .`.

Default command: `python -m app.main` (Compose overrides with migrate + main).

### The build fails at `pip install` with a certificate error

```text
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: unable to get local issuer certificate'))
```

The network between the build and `pypi.org` is intercepting TLS — a
corporate proxy, or a consumer antivirus with HTTPS scanning enabled. The
host trusts the interceptor's CA; the build container does not, so `pip`
cannot verify PyPI even though the browser on the same machine works.

Confirm it by checking who issued the certificate you are being served:

```bash
openssl s_client -connect pypi.org:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer
```

A public CA (Let's Encrypt, DigiCert, …) means the problem is elsewhere.
Anything else — `... Web/Mail Shield`, `... SSL Inspection`, your employer's
name — is the interceptor.

Fix it by giving the build that CA:

```bash
cp corporate-root.crt docker/ca-certificates/
docker compose build --no-cache bot
```

`docker/ca-certificates/` is empty by default and read only at build time;
the running bot does not use it. Do **not** commit the certificate — it is
specific to one network, and the directory's `.gitignore` excludes `*.crt`
for that reason.

Never "fix" this with `pip --trusted-host` or by disabling verification.
That accepts *any* certificate for PyPI, which is the actual attack this
check exists to stop.

This fixes the **build** only. The running bot needs `TELEGRAM_SSL_VERIFY=false`
on the same network, and installing the CA does **not** substitute for it:
Python 3.13's default TLS context enables `VERIFY_X509_STRICT`, which rejects a
CA certificate whose `basicConstraints` are not marked critical. Consumer
antivirus roots frequently are not conformant, so the bot rejects the very
certificate the build accepted:

```text
certificate verify failed: Basic Constraints of CA cert not marked critical
```

A machine behind TLS interception therefore needs **both**: the CA file for
`pip`, and `TELEGRAM_SSL_VERIFY=false` for the bot's own traffic.

### Order notifications stop arriving in the manager group

```text
Failed to send order notification order_id=… chat_id=…
Bad Request: group chat was upgraded to a supergroup chat
```

Telegram assigns a **new chat id** when a basic group becomes a supergroup,
which happens on its own the first time certain group settings are changed.
`getChat` on the old id keeps answering, so the configuration looks correct
while every send is rejected.

The rejection carries the replacement id:

```json
{"ok": false, "error_code": 400,
 "description": "Bad Request: group chat was upgraded to a supergroup chat",
 "parameters": {"migrate_to_chat_id": -1004453891123}}
```

Put that value in `MANAGER_CHAT_ID` and `docker compose up -d`. A supergroup
id always begins `-100`; an id without that prefix is a basic group and can
still migrate out from under you. The same applies to
`REVIEW_GROUP_CHAT_ID`.

The failure is logged rather than raised — a chat that cannot be reached must
not roll back an order that was already placed — so the log is where this is
visible:

```bash
docker compose logs bot | grep "order notification"
```

### `Ports are not available` when starting the database

```text
Error response from daemon: Ports are not available: exposing port TCP
127.0.0.1:5432 -> 0.0.0.0:0: listen tcp 127.0.0.1:5432: bind: An attempt was
made to access a socket in a way forbidden by its access permissions.
```

Windows phrases "already in use" as a permissions error. Almost always a
native PostgreSQL service already owns 5432:

```bash
netstat -ano | findstr :5432          # Windows — note the PID
sudo ss -lptn 'sport = :5432'         # Linux
```

Publish the container on a different host port instead — the port *inside*
the container is always 5432 and does not change:

```bash
POSTGRES_PORT=5433
```

### `password authentication failed for user "vshop"`

`POSTGRES_PASSWORD` is read **only when the data directory is first created**.
On every later start the image ignores it, so editing `.env` against an
existing volume changes what the bot sends but not what the database expects,
and the bot cannot log in to its own database.

Local socket connections are trusted inside the container, so the role can be
brought back in line without losing data:

```bash
docker exec vshop-db psql -U vshop -d vshop -c "ALTER USER vshop WITH PASSWORD 'new-password'"
```

Set the same value as `POSTGRES_PASSWORD` in `.env`, then
`docker compose up -d`. Starting a **new** volume is the other option, and it
discards the existing database — see [Destructive operations](#destructive-operations).

## Production checklist

- [ ] Real `BOT_TOKEN` (never the `.env.example` placeholder)
- [ ] Strong `POSTGRES_PASSWORD` / credentials in `DATABASE_URL`
- [ ] `ADMIN_IDS` limited to trusted operators
- [ ] `MANAGER_CHAT_ID` points at the correct ops chat
- [ ] `TELEGRAM_SSL_VERIFY=true`
- [ ] `APP_ENV=production` and `LOG_LEVEL=INFO` (or `WARNING`)
- [ ] Postgres not exposed publicly
- [ ] Host firewall / reverse proxy as needed (bot uses outbound HTTPS to Telegram only)
- [ ] `POSTGRES_VOLUME_NAME` matches the volume that actually holds your data
- [ ] `system_identifier` recorded, so it can be compared after each deploy
- [ ] No `docker volume prune` / `docker system prune --volumes` in any cron or cleanup job
- [ ] Backups scheduled **and a restore tested**
- [ ] Single bot instance per token (MemoryStorage FSM is process-local; multiple replicas need Redis FSM storage first)

## Scaling notes

Current defaults suit a **single** bot process:

- FSM uses `MemoryStorage` (lost on restart; not shared across workers).
- Category list uses a process-local TTL cache.
- Checkout/broadcast anti-double-submit uses in-process locks **plus** Postgres cart `FOR UPDATE` for orders.

For multiple workers, introduce Redis (or similar) FSM storage and shared locking before scaling out.

## Health expectations

On startup the app:

1. Configures logging
2. Initializes the DB engine and checks `SELECT 1`
3. Logs the database identity (URL, `system_identifier`, row counts) — read-only
4. Creates the bot/dispatcher
5. Calls Telegram `getMe` (startup check)
6. Begins long polling

Step 3 never writes and never blocks startup: if the probe fails it is logged at
`DEBUG` and the bot continues.

If DB or token checks fail, the process exits non-zero (Compose will restart if `restart: unless-stopped`).

## Reverse proxy / webhooks

The bot uses **long polling**, not webhooks. No inbound HTTP port is required for Telegram. If you later switch to webhooks, you would need a public HTTPS endpoint and webhook secret handling (not implemented in this repo).
