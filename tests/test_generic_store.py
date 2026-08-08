from datetime import UTC, datetime, timedelta

import pytest

from knx_telegram_store import KnxTelegramStoreException, StoredTelegram, TelegramQuery
from knx_telegram_store.store import wrap_store_errors


@pytest.fixture
def sample_telegrams():
    now = datetime.now(UTC)
    return [
        StoredTelegram(
            timestamp=now - timedelta(minutes=5),
            source="1.1.1",
            destination="1/1/1",
            telegramtype="GroupValueWrite",
            direction="Incoming",
            value=20.0,
            dpt_main=9,
        ),
        StoredTelegram(
            timestamp=now - timedelta(minutes=4),
            source="1.1.2",
            destination="1/1/1",
            telegramtype="GroupValueWrite",
            direction="Incoming",
            value=21.0,
            dpt_main=9,
        ),
        StoredTelegram(
            timestamp=now - timedelta(minutes=3),
            source="1.1.1",
            destination="1/1/2",
            telegramtype="GroupValueRead",
            direction="Outgoing",
            value=None,
            dpt_main=1,
        ),
        StoredTelegram(
            timestamp=now - timedelta(minutes=2),
            source="1.1.3",
            destination="1/1/1",
            telegramtype="GroupValueResponse",
            direction="Incoming",
            value=22.5,
            dpt_main=9,
        ),
    ]


async def test_store_and_count(store, sample_telegrams):
    await store.store(sample_telegrams[0])
    assert await store.count() == 1

    await store.store_many(sample_telegrams[1:])
    assert await store.count() == 4


async def test_query_all(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    result = await store.query(TelegramQuery())
    assert len(result.telegrams) == 4
    assert result.total_count == 4
    # Default order is descending
    assert result.telegrams[0].timestamp > result.telegrams[-1].timestamp


async def test_query_filters(store, sample_telegrams):
    await store.store_many(sample_telegrams)

    # Filter by destination
    result = await store.query(TelegramQuery(destinations=["1/1/1"]))
    assert len(result.telegrams) == 3

    # Filter by source
    result = await store.query(TelegramQuery(sources=["1.1.1"]))
    assert len(result.telegrams) == 2

    # Filter by type
    result = await store.query(TelegramQuery(telegram_types=["GroupValueRead"]))
    assert len(result.telegrams) == 1

    # Combined filter
    result = await store.query(TelegramQuery(destinations=["1/1/1"], dpt_mains=[9]))
    assert len(result.telegrams) == 3


async def test_query_dpt_pairs(store):
    now = datetime.now(UTC)

    def telegram(minutes_ago: int, dpt_main: int | None, dpt_sub: int | None) -> StoredTelegram:
        return StoredTelegram(
            timestamp=now - timedelta(minutes=minutes_ago),
            source="1.1.1",
            destination="1/1/1",
            telegramtype="GroupValueWrite",
            direction="Incoming",
            dpt_main=dpt_main,
            dpt_sub=dpt_sub,
        )

    await store.store_many(
        [
            telegram(5, 1, 1),
            telegram(4, 1, 8),
            telegram(3, 5, 1),
            telegram(2, 9, None),
            telegram(1, None, None),
        ]
    )

    # Exact (main, sub) pair
    result = await store.query(TelegramQuery(dpts=[(1, 1)]))
    assert [(t.dpt_main, t.dpt_sub) for t in result.telegrams] == [(1, 1)]

    # sub=None matches any subtype of that main, including sub NULL
    result = await store.query(TelegramQuery(dpts=[(1, None)]))
    assert len(result.telegrams) == 2
    result = await store.query(TelegramQuery(dpts=[(9, None)]))
    assert [(t.dpt_main, t.dpt_sub) for t in result.telegrams] == [(9, None)]

    # OR within the DPT group: pairs together, and pairs with dpt_mains
    result = await store.query(TelegramQuery(dpts=[(1, 1), (5, 1)]))
    assert len(result.telegrams) == 2
    result = await store.query(TelegramQuery(dpts=[(1, 8)], dpt_mains=[9]))
    assert len(result.telegrams) == 2

    # No match
    result = await store.query(TelegramQuery(dpts=[(1, 2)]))
    assert result.telegrams == []


async def test_query_time_range(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    now = datetime.now(UTC)

    # Start time (now - 3.5 mins) should include t2 (now-3) and t3 (now-2)
    result = await store.query(TelegramQuery(start_time=now - timedelta(minutes=3.5)))
    assert len(result.telegrams) == 2

    # End time (now - 3.5 mins) should include t0 (now-5) and t1 (now-4)
    result = await store.query(TelegramQuery(end_time=now - timedelta(minutes=3.5)))
    assert len(result.telegrams) == 2


async def test_query_time_delta(store, sample_telegrams):
    await store.store_many(sample_telegrams)

    # Find the Read telegram (3 mins ago) and everything within 1.5 mins before/after
    # This should include the telegram 4 mins ago (t1) and 2 mins ago (t3).
    query = TelegramQuery(
        telegram_types=["GroupValueRead"],
        delta_before_ms=90000,  # 1.5 mins
        delta_after_ms=90000,  # 1.5 mins
    )
    result = await store.query(query)
    # Pivot is t2 (3 mins ago).
    # t1 (4 mins ago) is 1 min before (included)
    # t3 (2 mins ago) is 1 min after (included)
    # t0 (5 mins ago) is 2 mins before (excluded)
    assert len(result.telegrams) == 3


async def test_pagination(store, sample_telegrams):
    await store.store_many(sample_telegrams)

    result = await store.query(TelegramQuery(limit=2, offset=0))
    assert len(result.telegrams) == 2
    assert result.limit_reached is True

    result = await store.query(TelegramQuery(limit=2, offset=2))
    assert len(result.telegrams) == 2
    assert result.limit_reached is False


async def test_clear(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    assert await store.count() == 4
    await store.clear()
    assert await store.count() == 0


async def test_eviction(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    assert await store.count() == 4

    # evict everything older than 3.5 minutes
    # sample_telegrams: now-5, now-4, now-3, now-2
    # expected to delete 2 telegrams (now-5 and now-4)
    cutoff = datetime.now(UTC) - timedelta(minutes=3.5)

    # dry run first
    count = await store.evict_older_than(cutoff, dry_run=True)
    if store.capabilities.max_storage is not None:
        # memory store doesn't support eviction, should return 0 as implemented
        assert count == 0
    else:
        assert count == 2
        assert await store.count() == 4

    # actual eviction
    count = await store.evict_older_than(cutoff)
    if store.capabilities.max_storage is not None:
        assert count == 0
        assert await store.count() == 4
    else:
        assert count == 2
        assert await store.count() == 2


async def test_evict_expired(store, sample_telegrams):
    await store.store_many(sample_telegrams)

    # SqliteStore in conftest has retention_days=10
    # Everything in sample_telegrams is only minutes old, so nothing should be evicted
    count = await store.evict_expired()
    assert count == 0
    assert await store.count() == 4


async def test_store_empty(store):
    """Test storing an empty list of telegrams."""
    await store.store_many([])
    assert await store.count() == 0


async def test_query_directions(store, sample_telegrams):
    """Test filtering by direction."""
    await store.store_many(sample_telegrams)
    result = await store.query(TelegramQuery(directions=["Incoming"]))
    assert len(result.telegrams) == 3
    result = await store.query(TelegramQuery(directions=["Outgoing"]))
    assert len(result.telegrams) == 1


async def test_query_order(store, sample_telegrams):
    """Test query ordering."""
    await store.store_many(sample_telegrams)
    # Default is descending
    result_desc = await store.query(TelegramQuery())
    # Ascending
    result_asc = await store.query(TelegramQuery(order_descending=False))
    assert result_desc.telegrams[0].timestamp > result_asc.telegrams[0].timestamp
    assert result_desc.telegrams[0].timestamp == result_asc.telegrams[-1].timestamp


async def test_evict_no_retention(store):
    """Test eviction when no retention is configured."""
    # Memory store has no retention_days configured in conftest
    if store.capabilities.max_storage is not None:
        assert await store.evict_expired() == 0


async def test_get_last_unique_telegrams(store, sample_telegrams):
    """Test get_last_unique_telegrams method."""
    await store.store_many(sample_telegrams)
    result = await store.get_last_unique_telegrams()

    # destinations: "1/1/1" has 3 telegrams, "1/1/2" has 1 telegram.
    # The newest for "1/1/1" is the one with value=22.5 (sample_telegrams[3]).
    # The newest for "1/1/2" is sample_telegrams[2] (value=None).
    assert len(result) == 2

    dest_map = {t.destination: t for t in result}
    assert "1/1/1" in dest_map
    assert "1/1/2" in dest_map

    assert dest_map["1/1/1"].value == 22.5
    assert dest_map["1/1/2"].value is None


async def test_exception_wrapping(store):
    """Test that underlying database/engine exceptions are wrapped in KnxTelegramStoreException."""
    from unittest.mock import patch

    from knx_telegram_store import KnxTelegramStoreException

    # We mock an internal operation to throw a DB API or raw ValueError/AttributeError
    # which is not already a KnxTelegramStoreException.
    if hasattr(store, "engine"):
        # For SQL stores
        with patch.object(store._lookup_cache, "get_or_create_ids", side_effect=ValueError("Simulated DB Crash")):
            with pytest.raises(KnxTelegramStoreException) as exc_info:
                await store.store(
                    StoredTelegram(
                        timestamp=datetime.now(UTC),
                        source="1.1.1",
                        destination="1/1/1",
                        telegramtype="GroupValueWrite",
                        direction="Incoming",
                        value=20.0,
                    )
                )
            assert "Simulated DB Crash" in str(exc_info.value)
    else:
        # For MemoryStore
        with patch.object(store, "_telegrams", new=None):
            with pytest.raises(KnxTelegramStoreException) as exc_info:
                await store.store(
                    StoredTelegram(
                        timestamp=datetime.now(UTC),
                        source="1.1.1",
                        destination="1/1/1",
                        telegramtype="GroupValueWrite",
                        direction="Incoming",
                        value=20.0,
                    )
                )
            # AttributeError should be raised and wrapped
            assert "Database error during store" in str(exc_info.value)


async def test_wrap_store_errors():
    """Test that the wrap_store_errors decorator behaves correctly."""

    @wrap_store_errors
    async def success_func():
        return "success"

    @wrap_store_errors
    async def store_exception_func():
        raise KnxTelegramStoreException("already wrapped")

    @wrap_store_errors
    async def other_exception_func():
        raise ValueError("some other error")

    # 1. Success case
    assert await success_func() == "success"

    # 2. Preserves existing KnxTelegramStoreException
    with pytest.raises(KnxTelegramStoreException, match="already wrapped"):
        await store_exception_func()

    # 3. Wraps other exceptions
    with pytest.raises(KnxTelegramStoreException, match="Database error during other_exception_func: some other error"):
        await other_exception_func()
