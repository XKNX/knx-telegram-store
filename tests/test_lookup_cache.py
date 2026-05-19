"""Unit tests for LookupCache in lookup.py.

Coverage targets (missing lines from coverage report):
  - Line 80:  early return when all pairs are already cached
  - Lines 84-88: PostgreSQL dialect upsert path (on_conflict_do_nothing)
  - Lines 95-98: generic fallback path (SELECT then INSERT)
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, insert
from sqlalchemy.ext.asyncio import create_async_engine

from knx_telegram_store.lookup import LookupCache, build_lookup_table

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    yield engine
    await engine.dispose()


@pytest.fixture
async def lookup_table(sqlite_engine):
    metadata = MetaData()
    table = build_lookup_table(metadata)
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return table


# ---------------------------------------------------------------------------
# warm()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_populates_cache(sqlite_engine, lookup_table):
    """warm() should pre-load all existing rows into the cache."""
    # Seed the DB directly
    async with sqlite_engine.begin() as conn:
        await conn.execute(insert(lookup_table).values(category="source", value="1.1.1"))
        await conn.execute(insert(lookup_table).values(category="destination", value="1/1/1"))

    cache = LookupCache()
    await cache.warm(sqlite_engine, lookup_table)

    assert ("source", "1.1.1") in cache._cache
    assert ("destination", "1/1/1") in cache._cache


# ---------------------------------------------------------------------------
# get_or_create_ids() — cache-hit fast path (line 80)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_pairs_cached_no_db_queries(sqlite_engine, lookup_table):
    """If every pair is already in cache, the DB is never queried (line 80 early return)."""
    cache = LookupCache()
    # Manually prime the cache — no DB row needed
    cache._cache[("source", "1.1.1")] = 42
    cache._cache[("destination", "1/1/1")] = 99

    async with sqlite_engine.begin() as conn:
        result = await cache.get_or_create_ids(conn, lookup_table, [("source", "1.1.1"), ("destination", "1/1/1")])

    assert result == {("source", "1.1.1"): 42, ("destination", "1/1/1"): 99}


@pytest.mark.asyncio
async def test_partial_cache_hit(sqlite_engine, lookup_table):
    """Cached pairs are resolved immediately; only uncached ones go to the DB."""
    cache = LookupCache()
    cache._cache[("source", "1.1.1")] = 7

    async with sqlite_engine.begin() as conn:
        result = await cache.get_or_create_ids(conn, lookup_table, [("source", "1.1.1"), ("destination", "1/1/1")])

    assert result[("source", "1.1.1")] == 7
    # The new pair should have been inserted and cached
    assert ("destination", "1/1/1") in result
    assert ("destination", "1/1/1") in cache._cache


# ---------------------------------------------------------------------------
# get_or_create_ids() — SQLite dialect (INSERT OR IGNORE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_insert_new_pairs(sqlite_engine, lookup_table):
    """New pairs are inserted and their IDs returned (SQLite path)."""
    cache = LookupCache()

    async with sqlite_engine.begin() as conn:
        result = await cache.get_or_create_ids(conn, lookup_table, [("source", "2.2.2"), ("direction", "Incoming")])

    assert len(result) == 2
    assert all(isinstance(v, int) for v in result.values())
    # IDs are now in the cache
    assert ("source", "2.2.2") in cache._cache
    assert ("direction", "Incoming") in cache._cache


@pytest.mark.asyncio
async def test_sqlite_idempotent_on_duplicate(sqlite_engine, lookup_table):
    """Inserting the same pair twice should not raise and should return the same ID."""
    cache = LookupCache()

    async with sqlite_engine.begin() as conn:
        r1 = await cache.get_or_create_ids(conn, lookup_table, [("source", "3.3.3")])

    cache2 = LookupCache()  # fresh cache — forces another DB round-trip
    async with sqlite_engine.begin() as conn:
        r2 = await cache2.get_or_create_ids(conn, lookup_table, [("source", "3.3.3")])

    assert r1[("source", "3.3.3")] == r2[("source", "3.3.3")]


# ---------------------------------------------------------------------------
# get_or_create_ids() — generic fallback path (lines 95-98)
# We exercise this by monkey-patching conn.dialect.name to an unknown dialect.
# ---------------------------------------------------------------------------


class _FakeDialect:
    name = "mssql"  # anything other than postgresql/sqlite


class _FakeConn:
    """Minimal async connection shim that delegates to a real SQLite connection
    but reports a fake dialect, forcing the generic fallback branch."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.dialect = _FakeDialect()

    async def execute(self, *args, **kwargs):
        return await self._real.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return await self._real.scalar(*args, **kwargs)


@pytest.mark.asyncio
async def test_generic_fallback_insert(sqlite_engine, lookup_table):
    """Lines 95-98: generic SELECT-then-INSERT fallback for unknown dialects."""
    cache = LookupCache()

    async with sqlite_engine.begin() as real_conn:
        fake_conn = _FakeConn(real_conn)
        result = await cache.get_or_create_ids(
            fake_conn,  # type: ignore[arg-type]
            lookup_table,
            [("telegramtype", "GroupValueWrite")],
        )

    assert ("telegramtype", "GroupValueWrite") in result
    assert isinstance(result[("telegramtype", "GroupValueWrite")], int)
    assert ("telegramtype", "GroupValueWrite") in cache._cache


@pytest.mark.asyncio
async def test_generic_fallback_existing_row(sqlite_engine, lookup_table):
    """Lines 95-98: if the row already exists the SELECT returns it (no INSERT)."""
    # Pre-insert the row via the real path
    cache = LookupCache()
    async with sqlite_engine.begin() as conn:
        await cache.get_or_create_ids(conn, lookup_table, [("unit", "°C")])

    existing_id = cache._cache[("unit", "°C")]

    # Fresh cache, fake dialect — should SELECT and find the existing row
    cache2 = LookupCache()
    async with sqlite_engine.begin() as real_conn:
        fake_conn = _FakeConn(real_conn)
        result = await cache2.get_or_create_ids(
            fake_conn,  # type: ignore[arg-type]
            lookup_table,
            [("unit", "°C")],
        )

    assert result[("unit", "°C")] == existing_id
