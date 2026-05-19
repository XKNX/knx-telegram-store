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
            # Fallback
            for cat, val in to_resolve:
                existing = await conn.scalar(select(table.c.id).where(table.c.category == cat, table.c.value == val))
                if existing is None:
                    await conn.execute(insert(table).values(category=cat, value=val))

        # Re-fetch the IDs for the ones we didn't have in cache
        # We fetch one by one to keep it simple and robust across dialects for now,
        # since to_resolve is usually small per batch.
        for pair in to_resolve:
            cat, val = pair
            row_id = await conn.scalar(select(table.c.id).where(table.c.category == cat, table.c.value == val))
            if row_id is not None:
                self._cache[pair] = row_id
                resolved[pair] = row_id

        return resolved
