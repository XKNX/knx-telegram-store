import asyncio
from datetime import UTC, datetime

import pytest

from knx_telegram_store import (
    BufferedMemoryStore,
    BufferedSqliteStore,
    KnxTelegramStoreException,
    StoredTelegram,
    TelegramQuery,
)
from knx_telegram_store.backends.memory import MemoryStore


@pytest.fixture
def sample_telegram():
    return StoredTelegram(
        timestamp=datetime.now(UTC),
        source="1.1.1",
        destination="1/1/1",
        telegramtype="GroupValueWrite",
        direction="Incoming",
    )


@pytest.fixture(params=["sqlite", "memory"])
async def buffered_store(request):
    if request.param == "sqlite":
        store = BufferedSqliteStore(":memory:", flush_interval=0.1)
    else:
        store = BufferedMemoryStore(flush_interval=0.1)
    await store.initialize()
    return store


async def test_store_buffers_until_flush(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)
    # Buffer has 1 entry; the DB should still be empty
    assert len(buffered_store._buffer) == 1
    assert await buffered_store.count() == 0

    await buffered_store.flush()
    assert len(buffered_store._buffer) == 0
    assert await buffered_store.count() == 1


async def test_store_sync_buffers_until_flush(buffered_store, sample_telegram):
    buffered_store.store_sync(sample_telegram)
    assert len(buffered_store._buffer) == 1
    assert await buffered_store.count() == 0

    await buffered_store.flush()
    assert len(buffered_store._buffer) == 0
    assert await buffered_store.count() == 1


async def test_periodic_flush(buffered_store, sample_telegram):
    buffered_store.start()
    await buffered_store.store(sample_telegram)

    # Wait for periodic flush (flush_interval is 0.1)
    await asyncio.sleep(0.2)

    assert await buffered_store.count() == 1
    await buffered_store.stop()


async def test_periodic_flush_retries_after_failure(sample_telegram, monkeypatch):
    store = BufferedMemoryStore(flush_interval=0.01)
    await store.initialize()
    original_store_many = store.store_many
    retried = asyncio.Event()
    call_count = 0

    async def _fail_once(telegrams):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("DB error")
        await original_store_many(telegrams)
        retried.set()

    monkeypatch.setattr(store, "store_many", _fail_once)
    await store.store(sample_telegram)
    store.start()

    await asyncio.wait_for(retried.wait(), timeout=1)

    assert call_count == 2
    assert store._flush_task is not None
    assert not store._flush_task.done()
    assert store._buffer == []
    assert await store.count() == 1
    await store.stop()


async def test_stop_flushes_remaining(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)
    await buffered_store.stop()

    # After stop the engine is disposed, but we verified the count before stop.
    # Re-open to verify persistence isn't needed here — just check buffer drained.
    assert len(buffered_store._buffer) == 0


async def test_close_flushes_pending_telegrams(sample_telegram):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.store(sample_telegram)

    await store.close()

    assert store._buffer == []
    assert await store.count() == 1


async def test_close_stops_periodic_task_and_is_idempotent(sample_telegram):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.store(sample_telegram)
    store.start()
    periodic_task = store._flush_task
    assert periodic_task is not None

    await store.close()
    await store.close()

    assert periodic_task.done()
    assert store._flush_task is None
    assert store._buffer == []
    assert await store.count() == 1


async def test_stop_remains_close_alias(sample_telegram):
    store = BufferedMemoryStore()
    await store.initialize()
    await store.store(sample_telegram)

    await store.stop()
    await store.stop()

    assert store._buffer == []
    assert await store.count() == 1


async def test_flush_failure_reprepends(buffered_store, sample_telegram, monkeypatch):
    async def _fail(telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(buffered_store, "store_many", _fail)

    await buffered_store.store(sample_telegram)
    with pytest.raises(KnxTelegramStoreException, match="DB error"):
        await buffered_store.flush()

    # Buffer should still contain the telegram after failure
    assert len(buffered_store._buffer) == 1
    assert buffered_store._buffer[0] == sample_telegram


async def test_explicit_flush_failure_raises_and_restores_buffer(buffered_store, sample_telegram, monkeypatch):
    async def _fail(_telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(buffered_store, "store_many", _fail)
    await buffered_store.store(sample_telegram)

    with pytest.raises(KnxTelegramStoreException, match="Database error during flush: DB error"):
        await buffered_store.flush()

    assert buffered_store._buffer == [sample_telegram]


async def test_query_flush_first_propagates_flush_failure(buffered_store, sample_telegram, monkeypatch):
    async def _fail(_telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(buffered_store, "store_many", _fail)
    await buffered_store.store(sample_telegram)

    with pytest.raises(KnxTelegramStoreException, match="DB error"):
        await buffered_store.query(TelegramQuery(), flush_first=True)

    assert buffered_store._buffer == [sample_telegram]


async def test_close_retries_failed_final_flush_before_closing(sample_telegram, monkeypatch):
    store = BufferedMemoryStore()
    await store.initialize()
    original_store_many = store.store_many
    flush_attempts = 0
    close_calls = 0

    async def _fail_once(telegrams):
        nonlocal flush_attempts
        flush_attempts += 1
        if flush_attempts == 1:
            raise RuntimeError("DB error")
        await original_store_many(telegrams)

    async def _close(_store):
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(store, "store_many", _fail_once)
    monkeypatch.setattr(MemoryStore, "close", _close)
    await store.store(sample_telegram)

    with pytest.raises(KnxTelegramStoreException, match="DB error"):
        await store.close()

    assert store._buffer == [sample_telegram]
    assert close_calls == 0

    await store.close()

    assert store._buffer == []
    assert await store.count() == 1
    assert close_calls == 1


async def test_flush_failure_then_recovery(buffered_store, sample_telegram, monkeypatch):
    call_count = 0

    original_store_many = buffered_store.store_many

    async def _fail_once(telegrams):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("DB error")
        await original_store_many(telegrams)

    monkeypatch.setattr(buffered_store, "store_many", _fail_once)

    await buffered_store.store(sample_telegram)
    with pytest.raises(KnxTelegramStoreException, match="DB error"):
        await buffered_store.flush()
    assert len(buffered_store._buffer) == 1

    await buffered_store.flush()
    assert len(buffered_store._buffer) == 0


async def test_store_many_passes_through(buffered_store, sample_telegram):
    """store_many bypasses the buffer and writes directly."""
    await buffered_store.store_many([sample_telegram])
    assert len(buffered_store._buffer) == 0
    assert await buffered_store.count() == 1


async def test_read_operations_pass_through(buffered_store, sample_telegram):
    await buffered_store.store_many([sample_telegram])

    query = TelegramQuery()
    result = await buffered_store.query(query)
    assert len(result.telegrams) == 1

    assert await buffered_store.count() == 1


@pytest.mark.asyncio
async def test_eviction_passes_through_to_backend(sample_telegram):
    # Time-based eviction is a SQL-backend capability; the memory backend prunes
    # by max_telegrams instead and reports 0 here.
    store = BufferedSqliteStore(":memory:", flush_interval=0.1)
    await store.initialize()
    await store.store_many([sample_telegram])

    cutoff = datetime.now(UTC)
    deleted = await store.evict_older_than(cutoff, dry_run=True)
    assert deleted == 1

    deleted = await store.evict_expired(dry_run=True)
    assert deleted == 0  # no retention_days configured


async def test_flush_empty_buffer_is_noop(buffered_store):
    await buffered_store.flush()
    assert await buffered_store.count() == 0


async def test_query_flush_first(buffered_store, sample_telegram):
    await buffered_store.store(sample_telegram)

    query = TelegramQuery()
    result = await buffered_store.query(query, flush_first=True)

    # flush_first drained the buffer before querying
    assert len(buffered_store._buffer) == 0
    assert len(result.telegrams) == 1


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


async def test_buffer_limit_failed_flush(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", max_buffer_size=3)
    await store.initialize()

    async def _fail(telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(store, "store_many", _fail)

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

    with pytest.raises(KnxTelegramStoreException, match="DB error"):
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

    with pytest.raises(KnxTelegramStoreException, match="DB error"):
        await store.flush()
    # Should be capped at 3, with oldest ("1.1.0") dropped
    assert len(store._buffer) == 3
    assert store._buffer[0].source == "1.1.1"
    assert store._buffer[1].source == "1.1.2"
    assert store._buffer[2].source == "1.1.3"


@pytest.mark.parametrize("operation", ["flush", "stop"])
async def test_lifecycle_operation_preserves_in_flight_batch(sample_telegram, monkeypatch, operation):
    store = BufferedMemoryStore(flush_interval=0.01)
    await store.initialize()
    original_store_many = store.store_many
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    call_count = 0

    async def _block_first_write(telegrams):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            write_started.set()
            await release_write.wait()
        await original_store_many(telegrams)

    monkeypatch.setattr(store, "store_many", _block_first_write)
    await store.store(sample_telegram)
    store.start()
    await asyncio.wait_for(write_started.wait(), timeout=1)

    operation_task = asyncio.create_task(getattr(store, operation)())
    await asyncio.sleep(0)
    release_write.set()
    await operation_task

    if operation == "flush":
        await store.stop()

    assert store._buffer == []
    assert await store.count() == 1
