# KNX Telegram Store - Agent Instructions

This document outlines the workflows, architectural decisions, and release procedures for future AI agents working on the `knx-telegram-store` library and its downstream Home Assistant integration.

---

## 1. Architecture & Core Concepts

The `knx-telegram-store` library provides standalone, host-agnostic persistence for KNX telegrams using a normalized lookup architecture for low-cardinality string columns (e.g. source/destination addresses, telegram types, directions).

### String Lookup Tables
- Frequently repeated string values are stored in a centralized `string_lookup` table to save storage space and optimize index performance.
- Memory-efficient `LookupCache` is implemented to minimize database roundtrips during massive batch writes.

### Legacy Value Recovery (Falsy & Numeric Data)
- During migrations from legacy JSON/SpectrumKNX schemas, ensure that the `value` column is populated from `value_numeric` whenever `value` is SQL `NULL` or the JSON string `'null'`.
- Both database-level upgrades (`_upgrade_schema`) and status checkers (`_needs_migration_sync`) in [sqlite.py](src/knx_telegram_store/backends/sqlite.py) and [postgres.py](src/knx_telegram_store/backends/postgres.py) must look for and recover these falsy `'null'` instances:
  - **SQLite**: `WHERE (value IS NULL OR value = 'null') AND value_numeric IS NOT NULL`
  - **PostgreSQL**: `WHERE (value IS NULL OR value = 'null'::jsonb) AND value_numeric IS NOT NULL`

---

## 2. Testing and Quality Checks

Before releasing any version, you **must** run all linters and tests to maintain absolute code health:

### Running Tests
Execute unit and migration integration tests using the local virtual environment:
```bash
# Run pytest with a timeout to avoid hangs
timeout 15 .venv/bin/pytest
```

### Checking Linters and Formatters
Ruff is used for coding standards and formatting. Always confirm cleanliness:
```bash
# Style guide check
.venv/bin/ruff check .

# Code formatter check
.venv/bin/ruff format --check .
```

---

## 3. Release and Tagging Workflow

To release a new version of the store (e.g., bumping from `0.3.1` to `0.3.2`):

1. **Bump Version**: Update the version tag in `pyproject.toml`:
   ```toml
   [project]
   version = "0.3.2"
   ```
2. **Verify Tests**: Re-run the tests and formatters to ensure zero regressions.
3. **Commit the Bump**: Commit the version bump with a clean message:
   ```bash
   git add pyproject.toml
   git commit -m "release: v0.3.2"
   ```
4. **Create a Git Tag**: Tag the release commit using the standard `v` prefix format:
   ```bash
   git tag v0.3.2
   ```
5. **Push to Remote**: Push both the current branch and the new tag to `origin`:
   ```bash
   git push origin <branch-name> && git push origin v0.3.2
   ```

---

## 4. Downstream Home Assistant Core Integration Guidelines

When integrating or testing changes downstream in `ha-core` under `homeassistant/components/knx`:

### Datetime Validation
- **Timezone/Offset Precision**: Never use standard voluptuous `vol.Datetime()` for historical queries since it strips offsets and parses naive datetimes, shifting time ranges by timezone offsets.
- Always use `cv.datetime` in the websocket schema (`websocket.py`) to parse fully timezone-aware ISO 8601 strings.

### Sorting & Reversal Optimization
- By design, the frontend group monitor sorts and manages its buffer chronologically ascending (oldest first).
- However, to keep database queries optimized, the websocket endpoint `ws_group_monitor_info` returns telegrams in raw descending order directly from the database query (newest first). The frontend's buffer automatically manages sorting on insertion.
- Backend test assertions in `test_websocket.py` must expect the descending order (e.g. newest outgoing telegram at index `0`, older incoming telegram at index `1`).

### Name Resolution Fallback
- Stored records in the database might have empty device or group address names.
- When converting models to dictionary responses in `telegrams.py`, always fall back dynamically to the loaded KNX project file (`self.project.devices` / `self.project.group_addresses`) to resolve names if they are empty in the database.
