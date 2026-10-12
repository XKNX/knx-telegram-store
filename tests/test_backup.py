import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from knx_telegram_store import (
    BufferedMemoryStore,
    BufferedSqliteStore,
    KnxTelegramStoreException,
    StoredTelegram,
    TelegramQuery,
)
from knx_telegram_store.backends.sqlite import SqliteStore


def make_telegram(minutes_ago: float = 0) -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source="1.1.1",
        destination="1/1/1",
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=20.0,
        dpt_main=9,
    )


def raw_count(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT count(*) FROM telegrams").fetchone()[0]


def raw_write_locked(db_path) -> bool:
    """Whether another connection can take the write lock right now."""
    conn = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as err:
        assert "locked" in str(err)
        return True
    else:
        conn.execute("ROLLBACK")
        return False
    finally:
        conn.close()


@pytest.fixture
async def file_store(tmp_path):
    store = SqliteStore(tmp_path / "telegrams.db")
    await store.initialize()
    await store.store_many([make_telegram(2), make_telegram(1)])
    yield store, tmp_path / "telegrams.db"
    await store.close()


async def test_begin_backup_checkpoints_wal_and_holds_write_lock(file_store):
    store, db_path = file_store
    wal_path = db_path.with_name(db_path.name + "-wal")
    assert wal_path.stat().st_size > 0

    await store.begin_backup()
    try:
        assert wal_path.stat().st_size == 0
        assert raw_write_locked(db_path)
        # readers are unaffected and see the checkpointed rows in the main file
        assert raw_count(db_path) == 2
    finally:
        await store.end_backup()

    assert not raw_write_locked(db_path)
    await store.store(make_telegram())
    assert await store.count() == 3


async def test_begin_backup_twice_raises(file_store):
    store, _ = file_store
    await store.begin_backup()
    try:
        with pytest.raises(KnxTelegramStoreException, match="already in progress"):
            await store.begin_backup()
    finally:
        await store.end_backup()


async def test_end_backup_without_begin_raises(file_store):
    store, _ = file_store
    with pytest.raises(KnxTelegramStoreException, match="No backup"):
        await store.end_backup()


async def test_read_only_store_rejects_backup(file_store):
    _, db_path = file_store
    reader = SqliteStore(db_path, read_only=True)
    await reader.initialize()
    try:
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.begin_backup()
    finally:
        await reader.close()


async def test_close_releases_backup_lock(tmp_path, caplog):
    db_path = tmp_path / "telegrams.db"
    store = SqliteStore(db_path)
    await store.initialize()
    await store.store(make_telegram())
    await store.begin_backup()
    assert raw_write_locked(db_path)

    await store.close()

    assert not raw_write_locked(db_path)
    assert "backup is in progress" in caplog.text


async def test_memory_database_backup_is_a_noop():
    store = SqliteStore(":memory:")
    await store.initialize()
    try:
        await store.begin_backup()
        with pytest.raises(KnxTelegramStoreException, match="already in progress"):
            await store.begin_backup()
        await store.store(make_telegram())
        await store.end_backup()
        assert await store.count() == 1
    finally:
        await store.close()


@pytest.fixture
async def buffered_file_store(tmp_path):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=60)
    await store.initialize()
    yield store, db_path
    await store.close()


async def test_buffered_backup_flushes_first_then_holds_writes(buffered_file_store):
    store, db_path = buffered_file_store
    await store.store(make_telegram(2))

    await store.begin_backup()
    try:
        assert raw_count(db_path) == 1
        assert raw_write_locked(db_path)

        await store.store(make_telegram(1))
        # flushes return without writing instead of waiting for the backup
        await asyncio.wait_for(store.flush(), timeout=1)
        result = await asyncio.wait_for(store.query(TelegramQuery(limit=10), flush_first=True), timeout=1)
        assert len(result.telegrams) == 1
        assert raw_count(db_path) == 1

        evict = asyncio.create_task(store.evict_older_than(datetime.now(UTC) - timedelta(days=1)))
        await asyncio.sleep(0.05)
        assert not evict.done()
    finally:
        await store.end_backup()

    assert await asyncio.wait_for(evict, timeout=1) == 0
    await store.flush()
    assert raw_count(db_path) == 2


async def test_buffered_periodic_flush_waits_for_backup_to_end(tmp_path):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=0.05)
    await store.initialize()
    store.start()
    try:
        await store.begin_backup()
        await store.store(make_telegram())
        await asyncio.sleep(0.2)
        assert raw_count(db_path) == 0
        await store.end_backup()
        await asyncio.sleep(0.2)
        assert raw_count(db_path) == 1
    finally:
        await store.close()


async def test_buffered_write_queued_before_backup_waits_for_it(buffered_file_store):
    """A mutation that gets the lock right after begin_backup() goes back to waiting."""
    store, db_path = buffered_file_store
    release = asyncio.Event()

    async def hold_mutation_lock():
        async with store._mutation():
            await release.wait()

    holder = asyncio.create_task(hold_mutation_lock())
    await asyncio.sleep(0)
    begin = asyncio.create_task(store.begin_backup())
    await asyncio.sleep(0)
    write = asyncio.create_task(store.store_many([make_telegram()]))
    await asyncio.sleep(0)
    release.set()

    await asyncio.wait_for(begin, timeout=1)
    await holder
    await asyncio.sleep(0.05)
    assert not write.done()
    assert raw_count(db_path) == 0

    await store.end_backup()
    await asyncio.wait_for(write, timeout=1)
    assert raw_count(db_path) == 1


async def test_buffered_begin_backup_twice_raises(buffered_file_store):
    store, _ = buffered_file_store
    await store.begin_backup()
    try:
        with pytest.raises(KnxTelegramStoreException, match="already in progress"):
            await store.begin_backup()
    finally:
        await store.end_backup()
    with pytest.raises(KnxTelegramStoreException, match="No backup"):
        await store.end_backup()


async def test_buffered_close_during_backup_persists_buffer(tmp_path, caplog):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=60)
    await store.initialize()
    await store.begin_backup()
    await store.store(make_telegram())

    await store.close()

    assert "backup is in progress" in caplog.text
    assert raw_count(db_path) == 1


async def test_buffered_memory_store_backup_gates_writes():
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    try:
        await store.store(make_telegram(1))
        await store.begin_backup()
        await store.store(make_telegram())
        await store.flush()
        assert await store.count() == 1
        await store.end_backup()
        await store.flush()
        assert await store.count() == 2
    finally:
        await store.close()
