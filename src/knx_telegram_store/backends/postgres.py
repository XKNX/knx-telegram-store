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
        existing_columns = {col["name"] for col in inspector.get_columns("telegrams")}
        
        # Mapping of library names to existing SpectrumKNX names for compatibility
        # If SpectrumKNX has 'source_address', we might want to aliasing or rename.
        # For now, we assume we want the library names.
        
        expected_columns = {
            "direction": "VARCHAR(20) DEFAULT ''",
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
