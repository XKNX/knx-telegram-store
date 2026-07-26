from datetime import UTC, datetime, timedelta

from knx_telegram_store import BufferedSqliteStore, StoredTelegram
from knx_telegram_store.backends.sqlite import SqliteStore


def make_telegram(minutes_ago: float, destination: str = "1/1/1", raw_data: str | None = None) -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source="1.1.1",
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=20.0,
        dpt_main=9,
        raw_data=raw_data,
    )


async def test_get_stats_empty(store):
    stats = await store.get_stats()
    assert stats.telegram_count == 0
    assert stats.oldest_timestamp is None
    assert stats.newest_timestamp is None
    assert stats.backend in ("memory", "sqlite")


async def test_get_stats(store):
    telegrams = [make_telegram(5), make_telegram(3), make_telegram(1)]
    await store.store_many(telegrams)

    stats = await store.get_stats()
    assert stats.telegram_count == 3
    assert stats.oldest_timestamp is not None
    assert stats.newest_timestamp is not None
    assert stats.oldest_timestamp < stats.newest_timestamp

    # Timestamps must match the stored extremes (compare in UTC; sqlite may
    # return naive datetimes depending on driver round-tripping).
    def _utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    assert abs(_utc(stats.oldest_timestamp) - telegrams[0].timestamp) < timedelta(seconds=1)
    assert abs(_utc(stats.newest_timestamp) - telegrams[2].timestamp) < timedelta(seconds=1)

    # Size reporting matches the declared capability
    if store.capabilities.supports_size_stats:
        assert isinstance(stats.size_bytes, int)
        assert stats.size_bytes > 0
    else:
        assert stats.size_bytes is None

    assert stats.retention_days == store.retention_days


async def test_sqlite_optimize_reclaims_space(tmp_path):
    store = SqliteStore(tmp_path / "vacuum.db")
    await store.initialize()
    try:
        # Write enough payload to grow the file measurably
        telegrams = [make_telegram(i / 60, destination=f"1/1/{i % 8}", raw_data="ab" * 512) for i in range(2000)]
        await store.store_many(telegrams)

        size_full = (await store.get_stats()).size_bytes
        assert size_full is not None

        await store.clear()
        size_after_clear = (await store.get_stats()).size_bytes
        assert size_after_clear is not None

        await store.optimize()
        size_after_vacuum = (await store.get_stats()).size_bytes
        assert size_after_vacuum is not None

        # DELETE alone does not shrink the file; VACUUM must
        assert size_after_vacuum < size_after_clear
    finally:
        await store.close()


async def test_memory_optimize_is_noop(store):
    # optimize() must be safe to call on every backend
    await store.optimize()
    assert await store.count() == 0


async def test_buffered_get_stats_flushes_first():
    store = BufferedSqliteStore(":memory:", flush_interval=60.0)
    await store.initialize()
    try:
        await store.store(make_telegram(1))
        assert await store.count() == 0  # still buffered

        stats = await store.get_stats()
        assert stats.telegram_count == 1  # get_stats flushed the buffer
        assert len(store._buffer) == 0
    finally:
        await store.close()


async def test_capability_flags(store):
    caps = store.capabilities
    if hasattr(store, "engine"):
        assert caps.supports_size_stats is True
        assert caps.supports_optimize is True
    else:
        assert caps.supports_size_stats is False
        assert caps.supports_optimize is False


def test_postgres_does_not_advertise_optimize():
    # Plain VACUUM doesn't shrink Postgres files, so the capability must be
    # off — UIs use it to decide whether to offer a "reclaim space" action.
    # Constructing the store is lazy; no server needed.
    from knx_telegram_store.backends.postgres import PostgresStore

    store = PostgresStore("postgresql+asyncpg://user:pw@localhost:5432/db")
    assert store.capabilities.supports_optimize is False
    assert store.capabilities.supports_size_stats is True
