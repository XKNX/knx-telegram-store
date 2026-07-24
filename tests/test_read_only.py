from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from knx_telegram_store import KnxTelegramStoreException, StoredTelegram, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore


def make_telegram(minutes_ago: float, destination: str = "1/1/1") -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source="1.1.1",
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=20.0,
        dpt_main=9,
    )


@pytest.fixture
async def writer_db(tmp_path):
    """A database file created and populated by a normal (writing) store."""
    db_path = tmp_path / "shared.db"
    writer = SqliteStore(db_path)
    await writer.initialize()
    await writer.store_many([make_telegram(5), make_telegram(3, "1/1/2"), make_telegram(1)])
    yield db_path, writer
    await writer.close()


async def test_writer_enables_wal(writer_db):
    db_path, writer = writer_db
    async with writer.engine.connect() as conn:
        mode = await conn.scalar(text("PRAGMA journal_mode"))
    assert mode == "wal"


async def test_read_only_queries(writer_db):
    db_path, _writer = writer_db
    reader = SqliteStore(db_path, read_only=True)
    await reader.initialize()
    try:
        assert reader.capabilities.read_only is True
        assert reader.capabilities.supports_optimize is False
        assert reader.capabilities.supports_size_stats is True

        assert await reader.count() == 3

        result = await reader.query(TelegramQuery(destinations=["1/1/2"]))
        assert result.total_count == 1

        stats = await reader.get_stats()
        assert stats.telegram_count == 3
        assert stats.size_bytes and stats.size_bytes > 0

        last = await reader.get_last_unique_telegrams()
        assert {t.destination for t in last} == {"1/1/1", "1/1/2"}
    finally:
        await reader.close()


async def test_read_only_rejects_mutations(writer_db):
    db_path, _writer = writer_db
    reader = SqliteStore(db_path, read_only=True)
    await reader.initialize()
    try:
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.store(make_telegram(1))
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.store_many([make_telegram(1)])
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.evict_older_than(datetime.now(UTC))
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.evict_older_than(datetime.now(UTC), dry_run=True)
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.clear()
        with pytest.raises(KnxTelegramStoreException, match="read-only"):
            await reader.optimize()
        # Nothing was deleted or written
        assert await reader.count() == 3
    finally:
        await reader.close()


async def test_read_only_sees_concurrent_writes(writer_db):
    """Reader on a second engine sees rows the writer commits afterwards."""
    db_path, writer = writer_db
    reader = SqliteStore(db_path, read_only=True)
    await reader.initialize()
    try:
        assert await reader.count() == 3
        await writer.store_many([make_telegram(0.5, "2/2/2")])
        assert await reader.count() == 4
        result = await reader.query(TelegramQuery(destinations=["2/2/2"]))
        assert result.total_count == 1
    finally:
        await reader.close()


async def test_read_only_never_creates_file(tmp_path):
    missing = tmp_path / "does" / "not" / "exist.db"
    reader = SqliteStore(missing, read_only=True)
    with pytest.raises(KnxTelegramStoreException):
        await reader.initialize()
    assert not missing.exists()
    await reader.close()


def test_read_only_memory_rejected():
    with pytest.raises(ValueError, match="in-memory"):
        SqliteStore(":memory:", read_only=True)


def test_check_config_read_only(tmp_path):
    missing = tmp_path / "missing.db"
    result = SqliteStore.check_config(missing, read_only=True)
    assert result.ok is False

    existing = tmp_path / "existing.db"
    existing.touch()
    result = SqliteStore.check_config(existing, read_only=True)
    assert result.ok is True

    result = SqliteStore.check_config(":memory:", read_only=True)
    assert result.ok is False


async def test_query_accepts_flush_first_noop(store):
    """Unbuffered stores accept flush_first for buffered-store signature parity."""
    await store.store(make_telegram(1))
    result = await store.query(TelegramQuery(), flush_first=True)
    assert result.total_count == 1
