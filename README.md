# knx-telegram-store

A standalone, host-agnostic Python library for KNX telegram persistence.

## Features

- **Canonical Data Model**: A unified model for KNX telegrams shared between Home Assistant and SpectrumKNX.
- **Pluggable Backends**:
  - **In-Memory**: Fast, deque-based storage with full filtering support.
  - **SQLite**: Lightweight persistent storage with SQL-based filtering.
  - **PostgreSQL + TimescaleDB**: Full-scale time-series storage.
- **Unified Query Model**: Powerful declarative filtering including time-delta context windows and pagination.
- **Zero Runtime Dependencies**: Core library (model, interface, in-memory) has no dependencies.
- **Automated Schema Management**: SQL backends handle their own creation and upgrades.

## Installation

```bash
pip install knx-telegram-store
```

For SQL support:

```bash
pip install knx-telegram-store[sqlite]
pip install knx-telegram-store[postgres]
```

## Usage

```python
from datetime import datetime
from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.memory import MemoryStore

async def main():
    store = MemoryStore(max_size=1000)
    await store.initialize()

    telegram = StoredTelegram(
        timestamp=datetime.now(),
        source="1.1.1",
        destination="1/1/1",
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=22.5,
        unit="°C"
    )

    await store.store(telegram)

    query = TelegramQuery(destinations=["1/1/1"])
    result = await store.query(query)
    
    for t in result.telegrams:
        print(f"{t.timestamp}: {t.source} -> {t.destination} | {t.value} {t.unit}")

    await store.close()
```

## Validating a config / connection

Before triggering an expensive operation such as a migration, you can validate that a
store is reachable. Both checks return a structured `ConnectionCheckResult`
(`ok`, `kind`, `message`, `detail`) instead of raising.

```python
from knx_telegram_store import ConnectionErrorKind
from knx_telegram_store.backends.sqlite import SqliteStore
from knx_telegram_store.backends.postgres import PostgresStore

# Static, side-effect-free config validation (before constructing a store):
#  - SQLite: sync — checks the file is writeable or can be created
result = SqliteStore.check_config("/data/telegrams.db")
#  - Postgres: async — actually connects to verify user/password/host/port/database
result = await PostgresStore.check_config("postgresql://user:pw@host:5432/knx")

if not result.ok:
    print(f"[{result.kind}] {result.message}")  # e.g. [auth] Authentication failed ...

# Live probe of an already-constructed store (no migrations, no schema changes):
store = SqliteStore("/data/telegrams.db")
result = await store.check_connection()
if result.kind is ConnectionErrorKind.OK:
    await store.initialize()
```

## License

MIT
