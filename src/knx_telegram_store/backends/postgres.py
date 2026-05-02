from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from .base_sql import BaseSQLStore


class PostgresStore(BaseSQLStore):
    """PostgreSQL + TimescaleDB implementation of TelegramStore."""

    def __init__(
        self, 
        dsn: str, 
        max_telegrams: int | None = None
    ) -> None:
        """Initialize the Postgres store."""
        # Ensure we use asyncpg
        if dsn.startswith("postgresql://"):
            dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        
        engine = create_async_engine(dsn)
        super().__init__(engine, max_telegrams)

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
            await conn.execute(text(
                "SELECT create_hypertable('telegrams', 'timestamp', if_not_exists => TRUE)"
            ))

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
            "value": "value_numeric"  # Legacy value was FLOAT, library value is JSONB
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
                    connection.execute(text("ALTER TABLE telegrams ALTER COLUMN raw_data TYPE TEXT USING encode(raw_data, 'hex')"))
        
        # 2. Ensure all library columns exist
        expected_columns = {
            "direction": "VARCHAR(20) DEFAULT 'Incoming'",
            "value": "JSONB",
            "value_numeric": "FLOAT",
            "payload": "JSONB",
            "dpt_name": "VARCHAR(100)",
            "unit": "VARCHAR(20)",
            "data_secure": "BOOLEAN",
            "source_name": "VARCHAR(255) DEFAULT ''",
            "destination_name": "VARCHAR(255) DEFAULT ''",
        }
        
        for col_name, col_type in expected_columns.items():
            if col_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {col_name} {col_type}"))
                existing_columns.add(col_name)

        # 3. Data migrations for old SpectrumKNX rows
        # Old schema had value_numeric (FLOAT) and value_json (now payload),
        # but no value (JSONB) column. Populate value from value_numeric
        # so the library's query returns it correctly.
        if "value" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(text(
                'UPDATE telegrams SET value = to_jsonb(value_numeric) '
                'WHERE value IS NULL AND value_numeric IS NOT NULL'
            ))

        # Handle edge case from intermediate migrations where value was
        # a FLOAT column renamed to value_legacy_float
        if "value_legacy_float" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(text(
                'UPDATE telegrams SET value_numeric = value_legacy_float '
                'WHERE value_numeric IS NULL AND value_legacy_float IS NOT NULL'
            ))
