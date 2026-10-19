"""The startup passes that scale with the file check free disk space first (#91)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_integer_timestamps import TEXT_STAMPS, _flag_set, _pre_0_15_db, _raw

from knx_telegram_store import InsufficientDiskSpaceError, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore

STAMPS = [f"2026-01-15 12:00:{second:02d}.000000" for second in range(5)]


def _types(path: Path, table: str) -> list[str]:
    return sorted(row[0] for row in _raw(path, f"SELECT DISTINCT typeof(timestamp) FROM {table}"))  # noqa: S608


def _index_exists(path: Path) -> bool:
    return _raw(path, "SELECT count(*) FROM sqlite_master WHERE name = 'ix_telegrams_timestamp'") == [(1,)]


def _fake_free_space(monkeypatch, free: int) -> None:
    monkeypatch.setattr(SqliteStore, "_free_space", lambda self: free)


def _count_vacuums(monkeypatch) -> list[str]:
    vacuums: list[str] = []
    original_optimize = SqliteStore.optimize

    async def counting_optimize(self: SqliteStore) -> None:
        vacuums.append("VACUUM")
        await original_optimize(self)

    monkeypatch.setattr(SqliteStore, "optimize", counting_optimize)
    return vacuums


async def test_conversion_commits_per_batch_and_resumes(tmp_path: Path, monkeypatch) -> None:
    """A start cut off mid-conversion keeps the batches it committed and finishes later."""
    path = tmp_path / "old.db"
    await _pre_0_15_db(path, STAMPS, [STAMPS[0]])
    monkeypatch.setattr(SqliteStore, "_CONVERSION_BATCH_ROWS", 2)
    original_batch = SqliteStore._convert_rows_to_integers
    batches: list[tuple[str, int, int]] = []

    async def cut_off_after_first_batch(self: SqliteStore, table: str, low: int, high: int) -> None:
        if batches:
            raise RuntimeError("cut off")
        batches.append((table, low, high))
        await original_batch(self, table, low, high)

    monkeypatch.setattr(SqliteStore, "_convert_rows_to_integers", cut_off_after_first_batch)
    store = SqliteStore(str(path))
    with pytest.raises(RuntimeError, match="cut off"):
        await store.initialize()
    await store.close()

    assert batches == [("telegrams", 0, 2)]
    assert _raw(path, "SELECT typeof(timestamp) FROM telegrams ORDER BY rowid") == [
        ("integer",),
        ("integer",),
        ("text",),
        ("text",),
        ("text",),
    ]
    assert not _flag_set(path, "timestamps_integer")
    assert not _index_exists(path)

    monkeypatch.setattr(SqliteStore, "_convert_rows_to_integers", original_batch)
    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    stamps = [
        t.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")
        for t in (await store.query(TelegramQuery(limit=10, order_descending=False))).telegrams
    ]
    assert stamps == STAMPS
    await store.close()

    assert _types(path, "telegrams") == ["integer"]
    assert _types(path, "last_ga_telegrams") == ["integer"]
    assert _flag_set(path, "timestamps_integer")
    assert _index_exists(path)


async def test_conversion_refuses_when_the_disk_is_short(tmp_path: Path, monkeypatch) -> None:
    """Without room for the index rebuild nothing is touched, and the pass stays pending."""
    path = tmp_path / "old.db"
    await _pre_0_15_db(path, TEXT_STAMPS, [TEXT_STAMPS[1]])
    _fake_free_space(monkeypatch, 0)
    vacuums = _count_vacuums(monkeypatch)

    store = SqliteStore(str(path))
    with pytest.raises(InsufficientDiskSpaceError, match="timestamp conversion") as err:
        await store.initialize()
    assert err.value.available == 0
    assert err.value.required == store._file_size() // 2 > 0
    assert await store.needs_migration() is True
    await store.close()

    assert vacuums == []
    assert _types(path, "telegrams") == ["text"]
    assert _types(path, "last_ga_telegrams") == ["text"]
    assert not _flag_set(path, "timestamps_integer")
    assert _index_exists(path)


async def test_vacuum_waits_for_room(tmp_path: Path, monkeypatch) -> None:
    """The conversion runs, the VACUUM is skipped and flagged, and a later start with room runs it."""
    path = tmp_path / "old.db"
    await _pre_0_15_db(path, TEXT_STAMPS, [TEXT_STAMPS[1]])
    vacuums = _count_vacuums(monkeypatch)
    size = SqliteStore(str(path))._file_size()
    # Enough for the conversion (half the file), not for the VACUUM (the file).
    _fake_free_space(monkeypatch, size - 1)

    store = SqliteStore(str(path))
    await store.initialize()
    assert vacuums == []
    assert _flag_set(path, "timestamps_integer")
    assert _flag_set(path, "vacuum_pending")
    assert _types(path, "telegrams") == ["integer"]
    # Nothing to run while the VACUUM would not fit.
    assert await store.needs_migration() is False
    await store.close()

    _fake_free_space(monkeypatch, size * 10)
    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert vacuums == ["VACUUM"]
    assert await store.needs_migration() is False
    await store.close()
    assert not _flag_set(path, "vacuum_pending")

    # And it stays done.
    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()
    assert vacuums == ["VACUUM"]


async def test_utc_conversion_refuses_when_the_disk_is_short(tmp_path: Path, monkeypatch) -> None:
    """The UTC rewrite is one transaction per table, so it needs room for about the file."""
    path = tmp_path / "old.db"
    await _pre_0_15_db(path, TEXT_STAMPS, [TEXT_STAMPS[1]])
    con = sqlite3.connect(str(path))
    con.execute("DELETE FROM store_metadata WHERE key = 'timestamps_utc'")
    con.commit()
    con.close()
    size = SqliteStore(str(path))._file_size()
    # Enough for the integer conversion, not for the UTC rewrite.
    _fake_free_space(monkeypatch, size - 1)

    store = SqliteStore(str(path), legacy_timestamp_timezone=ZoneInfo("Europe/Vienna"))
    with pytest.raises(InsufficientDiskSpaceError, match="UTC timestamp conversion"):
        await store.initialize()
    assert await store.needs_timestamp_migration() is True
    await store.close()

    _fake_free_space(monkeypatch, size * 10)
    store = SqliteStore(str(path), legacy_timestamp_timezone=ZoneInfo("Europe/Vienna"))
    await store.initialize()
    assert await store.needs_timestamp_migration() is False
    await store.close()


async def test_an_in_memory_store_never_checks_the_disk(monkeypatch) -> None:
    monkeypatch.setattr("shutil.disk_usage", lambda path: pytest.fail("checked the disk"))
    store = SqliteStore(":memory:")
    await store.initialize()
    assert store._file_size() == 0
    assert store._free_space() is None
    assert store._vacuum_fits() is True
    await store.close()
