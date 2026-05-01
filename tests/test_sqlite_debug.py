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
async def test_debug_sqlite_times(store, sample_telegrams):
    await store.store_many(sample_telegrams)
    result = await store.query(TelegramQuery())
    print("\nDEBUG TIMESTAMPS:")
    for t in result.telegrams:
        print(f"  {t.timestamp} ({t.telegramtype})")
    
    # Check if we can find the pivot
    pivot_query = TelegramQuery(telegram_types=["GroupValueRead"])
    pivot_result = await store.query(pivot_query)
    print(f"PIVOT COUNT: {len(pivot_result.telegrams)}")
    assert len(pivot_result.telegrams) == 1
    
    # Try the delta query
    delta_query = TelegramQuery(
        telegram_types=["GroupValueRead"],
        delta_before_ms=90000,
        delta_after_ms=90000
    )
    delta_result = await store.query(delta_query)
    print(f"DELTA COUNT: {len(delta_result.telegrams)}")
