from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from ..connection import (
    ConnectionCheckResult,
    ConnectionErrorKind,
    evaluate_sqlite_path,
    probe_engine,
)
from .base_sql import BaseSQLStore


def _classify_sqlite_error(exc: BaseException) -> ConnectionErrorKind:
    """Map a SQLite connection exception to a ConnectionErrorKind."""
    orig = getattr(exc, "orig", None) or exc
    if isinstance(orig, PermissionError | OSError):
        return ConnectionErrorKind.PERMISSION
    return ConnectionErrorKind.UNKNOWN


class SqliteStore(BaseSQLStore):
    """Async SQLite implementation of TelegramStore."""

    def __init__(self, db_path: str | Path, retention_days: int | None = None) -> None:
        """Initialize the SQLite store."""
        # Ensure parent directory exists (unless in-memory)
        if str(db_path) != ":memory:":
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{path}"
        else:
            url = "sqlite+aiosqlite:///:memory:"

        engine = create_async_engine(url)
        super().__init__(engine, retention_days)

    @staticmethod
    def check_config(db_path: str | Path) -> ConnectionCheckResult:
        """Validate a SQLite path without constructing a store or touching the disk.

        Synchronous — this is a pure filesystem check (no I/O await). Returns a
        structured result indicating whether the file is writeable or can be created.
        """
        return evaluate_sqlite_path(db_path)

    async def check_connection(self, *, timeout: float = 5.0) -> ConnectionCheckResult:
        """Probe the SQLite database with ``SELECT 1`` (creates an empty file if missing)."""
        return await probe_engine(self.engine, timeout=timeout, classify=_classify_sqlite_error)

    async def initialize(self) -> None:
        """Set up the database schema and perform upgrades."""
        async with self.engine.begin() as conn:
            # 1. Create table if not exists
            await conn.run_sync(self._metadata.create_all)

            # 2. Perform column-level upgrades
            await conn.run_sync(self._upgrade_schema)

        # 3. Warm the cache
        await super().initialize()

    def _upgrade_schema(self, connection) -> None:
        """Synchronous part of schema upgrade (run via run_sync)."""
        inspector = inspect(connection)
        columns = inspector.get_columns("telegrams")
        existing_columns = {col["name"] for col in columns}

        cols_to_migrate = {
            "source": "source",
            "destination": "destination",
            "telegramtype": "telegramtype",
            "direction": "direction",
            "source_name": "source_name",
            "destination_name": "destination_name",
        }

        # Legacy columns that should not exist in the final schema
        legacy_columns = set(cols_to_migrate.values()) | {"dpt_name", "unit", "dpt_name_id", "unit_id"}

        # Check if any legacy columns still exist (drop failed in a previous run or first run)
        has_legacy_columns = bool(existing_columns & legacy_columns)
        has_old_string_cols = "source" in existing_columns  # Pre-normalized schema

        if has_old_string_cols:
            # 1a. Populate string_lookup table from old string columns
            for cat, old_col in cols_to_migrate.items():
                if old_col in existing_columns:
                    connection.execute(
                        text(
                            f"INSERT OR IGNORE INTO string_lookup (category, value) "
                            f"SELECT DISTINCT '{cat}', CAST({old_col} AS TEXT) FROM telegrams WHERE {old_col} IS NOT NULL"
                        )
                    )

            # 1b. Add *_id columns (nullable for now — will be enforced via table rebuild)
            for cat in cols_to_migrate:
                id_col = f"{cat}_id"
                if id_col not in existing_columns:
                    connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {id_col} INTEGER"))
                    existing_columns.add(id_col)

            # 1c. Populate *_id values from string_lookup
            for cat, old_col in cols_to_migrate.items():
                connection.execute(
                    text(
                        f"UPDATE telegrams SET {cat}_id = ("
                        f"SELECT id FROM string_lookup WHERE category='{cat}' AND value=CAST(telegrams.{old_col} AS TEXT))"
                    )
                )

        # 2. Add any missing expected columns (intermediate schema versions)
        expected_columns = {
            "payload": "JSON",
            "dpt_main": "INTEGER",
            "dpt_sub": "INTEGER",
            "value": "JSON",
            "value_numeric": "DOUBLE",
            "data_secure": "BOOLEAN",
        }
        for col_name, col_type in expected_columns.items():
            if col_name not in existing_columns and f"{col_name}_id" not in existing_columns:
                connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {col_name} {col_type}"))
                existing_columns.add(col_name)

        # 3. Rebuild the table to drop legacy columns (works on all SQLite versions and
        #    handles indexed columns correctly, unlike ALTER TABLE DROP COLUMN).
        if has_legacy_columns:
            # Determine the final set of columns we want to keep
            keep_cols = [
                "timestamp",
                "source_id",
                "destination_id",
                "telegramtype_id",
                "direction_id",
                "source_name_id",
                "destination_name_id",
                "payload",
                "dpt_main",
                "dpt_sub",
                "value",
                "value_numeric",
                "raw_data",
                "data_secure",
            ]
            # Only copy columns that actually exist to avoid errors
            copy_cols = [c for c in keep_cols if c in existing_columns]
            cols_sql = ", ".join(copy_cols)

            # Drop all indexes on the telegrams table before renaming — SQLite
            # preserves index names when renaming a table, which would conflict
            # with the new table's indexes created by metadata.create().
            old_indexes = inspector.get_indexes("telegrams")
            for idx in old_indexes:
                connection.execute(text(f"DROP INDEX IF EXISTS {idx['name']}"))

            connection.execute(text("DROP TABLE IF EXISTS _telegrams_old"))
            connection.execute(text("ALTER TABLE telegrams RENAME TO _telegrams_old"))
            # Recreate from SQLAlchemy metadata (enforces correct NOT NULL / types)
            self._metadata.tables["telegrams"].create(connection)
            connection.execute(text(f"INSERT INTO telegrams ({cols_sql}) SELECT {cols_sql} FROM _telegrams_old"))
            connection.execute(text("DROP TABLE _telegrams_old"))

        # 3.5. Populate value from value_numeric if it is missing/null (legacy SpectrumKNX schema)
        if "value" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(
                text(
                    "UPDATE telegrams SET value = CAST(value_numeric AS TEXT) "
                    "WHERE (value IS NULL OR value = 'null') AND value_numeric IS NOT NULL"
                )
            )

        # 4. Data unwrapping pass for legacy {"value": ...} wrapped structures
        try:
            # Query rowid, value, payload from telegrams where they are legacy JSON wrapped
            rows = connection.execute(
                text(
                    "SELECT rowid, value, payload FROM telegrams WHERE value LIKE '{\"value\":%' OR payload LIKE '{\"value\":%'"
                )
            ).fetchall()

            if rows:
                import json

                for row in rows:
                    row_id = row[0]
                    val_str = row[1]
                    pay_str = row[2]

                    new_val = None
                    new_pay = None
                    needs_update = False

                    def unwrap(s):
                        if s is None:
                            return None, False
                        try:
                            if isinstance(s, dict):
                                d = s
                            else:
                                d = json.loads(s)
                            if isinstance(d, dict) and "value" in d and len(d) == 1:
                                return d["value"], True
                        except Exception:
                            pass
                        return s, False

                    if val_str is not None:
                        unwrapped_val, unwrapped = unwrap(val_str)
                        if unwrapped:
                            new_val = unwrapped_val
                            needs_update = True
                        else:
                            new_val = val_str

                    if pay_str is not None:
                        unwrapped_pay, unwrapped = unwrap(pay_str)
                        if unwrapped:
                            new_pay = unwrapped_pay
                            needs_update = True
                        else:
                            new_pay = pay_str

                    if needs_update:

                        def to_json_str(orig_val, new_val_unwrapped, did_unwrap):
                            if did_unwrap:
                                return json.dumps(new_val_unwrapped)
                            if orig_val is None:
                                return None
                            if isinstance(orig_val, dict | list | int | float | bool):
                                return json.dumps(orig_val)
                            try:
                                json.loads(orig_val)
                                return orig_val
                            except Exception:
                                return json.dumps(orig_val)

                        json_val = to_json_str(val_str, new_val, val_str != new_val)
                        json_pay = to_json_str(pay_str, new_pay, pay_str != new_pay)

                        connection.execute(
                            text("UPDATE telegrams SET value = :value, payload = :payload WHERE rowid = :rowid"),
                            {"value": json_val, "payload": json_pay, "rowid": row_id},
                        )

            # Record successful migration state in store_metadata
            connection.execute(
                text("INSERT OR REPLACE INTO store_metadata (key, value) VALUES ('data_unwrapped', 'true')")
            )
        except Exception:
            pass

    def _needs_migration_sync(self, connection) -> bool:
        """Synchronously check if legacy SQLite schema migration is required."""
        inspector = inspect(connection)
        if not inspector.has_table("telegrams"):
            return False
        columns = inspector.get_columns("telegrams")
        existing_columns = {col["name"] for col in columns}

        # 1. Pre-normalized legacy schema (string columns instead of *_id)
        if "source" in existing_columns:
            return True

        # 2. Partially-migrated: old string columns still present (DROP COLUMN
        #    failed silently on a previous run, e.g. due to indexed columns).
        legacy_string_cols = {"destination", "telegramtype", "direction", "source_name", "destination_name"}
        if existing_columns & legacy_string_cols:
            return True

        # 3. Legacy intermediate columns from earlier schema versions
        for col in ["dpt_name_id", "unit_id", "dpt_name", "unit"]:
            if col in existing_columns:
                return True

        # 4. Missing expected columns from intermediate versions
        expected_columns = {
            "payload",
            "dpt_main",
            "dpt_sub",
            "value",
            "value_numeric",
            "data_secure",
        }
        for col_name in expected_columns:
            if col_name not in existing_columns and f"{col_name}_id" not in existing_columns:
                return True

        # 4.5. Check if there are any legacy 'null' values to recover from value_numeric
        if "value" in existing_columns and "value_numeric" in existing_columns:
            try:
                row = connection.execute(
                    text(
                        "SELECT 1 FROM telegrams WHERE (value IS NULL OR value = 'null') AND value_numeric IS NOT NULL LIMIT 1"
                    )
                ).fetchone()
                if row:
                    return True
            except Exception:
                pass

        # 5. Check if any rows contain legacy {"value": ...} wrapped values
        # Skip this scan entirely if the metadata table indicates we already unwrapped
        is_unwrapped = False
        try:
            if inspector.has_table("store_metadata"):
                row = connection.execute(
                    text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")
                ).fetchone()
                if row and row[0] == "true":
                    is_unwrapped = True
        except Exception:
            pass

        if not is_unwrapped:
            try:
                row = connection.execute(
                    text(
                        "SELECT 1 FROM telegrams WHERE value LIKE '{\"value\":%' OR payload LIKE '{\"value\":%' LIMIT 1"
                    )
                ).fetchone()
                if row:
                    return True
            except Exception:
                pass

        return False
