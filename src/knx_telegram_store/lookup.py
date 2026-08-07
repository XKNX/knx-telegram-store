from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    or_,
    select,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_LOGGER = logging.getLogger(__name__)

LOOKUP_CATEGORIES = [
    "source",
    "destination",
    "telegramtype",
    "direction",
    "source_name",
    "destination_name",
]


def build_lookup_table(metadata: MetaData) -> Table:
    """Build the string_lookup table definition."""
    return Table(
        "string_lookup",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("category", String(30), nullable=False),
        Column("value", Text, nullable=False),
        UniqueConstraint("category", "value", name="uq_category_value"),
    )


class LookupCache:
    """In-memory cache for string_lookup IDs."""

    def __init__(self) -> None:
        """Initialize the cache."""
        # (category, value) -> id
        self._cache: dict[tuple[str, str], int] = {}
        self._initialized = False

    async def warm(self, engine: AsyncEngine, table: Table) -> None:
        """Pre-populate the cache from the database."""
        async with engine.connect() as conn:
            result = await conn.execute(select(table.c.category, table.c.value, table.c.id))
            for cat, val, row_id in result:
                self._cache[(cat, val)] = row_id
        self._initialized = True
        _LOGGER.debug("LookupCache warmed with %d entries", len(self._cache))

    async def get_or_create_ids(
        self, conn: AsyncConnection, table: Table, pairs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
        """Resolve (category, value) pairs to IDs, creating missing ones."""
        resolved: dict[tuple[str, str], int] = {}
        to_resolve: list[tuple[str, str]] = []

        for pair in pairs:
            if pair in self._cache:
                resolved[pair] = self._cache[pair]
            else:
                to_resolve.append(pair)

        if not to_resolve:
            return resolved

        # Dialect-specific batch upsert
        if conn.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            for cat, val in to_resolve:
                stmt = pg_insert(table).values(category=cat, value=val).on_conflict_do_nothing()
                await conn.execute(stmt)
        elif conn.dialect.name == "sqlite":
            # SQLite supports INSERT OR IGNORE
            for cat, val in to_resolve:
                await conn.execute(insert(table).values(category=cat, value=val).prefix_with("OR IGNORE"))
        else:
            # Fallback (batch to avoid N+1 and parameter limits)
            # We use dict.fromkeys to preserve order and remove duplicates
            unique_to_resolve = list(dict.fromkeys(to_resolve))
            batch_size = 100
            for i in range(0, len(unique_to_resolve), batch_size):
                batch = unique_to_resolve[i : i + batch_size]
                conds = [(table.c.category == cat) & (table.c.value == val) for cat, val in batch]

                existing_result = await conn.execute(
                    select(table.c.category, table.c.value).where(or_(*conds))
                )
                existing_set = {(row[0], row[1]) for row in existing_result}

                missing = [
                    {"category": cat, "value": val}
                    for cat, val in batch
                    if (cat, val) not in existing_set
                ]
                if missing:
                    await conn.execute(insert(table).values(missing))

        # Re-fetch the IDs for the ones we didn't have in cache
        # We fetch in batches to avoid N+1 queries.
        unique_to_resolve = list(dict.fromkeys(to_resolve))
        # Use a larger batch size (e.g. 500) since we only have up to 1000 items usually
        batch_size = 400
        for i in range(0, len(unique_to_resolve), batch_size):
            batch = unique_to_resolve[i : i + batch_size]
            conds = [(table.c.category == cat) & (table.c.value == val) for cat, val in batch]

            result = await conn.execute(
                select(table.c.category, table.c.value, table.c.id).where(or_(*conds))
            )
            for cat, val, row_id in result:
                pair = (cat, val)
                self._cache[pair] = row_id

        for pair in to_resolve:
            if pair in self._cache:
                resolved[pair] = self._cache[pair]

        return resolved
