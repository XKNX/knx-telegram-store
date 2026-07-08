# knx-telegram-store

A standalone, host-agnostic Python library for KNX telegram persistence.

## Features

- **Canonical Data Model**: A unified model for KNX telegrams shared between Home Assistant and SpectrumKNX.
- **Pluggable Backends**:
  - **In-Memory**: Fast, deque-based storage with full filtering support.
  - **SQLite**: Lightweight persistent storage with SQL-based filtering.
  - **PostgreSQL + TimescaleDB**: Full-scale time-series storage.
- **Unified Query Model**: Powerful declarative filtering including time-delta context windows and pagination.
- **Stats & Maintenance**: `get_stats()` reports count, covered time range and on-disk size; `evict_older_than()` supports dry runs; `optimize()` reclaims disk space (VACUUM).
- **Read-Only Mode**: Open a SQLite store owned and written by another process (e.g. Home Assistant's KNX telegram store) without running migrations or allowing writes.
- **Concurrent Access**: Writing SQLite stores use WAL journaling and a busy timeout, so a single writer and multiple (cross-process) readers coexist safely.
- **Capability Flags**: `store.capabilities` declares what a backend supports (`supports_optimize`, `supports_size_stats`, `read_only`, …) so hosts can gate UI instead of hardcoding backends.
- **Log Container Format**: `formats.ets_xml` streams the KNX `CommunicationLog` XML container (ETS6 group-monitor exports, Gira IP-Router data-logger dumps) to/from raw cEMI frames — constant memory, no protocol decoding, stdlib-only.
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

## Stats, purging and space reclamation

```python
from datetime import UTC, datetime, timedelta
from knx_telegram_store.backends.sqlite import SqliteStore

store = SqliteStore("/data/telegrams.db", retention_days=90)
await store.initialize()

stats = await store.get_stats()
print(f"{stats.telegram_count} telegrams, {stats.size_bytes} bytes, "
      f"{stats.oldest_timestamp} .. {stats.newest_timestamp}")

cutoff = datetime.now(UTC) - timedelta(days=30)
would_delete = await store.evict_older_than(cutoff, dry_run=True)  # preview only
deleted = await store.evict_older_than(cutoff)

# Deleting rows does not shrink the database on disk by itself:
if store.capabilities.supports_optimize:
    await store.optimize()  # VACUUM — blocks writers, can take a while on large DBs
```

## Read-only access to a shared store

Another process (e.g. Home Assistant's KNX integration) owns and writes the
database; you only want to read it:

```python
store = SqliteStore("/homeassistant/.storage/knx/telegrams.db", read_only=True)
await store.initialize()   # never runs DDL/migrations against a foreign schema

if await store.needs_migration():
    ...  # schema is older/newer than this library version — surface a warning

result = await store.query(TelegramQuery(limit=100))
await store.store(telegram)  # raises KnxTelegramStoreException — writes rejected
```

The file is opened with SQLite's `mode=ro`, so writes are impossible at the
driver level. `capabilities.read_only` is `True` and `supports_optimize` is
`False` in this mode. Writing stores enable WAL journaling, which makes this
single-writer/multi-reader setup safe across processes.

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
#    (with read_only=True: checks the file exists and is readable instead)
result = SqliteStore.check_config("/data/telegrams.db", read_only=True)
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

## Reading / writing telegram log files

`formats.ets_xml` handles the KNX `CommunicationLog` XML container (namespace
`http://knx.org/xml/telegrams/01`) produced by ETS6 exports and Gira data loggers.
It operates on **raw cEMI frames** — no protocol decoding, no `xknx` dependency —
so any consumer can stream large logs with constant memory.

```python
from knx_telegram_store.formats import iter_communication_log, write_communication_log

# Incremental read (file path or binary stream, e.g. a zip entry):
for record in iter_communication_log("2026_03_05_TP1.xml"):
    print(record.timestamp, record.service, record.raw_data.hex())
    #      aware UTC        "L_Data.ind"   cEMI frame as logged

# Streaming write (records may be a generator; ETS6-compatible output):
with open("export.xml", "w", encoding="utf-8") as fh:
    count = write_communication_log(records, fh, connection_name="My Export")
```

A Gira-style `<!-- timezone offset +01:00 hour -->` comment is honored, and ETS's
7-digit fractional seconds are normalized to microseconds.

## License

MIT
