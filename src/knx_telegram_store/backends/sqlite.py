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

        # 1. Detect if we are on the legacy (pre-normalized) schema
        if "source" in existing_columns:
            # Populate string_lookup table
            for cat, old_col in cols_to_migrate.items():
                if old_col in existing_columns:
                    connection.execute(
                        text(
                            f"INSERT OR IGNORE INTO string_lookup (category, value) "
                            f"SELECT DISTINCT '{cat}', CAST({old_col} AS TEXT) FROM telegrams WHERE {old_col} IS NOT NULL"
                        )
                    )

            # Add *_id columns
            for cat in cols_to_migrate:
                id_col = f"{cat}_id"
                if id_col not in existing_columns:
                    connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {id_col} INTEGER"))

            # Update IDs
            for cat, old_col in cols_to_migrate.items():
                connection.execute(
                    text(
                        f"UPDATE telegrams SET {cat}_id = ("
                        f"SELECT id FROM string_lookup WHERE category='{cat}' AND value=CAST(telegrams.{old_col} AS TEXT))"
                    )
                )

            # Drop old columns (requires SQLite 3.35.0+)
            for old_col in list(cols_to_migrate.values()) + ["dpt_name", "unit"]:
                try:
                    connection.execute(text(f"ALTER TABLE telegrams DROP COLUMN {old_col}"))
                    if old_col in existing_columns:
                        existing_columns.remove(old_col)
                except Exception:
                    # Older SQLite versions don't support DROP COLUMN.
                    # We leave them as redundant columns.
                    pass

        # Drop legacy normalized columns if present
        for col in ["dpt_name_id", "unit_id", "dpt_name", "unit"]:
            if col in existing_columns and col not in cols_to_migrate:
                try:
                    connection.execute(text(f"ALTER TABLE telegrams DROP COLUMN {col}"))
                except Exception:
                    pass

        # 2. Handle missing columns from intermediate versions (non-normalized ones)
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

    def _needs_migration_sync(self, connection) -> bool:
        """Synchronously check if legacy SQLite schema migration is required."""
        inspector = inspect(connection)
        if not inspector.has_table("telegrams"):
            return False
        columns = inspector.get_columns("telegrams")
        existing_columns = {col["name"] for col in columns}

        # 1. Pre-normalized legacy schema needs migration
        if "source" in existing_columns:
            return True

        # 2. Missing columns from intermediate/non-normalized versions
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

        # 3. Drop legacy columns
        for col in ["dpt_name_id", "unit_id", "dpt_name", "unit"]:
            if col in existing_columns:
                return True

        return False
