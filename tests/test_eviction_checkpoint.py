from datetime import UTC, datetime, timedelta

from knx_telegram_store import StoredTelegram
from knx_telegram_store.backends.sqlite import SqliteStore


def make_telegram(days_ago: int) -> StoredTelegram:
    return StoredTelegram(
        timestamp=datetime.now(UTC) - timedelta(days=days_ago),
        source="1.1.1",
        destination="1/1/1",
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=20.0,
        dpt_main=9,
    )


async def test_eviction_truncates_the_wal(tmp_path):
    db_path = tmp_path / "telegrams.db"
    wal_path = tmp_path / "telegrams.db-wal"
    store = SqliteStore(db_path)
    await store.initialize()
    try:
        await store.store_many([make_telegram(3)] * 2000 + [make_telegram(0)])
        assert wal_path.stat().st_size > 0

        assert await store.evict_older_than(datetime.now(UTC) - timedelta(days=1)) == 2000

        assert wal_path.stat().st_size == 0
        assert await store.count() == 1
    finally:
        await store.close()


async def test_eviction_without_deletions_leaves_the_wal_alone(tmp_path):
    db_path = tmp_path / "telegrams.db"
    wal_path = tmp_path / "telegrams.db-wal"
    store = SqliteStore(db_path)
    await store.initialize()
    try:
        await store.store_many([make_telegram(0)])
        wal_size = wal_path.stat().st_size
        assert wal_size > 0

        assert await store.evict_older_than(datetime.now(UTC) - timedelta(days=1)) == 0
        assert await store.evict_older_than(datetime.now(UTC), dry_run=True) == 1

        # the statistics refresh may add frames, but nothing truncates the file
        assert wal_path.stat().st_size >= wal_size
    finally:
        await store.close()
