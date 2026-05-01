from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from .base_sql import BaseSQLStore


class SqliteStore(BaseSQLStore):
    """Async SQLite implementation of TelegramStore."""

    def __init__(
        self, 
        db_path: str | Path, 
        max_telegrams: int | None = None
    ) -> None:
        """Initialize the SQLite store."""
        # Ensure parent directory exists (unless in-memory)
        if str(db_path) != ":memory:":
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{path}"
        else:
            url = "sqlite+aiosqlite:///:memory:"
        
        engine = create_async_engine(url)
        super().__init__(engine, max_telegrams)

    async def initialize(self) -> None:
        """Set up the database schema and perform upgrades."""
        async with self.engine.begin() as conn:
            # 1. Create table if not exists
            await conn.run_sync(self._metadata.create_all)
            
            # 2. Perform column-level upgrades
            await conn.run_sync(self._upgrade_schema)

    def _upgrade_schema(self, connection) -> None:
        """Synchronous part of schema upgrade (run via run_sync)."""
        inspector = inspect(connection)
        existing_columns = {col["name"] for col in inspector.get_columns("telegrams")}
        
        # Define missing columns that should be added to existing schemas
        # (e.g. from early SpectrumKNX or HA versions)
        expected_columns = {
            "direction": "VARCHAR(20) DEFAULT ''",
            "payload": "JSON",
            "dpt_name": "VARCHAR(100)",
            "unit": "VARCHAR(20)",
            "data_secure": "BOOLEAN",
            "source_name": "VARCHAR(255) DEFAULT ''",
            "destination_name": "VARCHAR(255) DEFAULT ''",
        }
        
        for col_name, col_type in expected_columns.items():
            if col_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {col_name} {col_type}"))
