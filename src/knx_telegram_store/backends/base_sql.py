from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from ..model import StoredTelegram
from ..query import TelegramQuery, TelegramQueryResult
from ..store import StoreCapabilities, TelegramStore


class BaseSQLStore(TelegramStore):
    """Base class for SQLAlchemy-based telegram stores."""

    def __init__(self, engine: AsyncEngine, max_telegrams: int | None = None) -> None:
        """Initialize the SQL store."""
        self.engine = engine
        self._max_telegrams = max_telegrams
        self._metadata = MetaData()
        self.telegrams = Table(
            "telegrams",
            self._metadata,
            Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
            Column("source", String(20), nullable=False),
            Column("destination", String(20), nullable=False, index=True),
            Column("telegramtype", String(50), nullable=False),
            Column("direction", String(20), nullable=False, server_default=""),
            Column("payload", JSON, nullable=True),
            Column("dpt_main", Integer, nullable=True),
            Column("dpt_sub", Integer, nullable=True),
            Column("dpt_name", String(100), nullable=True),
            Column("unit", String(20), nullable=True),
            Column("value", JSON, nullable=True),
            Column("value_numeric", Double, nullable=True),
            Column("raw_data", Text, nullable=True),  # Hex encoded string
            Column("data_secure", Boolean, nullable=True),
            Column("source_name", String(255), server_default=""),
            Column("destination_name", String(255), server_default=""),
        )
        self._capabilities = StoreCapabilities(
            supports_time_range=True,
            supports_time_delta=True,
            supports_pagination=True,
            supports_count=True,
            max_storage=max_telegrams,
        )

    @property
    def capabilities(self) -> StoreCapabilities:
        """Return the capabilities of this backend."""
        return self._capabilities

    @abstractmethod
    async def initialize(self) -> None:
        """Set up the store (create tables, upgrades)."""

    async def close(self) -> None:
        """Close the engine."""
        await self.engine.dispose()

    async def store(self, telegram: StoredTelegram) -> None:
        """Persist a single telegram."""
        await self.store_many([telegram])

    async def store_many(self, telegrams: Sequence[StoredTelegram]) -> None:
        """Persist multiple telegrams."""
        if not telegrams:
            return

        values = [
            {
                "timestamp": t.timestamp,
                "source": t.source,
                "destination": t.destination,
                "telegramtype": t.telegramtype,
                "direction": t.direction,
                "payload": t.payload,
                "dpt_main": t.dpt_main,
                "dpt_sub": t.dpt_sub,
                "dpt_name": t.dpt_name,
                "unit": t.unit,
                "value": t.value,
                "value_numeric": t.value_numeric,
                "raw_data": t.raw_data,
                "data_secure": t.data_secure,
                "source_name": t.source_name,
                "destination_name": t.destination_name,
            }
            for t in telegrams
        ]

        async with self.engine.begin() as conn:
            await conn.execute(self.telegrams.insert(), values)
            if self._max_telegrams:
                await self._prune(conn)

    async def _prune(self, conn: Any) -> None:
        """Prune old telegrams if max_storage is exceeded."""
        # Simple pruning: delete oldest rows beyond max_telegrams
        # Note: This could be optimized for large databases
        count_stmt = select(func.count()).select_from(self.telegrams)
        count = await conn.scalar(count_stmt)
        if count > self._max_telegrams:
            to_delete = count - self._max_telegrams
            # Use a subquery to find the IDs to delete (SQLite/Postgres compatible)
            # Since we don't have a PK, we use the timestamp as a proxy or just delete N oldest
            # In PostgreSQL/TimescaleDB we might want a different strategy, but for now:
            subq = (
                select(self.telegrams.c.timestamp)
                .order_by(self.telegrams.c.timestamp.asc())
                .limit(to_delete)
            )
            delete_stmt = self.telegrams.delete().where(
                self.telegrams.c.timestamp.in_(subq)
            )
            await conn.execute(delete_stmt)

    async def query(self, query: TelegramQuery) -> TelegramQueryResult:
        """Retrieve telegrams matching the given query."""
        stmt = select(self.telegrams)

        # 1. Base Filters
        filters = []
        if query.sources:
            filters.append(self.telegrams.c.source.in_(query.sources))
        if query.destinations:
            filters.append(self.telegrams.c.destination.in_(query.destinations))
        if query.telegram_types:
            filters.append(self.telegrams.c.telegramtype.in_(query.telegram_types))
        if query.directions:
            filters.append(self.telegrams.c.direction.in_(query.directions))
        if query.dpt_mains:
            filters.append(self.telegrams.c.dpt_main.in_(query.dpt_mains))

        if query.start_time:
            filters.append(self.telegrams.c.timestamp >= query.start_time)
        if query.end_time:
            filters.append(self.telegrams.c.timestamp <= query.end_time)

        # 2. Time-Delta Context Logic
        if (query.delta_before_ms > 0 or query.delta_after_ms > 0) and filters:
            # Create a subquery for the matching pivots
            pivots = select(self.telegrams.c.timestamp).where(and_(*filters)).alias("pivots")
            
            if self.engine.dialect.name == "sqlite":
                # SQLite specific date math
                before_str = f"-{query.delta_before_ms / 1000.0} seconds"
                after_str = f"+{query.delta_after_ms / 1000.0} seconds"
                
                cond = and_(
                    self.telegrams.c.timestamp >= func.datetime(pivots.c.timestamp, before_str),
                    self.telegrams.c.timestamp <= func.datetime(pivots.c.timestamp, after_str)
                )
            else:
                # Standard math (Postgres, etc.)
                delta_before = timedelta(milliseconds=query.delta_before_ms)
                delta_after = timedelta(milliseconds=query.delta_after_ms)
                cond = and_(
                    self.telegrams.c.timestamp >= pivots.c.timestamp - delta_before,
                    self.telegrams.c.timestamp <= pivots.c.timestamp + delta_after
                )

            # Use EXISTS to find rows within range of any pivot
            stmt = select(self.telegrams).where(
                select(pivots).where(cond).exists()
            )
        else:
            if filters:
                stmt = stmt.where(and_(*filters))

        # 3. Total Count (before pagination)
        count_stmt = select(func.count()).select_from(stmt.alias("unpaginated"))
        
        # 4. Ordering
        if query.order_descending:
            stmt = stmt.order_by(self.telegrams.c.timestamp.desc())
        else:
            stmt = stmt.order_by(self.telegrams.c.timestamp.asc())

        # 5. Pagination
        stmt = stmt.offset(query.offset).limit(query.limit)

        async with self.engine.connect() as conn:
            total_count = await conn.scalar(count_stmt)
            result = await conn.execute(stmt)
            rows = result.fetchall()

        telegrams = [
            StoredTelegram(
                timestamp=row.timestamp,
                source=row.source,
                destination=row.destination,
                telegramtype=row.telegramtype,
                direction=row.direction,
                payload=row.payload,
                dpt_main=row.dpt_main,
                dpt_sub=row.dpt_sub,
                dpt_name=row.dpt_name,
                unit=row.unit,
                value=row.value,
                value_numeric=row.value_numeric,
                raw_data=row.raw_data,
                data_secure=row.data_secure,
                source_name=row.source_name,
                destination_name=row.destination_name,
            )
            for row in rows
        ]

        limit_reached = total_count > (query.offset + query.limit)

        return TelegramQueryResult(
            telegrams=telegrams,
            total_count=total_count or 0,
            limit_reached=limit_reached,
        )

    async def count(self) -> int:
        """Return the total number of stored telegrams."""
        async with self.engine.connect() as conn:
            return await conn.scalar(select(func.count()).select_from(self.telegrams))

    async def clear(self) -> None:
        """Remove all stored telegrams."""
        async with self.engine.begin() as conn:
            await conn.execute(self.telegrams.delete())
