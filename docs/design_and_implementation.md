# `knx-telegram-store` — Design & Implementation Plan

> **Status:** In Progress — Syncing with implementation plan  
> **Date:** 2026-05-01  
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
9. [Open Questions](#9-open-questions)
10. [Implementation Roadmap](#10-implementation-roadmap)
11. [Verification Plan](#11-verification-plan)

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
3. Ships three backends: **In-Memory** (testing and small deployments), **SQLite** (lightweight persistent), **PostgreSQL/TimescaleDB** (full scale).
4. Provides a **unified query/filter model** implemented natively by all backends.
5. Is usable from both Home Assistant KNX and SpectrumKNX **without pulling in their respective framework dependencies**.

---

## 2. Design Goals & Constraints

### Goals

- **Backend independence:** The library has no dependency on Home Assistant, SpectrumKNX, FastAPI, or xknx. Consumers handle telegram enrichment (DPT decoding, name resolution) before writing to the store.
- **Shared features:** Filtering, time-delta context windows, pagination, and time-range queries are implemented once in SQL backends and benefit both consumers.
- **Zero-migration:** The library defines the interface; consumers wrap their existing storage or use the provided SQL backends.
- **Batch Storage Efficiency:** For high-throughput backends (SQL), `store_many()` is recommended with a minimum batch size of **10 telegrams** for better write performance.
- **Incremental adoption:** Each consumer can adopt the library independently:
  1. Define the interface in the library.
  2. Implement/Wrap existing storage as a backend.
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
        
        All backends MUST implement full filtering as defined in TelegramQuery.
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

Home Assistant currently maintains a `last_ga_telegrams` dict (most recent telegram per group address). This remains a **consumer-side concern** in HA. At startup, HA can compute this dict by iterating over the results of an initial "recent telegrams" query from the store. The storage library focuses on bulk storage and querying.

> [!NOTE]
> Since this is removed from the library interface, it **must be explicitly implemented/maintained in the HA `Telegrams` class** during integration.

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
    limit_reached: bool      # True = more results exist beyond limit
```

### Graceful degradation matrix

| Feature | In-Memory | SQLite | PostgreSQL |
|---|---|---|---|
| Multi-value filters | ✅ list comprehension | ✅ SQL `IN()` | ✅ SQL `IN()` |
| Time range | ✅ timestamp check | ✅ `WHERE ts BETWEEN` | ✅ hypertable-optimized |
| Time-delta context | ✅ two-pass filter | ✅ SQL subquery | ✅ native (current SpectrumKNX impl) |
| Pagination | ✅ list slicing | ✅ `LIMIT/OFFSET` | ✅ `LIMIT/OFFSET` |
| Count | ✅ `len()` | ✅ `SELECT COUNT(*)` | ✅ `SELECT COUNT(*)` |

Since all backends support native filtering, the consumer can trust that the results returned by `query()` are accurate and do not require further client-side processing.

---

## 6. Backend Implementations

### 6a. In-Memory Backend (`backends/memory.py`)

**Purpose:** Unit testing and development environments, or simple deployments that don't require persistence.

```
MemoryStore(max_size: int = 500)
```

- Stores telegrams in a `collections.deque(maxlen=max_size)`.
- **Full Query Support:** Implements the `TelegramQuery` model natively via Python list processing.
- `query()` returns filtered telegrams.
- `store()` / `store_many()` append to the deque (oldest are evicted when full).
- No persistence — data is lost when the process exits.
- Zero dependencies.

### 6b. SQLite Backend (`backends/sqlite.py`)

**Purpose:** Lightweight persistent storage for HA users who want longer history without PostgreSQL, and for single-user SpectrumKNX deployments.

```
SqliteStore(db_path: str | Path, max_telegrams: int | None = None)
```

- Uses `aiosqlite` for async I/O.
- **Automated Schema Management:** `initialize()` handles creation of the `telegrams` table and indices. It also manages idempotent schema upgrades (adding missing columns) if an existing database is found.
- Implements full `TelegramQuery` filtering via SQL `WHERE` clauses.
- Supports time-delta context windows via SQL subqueries.
- Optional `max_telegrams` cap with automatic pruning of oldest rows (`DELETE FROM telegrams WHERE rowid IN (SELECT rowid FROM telegrams ORDER BY timestamp ASC LIMIT ?)`).
- Optional dependency: `knx-telegram-store[sqlite]` → `aiosqlite`.

> [!NOTE]
> `aiosqlite` is chosen for its async-friendly interface. While HA's `recorder` uses `SQLAlchemy` + `sqlite3`, `aiosqlite` is safe for the event loop as it offloads I/O to a thread.

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

### 6c. PostgreSQL + TimescaleDB Backend (`backends/postgres.py`)

**Purpose:** Full-scale time-series storage for SpectrumKNX and advanced HA deployments.

```
PostgresStore(dsn: str)
```

- **Automated Schema Management:** `initialize()` handles creation of the `telegrams` hypertable and indices. It supports idempotent schema upgrades for existing SpectrumKNX/Postgres databases by detecting and adding missing columns.
- Full `TelegramQuery` support including time-delta context windows (ported from SpectrumKNX's current `api.py` implementation).
- **TimescaleDB Optimization:** The backend leverages TimescaleDB hypertables and should use time-series specific functions (like `time_bucket`) for efficient time-range queries where applicable.
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

- The `Telegrams` class receives a `TelegramStore` instance instead of managing its own `deque`.
- `telegram_to_dict()` produces a `StoredTelegram` (internally converting HA-specific xknx enrichment).
- `recent_telegrams` property → `store.query(TelegramQuery())`.
- `last_ga_telegrams` remains a consumer-side dict maintained by the `Telegrams` class. It is populated at startup by computing it from the initial set of recent telegrams loaded from the store.
- **Configuration:** HA must allow users to configure the preferred backend (Memory, SQLite, or Postgres) via integration options.

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
├── LICENSE                          # MIT License (matching knx-frontend)
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

## 9. Open Questions

*(No open questions remaining)*

---

## 10. Future Ideas

- **Backend Migration:** Provide a utility to migrate data between backends (e.g., from HA Storage JSON to SQLite).

---

## 11. Implementation Roadmap

### Phase 1: Core Library (this PR)

1. **Define the interface** — `StoredTelegram`, `TelegramStore`, `TelegramQuery`, `StoreCapabilities`
2. **In-Memory backend** — For testing; simple deque-based implementation
3. **Unit tests** — Shared test suite parametrized across backends
4. **Project Setup** — Initialize `LICENSE` (MIT) and project documentation

### Phase 2: PostgreSQL Backend

7. **Extract from SpectrumKNX** — Port the SQLAlchemy storage and time-delta query logic into `PostgresStore`
8. **Refactor SpectrumKNX** — Replace `models.py` + inline queries with the library
9. **Verify** — SpectrumKNX integration tests, existing live/history views work

### Phase 3: SQLite Backend

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
