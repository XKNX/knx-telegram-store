from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from ..lookup import LookupCache, build_lookup_table
from ..model import StoredTelegram
from ..query import TelegramQuery, TelegramQueryResult
from ..store import StoreCapabilities, TelegramStore, wrap_store_errors


class BaseSQLStore(TelegramStore):
    """Base class for SQLAlchemy-based telegram stores."""

    def __init__(self, engine: AsyncEngine, retention_days: int | None = None) -> None:
        """Initialize the SQL store."""
        self.engine = engine
        self._retention_days = retention_days
        self._metadata = MetaData()
        self._lookup_cache = LookupCache()
        self.string_lookup = build_lookup_table(self._metadata)

        self.telegrams = Table(
            "telegrams",
            self._metadata,
            Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
            Column("source_id", Integer, nullable=False),
            Column("destination_id", Integer, nullable=False, index=True),
            Column("telegramtype_id", Integer, nullable=False),
            Column("direction_id", Integer, nullable=False),
            Column("source_name_id", Integer, nullable=True),
            Column("destination_name_id", Integer, nullable=True),
            Column("payload", JSON, nullable=True),
            Column("dpt_main", Integer, nullable=True),
            Column("dpt_sub", Integer, nullable=True),
            Column("value", JSON, nullable=True),
            Column("value_numeric", Double, nullable=True),
            Column("raw_data", Text, nullable=True),  # Hex encoded string
            Column("data_secure", Boolean, nullable=True),
        )
        self.last_ga_telegrams = Table(
            "last_ga_telegrams",
            self._metadata,
            Column("destination_id", Integer, primary_key=True),
            Column("timestamp", DateTime(timezone=True), nullable=False),
            Column("source_id", Integer, nullable=False),
            Column("telegramtype_id", Integer, nullable=False),
            Column("direction_id", Integer, nullable=False),
            Column("source_name_id", Integer, nullable=True),
            Column("destination_name_id", Integer, nullable=True),
            Column("payload", JSON, nullable=True),
            Column("dpt_main", Integer, nullable=True),
            Column("dpt_sub", Integer, nullable=True),
            Column("value", JSON, nullable=True),
            Column("value_numeric", Double, nullable=True),
            Column("raw_data", Text, nullable=True),
            Column("data_secure", Boolean, nullable=True),
        )

        self._capabilities = StoreCapabilities(
            supports_time_range=True,
            supports_time_delta=True,
            supports_pagination=True,
            supports_count=True,
            max_storage=None,
        )

    @property
    def capabilities(self) -> StoreCapabilities:
        """Return the capabilities of this backend."""
        return self._capabilities

    @property
    def retention_days(self) -> int | None:
        """Return the configured retention period in days."""
        return self._retention_days

    @property
    def max_telegrams(self) -> int | None:
        """SQL stores are typically not limited by count."""
        return None

    @wrap_store_errors
    async def initialize(self) -> None:
        """Set up the store (create tables, upgrades)."""
        # Subclasses should call this or implement their own with super().initialize()
        await self._lookup_cache.warm(self.engine, self.string_lookup)
        await self._populate_last_ga_telegrams_if_empty()

    @wrap_store_errors
    async def close(self) -> None:
        """Close the engine."""
        await self.engine.dispose()

    @wrap_store_errors
    async def store(self, telegram: StoredTelegram) -> None:
        """Persist a single telegram."""
        await self.store_many([telegram])

    @wrap_store_errors
    async def store_many(self, telegrams: Sequence[StoredTelegram]) -> None:
        """Persist multiple telegrams."""
        if not telegrams:
            return

        # 1. Resolve lookup IDs
        pairs: set[tuple[str, str]] = set()
        for t in telegrams:
            pairs.add(("source", t.source))
            pairs.add(("destination", t.destination))
            pairs.add(("telegramtype", t.telegramtype))
            pairs.add(("direction", t.direction))
            pairs.add(("source_name", t.source_name))
            pairs.add(("destination_name", t.destination_name))

        async with self.engine.begin() as conn:
            lookup_ids = await self._lookup_cache.get_or_create_ids(conn, self.string_lookup, pairs)

            values = []
            for t in telegrams:
                values.append(
                    {
                        "timestamp": t.timestamp,
                        "source_id": lookup_ids[("source", t.source)],
                        "destination_id": lookup_ids[("destination", t.destination)],
                        "telegramtype_id": lookup_ids[("telegramtype", t.telegramtype)],
                        "direction_id": lookup_ids[("direction", t.direction)],
                        "source_name_id": lookup_ids[("source_name", t.source_name)],
                        "destination_name_id": lookup_ids[("destination_name", t.destination_name)],
                        "payload": t.payload,
                        "dpt_main": t.dpt_main,
                        "dpt_sub": t.dpt_sub,
                        "value": t.value,
                        "value_numeric": t.value_numeric,
                        "raw_data": t.raw_data,
                        "data_secure": t.data_secure,
                    }
                )

            await conn.execute(self.telegrams.insert(), values)

            # 2. Update last_ga_telegrams with the latest telegram for each unique destination_id in the batch
            from typing import cast

            latest_per_destination: dict[int, dict[str, Any]] = {}
            for val in values:
                dest_id: int = cast(int, val["destination_id"])
                if (
                    dest_id not in latest_per_destination
                    or val["timestamp"] > latest_per_destination[dest_id]["timestamp"]
                ):
                    latest_per_destination[dest_id] = val

            upsert_values = list(latest_per_destination.values())
            if upsert_values:
                if self.engine.dialect.name == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    sqlite_stmt = sqlite_insert(self.last_ga_telegrams).values(upsert_values)
                    sqlite_upsert = sqlite_stmt.on_conflict_do_update(
                        index_elements=["destination_id"],
                        set_={col: sqlite_stmt.excluded[col] for col in upsert_values[0] if col != "destination_id"},
                    )
                    await conn.execute(sqlite_upsert)
                else:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    pg_stmt = pg_insert(self.last_ga_telegrams).values(upsert_values)
                    pg_upsert = pg_stmt.on_conflict_do_update(
                        index_elements=["destination_id"],
                        set_={col: pg_stmt.excluded[col] for col in upsert_values[0] if col != "destination_id"},
                    )
                    await conn.execute(pg_upsert)

    @wrap_store_errors
    async def evict_older_than(self, cutoff: datetime, *, dry_run: bool = False) -> int:
        """Delete all telegrams with timestamp < cutoff."""
        if dry_run:
            stmt = select(func.count()).select_from(self.telegrams).where(self.telegrams.c.timestamp < cutoff)
            async with self.engine.connect() as conn:
                return await conn.scalar(stmt) or 0

        delete_stmt = self.telegrams.delete().where(self.telegrams.c.timestamp < cutoff)
        async with self.engine.begin() as conn:
            result = await conn.execute(delete_stmt)
            return result.rowcount

    @wrap_store_errors
    async def evict_expired(self, *, dry_run: bool = False) -> int:
        """Delete telegrams older than the configured retention period."""
        if self._retention_days is None:
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        return await self.evict_older_than(cutoff, dry_run=dry_run)

    @wrap_store_errors
    async def query(self, query: TelegramQuery) -> TelegramQueryResult:
        """Retrieve telegrams matching the given query."""
        # Aliases for lookup JOINs
        s_lk = self.string_lookup.alias("s_lk")
        d_lk = self.string_lookup.alias("d_lk")
        tt_lk = self.string_lookup.alias("tt_lk")
        dir_lk = self.string_lookup.alias("dir_lk")
        sn_lk = self.string_lookup.alias("sn_lk")
        den_lk = self.string_lookup.alias("den_lk")

        stmt = select(
            self.telegrams.c.timestamp,
            s_lk.c.value.label("source"),
            d_lk.c.value.label("destination"),
            tt_lk.c.value.label("telegramtype"),
            dir_lk.c.value.label("direction"),
            sn_lk.c.value.label("source_name"),
            den_lk.c.value.label("destination_name"),
            self.telegrams.c.payload,
            self.telegrams.c.dpt_main,
            self.telegrams.c.dpt_sub,
            self.telegrams.c.value,
            self.telegrams.c.value_numeric,
            self.telegrams.c.raw_data,
            self.telegrams.c.data_secure,
        )

        # Joins to lookup table
        stmt = stmt.join(s_lk, and_(s_lk.c.id == self.telegrams.c.source_id, s_lk.c.category == "source"))
        stmt = stmt.join(d_lk, and_(d_lk.c.id == self.telegrams.c.destination_id, d_lk.c.category == "destination"))
        stmt = stmt.join(
            tt_lk, and_(tt_lk.c.id == self.telegrams.c.telegramtype_id, tt_lk.c.category == "telegramtype")
        )
        stmt = stmt.join(dir_lk, and_(dir_lk.c.id == self.telegrams.c.direction_id, dir_lk.c.category == "direction"))
        stmt = stmt.outerjoin(
            sn_lk, and_(sn_lk.c.id == self.telegrams.c.source_name_id, sn_lk.c.category == "source_name")
        )
        stmt = stmt.outerjoin(
            den_lk, and_(den_lk.c.id == self.telegrams.c.destination_name_id, den_lk.c.category == "destination_name")
        )

        # 1. Base Filters
        filters: list[Any] = []
        if query.sources:
            # Subquery to get IDs for sources
            source_ids = select(self.string_lookup.c.id).where(
                self.string_lookup.c.category == "source", self.string_lookup.c.value.in_(query.sources)
            )
            filters.append(self.telegrams.c.source_id.in_(source_ids))
        if query.destinations:
            dest_ids = select(self.string_lookup.c.id).where(
                self.string_lookup.c.category == "destination", self.string_lookup.c.value.in_(query.destinations)
            )
            filters.append(self.telegrams.c.destination_id.in_(dest_ids))
        if query.telegram_types:
            tt_ids = select(self.string_lookup.c.id).where(
                self.string_lookup.c.category == "telegramtype", self.string_lookup.c.value.in_(query.telegram_types)
            )
            filters.append(self.telegrams.c.telegramtype_id.in_(tt_ids))
        if query.directions:
            dir_ids = select(self.string_lookup.c.id).where(
                self.string_lookup.c.category == "direction", self.string_lookup.c.value.in_(query.directions)
            )
            filters.append(self.telegrams.c.direction_id.in_(dir_ids))
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
                    self.telegrams.c.timestamp <= func.datetime(pivots.c.timestamp, after_str),
                )
            else:
                # Standard math (Postgres, etc.)
                delta_before = timedelta(milliseconds=query.delta_before_ms)
                delta_after = timedelta(milliseconds=query.delta_after_ms)
                cond = and_(
                    self.telegrams.c.timestamp >= pivots.c.timestamp - delta_before,
                    self.telegrams.c.timestamp <= pivots.c.timestamp + delta_after,
                )

            # Use EXISTS to find rows within range of any pivot
            stmt = stmt.where(select(pivots).where(cond).exists())
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
                value=row.value,
                value_numeric=row.value_numeric,
                raw_data=row.raw_data,
                data_secure=row.data_secure,
                source_name=row.source_name or "",
                destination_name=row.destination_name or "",
            )
            for row in rows
        ]

        limit_reached = (total_count or 0) > (query.offset + query.limit)

        return TelegramQueryResult(
            telegrams=telegrams,
            total_count=total_count or 0,
            limit_reached=limit_reached,
        )

    @wrap_store_errors
    async def count(self) -> int:
        """Return the total number of stored telegrams."""
        async with self.engine.connect() as conn:
            return await conn.scalar(select(func.count()).select_from(self.telegrams)) or 0

    @wrap_store_errors
    async def clear(self) -> None:
        """Remove all stored telegrams."""
        async with self.engine.begin() as conn:
            await conn.execute(self.telegrams.delete())
            await conn.execute(self.last_ga_telegrams.delete())

    @wrap_store_errors
    async def get_last_unique_telegrams(self) -> list[StoredTelegram]:
        """Retrieve the latest unique telegram for each destination group address."""
        s_lk = self.string_lookup.alias("s_lk")
        d_lk = self.string_lookup.alias("d_lk")
        tt_lk = self.string_lookup.alias("tt_lk")
        dir_lk = self.string_lookup.alias("dir_lk")
        sn_lk = self.string_lookup.alias("sn_lk")
        den_lk = self.string_lookup.alias("den_lk")

        stmt = select(
            self.last_ga_telegrams.c.timestamp,
            s_lk.c.value.label("source"),
            d_lk.c.value.label("destination"),
            tt_lk.c.value.label("telegramtype"),
            dir_lk.c.value.label("direction"),
            sn_lk.c.value.label("source_name"),
            den_lk.c.value.label("destination_name"),
            self.last_ga_telegrams.c.payload,
            self.last_ga_telegrams.c.dpt_main,
            self.last_ga_telegrams.c.dpt_sub,
            self.last_ga_telegrams.c.value,
            self.last_ga_telegrams.c.value_numeric,
            self.last_ga_telegrams.c.raw_data,
            self.last_ga_telegrams.c.data_secure,
        )

        # Joins to lookup table
        stmt = stmt.join(s_lk, and_(s_lk.c.id == self.last_ga_telegrams.c.source_id, s_lk.c.category == "source"))
        stmt = stmt.join(
            d_lk, and_(d_lk.c.id == self.last_ga_telegrams.c.destination_id, d_lk.c.category == "destination")
        )
        stmt = stmt.join(
            tt_lk, and_(tt_lk.c.id == self.last_ga_telegrams.c.telegramtype_id, tt_lk.c.category == "telegramtype")
        )
        stmt = stmt.join(
            dir_lk, and_(dir_lk.c.id == self.last_ga_telegrams.c.direction_id, dir_lk.c.category == "direction")
        )
        stmt = stmt.outerjoin(
            sn_lk, and_(sn_lk.c.id == self.last_ga_telegrams.c.source_name_id, sn_lk.c.category == "source_name")
        )
        stmt = stmt.outerjoin(
            den_lk,
            and_(den_lk.c.id == self.last_ga_telegrams.c.destination_name_id, den_lk.c.category == "destination_name"),
        )

        async with self.engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = result.fetchall()

        return [
            StoredTelegram(
                timestamp=row.timestamp,
                source=row.source,
                destination=row.destination,
                telegramtype=row.telegramtype,
                direction=row.direction,
                payload=row.payload,
                dpt_main=row.dpt_main,
                dpt_sub=row.dpt_sub,
                value=row.value,
                value_numeric=row.value_numeric,
                raw_data=row.raw_data,
                data_secure=row.data_secure,
                source_name=row.source_name or "",
                destination_name=row.destination_name or "",
            )
            for row in rows
        ]

    async def _populate_last_ga_telegrams_if_empty(self) -> None:
        """Populate the last_ga_telegrams table from telegrams if it is empty."""
        async with self.engine.begin() as conn:
            count = await conn.scalar(select(func.count()).select_from(self.last_ga_telegrams))
            if count == 0:
                t2 = self.telegrams.alias("t2")
                subq = (
                    select(func.max(t2.c.timestamp))
                    .where(t2.c.destination_id == self.telegrams.c.destination_id)
                    .scalar_subquery()
                )

                select_stmt = select(
                    self.telegrams.c.destination_id,
                    self.telegrams.c.timestamp,
                    self.telegrams.c.source_id,
                    self.telegrams.c.telegramtype_id,
                    self.telegrams.c.direction_id,
                    self.telegrams.c.source_name_id,
                    self.telegrams.c.destination_name_id,
                    self.telegrams.c.payload,
                    self.telegrams.c.dpt_main,
                    self.telegrams.c.dpt_sub,
                    self.telegrams.c.value,
                    self.telegrams.c.value_numeric,
                    self.telegrams.c.raw_data,
                    self.telegrams.c.data_secure,
                ).where(self.telegrams.c.timestamp == subq)

                if self.engine.dialect.name == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    sqlite_stmt = (
                        sqlite_insert(self.last_ga_telegrams)
                        .from_select([c.name for c in select_stmt.selected_columns], select_stmt)
                        .on_conflict_do_nothing()
                    )
                    await conn.execute(sqlite_stmt)
                else:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    pg_stmt = (
                        pg_insert(self.last_ga_telegrams)
                        .from_select([c.name for c in select_stmt.selected_columns], select_stmt)
                        .on_conflict_do_nothing()
                    )
                    await conn.execute(pg_stmt)
