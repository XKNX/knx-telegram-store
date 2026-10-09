"""needs_migration() reports the one-off passes initialize() runs on first start.

Hosts use needs_migration() to decide whether initialize() may run under a
timeout - Home Assistant allows 10 s otherwise - so a pass that scales with the
table but is not a schema change has to be reported too, or it is cancelled on
every attempt on a large database (XKNX/knx-telegram-store#76).
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from knx_telegram_store import StoredTelegram
from knx_telegram_store.backends.sqlite import SqliteStore

BERLIN = ZoneInfo("Europe/Berlin")


def _telegram(destination: str) -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        source="1.1.1",
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
        payload=(1,),
        value=1,
        value_numeric=1.0,
    )


async def _database(path: Path, *, rows: int) -> None:
    """Create an up-to-date database, holding ``rows`` telegrams."""
    store = SqliteStore(str(path))
    await store.initialize()
    if rows:
        await store.store_many([_telegram(f"1/1/{i}") for i in range(rows)])
    await store.close()


def _sql(path: Path, *statements: str) -> None:
    con = sqlite3.connect(str(path))
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


def _index_names(path: Path) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()


async def test_up_to_date_database_reports_nothing(tmp_path):
    """The common case: every later start must stay on the host's fast path."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    assert await store.needs_migration() is False
    await store.close()


async def test_empty_database_reports_nothing_even_without_flags(tmp_path):
    """Passes over an empty table are instant, so they are not worth a warning."""
    path = tmp_path / "t.db"
    await _database(path, rows=0)
    _sql(
        path,
        "DELETE FROM store_metadata WHERE key IN ('timestamps_utc', 'timestamps_integer', 'last_ga_newest_reconciled')",
    )

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    assert await store.needs_migration() is False
    await store.close()


async def test_pending_integer_timestamp_conversion_is_reported(tmp_path):
    """Rewriting the timestamps as integers touches every row (XKNX/knx-telegram-store#78)."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _sql(path, "DELETE FROM store_metadata WHERE key = 'timestamps_integer'")

    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    await store.close()


async def test_pending_utc_conversion_is_reported_only_when_the_zone_is_given(tmp_path):
    """Without the zone initialize() skips the conversion, so there is nothing to wait for."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _sql(path, "DELETE FROM store_metadata WHERE key = 'timestamps_utc'")

    offered_only = SqliteStore(str(path))
    assert await offered_only.needs_migration() is False
    await offered_only.close()

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    await store.close()


async def test_missing_declared_index_is_reported(tmp_path):
    """An index added to the model after the database was created is built by initialize()."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _sql(path, "DROP INDEX ix_telegrams_source_id")
    assert "ix_telegrams_source_id" not in _index_names(path)

    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    await store.close()
    assert "ix_telegrams_source_id" in _index_names(path)


async def test_pending_last_value_reconcile_is_reported(tmp_path):
    """The one-time last-value reconcile groups the whole table."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _sql(path, "DELETE FROM store_metadata WHERE key = 'last_ga_newest_reconciled'")

    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    await store.close()


async def test_missing_last_value_summaries_are_reported(tmp_path):
    """Summaries wiped after the flag was set are rebuilt from the whole table as well."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _sql(path, "DELETE FROM last_ga_telegrams")

    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    await store.close()
