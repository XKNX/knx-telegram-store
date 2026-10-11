"""Indexes the model dropped are removed from existing databases.

``ix_telegrams_telegramtype_id`` and ``ix_telegrams_direction_id`` index
columns with two or three distinct values, which never narrows a search, yet
each cost about 12 bytes per row on SQLite (XKNX/knx-telegram-store#78).
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore

OBSOLETE = {"ix_telegrams_telegramtype_id", "ix_telegrams_direction_id"}


def _index_names(path: Path) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()


def _telegram(direction: str, telegramtype: str) -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        source="1.1.1",
        destination="1/1/1",
        telegramtype=telegramtype,
        direction=direction,
    )


async def test_fresh_database_has_no_low_cardinality_indexes(tmp_path: Path) -> None:
    path = tmp_path / "t.db"
    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()

    names = _index_names(path)
    assert not names & OBSOLETE
    assert "ix_telegrams_timestamp" in names


async def test_existing_database_loses_them_on_start(tmp_path: Path) -> None:
    """A file written by an earlier version still carries them; initialize() drops them."""
    path = tmp_path / "t.db"
    store = SqliteStore(str(path))
    await store.initialize()
    await store.store_many([_telegram("Incoming", "GroupValueWrite"), _telegram("Outgoing", "GroupValueRead")])
    await store.close()

    con = sqlite3.connect(str(path))
    con.execute("CREATE INDEX ix_telegrams_telegramtype_id ON telegrams (telegramtype_id)")
    con.execute("CREATE INDEX ix_telegrams_direction_id ON telegrams (direction_id)")
    con.commit()
    con.close()
    assert OBSOLETE <= _index_names(path)

    store = SqliteStore(str(path))
    # Dropping an index is instant, so it is not a pass worth a host warning.
    assert await store.needs_migration() is False
    await store.initialize()
    assert not _index_names(path) & OBSOLETE

    result = await store.query(TelegramQuery(directions=["Outgoing"], telegram_types=["GroupValueRead"]))
    assert [t.direction for t in result.telegrams] == ["Outgoing"]
    await store.close()


async def test_read_only_store_leaves_them_alone(tmp_path: Path) -> None:
    """A read-only store never runs DDL, so a foreign file keeps whatever it has."""
    path = tmp_path / "foreign.db"
    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()
    con = sqlite3.connect(str(path))
    con.execute("CREATE INDEX ix_telegrams_direction_id ON telegrams (direction_id)")
    con.commit()
    con.close()

    reader = SqliteStore(str(path), read_only=True)
    await reader.initialize()
    await reader.close()
    assert "ix_telegrams_direction_id" in _index_names(path)
