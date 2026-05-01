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

## License

MIT
