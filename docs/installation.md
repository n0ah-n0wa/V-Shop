# Installation

## Prerequisites

- Python **3.13+**
- PostgreSQL **16** (local or Docker)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (for `ADMIN_IDS`) — e.g. via [@userinfobot](https://t.me/userinfobot)

## Option A — Docker Compose (recommended)

1. Clone the repository and enter it.

2. Create environment file:

```bash
cp .env.example .env
```

3. Edit `.env` and set at least:

- `BOT_TOKEN` — real bot token (not the placeholder)
- `ADMIN_IDS` — your Telegram numeric user ID
- `MANAGER_CHAT_ID` — chat that receives new-order alerts (can be the same as your user ID)
- `POSTGRES_VOLUME_NAME` — the Docker volume that holds the database (next step)

4. Create the database volume. Compose never creates it: an unset or wrong
   `POSTGRES_VOLUME_NAME` stops the start instead of opening an empty database.
   Upgrading an existing deployment? Name the volume you already have instead —
   see [Deployment](deployment.md#upgrading-an-existing-deployment).

```bash
docker volume create vshop_pgdata
echo "POSTGRES_VOLUME_NAME=vshop_pgdata" >> .env
```

5. Build and start:

```bash
docker compose up --build
```

This starts:

1. `db` — PostgreSQL 16
2. `bot` — waits for DB health, runs migrations, starts polling

Logs appear in the Compose output. Stop with `Ctrl+C` or `docker compose down`.

Data persists in the Docker volume named by `POSTGRES_VOLUME_NAME`; Compose never
removes it, not even with `docker compose down -v`.

## Option B — Local virtualenv

1. Create and activate a virtual environment:

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements-dev.txt
pip install -e .
```

3. Configure environment:

```bash
cp .env.example .env
```

Set `DATABASE_URL` for a reachable Postgres, for example:

```env
DATABASE_URL=postgresql+asyncpg://vshop:vshop@localhost:5432/vshop
```

You can run only the database via Compose (with `POSTGRES_VOLUME_NAME` set and
its volume created, as in Option A step 4):

```bash
docker compose up -d db
```

4. Apply migrations:

```bash
alembic upgrade head
```

5. Start the bot:

```bash
python -m app.main
```

## Verifying

- Startup runs a DB connectivity check and Telegram `getMe`.
- If `BOT_TOKEN` is still the placeholder from `.env.example`, startup fails with a clear error.
- Send `/start` to the bot in Telegram to begin onboarding.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Tests use in-memory SQLite and do not require PostgreSQL or a bot token.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `BOT_TOKEN` rejected | Invalid/revoked token or placeholder left in `.env` |
| Cannot connect to database | Wrong `DATABASE_URL`, Postgres not running, or Windows host networking issues — prefer Docker DB |
| TLS / SSL errors to Telegram | Corporate proxy: set `TELEGRAM_SSL_VERIFY=false` **only for local debugging** |
| Bot starts but `/admin` denied | Your Telegram ID is missing from `ADMIN_IDS` |
