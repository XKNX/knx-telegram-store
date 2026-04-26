# knx-telegram-store

A standalone, host-agnostic Python library for KNX telegram persistence with pluggable storage backends.

## Motivation

KNX telegram storage is needed in multiple projects — [Home Assistant KNX](https://www.home-assistant.io/integrations/knx/) and [SpectrumKNX](https://github.com/spectrumknx/spectrumknx) — but currently each implements its own storage layer with zero shared code. This library extracts a common interface and ships multiple backends so both projects (and others) can share the same well-tested storage implementation.

## Features

- **Canonical data model** — A single `StoredTelegram` dataclass covering all KNX telegram fields.
- **Abstract storage interface** — `TelegramStore` ABC with pluggable backends.
- **Declarative query model** — `TelegramQuery` supports multi-value filters, time ranges, time-delta context windows, and pagination.
- **Graceful degradation** — Simple backends return all data; consumers apply client-side filtering. SQL backends filter server-side.
- **Zero core dependencies** — The core library (model + interface + in-memory backend) has no runtime dependencies.

## Backends

| Backend | Use Case | Dependencies |
|---|---|---|
| **In-Memory** | Testing, development | None |
| **HA Storage** | Home Assistant native persistence | None (uses HA's `Store`) |
| **SQLite** | Lightweight persistent storage | `aiosqlite` |
| **PostgreSQL + TimescaleDB** | Full-scale time-series storage | `asyncpg`, `sqlalchemy` |

## Installation

```bash
pip install knx-telegram-store              # Core only
pip install knx-telegram-store[sqlite]      # + SQLite backend
pip install knx-telegram-store[postgres]    # + PostgreSQL backend
```

## Documentation

See [docs/design_and_implementation.md](docs/design_and_implementation.md) for the full design document.

## License

Apache License 2.0
