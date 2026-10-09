"""SQLite query planner statistics.

Without ``sqlite_stat1`` the planner guesses, and the same filter gets a good
plan on one database and a several times slower one on another
(XKNX/knx-telegram-store#78). initialize() runs ANALYZE when statistics are
missing or stale.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from knx_telegram_store import StoredTelegram
from knx_telegram_store.backends.sqlite import SqliteStore


def _telegram(destination: str, index: int = 0) -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime(2026, 7, 15, 12, 0, tzinfo=UTC) + timedelta(seconds=index),
        source="1.1.1",
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
    )


async def _database(path: Path, rows: int) -> None:
    store = SqliteStore(str(path))
    await store.initialize()
    await store.store_many([_telegram(f"1/1/{i}", i) for i in range(rows)])
    await store.close()


def _forget_statistics(path: Path) -> None:
    """Make the file look like one written by a version that never analysed."""
    con = sqlite3.connect(str(path))
    con.execute("DELETE FROM sqlite_stat1")
    con.commit()
    con.close()


def _timestamp_stat(path: Path) -> str | None:
    con = sqlite3.connect(str(path))
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone():
            return None
        row = con.execute(
            "SELECT stat FROM sqlite_stat1 WHERE tbl = 'telegrams' AND idx = 'ix_telegrams_timestamp'"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _set_timestamp_stat(path: Path, stat: str) -> None:
    con = sqlite3.connect(str(path))
    con.execute("ANALYZE")
    con.execute("UPDATE sqlite_stat1 SET stat = ? WHERE tbl = 'telegrams' AND idx = 'ix_telegrams_timestamp'", (stat,))
    con.commit()
    con.close()


def _capture_sql(store: SqliteStore) -> list[str]:
    statements: list[str] = []
    event.listen(
        store.engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement),
    )
    return statements


async def test_close_collects_statistics(tmp_path: Path) -> None:
    """PRAGMA optimize on close analyses a table this connection used without statistics."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    assert _timestamp_stat(path) == "3 1"


async def test_unanalysed_file_is_analysed_on_start(tmp_path: Path) -> None:
    """A file written by an earlier version has no statistics until the next start."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _forget_statistics(path)
    assert _timestamp_stat(path) is None

    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()
    assert _timestamp_stat(path) == "3 1"


async def test_small_table_is_analysed_but_not_reported(tmp_path: Path) -> None:
    """ANALYZE on a few thousand rows is instant, not worth a host warning."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _forget_statistics(path)

    store = SqliteStore(str(path))
    assert await store.needs_migration() is False
    await store.initialize()
    await store.close()
    assert _timestamp_stat(path) == "3 1"


async def test_large_table_is_reported_until_analysed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _forget_statistics(path)
    monkeypatch.setattr(SqliteStore, "_STATISTICS_REPORT_ROWS", 2)

    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    await store.close()


@pytest.mark.parametrize(
    ("recorded", "refreshed"),
    [
        pytest.param("100000 1", True, id="shrunk-tenfold"),
        pytest.param("1 1", False, id="grown-threefold"),
        pytest.param("3 1", False, id="exact"),
    ],
)
async def test_stale_statistics_are_refreshed(tmp_path: Path, recorded: str, refreshed: bool) -> None:
    """A tenfold change in row count since the last ANALYZE triggers another one."""
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _set_timestamp_stat(path, recorded)

    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()
    assert _timestamp_stat(path) == ("3 1" if refreshed else recorded)


async def test_read_only_store_never_analyses(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    await _database(path, rows=3)
    _forget_statistics(path)

    store = SqliteStore(str(path), read_only=True)
    statements = _capture_sql(store)
    await store.initialize()
    await store.close()
    assert _timestamp_stat(path) is None
    assert not [s for s in statements if "ANALYZE" in s or "optimize" in s]


async def test_close_runs_pragma_optimize(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = SqliteStore(str(path))
    statements = _capture_sql(store)
    await store.initialize()
    await store.close()
    assert "PRAGMA optimize" in statements
