import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from knx_telegram_store import (
    BufferedMemoryStore,
    BufferedSqliteStore,
    KnxTelegramStoreException,
    StoredTelegram,
    TelegramQuery,
)
from knx_telegram_store.backends.memory import MemoryStore
from knx_telegram_store.backends.sqlite import SqliteStore


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
    try:
        yield store
    finally:
        await store.close()


async def _invoke_mutation(store, operation, sample_telegram):
    if operation == "store_many":
        await store.store_many([sample_telegram])
    elif operation == "flush":
        await store.flush()
    elif operation == "evict":
        await store.evict_older_than(sample_telegram.timestamp)
    elif operation == "clear":
        await store.clear()
    else:
        await store.optimize()


async def _settle_tasks(*tasks):
    tasks = [task for task in tasks if task is not None]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
    except TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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


async def test_close_persists_pending_telegrams_to_file(tmp_path, sample_telegram):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=60)
    await store.initialize()
    try:
        await store.store(sample_telegram)
        await store.close()

        reader = SqliteStore(db_path, read_only=True)
        await reader.initialize()
        try:
            assert await reader.count() == 1
        finally:
            await reader.close()
    finally:
        await store.close()


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


@pytest.mark.parametrize("operation", ["store_many", "flush", "evict", "clear", "optimize"])
async def test_mutations_are_rejected_after_close(sample_telegram, operation):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.close()

    with pytest.raises(KnxTelegramStoreException, match="closing"):
        await _invoke_mutation(store, operation, sample_telegram)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "sqlite_retention"])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_evict_expired_is_rejected_after_close(backend, dry_run):
    if backend == "memory":
        store = BufferedMemoryStore(flush_interval=60)
    else:
        store = BufferedSqliteStore(
            ":memory:", retention_days=1 if backend == "sqlite_retention" else None, flush_interval=60
        )
    await store.initialize()
    try:
        await store.close()

        with pytest.raises(KnxTelegramStoreException, match="closing"):
            await asyncio.wait_for(store.evict_expired(dry_run=dry_run), timeout=1)
    finally:
        await store.close()


async def test_start_after_close_does_not_create_periodic_task():
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.close()

    store.start()

    assert store._flush_task is None


async def test_single_stores_after_close_are_dropped(sample_telegram):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.close()

    await store.store(sample_telegram)
    store.store_sync(sample_telegram)

    assert store._buffer == []


async def test_stop_remains_close_alias(sample_telegram):
    store = BufferedMemoryStore()
    await store.initialize()
    await store.store(sample_telegram)

    await store.stop()
    await store.stop()

    assert store._buffer == []
    assert await store.count() == 1


async def test_flush_failure_reprepends(buffered_store, sample_telegram, monkeypatch):
    original_store_many = buffered_store.store_many

    async def _fail(telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(buffered_store, "store_many", _fail)
    try:
        await buffered_store.store(sample_telegram)
        with pytest.raises(KnxTelegramStoreException, match="DB error"):
            await buffered_store.flush()

        # Buffer should still contain the telegram after failure
        assert len(buffered_store._buffer) == 1
        assert buffered_store._buffer[0] == sample_telegram
    finally:
        monkeypatch.setattr(buffered_store, "store_many", original_store_many)


async def test_cancelled_flush_restores_detached_batch(sample_telegram, monkeypatch):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    original_store_many = store.store_many
    write_started = asyncio.Event()
    never_release = asyncio.Event()
    flush_task = None

    async def _block_write(_telegrams):
        write_started.set()
        await never_release.wait()

    monkeypatch.setattr(store, "store_many", _block_write)
    try:
        await store.store(sample_telegram)
        flush_task = asyncio.create_task(store.flush())
        await asyncio.wait_for(write_started.wait(), timeout=1)
        flush_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await flush_task
        assert store._buffer == [sample_telegram]
    finally:
        never_release.set()
        await _settle_tasks(flush_task)
        monkeypatch.setattr(store, "store_many", original_store_many)
        await store.close()


async def test_explicit_flush_failure_raises_and_restores_buffer(buffered_store, sample_telegram, monkeypatch):
    original_store_many = buffered_store.store_many

    async def _fail(_telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(buffered_store, "store_many", _fail)
    try:
        await buffered_store.store(sample_telegram)

        with pytest.raises(KnxTelegramStoreException, match="Database error during flush: DB error"):
            await buffered_store.flush()

        assert buffered_store._buffer == [sample_telegram]
    finally:
        monkeypatch.setattr(buffered_store, "store_many", original_store_many)


async def test_query_flush_first_propagates_flush_failure(buffered_store, sample_telegram, monkeypatch):
    original_store_many = buffered_store.store_many

    async def _fail(_telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(buffered_store, "store_many", _fail)
    try:
        await buffered_store.store(sample_telegram)

        with pytest.raises(KnxTelegramStoreException, match="DB error"):
            await buffered_store.query(TelegramQuery(), flush_first=True)

        assert buffered_store._buffer == [sample_telegram]
    finally:
        monkeypatch.setattr(buffered_store, "store_many", original_store_many)


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


async def test_close_waits_for_concurrent_store_many(sample_telegram, monkeypatch, tmp_path):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=60)
    await store.initialize()
    original_store_many = SqliteStore.store_many
    original_close = SqliteStore.close
    backend_store_started = asyncio.Event()
    release_store = asyncio.Event()
    backend_close_started = asyncio.Event()
    store_task = None
    close_task = None
    reader = None

    async def _pause_backend_store(self, telegrams):
        backend_store_started.set()
        await release_store.wait()
        await original_store_many(self, telegrams)

    async def _observe_backend_close(self):
        backend_close_started.set()
        await original_close(self)

    monkeypatch.setattr(SqliteStore, "store_many", _pause_backend_store)
    monkeypatch.setattr(SqliteStore, "close", _observe_backend_close)
    try:
        store_task = asyncio.create_task(store.store_many([sample_telegram]))
        await asyncio.wait_for(backend_store_started.wait(), timeout=1)
        close_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        close_started_before_store_finished = backend_close_started.is_set()
        release_store.set()
        await store_task
        await close_task

        assert not close_started_before_store_finished
        reader = SqliteStore(db_path)
        await reader.initialize()
        assert await reader.count() == 1
    finally:
        release_store.set()
        await _settle_tasks(store_task, close_task)
        if reader is not None:
            await reader.close()
        await store.close()


async def test_close_waits_for_concurrent_optimize(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    original_optimize = SqliteStore.optimize
    original_close = SqliteStore.close
    backend_optimize_started = asyncio.Event()
    release_backend_optimize = asyncio.Event()
    backend_close_started = asyncio.Event()
    optimize_task = None
    close_task = None

    async def _pause_backend_optimize(self):
        backend_optimize_started.set()
        await release_backend_optimize.wait()
        await original_optimize(self)

    async def _observe_backend_close(self):
        backend_close_started.set()
        await original_close(self)

    monkeypatch.setattr(SqliteStore, "optimize", _pause_backend_optimize)
    monkeypatch.setattr(SqliteStore, "close", _observe_backend_close)
    try:
        await store.store(sample_telegram)
        optimize_task = asyncio.create_task(store.optimize())
        await asyncio.wait_for(backend_optimize_started.wait(), timeout=1)
        close_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)

        assert not backend_close_started.is_set()

        release_backend_optimize.set()
        await optimize_task
        await close_task
        assert store._buffer == []
    finally:
        release_backend_optimize.set()
        await _settle_tasks(optimize_task, close_task)
        await store.close()


async def test_close_rejects_eviction_queued_during_final_flush(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    original_store_many = SqliteStore.store_many
    original_evict = SqliteStore.evict_older_than
    final_write_started = asyncio.Event()
    release_final_write = asyncio.Event()
    backend_eviction_called = asyncio.Event()
    close_task = None
    eviction_task = None

    async def _pause_final_write(self, telegrams):
        final_write_started.set()
        await release_final_write.wait()
        await original_store_many(self, telegrams)

    async def _observe_eviction(self, cutoff, *, dry_run=False):
        backend_eviction_called.set()
        return 0

    monkeypatch.setattr(SqliteStore, "store_many", _pause_final_write)
    monkeypatch.setattr(SqliteStore, "evict_older_than", _observe_eviction)
    try:
        await store.store(sample_telegram)
        close_task = asyncio.create_task(store.close())
        await asyncio.wait_for(final_write_started.wait(), timeout=1)
        eviction_task = asyncio.create_task(store.evict_older_than(sample_telegram.timestamp))
        await asyncio.sleep(0)
        release_final_write.set()

        await close_task
        with pytest.raises(KnxTelegramStoreException, match="closing"):
            await eviction_task
        assert not backend_eviction_called.is_set()
    finally:
        release_final_write.set()
        await _settle_tasks(close_task, eviction_task)
        monkeypatch.setattr(SqliteStore, "store_many", original_store_many)
        monkeypatch.setattr(SqliteStore, "evict_older_than", original_evict)
        await store.close()


async def test_close_rejects_store_many_queued_during_shutdown(sample_telegram, monkeypatch, tmp_path):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=60)
    await store.initialize()
    original_close = SqliteStore.close
    backend_close_started = asyncio.Event()
    release_backend_close = asyncio.Event()
    close_task = None
    queued_store_task = None
    reader = None

    async def _pause_backend_close(self):
        backend_close_started.set()
        await release_backend_close.wait()
        await original_close(self)

    monkeypatch.setattr(SqliteStore, "close", _pause_backend_close)
    try:
        close_task = asyncio.create_task(store.close())
        await asyncio.wait_for(backend_close_started.wait(), timeout=1)
        queued_store_task = asyncio.create_task(store.store_many([sample_telegram]))
        release_backend_close.set()

        await close_task
        with pytest.raises(KnxTelegramStoreException, match="closing"):
            await queued_store_task

        reader = SqliteStore(db_path)
        await reader.initialize()
        assert await reader.count() == 0
    finally:
        release_backend_close.set()
        await _settle_tasks(close_task, queued_store_task)
        if reader is not None:
            await reader.close()
        await store.close()


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
    try:
        await store.store_many([sample_telegram])

        cutoff = datetime.now(UTC)
        deleted = await store.evict_older_than(cutoff, dry_run=True)
        assert deleted == 1

        deleted = await store.evict_expired(dry_run=True)
        assert deleted == 0  # no retention_days configured
    finally:
        await store.close()


async def test_evict_older_than_prunes_matching_buffered_telegrams(sample_telegram):
    cutoff = sample_telegram.timestamp
    old = replace(sample_telegram, timestamp=cutoff - timedelta(days=2), value="old")
    current = replace(
        sample_telegram,
        timestamp=cutoff + timedelta(seconds=1),
        source="1.1.2",
        value="current",
    )
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    try:
        await store.store(old)
        await store.store(current)

        deleted = await store.evict_older_than(cutoff)
        await store.flush()
        result = await store.query(TelegramQuery())

        assert deleted == 1
        assert [telegram.value for telegram in result.telegrams] == ["current"]
    finally:
        await store.close()


async def test_evict_older_than_dry_run_counts_buffer_without_pruning(sample_telegram):
    cutoff = sample_telegram.timestamp + timedelta(seconds=1)
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    try:
        await store.store(sample_telegram)

        deleted = await store.evict_older_than(cutoff, dry_run=True)

        assert deleted == 1
        assert store._buffer == [sample_telegram]
    finally:
        await store.close()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_evict_expired_prunes_expired_buffered_telegrams(sample_telegram, dry_run):
    now = datetime.now(UTC)
    old = replace(sample_telegram, timestamp=now - timedelta(days=2), value="old")
    current = replace(sample_telegram, timestamp=now, source="1.1.2", value="current")
    store = BufferedSqliteStore(":memory:", retention_days=1, flush_interval=60)
    await store.initialize()
    try:
        await store.store(old)
        await store.store(current)

        deleted = await asyncio.wait_for(store.evict_expired(dry_run=dry_run), timeout=1)
        await store.flush()
        result = await store.query(TelegramQuery())

        assert deleted == 1
        assert [telegram.value for telegram in result.telegrams] == (["current", "old"] if dry_run else ["current"])
    finally:
        await store.close()


async def test_eviction_serializes_with_concurrent_flush(sample_telegram, monkeypatch):
    cutoff = sample_telegram.timestamp + timedelta(seconds=1)
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    original_evict = SqliteStore.evict_older_than
    backend_delete_finished = asyncio.Event()
    release_eviction = asyncio.Event()
    eviction_task = None
    flush_task = None

    async def _pause_after_backend_delete(self, cutoff, *, dry_run=False):
        deleted = await original_evict(self, cutoff, dry_run=dry_run)
        backend_delete_finished.set()
        await release_eviction.wait()
        return deleted

    monkeypatch.setattr(SqliteStore, "evict_older_than", _pause_after_backend_delete)
    try:
        await store.store(sample_telegram)
        eviction_task = asyncio.create_task(store.evict_older_than(cutoff))
        await asyncio.wait_for(backend_delete_finished.wait(), timeout=1)
        flush_task = asyncio.create_task(store.flush())
        await asyncio.sleep(0)
        release_eviction.set()

        assert await eviction_task == 1
        await flush_task
        assert await store.count() == 0
    finally:
        release_eviction.set()
        await _settle_tasks(eviction_task, flush_task)
        monkeypatch.setattr(SqliteStore, "evict_older_than", original_evict)
        await store.close()


async def test_eviction_serializes_with_concurrent_store_many(sample_telegram, monkeypatch):
    cutoff = sample_telegram.timestamp + timedelta(seconds=1)
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    original_evict = SqliteStore.evict_older_than
    original_store_many = SqliteStore.store_many
    backend_delete_finished = asyncio.Event()
    release_eviction = asyncio.Event()
    backend_store_started = asyncio.Event()
    eviction_task = None
    store_task = None

    async def _pause_after_backend_delete(self, cutoff, *, dry_run=False):
        deleted = await original_evict(self, cutoff, dry_run=dry_run)
        backend_delete_finished.set()
        await release_eviction.wait()
        return deleted

    async def _observe_backend_store(self, telegrams):
        backend_store_started.set()
        await original_store_many(self, telegrams)

    monkeypatch.setattr(SqliteStore, "evict_older_than", _pause_after_backend_delete)
    monkeypatch.setattr(SqliteStore, "store_many", _observe_backend_store)
    try:
        eviction_task = asyncio.create_task(store.evict_older_than(cutoff))
        await asyncio.wait_for(backend_delete_finished.wait(), timeout=1)
        store_task = asyncio.create_task(store.store_many([sample_telegram]))
        await asyncio.sleep(0)

        assert not backend_store_started.is_set()

        release_eviction.set()
        assert await eviction_task == 0
        await store_task
        assert await store.count() == 1
    finally:
        release_eviction.set()
        await _settle_tasks(eviction_task, store_task)
        monkeypatch.setattr(SqliteStore, "evict_older_than", original_evict)
        monkeypatch.setattr(SqliteStore, "store_many", original_store_many)
        await store.close()


async def test_cancelled_eviction_waiting_for_admission_has_no_side_effects(sample_telegram, monkeypatch):
    cutoff = sample_telegram.timestamp + timedelta(seconds=1)
    persisted = replace(sample_telegram, value="persisted")
    buffered = replace(sample_telegram, source="1.1.2", value="buffered")
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    original_store_many = SqliteStore.store_many
    original_mutation = store._mutation
    holder_admitted = asyncio.Event()
    release_holder = asyncio.Event()
    eviction_queued = asyncio.Event()
    holder_task = None
    eviction_task = None

    async def _hold_backend_write(self, telegrams):
        holder_admitted.set()
        await release_holder.wait()
        await original_store_many(self, telegrams)

    @asynccontextmanager
    async def _observe_admission(**kwargs):
        eviction_queued.set()
        async with original_mutation(**kwargs):
            yield

    try:
        await store.store_many([persisted])
        await store.store(buffered)
        monkeypatch.setattr(SqliteStore, "store_many", _hold_backend_write)
        holder_task = asyncio.create_task(store.store_many([]))
        await asyncio.wait_for(holder_admitted.wait(), timeout=1)
        monkeypatch.setattr(store, "_mutation", _observe_admission)
        eviction_task = asyncio.create_task(store.evict_older_than(cutoff))
        await asyncio.wait_for(eviction_queued.wait(), timeout=1)

        eviction_task.cancel()
        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eviction_task, timeout=1)

        assert (await store.count(), store._buffer) == (1, [buffered])
    finally:
        release_holder.set()
        await _settle_tasks(holder_task, eviction_task)
        monkeypatch.setattr(store, "_mutation", original_mutation)
        monkeypatch.setattr(SqliteStore, "store_many", original_store_many)
        await store.close()


@pytest.mark.parametrize("cancellation_count", [1, 2])
async def test_cancellation_finishes_buffer_reconciliation_after_backend_eviction(
    sample_telegram, monkeypatch, cancellation_count
):
    cutoff = sample_telegram.timestamp + timedelta(seconds=1)
    persisted = replace(sample_telegram, source="1.1.2", value="persisted")
    buffered = replace(sample_telegram, source="1.1.3", value="buffered")
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    original_evict = SqliteStore.evict_older_than
    backend_delete_committed = asyncio.Event()
    release_backend_return = asyncio.Event()
    eviction_task = None

    async def _pause_after_backend_delete(self, cutoff, *, dry_run=False):
        deleted = await original_evict(self, cutoff, dry_run=dry_run)
        backend_delete_committed.set()
        await release_backend_return.wait()
        return deleted

    monkeypatch.setattr(SqliteStore, "evict_older_than", _pause_after_backend_delete)
    try:
        await SqliteStore.store_many(store, [persisted])
        await store.store(buffered)
        eviction_task = asyncio.create_task(store.evict_older_than(cutoff))
        await asyncio.wait_for(backend_delete_committed.wait(), timeout=1)

        for _ in range(cancellation_count):
            eviction_task.cancel()
            await asyncio.sleep(0)
        operation_finishing = not eviction_task.done()
        release_backend_return.set()

        with pytest.raises(asyncio.CancelledError):
            await eviction_task

        await store.flush()
        assert operation_finishing
        assert store._buffer == []
        assert await store.count() == 0
    finally:
        release_backend_return.set()
        await _settle_tasks(eviction_task)
        monkeypatch.setattr(SqliteStore, "evict_older_than", original_evict)
        await store.close()


async def test_cancellation_remains_primary_when_eviction_finishing_fails(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    eviction_started = asyncio.Event()
    release_failure = asyncio.Event()
    eviction_task = None

    async def _fail_after_cancellation(self, cutoff, *, dry_run):
        eviction_started.set()
        await release_failure.wait()
        raise RuntimeError("eviction failed")

    monkeypatch.setattr(BufferedSqliteStore, "_evict_older_than", _fail_after_cancellation)
    try:
        eviction_task = asyncio.create_task(store.evict_older_than(sample_telegram.timestamp))
        await asyncio.wait_for(eviction_started.wait(), timeout=1)
        eviction_task.cancel()
        await asyncio.sleep(0)
        release_failure.set()

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await eviction_task

        assert isinstance(cancelled.value.__cause__, RuntimeError)
    finally:
        release_failure.set()
        await _settle_tasks(eviction_task)
        await store.close()


async def test_buffered_memory_store_keeps_memory_retention_semantics(sample_telegram):
    cutoff = sample_telegram.timestamp + timedelta(seconds=1)
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.store(sample_telegram)

    assert await store.evict_older_than(cutoff) == 0
    assert store._buffer == [sample_telegram]

    await store.flush()
    assert await store.count() == 1


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
    try:
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
    finally:
        await store.close()


async def test_buffer_limit_failed_flush(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", max_buffer_size=3)
    await store.initialize()
    original_store_many = store.store_many

    async def _fail(telegrams):
        raise RuntimeError("DB error")

    monkeypatch.setattr(store, "store_many", _fail)
    try:
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
    finally:
        monkeypatch.setattr(store, "store_many", original_store_many)
        await store.close()


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
