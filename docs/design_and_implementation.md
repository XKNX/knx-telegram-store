# `knx-telegram-store` — Design & Implementation Plan

> **Status:** Draft — Pending Review  
> **Date:** 2026-04-26  
> **Authors:** Martin  
> **PyPI Name:** `knx-telegram-store` (confirmed available)

---

## Table of Contents

1. [Problem & Motivation](#1-problem--motivation)
2. [Design Goals & Constraints](#2-design-goals--constraints)
3. [Canonical Data Model](#3-canonical-data-model)
4. [Abstract Store Interface](#4-abstract-store-interface)
5. [Query / Filter Model](#5-query--filter-model)
6. [Backend Implementations](#6-backend-implementations)
7. [Integration Strategy](#7-integration-strategy)
8. [Package Structure](#8-package-structure)
9. [Implementation Roadmap](#9-implementation-roadmap)
10. [Verification Plan](#10-verification-plan)

---

## 1. Problem & Motivation

KNX telegram persistence currently lives in two completely separate, tightly coupled systems:

| System | Storage | Capabilities | Limitations |
|---|---|---|---|
| **Home Assistant KNX** ([telegrams.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/knx/telegrams.py)) | HA `Store` (JSON file) + in-memory `deque` | Persist across restarts, WebSocket live stream | Hard cap on `log_size` (default 500). No server-side filtering. Entire history loaded into memory. |
| **SpectrumKNX** ([knx_daemon.py](https://github.com/spectrumknx/spectrumknx/)) | PostgreSQL + TimescaleDB | Full time-series storage, server-side SQL filtering, time-delta context windows, pagination | Requires PostgreSQL infrastructure. Storage logic is embedded in the application. |

Both systems perform the same fundamental task — receive KNX telegrams, enrich them with DPT/name metadata, persist them, and serve them to a frontend — but share zero code.

### What we want

A **standalone, host-agnostic Python library** (`knx-telegram-store`) that:

1. Defines a **single canonical data model** for a stored KNX telegram.
2. Provides an **abstract storage interface** with pluggable backends.
3. Ships four backends: **In-Memory** (testing), **HA Storage** (Home Assistant native), **SQLite** (lightweight persistent), **PostgreSQL/TimescaleDB** (full scale).
4. Provides a **unified query/filter model** that advanced backends implement natively (SQL) and simple backends degrade gracefully (return all telegrams unfiltered).
5. Is usable from both Home Assistant KNX and SpectrumKNX **without pulling in their respective framework dependencies**.

---

## 2. Design Goals & Constraints

### Goals

- **Backend independence:** The library has no dependency on Home Assistant, SpectrumKNX, FastAPI, or xknx. Consumers handle telegram enrichment (DPT decoding, name resolution) before writing to the store.
- **Shared features:** Filtering, time-delta context windows, pagination, and time-range queries are implemented once in SQL backends and benefit both consumers.
- **Zero-migration for HA Storage:** The HA Storage backend wraps the existing `Store` mechanism with the same data format. No migration of existing JSON data is required.
- **Incremental adoption:** Each consumer can adopt the library independently:
  1. Define the interface in the library.
  2. Wrap HA's existing storage as one backend (no data migration).
  3. Extract SpectrumKNX's PostgreSQL storage into the Postgres backend.
  4. Implement SQLite as an additional backend available to both systems.

### Constraints

- Python ≥ 3.12 (aligned with Home Assistant's minimum).
- Core library (model + interface + in-memory backend) must have **zero runtime dependencies**.
- SQL backends are **optional extras**: `knx-telegram-store[sqlite]`, `knx-telegram-store[postgres]`.
- The library does **not** decode raw xknx `Telegram` objects. Consumers call their own enrichment logic (using their xknx context and project data) and pass a pre-enriched `StoredTelegram` to the store.

---

## 3. Canonical Data Model

A single `StoredTelegram` dataclass representing a telegram as persisted. This is the **superset** of HA's `TelegramDict` and SpectrumKNX's database row.

Names and decoded values are stored **at write time**. This preserves the state at the moment of capture — important when users later change their KNX project (rename group addresses, reassign DPTs). The consumer handles enrichment before writing; the library stores what it receives.

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class StoredTelegram:
    """A KNX telegram in its stored/serialized form."""

    # ── Core identity ─────────────────────────────────────────────
    timestamp: datetime                         # timezone-aware (UTC or local)

    # ── Addressing ────────────────────────────────────────────────
    source: str                                 # Individual address, e.g. "1.2.3"
    destination: str                            # Group address, e.g. "1/2/3"

    # ── Telegram classification ───────────────────────────────────
    telegramtype: str                           # "GroupValueWrite" | "GroupValueRead" | "GroupValueResponse"
    direction: str                              # "Incoming" | "Outgoing"

    # ── Payload ───────────────────────────────────────────────────
    payload: int | tuple[int, ...] | None = None  # Raw KNX payload (DPTBinary int or DPTArray tuple)

    # ── DPT metadata ─────────────────────────────────────────────
    dpt_main: int | None = None
    dpt_sub: int | None = None
    dpt_name: str | None = None
    unit: str | None = None

    # ── Decoded value (consumer-enriched at write time) ───────────
    value: bool | str | int | float | dict[str, Any] | None = None

    # ── Numeric value for time-series queries (SQL backends) ──────
    value_numeric: float | None = None

    # ── Raw bytes (hex-encoded string for JSON safety) ────────────
    raw_data: str | None = None                 # e.g. "0a1b2c"

    # ── Security ──────────────────────────────────────────────────
    data_secure: bool | None = None

    # ── Display names (consumer-enriched at write time) ───────────
    source_name: str = ""
    destination_name: str = ""
```

### Mapping from existing models

| `StoredTelegram` field | HA `TelegramDict` | SpectrumKNX DB column | Notes |
|---|---|---|---|
| `timestamp` | `timestamp` (ISO str) | `timestamp` (TIMESTAMPTZ) | HA stores as ISO string; library uses `datetime` |
| `source` | `source` | `source_address` | |
| `destination` | `destination` | `target_address` | Renamed for consistency |
| `telegramtype` | `telegramtype` | `telegram_type` | |
| `direction` | `direction` | *(not stored)* | Added to SpectrumKNX schema |
| `payload` | `payload` | *(via raw_data)* | |
| `dpt_main` | `dpt_main` | `dpt_main` | |
| `dpt_sub` | `dpt_sub` | `dpt_sub` | |
| `dpt_name` | `dpt_name` | *(derived at read)* | Now stored at write time |
| `unit` | `unit` | *(derived at read)* | Now stored at write time |
| `value` | `value` | `value_json` | |
| `value_numeric` | *(not stored)* | `value_numeric` | New for HA; enables future charting |
| `raw_data` | *(not stored)* | `raw_data` (BYTEA) | Hex string in model; BYTEA in Postgres |
| `data_secure` | `data_secure` | *(not stored)* | Added to SpectrumKNX schema |
| `source_name` | `source_name` | *(enriched at API read)* | Now stored at write time |
| `destination_name` | `destination_name` | *(enriched at API read)* | Now stored at write time |

---

## 4. Abstract Store Interface

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from .model import StoredTelegram
from .query import TelegramQuery, TelegramQueryResult


@dataclass(frozen=True, slots=True)
class StoreCapabilities:
    """Declares what a backend can do natively.
    
    Consumers use this to decide whether to apply client-side post-filtering.
    """
    supports_server_filtering: bool = False
    supports_time_range: bool = False
    supports_time_delta: bool = False
    supports_pagination: bool = False
    supports_count: bool = False
    max_storage: int | None = None  # None = unlimited


class TelegramStore(ABC):
    """Abstract interface for KNX telegram persistence."""

    @property
    @abstractmethod
    def capabilities(self) -> StoreCapabilities:
        """Return the capabilities of this backend."""

    @abstractmethod
    async def initialize(self) -> None:
        """Set up the store (create tables, open connections, etc.).
        
        Called once at startup. Must be idempotent.
        """

    @abstractmethod
    async def close(self) -> None:
        """Tear down the store (close connections, flush buffers).
        
        Called once at shutdown.
        """

    @abstractmethod
    async def store(self, telegram: StoredTelegram) -> None:
        """Persist a single telegram."""

    @abstractmethod
    async def store_many(self, telegrams: Sequence[StoredTelegram]) -> None:
        """Persist multiple telegrams in a single batch."""

    @abstractmethod
    async def query(self, query: TelegramQuery) -> TelegramQueryResult:
        """Retrieve telegrams matching the given query.
        
        Backends that do not support server-side filtering SHOULD return
        all stored telegrams and set `result.server_filtered = False`.
        The consumer is then responsible for client-side filtering.
        """

    @abstractmethod
    async def count(self) -> int:
        """Return the total number of stored telegrams."""

    async def clear(self) -> None:
        """Remove all stored telegrams.
        
        Optional — backends may raise NotImplementedError.
        """
        raise NotImplementedError
```

### Key design decision: no `get_latest_per_destination()` in the interface

Home Assistant currently maintains a `last_ga_telegrams` dict (most recent telegram per group address). This remains a **consumer-side concern** in HA. In the future, it can be replaced by a frontend query fetching the last N telegrams with appropriate filtering. The storage library focuses on bulk storage and querying.

---

## 5. Query / Filter Model

A single, declarative query object that all backends receive. SQL backends translate it to queries; simple backends return all data and let the consumer filter.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime

from .model import StoredTelegram


@dataclass
class TelegramQuery:
    """Declarative query for telegram retrieval.
    
    Filter semantics:
    - Empty list = no restriction (pass-through)
    - Within a category = OR logic (any match passes)
    - Across categories = AND logic (must pass all active categories)
    """

    # ── Multi-value filters (OR within, AND across) ───────────────
    sources: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    telegram_types: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    dpt_mains: list[int] = field(default_factory=list)

    # ── Time range ────────────────────────────────────────────────
    start_time: datetime | None = None
    end_time: datetime | None = None

    # ── Time-delta context window (milliseconds) ──────────────────
    #    When set, rows matching the filters are found first, then
    #    ALL rows within ±delta of any matching row's timestamp are
    #    included — even if they don't match the filters themselves.
    delta_before_ms: int = 0
    delta_after_ms: int = 0

    # ── Pagination ────────────────────────────────────────────────
    limit: int = 25_000
    offset: int = 0

    # ── Ordering ──────────────────────────────────────────────────
    order_descending: bool = True  # newest first by default


@dataclass
class TelegramQueryResult:
    """Result of a telegram query."""

    telegrams: list[StoredTelegram]
    total_count: int
    server_filtered: bool    # True = backend applied all query filters
    limit_reached: bool      # True = more results exist beyond limit
```

### Graceful degradation matrix

| Feature | In-Memory | HA Storage | SQLite | PostgreSQL |
|---|---|---|---|---|
| Multi-value filters | ❌ returns all | ❌ returns all | ✅ SQL `IN()` | ✅ SQL `IN()` |
| Time range | ❌ returns all | ❌ returns all | ✅ `WHERE ts BETWEEN` | ✅ hypertable-optimized |
| Time-delta context | ❌ returns all | ❌ returns all | ✅ SQL subquery | ✅ native (current SpectrumKNX impl) |
| Pagination | ❌ returns all | ❌ returns all | ✅ `LIMIT/OFFSET` | ✅ `LIMIT/OFFSET` |
| Count | ✅ `len()` | ✅ `len()` | ✅ `SELECT COUNT(*)` | ✅ `SELECT COUNT(*)` |
| `server_filtered` | `False` | `False` | `True` | `True` |

When `server_filtered = False`, the consumer's frontend applies the same `TelegramQuery` filter logic client-side. This is exactly what Home Assistant does today — the group monitor filters in the browser.

---

## 6. Backend Implementations

### 6a. In-Memory Backend (`backends/memory.py`)

**Purpose:** Unit testing and development environments.

```
MemoryStore(max_size: int = 500)
```

- Stores telegrams in a `collections.deque(maxlen=max_size)`.
- `query()` returns all telegrams with `server_filtered=False`.
- `store()` / `store_many()` append to the deque (oldest are evicted when full).
- No persistence — data is lost when the process exits.
- Zero dependencies.

### 6b. HA Storage Backend (`backends/ha_storage.py`)

**Purpose:** Home Assistant's native storage. Wraps the existing HA `Store` JSON mechanism to maintain full backward compatibility. **No data migration required.**

```
HAStorageStore(store: Store, max_size: int = 500)
```

- Receives a pre-configured HA `Store[list[dict]]` instance from the HA integration.
- Internally uses `deque(maxlen=max_size)` for in-memory access, same as today.
- `load()` reads from `Store.async_load()` — handles the existing JSON format (including tuple↔list conversion for payloads).
- `save()` writes to `Store.async_save()` — same JSON format as today.
- `query()` returns all telegrams with `server_filtered=False`.
- The HA integration calls `load()` at startup and `save()` at shutdown, exactly as it does today.

> **Why this is not a breaking change:** The HA Storage backend reads and writes the same JSON structure that HA uses today. The `StoredTelegram` ↔ `TelegramDict` conversion happens at the boundary (in the HA integration code), not in the stored data. Existing `.storage/knx/telegrams_history.json` files continue to work unchanged.

### 6c. SQLite Backend (`backends/sqlite.py`)

**Purpose:** Lightweight persistent storage for HA users who want longer history without PostgreSQL, and for single-user SpectrumKNX deployments.

```
SqliteStore(db_path: str | Path, max_telegrams: int | None = None)
```

- Uses `aiosqlite` for async I/O.
- Creates a single `telegrams` table with proper indexes on `timestamp`, `source`, `destination`, `telegramtype`, `dpt_main`.
- Implements full `TelegramQuery` filtering via SQL `WHERE` clauses.
- Supports time-delta context windows via SQL subqueries.
- Optional `max_telegrams` cap with automatic pruning of oldest rows (`DELETE FROM telegrams WHERE rowid IN (SELECT rowid FROM telegrams ORDER BY timestamp ASC LIMIT ?)`).
- Optional dependency: `knx-telegram-store[sqlite]` → `aiosqlite`.

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS telegrams (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,          -- ISO 8601 with timezone
    source        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    telegramtype  TEXT NOT NULL,
    direction     TEXT NOT NULL,
    payload       TEXT,                   -- JSON-encoded
    dpt_main      INTEGER,
    dpt_sub       INTEGER,
    dpt_name      TEXT,
    unit          TEXT,
    value         TEXT,                   -- JSON-encoded
    value_numeric REAL,
    raw_data      TEXT,                   -- hex-encoded
    data_secure   INTEGER,               -- 0/1/NULL
    source_name   TEXT DEFAULT '',
    destination_name TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_telegrams_timestamp ON telegrams (timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_source ON telegrams (source, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_destination ON telegrams (destination, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_type ON telegrams (telegramtype, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_dpt ON telegrams (dpt_main, dpt_sub, timestamp DESC);
```

### 6d. PostgreSQL + TimescaleDB Backend (`backends/postgres.py`)

**Purpose:** Full-scale time-series storage for SpectrumKNX and advanced HA deployments.

```
PostgresStore(dsn: str)
```

- Uses `asyncpg` via SQLAlchemy async (matches SpectrumKNX's current stack).
- Schema extends the existing SpectrumKNX `telegrams` hypertable with new columns (`direction`, `dpt_name`, `unit`, `data_secure`, `source_name`, `destination_name`).
- `initialize()` creates the hypertable with `IF NOT EXISTS` and adds missing columns for backward compatibility with existing SpectrumKNX databases.
- Full `TelegramQuery` support including time-delta context windows (ported from SpectrumKNX's current `api.py` implementation).
- Optional dependency: `knx-telegram-store[postgres]` → `asyncpg`, `sqlalchemy[asyncio]`.

**Schema (extends existing SpectrumKNX):**

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS telegrams (
    timestamp         TIMESTAMPTZ NOT NULL,
    source            VARCHAR(20) NOT NULL,
    destination       VARCHAR(20) NOT NULL,
    telegramtype      VARCHAR(50) NOT NULL,
    direction         VARCHAR(20) NOT NULL DEFAULT '',
    payload           JSONB,
    dpt_main          INTEGER,
    dpt_sub           INTEGER,
    dpt_name          VARCHAR(100),
    unit              VARCHAR(20),
    value             JSONB,
    value_numeric     DOUBLE PRECISION,
    raw_data          BYTEA,
    data_secure       BOOLEAN,
    source_name       VARCHAR(255) DEFAULT '',
    destination_name  VARCHAR(255) DEFAULT ''
);

-- Convert to hypertable (idempotent)
SELECT create_hypertable('telegrams', 'timestamp', if_not_exists => TRUE);

-- Indexes
CREATE INDEX IF NOT EXISTS ix_telegrams_destination ON telegrams (destination, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_source ON telegrams (source, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_dpt ON telegrams (dpt_main, dpt_sub, timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_telegrams_type ON telegrams (telegramtype, timestamp DESC);
```

---

## 7. Integration Strategy

### 7a. Home Assistant KNX

```
┌──────────────────────────────────────────────────────────────┐
│  Home Assistant KNX Integration                              │
│                                                              │
│  xknx Telegram ──► telegram_to_dict() ──► StoredTelegram     │
│                          │                                   │
│                          ▼                                   │
│                   TelegramStore                              │
│                   (HA Storage / SQLite / Postgres)            │
│                          │                                   │
│                          ▼                                   │
│                   WebSocket API ──► knx-frontend              │
│                   (serializes StoredTelegram → TelegramDict)  │
└──────────────────────────────────────────────────────────────┘
```

**Changes to `telegrams.py`:**

- The `Telegrams` class receives a `TelegramStore` instance instead of managing its own `deque` + `Store`.
- `telegram_to_dict()` produces a `StoredTelegram` (internally converting HA-specific xknx enrichment).
- For the HA Storage backend: `load_history()` → `store.load()`, `save_history()` → `store.save()`. Format unchanged.
- `recent_telegrams` property → `store.query(TelegramQuery())`.
- `last_ga_telegrams` remains a consumer-side dict maintained by the `Telegrams` class (unchanged behavior).

**Changes to `websocket.py`:**

- `ws_group_monitor_info` serializes `StoredTelegram` → `TelegramDict` (backward-compatible with the frontend's existing interface).
- Future enhancement: add a `ws_query_telegrams` command that forwards `TelegramQuery` to the store, enabling server-side filtering when a SQL backend is configured.

**Frontend impact (`knx-frontend`):**

- `TelegramDict` TypeScript interface remains unchanged.
- When server-side filtering becomes available (SQL backend configured), the frontend can optionally delegate filtering to the backend via the new WS command. This is a future enhancement, not required for the initial library release.

### 7b. SpectrumKNX

```
┌──────────────────────────────────────────────────────────────┐
│  SpectrumKNX                                                 │
│                                                              │
│  xknx Telegram ──► parse_telegram_payload() ──► StoredTelegram│
│                          │                                   │
│                          ▼                                   │
│                   TelegramStore                              │
│                   (Postgres / SQLite)                         │
│                          │                                   │
│                          ▼                                   │
│                   FastAPI API ──► React frontend              │
│                   (enriches with project names at read time)  │
└──────────────────────────────────────────────────────────────┘
```

**Changes to `knx_daemon.py`:**

- Replace direct SQLAlchemy `insert()` in `process_telegram_async()` with `store.store(StoredTelegram(...))`.
- The enrichment logic (DPT parsing, value formatting) stays in SpectrumKNX — it knows about xknx context.

**Changes to `api.py`:**

- Replace hand-built SQLAlchemy queries in `get_telegrams()` with `store.query(TelegramQuery(...))`.
- The `_build_telegram_response()` display enrichment (simplified type names, formatted values) stays in the API layer.

**Changes to `models.py` / `database.py`:**

- Replaced by the library's `PostgresStore`. These files can be removed or kept as thin wrappers.

---

## 8. Package Structure

```
knx-telegram-store/
├── pyproject.toml
├── README.md
├── LICENSE                          # Apache 2.0 (matching xknx ecosystem)
├── docs/
│   └── design_and_implementation.md # This document
├── src/
│   └── knx_telegram_store/
│       ├── __init__.py              # Public API re-exports
│       ├── model.py                 # StoredTelegram dataclass
│       ├── store.py                 # TelegramStore ABC + StoreCapabilities
│       ├── query.py                 # TelegramQuery + TelegramQueryResult
│       ├── backends/
│       │   ├── __init__.py
│       │   ├── memory.py            # In-memory backend (testing)
│       │   ├── ha_storage.py        # HA Storage backend (wraps HA Store)
│       │   ├── sqlite.py            # SQLite backend
│       │   └── postgres.py          # PostgreSQL + TimescaleDB backend
│       └── _version.py
└── tests/
    ├── conftest.py                  # Shared fixtures, parametrized backend tests
    ├── test_model.py
    ├── test_query.py
    ├── test_memory_backend.py
    ├── test_ha_storage_backend.py
    ├── test_sqlite_backend.py
    └── test_postgres_backend.py
```

**`pyproject.toml` dependencies:**

```toml
[project]
name = "knx-telegram-store"
requires-python = ">=3.12"
dependencies = []  # Zero runtime dependencies for core

[project.optional-dependencies]
sqlite = ["aiosqlite>=0.20"]
postgres = ["asyncpg>=0.29", "sqlalchemy[asyncio]>=2.0"]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "aiosqlite", "asyncpg", "sqlalchemy[asyncio]"]
```

---

## 9. Implementation Roadmap

### Phase 1: Core Library (this PR)

1. **Define the interface** — `StoredTelegram`, `TelegramStore`, `TelegramQuery`, `StoreCapabilities`
2. **In-Memory backend** — For testing; simple deque-based implementation
3. **Unit tests** — Shared test suite parametrized across backends

### Phase 2: HA Storage Backend

4. **HA Storage backend** — Wraps HA's existing `Store` mechanism with zero data migration
5. **Integrate into HA** — Refactor `telegrams.py` to use `TelegramStore` interface
6. **Verify** — Existing `test_telegrams.py` tests must pass unchanged

### Phase 3: PostgreSQL Backend

7. **Extract from SpectrumKNX** — Port the SQLAlchemy storage and time-delta query logic into `PostgresStore`
8. **Refactor SpectrumKNX** — Replace `models.py` + inline queries with the library
9. **Verify** — SpectrumKNX integration tests, existing live/history views work

### Phase 4: SQLite Backend

10. **Implement SQLite** — Async SQLite with full query support
11. **Test in both consumers** — HA with SQLite backend, SpectrumKNX with SQLite backend
12. **Publish to PyPI**

### Phase 5: Frontend Enhancements (future)

13. **Server-side filtering in HA** — New `ws_query_telegrams` WebSocket command
14. **Frontend adaptation** — `knx-frontend` optionally delegates filtering when backend supports it

---

## 10. Verification Plan

### Shared Test Suite

All backends are tested against a **shared, parametrized test contract**. Each test creates the backend via a fixture and runs identical assertions:

```
test_store_single_telegram
test_store_many_telegrams
test_query_returns_all_when_no_filters
test_query_by_source                    # SQL backends only
test_query_by_destination               # SQL backends only
test_query_by_telegram_type             # SQL backends only
test_query_by_time_range                # SQL backends only
test_query_time_delta_context           # SQL backends only
test_query_pagination                   # SQL backends only
test_count
test_clear
test_max_size_pruning                   # Memory, SQLite
test_server_filtered_flag               # Memory returns False, SQL returns True
test_order_descending
test_order_ascending
```

### Backend-Specific Tests

- **HA Storage:** Mock the HA `Store` class, verify `load()` / `save()` produce the same JSON format as the current integration.
- **SQLite:** Use `tmp_path` fixture for ephemeral databases.
- **PostgreSQL:** Marked as integration tests (`pytest.mark.postgres`), require a running Postgres+TimescaleDB instance (CI uses Docker).

### Consumer Integration Tests

- **Home Assistant:** Run existing `test_telegrams.py` against the refactored `Telegrams` class. The tests should pass without modification, confirming backward compatibility.
- **SpectrumKNX:** Seed data via the library's `PostgresStore`, query via the existing `/api/telegrams` endpoint, verify identical JSON output.

### Manual Verification

1. Deploy HA with HA Storage backend → group monitor works identically to today.
2. Deploy HA with SQLite backend → persistent history survives restart.
3. Deploy SpectrumKNX with Postgres backend via library → no regression in live + history views.
4. Deploy SpectrumKNX with SQLite backend → verify lightweight deployment works.
