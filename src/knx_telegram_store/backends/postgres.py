from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from .base_sql import BaseSQLStore


class PostgresStore(BaseSQLStore):
    """PostgreSQL + TimescaleDB implementation of TelegramStore."""

    def __init__(self, dsn: str, retention_days: int | None = None) -> None:
        """Initialize the Postgres store."""
        # Ensure we use asyncpg
        if dsn.startswith("postgresql://"):
            dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

        connect_args = {}
        if "sslmode=require" not in dsn and "ssl=" not in dsn:
            # Default to no SSL if not explicitly requested, to avoid blocking cert loading
            connect_args["ssl"] = False

        engine = create_async_engine(dsn, connect_args=connect_args)
        super().__init__(engine, retention_days)

    async def initialize(self) -> None:
        """Set up the database schema and perform upgrades."""
        async with self.engine.begin() as conn:
            # 1. Enable TimescaleDB extension
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))

            # 2. Create table if not exists
            await conn.run_sync(self._metadata.create_all)

            # 3. Perform column-level upgrades
            await conn.run_sync(self._upgrade_schema)

            # 4. Convert to hypertable (idempotent)
            await conn.execute(text("SELECT create_hypertable('telegrams', 'timestamp', if_not_exists => TRUE)"))

        # 5. Warm the cache
        await super().initialize()

    def _upgrade_schema(self, connection) -> None:
        """Synchronous part of schema upgrade (run via run_sync)."""
        inspector = inspect(connection)
        try:
            columns = inspector.get_columns("telegrams")
        except Exception:
            # Table might not exist yet
            return
        existing_columns = {col["name"] for col in columns}

        # 1. Handle renames from legacy SpectrumKNX schema
        renames = {
            "source_address": "source",
            "target_address": "destination",
            "telegram_type": "telegramtype",
            "value_json": "payload",
            "value": "value_numeric",  # Legacy value was FLOAT, library value is JSONB
        }
        for old, new in renames.items():
            if old in existing_columns:
                if new not in existing_columns:
                    connection.execute(text(f'ALTER TABLE telegrams RENAME COLUMN "{old}" TO "{new}"'))
                    existing_columns.remove(old)
                    existing_columns.add(new)
                elif old == "value":
                    # Special case: 'value' (float) and 'value_numeric' (float) both exist.
                    # We must move 'value' out of the way so it can be recreated as JSONB.
                    is_float = any(c["name"] == "value" and "double" in str(c["type"]).lower() for c in columns)
                    if is_float:
                        connection.execute(text('ALTER TABLE telegrams RENAME COLUMN "value" TO "value_legacy_float"'))
                        existing_columns.remove("value")
                        existing_columns.add("value_legacy_float")

        # Migrate raw_data from bytea to text (hex encoded)
        if "raw_data" in existing_columns:
            for col in columns:
                if col["name"] == "raw_data" and "bytea" in str(col["type"]).lower():
                    connection.execute(
                        text("ALTER TABLE telegrams ALTER COLUMN raw_data TYPE TEXT USING encode(raw_data, 'hex')")
                    )

        cols_to_migrate = {
            "source": "source",
            "destination": "destination",
            "telegramtype": "telegramtype",
            "direction": "direction",
            "source_name": "source_name",
            "destination_name": "destination_name",
        }

        # 2. Handle normalization to string_lookup
        if "source" in existing_columns:
            # Populate string_lookup table
            for cat, old_col in cols_to_migrate.items():
                if old_col in existing_columns:
                    connection.execute(
                        text(
                            f"INSERT INTO string_lookup (category, value) "
                            f"SELECT DISTINCT '{cat}', {old_col} FROM telegrams WHERE {old_col} IS NOT NULL "
                            f"ON CONFLICT DO NOTHING"
                        )
                    )

            # Add *_id columns
            for cat in cols_to_migrate:
                id_col = f"{cat}_id"
                if id_col not in existing_columns:
                    connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {id_col} INTEGER"))

            # Update IDs using JOIN
            for cat, old_col in cols_to_migrate.items():
                connection.execute(
                    text(
                        f"UPDATE telegrams SET {cat}_id = sl.id "
                        f"FROM string_lookup sl WHERE sl.category='{cat}' AND sl.value=telegrams.{old_col}"
                    )
                )

            # Drop old columns
            for old_col in list(cols_to_migrate.values()) + ["dpt_name", "unit"]:
                if old_col in existing_columns:
                    connection.execute(text(f'ALTER TABLE telegrams DROP COLUMN "{old_col}"'))
                    existing_columns.remove(old_col)

            # Re-fetch existing columns after drops
            columns = inspector.get_columns("telegrams")
            existing_columns = {col["name"] for col in columns}

        # Drop legacy normalized columns if present
        for col in ["dpt_name_id", "unit_id", "dpt_name", "unit"]:
            if col in existing_columns and col not in cols_to_migrate:
                try:
                    connection.execute(text(f'ALTER TABLE telegrams DROP COLUMN "{col}"'))
                except Exception:
                    pass

        # 3. Ensure all non-normalized library columns exist
        expected_columns = {
            "value": "JSONB",
            "value_numeric": "FLOAT",
            "payload": "JSONB",
            "data_secure": "BOOLEAN",
            "dpt_main": "INTEGER",
            "dpt_sub": "INTEGER",
        }

        for col_name, col_type in expected_columns.items():
            if col_name not in existing_columns and f"{col_name}_id" not in existing_columns:
                connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {col_name} {col_type}"))
                existing_columns.add(col_name)

        # 4. Data migrations for old SpectrumKNX rows
        # Old schema had value_numeric (FLOAT) and value_json (now payload),
        # but no value (JSONB) column. Populate value from value_numeric
        # so the library's query returns it correctly.
        if "value" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(
                text(
                    "UPDATE telegrams SET value = to_jsonb(value_numeric) "
                    "WHERE value IS NULL AND value_numeric IS NOT NULL"
                )
            )

        # Handle edge case from intermediate migrations where value was
        # a FLOAT column renamed to value_legacy_float
        if "value_legacy_float" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(
                text(
                    "UPDATE telegrams SET value_numeric = value_legacy_float "
                    "WHERE value_numeric IS NULL AND value_legacy_float IS NOT NULL"
                )
            )
