# V-Shop

Telegram shop bot for a vape e-liquid store.

Customers browse a four-language catalog (Russian, English, German, Ukrainian),
manage a cart, and check out through a guided FSM. Admins run the whole shop —
catalog, orders, statistics and broadcasts — from inside Telegram.

## Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.13 |
| Bot | aiogram 3.x (long polling) |
| ORM | SQLAlchemy 2.x async + asyncpg |
| Database | PostgreSQL 16 |
| Migrations | Alembic |
| Config | Pydantic Settings |
| Deploy | Docker / Docker Compose |

## Features

**Customer**
- Onboarding: language (`ru` / `en` / `de` / `uk`) → city (`berlin` / `delivery`)
- Three-level catalog: category → subcategory (brand) → product, with localized
  names and descriptions on every level
- Product cards with photo, flavor, volume, nicotine strength and price
- Cart with quantity controls, and a guided checkout FSM
- Checkout captures delivery method (city-gated), address, preferred time,
  contact, and payment method (`cash` / `card`)
- Order status notifications in the customer's own language
- 🪪 My Stamp Card: a stamp for every €20 of a completed order (configurable);
  a full card of 10 unlocks a free bottle, claimed with one tap
- 🎰 Lucky Roulette: a welcome spin, and one for every 5th completed order
  (configurable); every spin wins stamps, a discount or a free bottle, drawn by
  the server
- Information pages, and an invite link to the private reviews group
- Language and city can be changed at any time from Information

**Admin** (`/admin`, `ADMIN_IDS` only, private chats only)
- Products: add, list, edit (all four languages), price/description,
  enable/disable, delete
- Categories and subcategories: create, edit names, reorder, activate, delete,
  reassign
- Orders: new/completed lists, search, and the full lifecycle
  `New → Accepted → Shipped → Completed`, plus `Cancelled` (with undo)
- Statistics: totals, orders and completed revenue for all time / this month /
  last month, and the three best- and worst-selling products
- Broadcast: text and/or photo to all registered customers

**Operational**
- Group chats are ignored entirely — manager and review groups receive
  notifications, never commands or buttons
- All data lives in PostgreSQL on a pinned Docker volume; every routine deploy
  operation preserves it (see [Deployment](docs/deployment.md#what-preserves-data-and-what-destroys-it))

## Documentation

| Guide | Description |
|---|---|
| [Installation](docs/installation.md) | Local and Docker setup |
| [Configuration](docs/configuration.md) | Environment variables |
| [Deployment](docs/deployment.md) | Production Docker notes |
| [Architecture](docs/architecture.md) | Layers, middleware, flows |
| [Database schema](docs/database-schema.md) | Tables, indexes, relations |
| [Admin guide](docs/admin-guide.md) | Operating the admin panel |

## Quick start

```bash
cp .env.example .env
# Set BOT_TOKEN, ADMIN_IDS, MANAGER_CHAT_ID

docker compose up --build
```

Compose starts PostgreSQL, runs `alembic upgrade head`, then launches the bot.

## Development

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements-dev.txt
pip install -e .
cp .env.example .env
# Point DATABASE_URL at local Postgres

alembic upgrade head
python -m app.main
```

Run tests:

```bash
python -m pytest tests -q
```

## Project layout

```
app/
  handlers/        # Telegram routers (user / admin)
  keyboards/       # Reply & inline keyboards
  middlewares/     # Logging, errors, DB session, i18n
  services/        # Business orchestration
  repositories/    # Data access
  models/          # SQLAlchemy ORM
  states/          # FSM groups
  locales/         # en / ru / de / uk JSON
  utils/           # Validators, labels, cache, …
  errors/          # Error classification & safe user text
docs/              # Extended documentation
tests/             # pytest suite (async SQLite)
```

## License

Proprietary.
