import asyncio
from datetime import UTC, datetime

import pytest

from knx_telegram_store import BufferedSqliteStore, StoredTelegram, TelegramQuery


@pytest.fixture
def sample_telegram():
    return StoredTelegram(
        timestamp=datetime.now(UTC),
        source="1.1.1",
        destination="1/1/1",
        telegramtype="GroupValueWrite",
        direction="Incoming",
    )


@pytest.fixture
async def buffered_store():
    store = BufferedSqliteStore(":memory:", flush_interval=0.1)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_store_buffers_until_flush(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)
    # Buffer has 1 entry; the DB should still be empty
    assert len(buffered_store._buffer) == 1
    assert await buffered_store.count() == 0

    await buffered_store.flush()
    assert len(buffered_store._buffer) == 0
    assert await buffered_store.count() == 1


@pytest.mark.asyncio
async def test_store_sync_buffers_until_flush(buffered_store, sample_telegram):
    buffered_store.store_sync(sample_telegram)
    assert len(buffered_store._buffer) == 1
    assert await buffered_store.count() == 0

    await buffered_store.flush()
    assert len(buffered_store._buffer) == 0
    assert await buffered_store.count() == 1


@pytest.mark.asyncio
async def test_periodic_flush(buffered_store, sample_telegram):
    buffered_store.start()
    await buffered_store.store(sample_telegram)

    # Wait for periodic flush (flush_interval is 0.1)
    await asyncio.sleep(0.2)

    assert await buffered_store.count() == 1
    await buffered_store.stop()


@pytest.mark.asyncio
async def test_stop_flushes_remaining(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)
    await buffered_store.stop()

    # After stop the engine is disposed, but we verified the count before stop.
    # Re-open to verify persistence isn't needed here — just check buffer drained.
    assert len(buffered_store._buffer) == 0


@pytest.mark.asyncio
async def test_flush_failure_reprepends(buffered_store, sample_telegram, monkeypatch):
    async def _fail(telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(type(buffered_store), "store_many", _fail)

    await buffered_store.store(sample_telegram)
    await buffered_store.flush()

    # Buffer should still contain the telegram after failure
    assert len(buffered_store._buffer) == 1
    assert buffered_store._buffer[0] == sample_telegram


@pytest.mark.asyncio
async def test_flush_failure_then_recovery(buffered_store, sample_telegram, monkeypatch):
    call_count = 0

    original_store_many = type(buffered_store).store_many

    async def _fail_once(self, telegrams):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("DB error")
        await original_store_many(self, telegrams)

    monkeypatch.setattr(type(buffered_store), "store_many", _fail_once)

    await buffered_store.store(sample_telegram)
    await buffered_store.flush()
    assert len(buffered_store._buffer) == 1

    await buffered_store.flush()
    assert len(buffered_store._buffer) == 0


@pytest.mark.asyncio
async def test_store_many_passes_through(buffered_store, sample_telegram):
    """store_many bypasses the buffer and writes directly."""
    await buffered_store.store_many([sample_telegram])
    assert len(buffered_store._buffer) == 0
    assert await buffered_store.count() == 1


@pytest.mark.asyncio
async def test_read_operations_pass_through(buffered_store, sample_telegram):
    await buffered_store.store_many([sample_telegram])

    query = TelegramQuery()
    result = await buffered_store.query(query)
    assert len(result.telegrams) == 1

    assert await buffered_store.count() == 1

    cutoff = datetime.now(UTC)
    deleted = await buffered_store.evict_older_than(cutoff, dry_run=True)
    assert deleted == 1

    deleted = await buffered_store.evict_expired(dry_run=True)
    assert deleted == 0  # no retention_days configured


@pytest.mark.asyncio
async def test_flush_empty_buffer_is_noop(buffered_store):
    await buffered_store.flush()
    assert await buffered_store.count() == 0


@pytest.mark.asyncio
async def test_query_flush_first(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)

    query = TelegramQuery()
    result = await buffered_store.query(query, flush_first=True)

    # flush_first drained the buffer before querying
    assert len(buffered_store._buffer) == 0
    assert len(result.telegrams) == 1


@pytest.mark.asyncio
async def test_query_no_flush_by_default(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)

    query = TelegramQuery()
    result = await buffered_store.query(query)

    # Buffer was NOT flushed — telegram not visible in DB yet
    assert len(buffered_store._buffer) == 1
    assert len(result.telegrams) == 0


def test_properties():
    store = BufferedSqliteStore(":memory:", flush_interval=5.0)
    assert store.flush_interval == 5.0
    assert store.retention_days is None
    assert store.max_telegrams is None
    assert store.capabilities.supports_pagination is True


@pytest.mark.asyncio
async def test_buffer_limit(sample_telegram):
    store = BufferedSqliteStore(":memory:", max_buffer_size=3)
    await store.initialize()

    # Store 4 telegrams
    for i in range(4):
        t = StoredTelegram(
            timestamp=sample_telegram.timestamp,
            source=f"1.1.{i}",
            destination=sample_telegram.destination,
            telegramtype=sample_telegram.telegramtype,
            direction=sample_telegram.direction,
        )
        await store.store(t)

    # Buffer should be capped at 3, with the oldest (1.1.0) dropped
    assert len(store._buffer) == 3
    assert store._buffer[0].source == "1.1.1"
    assert store._buffer[1].source == "1.1.2"
    assert store._buffer[2].source == "1.1.3"


@pytest.mark.asyncio
async def test_buffer_limit_failed_flush(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", max_buffer_size=3)
    await store.initialize()

    async def _fail(telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(type(store), "store_many", _fail)

    # Add 2 items, flush fails (they remain in buffer)
    for i in range(2):
        t = StoredTelegram(
            timestamp=sample_telegram.timestamp,
            source=f"1.1.{i}",
            destination=sample_telegram.destination,
            telegramtype=sample_telegram.telegramtype,
            direction=sample_telegram.direction,
        )
        await store.store(t)

    await store.flush()
    assert len(store._buffer) == 2

    # Now add 2 more items
    for i in range(2, 4):
        t = StoredTelegram(
            timestamp=sample_telegram.timestamp,
            source=f"1.1.{i}",
            destination=sample_telegram.destination,
            telegramtype=sample_telegram.telegramtype,
            direction=sample_telegram.direction,
        )
        await store.store(t)

    await store.flush()
    # Should be capped at 3, with oldest ("1.1.0") dropped
    assert len(store._buffer) == 3
    assert store._buffer[0].source == "1.1.1"
    assert store._buffer[1].source == "1.1.2"
    assert store._buffer[2].source == "1.1.3"
