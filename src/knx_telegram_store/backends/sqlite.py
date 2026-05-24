from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from .base_sql import BaseSQLStore


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
                "timestamp", "source_id", "destination_id", "telegramtype_id",
                "direction_id", "source_name_id", "destination_name_id",
                "payload", "dpt_main", "dpt_sub", "value", "value_numeric",
                "raw_data", "data_secure",
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
            connection.execute(
                text(f"INSERT INTO telegrams ({cols_sql}) SELECT {cols_sql} FROM _telegrams_old")
            )
            connection.execute(text("DROP TABLE _telegrams_old"))




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

        return False
