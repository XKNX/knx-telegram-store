"""Converting pre-UTC timestamps in an existing SQLite database.

Databases written before timestamps were normalised hold local wall-clock digits
with no offset, so reading them as UTC shifts them by whatever offset wrote them.
The conversion needs the writer's timezone, which this library cannot know — with
Home Assistant it is HA's *configured* zone, not necessarily the host's — so the
caller supplies it (XKNX/knx-frontend#459).
"""

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore

BERLIN = ZoneInfo("Europe/Berlin")  # +01:00 winter, +02:00 summer


async def _legacy_db(path, wall_clock_stamps: list[str]) -> None:
    """Create a database holding rows as the pre-UTC code wrote them."""
    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()

    con = sqlite3.connect(str(path))
    con.execute(
        "INSERT INTO string_lookup (category, value) VALUES "
        "('source','1.1.1'),('destination','1/1/1'),"
        "('telegramtype','GroupValueWrite'),('direction','Incoming')"
    )
    ids = {row[0]: row[1] for row in con.execute("SELECT category, id FROM string_lookup")}
    for stamp in wall_clock_stamps:
        con.execute(
            "INSERT INTO telegrams (timestamp, source_id, destination_id, telegramtype_id, direction_id) "
            "VALUES (?,?,?,?,?)",
            (stamp, ids["source"], ids["destination"], ids["telegramtype"], ids["direction"]),
        )
    # Undo the flag initialize() set, so the file looks like it predates the fix.
    con.execute("DELETE FROM store_metadata WHERE key='timestamps_utc'")
    con.commit()
    con.close()


async def test_conversion_uses_the_offset_in_force_at_each_row(tmp_path):
    """A single fixed offset would be wrong for half the year."""
    path = tmp_path / "dst.db"
    await _legacy_db(path, ["2026-01-15 12:00:00.000000", "2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path))
    await store.initialize()
    assert await store.needs_timestamp_migration() is True
    assert await store.migrate_timestamps_to_utc(BERLIN) == 2

    stamps = sorted(t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams)
    assert stamps[0] == datetime(2026, 1, 15, 11, 0, tzinfo=UTC), "winter row should shift by 1h (CET)"
    assert stamps[1] == datetime(2026, 7, 15, 10, 0, tzinfo=UTC), "summer row should shift by 2h (CEST)"
    await store.close()


async def test_rows_written_after_the_upgrade_are_not_shifted_again(tmp_path):
    """The dangerous case: a store that ran before anyone migrated.

    Rows written since the upgrade are already UTC. Shifting them a second time
    would corrupt data that was correct.
    """
    path = tmp_path / "mixed.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path))
    await store.initialize()  # records the boundary
    fresh = datetime.now(UTC)
    await store.store_many(
        [
            StoredTelegram(
                timestamp=fresh,
                source="1.1.2",
                destination="1/1/2",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                value=1.0,
            )
        ]
    )

    assert await store.migrate_timestamps_to_utc(BERLIN) == 1, "only the legacy row should be converted"

    stamps = {t.source: t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams}
    assert stamps["1.1.1"] == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert stamps["1.1.2"] == fresh, "an already-UTC row was shifted"
    await store.close()


async def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path))
    await store.initialize()
    assert await store.migrate_timestamps_to_utc(BERLIN) == 1
    assert await store.needs_timestamp_migration() is False
    assert await store.migrate_timestamps_to_utc(BERLIN) == 0, "a second run must be a no-op"

    (only,) = (await store.query(TelegramQuery(limit=10))).telegrams
    assert only.timestamp == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    await store.close()


async def test_a_fresh_database_needs_no_migration(tmp_path):
    store = SqliteStore(str(tmp_path / "new.db"))
    await store.initialize()
    assert await store.needs_timestamp_migration() is False
    assert await store.migrate_timestamps_to_utc(BERLIN) == 0
    await store.close()


async def test_an_empty_pre_existing_database_needs_no_migration(tmp_path):
    """Nothing to convert, so it should not nag the caller."""
    path = tmp_path / "empty.db"
    await _legacy_db(path, [])

    store = SqliteStore(str(path))
    await store.initialize()
    assert await store.needs_timestamp_migration() is False
    await store.close()


async def test_utc_source_timezone_leaves_values_alone(tmp_path):
    """A caller that was already writing UTC just needs the flag set."""
    path = tmp_path / "utc.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path))
    await store.initialize()
    await store.migrate_timestamps_to_utc(UTC)

    (only,) = (await store.query(TelegramQuery(limit=10))).telegrams
    assert only.timestamp == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert await store.needs_timestamp_migration() is False
    await store.close()


async def test_last_values_are_converted_too(tmp_path):
    """last_ga_telegrams carries its own copy of the timestamp."""
    path = tmp_path / "lastvals.db"
    store = SqliteStore(str(path))
    await store.initialize()
    await store.store_many(
        [
            StoredTelegram(
                timestamp=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
                source="1.1.1",
                destination="1/1/1",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                value=1.0,
            )
        ]
    )
    await store.close()

    # Rewrite both tables to local wall clock and clear the flag.
    con = sqlite3.connect(str(path))
    con.execute("UPDATE telegrams SET timestamp = '2026-07-15 12:00:00.000000'")
    con.execute("UPDATE last_ga_telegrams SET timestamp = '2026-07-15 12:00:00.000000'")
    con.execute("DELETE FROM store_metadata WHERE key='timestamps_utc'")
    con.commit()
    con.close()

    store = SqliteStore(str(path))
    await store.initialize()
    await store.migrate_timestamps_to_utc(BERLIN)
    (last,) = await store.get_last_unique_telegrams()
    assert last.timestamp == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    await store.close()


@pytest.mark.parametrize("zone", ["UTC", "Europe/Berlin", "America/New_York", "Asia/Kolkata"])
async def test_round_trip_through_migration_for_several_zones(tmp_path, zone):
    """Including a half-hour offset, which a naive hours-only shift would miss."""
    tz = ZoneInfo(zone)
    local_noon = datetime(2026, 7, 15, 12, 0, tzinfo=tz)
    path = tmp_path / f"{zone.replace('/', '_')}.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path))
    await store.initialize()
    await store.migrate_timestamps_to_utc(tz)

    (only,) = (await store.query(TelegramQuery(limit=10))).telegrams
    assert only.timestamp == local_noon.astimezone(UTC)
    await store.close()


async def test_read_only_store_refuses_to_migrate(tmp_path):
    path = tmp_path / "ro.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path), read_only=True)
    await store.initialize()
    with pytest.raises(Exception):  # noqa: B017 - the store's own error type
        await store.migrate_timestamps_to_utc(BERLIN)
    await store.close()
