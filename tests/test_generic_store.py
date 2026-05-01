from datetime import UTC, datetime, timedelta

import pytest

from knx_telegram_store import StoredTelegram, TelegramQuery


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
            dpt_main=9
        ),
        StoredTelegram(
            timestamp=now - timedelta(minutes=4),
            source="1.1.2",
            destination="1/1/1",
            telegramtype="GroupValueWrite",
            direction="Incoming",
            value=21.0,
            dpt_main=9
        ),
        StoredTelegram(
            timestamp=now - timedelta(minutes=3),
            source="1.1.1",
            destination="1/1/2",
            telegramtype="GroupValueRead",
            direction="Outgoing",
            value=None,
            dpt_main=1
        ),
        StoredTelegram(
            timestamp=now - timedelta(minutes=2),
            source="1.1.3",
            destination="1/1/1",
            telegramtype="GroupValueResponse",
            direction="Incoming",
            value=22.5,
            dpt_main=9
        ),
    ]

@pytest.mark.asyncio
async def test_store_and_count(store, sample_telegrams):
    await store.store(sample_telegrams[0])
    assert await store.count() == 1
    
    await store.store_many(sample_telegrams[1:])
    assert await store.count() == 4

@pytest.mark.asyncio
async def test_query_all(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    result = await store.query(TelegramQuery())
    assert len(result.telegrams) == 4
    assert result.total_count == 4
    # Default order is descending
    assert result.telegrams[0].timestamp > result.telegrams[-1].timestamp

@pytest.mark.asyncio
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

@pytest.mark.asyncio
async def test_query_time_range(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    now = datetime.now(UTC)
    
    # Start time (now - 3.5 mins) should include t2 (now-3) and t3 (now-2)
    result = await store.query(TelegramQuery(start_time=now - timedelta(minutes=3.5)))
    assert len(result.telegrams) == 2
    
    # End time (now - 3.5 mins) should include t0 (now-5) and t1 (now-4)
    result = await store.query(TelegramQuery(end_time=now - timedelta(minutes=3.5)))
    assert len(result.telegrams) == 2

@pytest.mark.asyncio
async def test_query_time_delta(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    
    # Find the Read telegram (3 mins ago) and everything within 1.5 mins before/after
    # This should include the telegram 4 mins ago (t1) and 2 mins ago (t3).
    query = TelegramQuery(
        telegram_types=["GroupValueRead"],
        delta_before_ms=90000, # 1.5 mins
        delta_after_ms=90000   # 1.5 mins
    )
    result = await store.query(query)
    # Pivot is t2 (3 mins ago). 
    # t1 (4 mins ago) is 1 min before (included)
    # t3 (2 mins ago) is 1 min after (included)
    # t0 (5 mins ago) is 2 mins before (excluded)
    assert len(result.telegrams) == 3

@pytest.mark.asyncio
async def test_pagination(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    
    result = await store.query(TelegramQuery(limit=2, offset=0))
    assert len(result.telegrams) == 2
    assert result.limit_reached is True
    
    result = await store.query(TelegramQuery(limit=2, offset=2))
    assert len(result.telegrams) == 2
    assert result.limit_reached is False

@pytest.mark.asyncio
async def test_clear(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    assert await store.count() == 4
    await store.clear()
    assert await store.count() == 0
