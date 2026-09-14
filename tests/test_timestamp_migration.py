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


async def test_initialize_converts_automatically_when_told_the_zone(tmp_path):
    """The host names the zone once; the conversion then runs like any migration."""
    path = tmp_path / "auto.db"
    await _legacy_db(path, ["2026-01-15 12:00:00.000000", "2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    await store.initialize()

    assert await store.needs_timestamp_migration() is False
    stamps = sorted(t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams)
    assert stamps[0] == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
    assert stamps[1] == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    await store.close()


async def test_automatic_conversion_includes_recent_legacy_rows_east_of_utc(tmp_path):
    """The UTC cutoff must not exclude a legacy row's local wall clock."""
    path = tmp_path / "recent-legacy.db"
    legacy_timestamp = datetime.now(BERLIN).replace(microsecond=123456)
    await _legacy_db(path, [legacy_timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")])

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    await store.initialize()

    (telegram,) = (await store.query(TelegramQuery(limit=1))).telegrams
    assert telegram.timestamp == legacy_timestamp.astimezone(UTC)
    await store.close()


async def test_automatic_conversion_runs_once_across_restarts(tmp_path):
    """Re-opening the store must not shift the same rows again."""
    path = tmp_path / "restart.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    for _ in range(3):
        store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
        await store.initialize()
        stamps = [t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams]
        await store.close()

    assert stamps == [datetime(2026, 7, 15, 10, 0, tzinfo=UTC)]


async def test_a_utc_writer_declares_itself_and_nothing_moves(tmp_path):
    """SpectrumKNX already stored datetime.now(UTC); its rows must not move."""
    path = tmp_path / "already-utc.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path), legacy_timestamp_timezone=UTC)
    await store.initialize()

    assert await store.needs_timestamp_migration() is False
    stamps = [t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams]
    assert stamps == [datetime(2026, 7, 15, 12, 0, tzinfo=UTC)], "declaring UTC must be a no-op"
    await store.close()


async def test_without_a_zone_nothing_is_guessed(tmp_path):
    """A host that says nothing keeps its rows untouched, whatever the system zone."""
    path = tmp_path / "unset.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path))
    await store.initialize()

    assert await store.needs_timestamp_migration() is True
    raw = sqlite3.connect(str(path)).execute("SELECT timestamp FROM telegrams").fetchone()[0]
    assert raw.startswith("2026-07-15 12:00:00")
    await store.close()


async def test_the_assumed_zone_is_recorded(tmp_path):
    """A conversion run with the wrong zone should be recognisable afterwards."""
    path = tmp_path / "recorded.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    await store.initialize()
    await store.close()

    recorded = (
        sqlite3.connect(str(path))
        .execute("SELECT value FROM store_metadata WHERE key='timestamps_utc_source_zone'")
        .fetchone()
    )
    assert recorded[0] == "Europe/Berlin"


# ── Review findings on PR #40 (philippwaller) ────────────────────────────────


async def test_microseconds_survive_the_conversion(tmp_path):
    """SQLite's datetime() drops the fraction, which would reorder a busy second."""
    path = tmp_path / "micro.db"
    await _legacy_db(path, ["2026-01-15 12:00:00.123456", "2026-07-15 12:00:00.654321"])

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    await store.initialize()
    await store.close()

    raw = [r[0] for r in sqlite3.connect(str(path)).execute("SELECT timestamp FROM telegrams ORDER BY timestamp")]
    assert raw == ["2026-01-15 11:00:00.123456", "2026-07-15 10:00:00.654321"]


async def test_west_of_utc_rows_are_shifted_exactly_once(tmp_path):
    """Shifting forward can push a row into a later interval's range.

    With one statement per interval the row would then be converted a second
    time, so a whole table has to be rewritten in a single pass.
    """
    path = tmp_path / "west.db"
    new_york = ZoneInfo("America/New_York")
    await _legacy_db(path, ["2026-03-08 01:00:00.000000", "2026-03-09 12:00:00.000000"])

    store = SqliteStore(str(path), legacy_timestamp_timezone=new_york)
    await store.initialize()

    stamps = sorted(t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams)
    assert stamps[0] == datetime(2026, 3, 8, 1, 0, tzinfo=new_york).astimezone(UTC), "EST row, shifted once"
    assert stamps[1] == datetime(2026, 3, 9, 12, 0, tzinfo=new_york).astimezone(UTC), "EDT row, shifted once"
    await store.close()


async def test_last_values_older_than_any_telegram_are_converted(tmp_path):
    """last_ga_telegrams escapes retention, so it can outlive the telegrams table."""
    path = tmp_path / "stale-last.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    con = sqlite3.connect(str(path))
    ids = {row[0]: row[1] for row in con.execute("SELECT category, id FROM string_lookup")}
    con.execute(
        "INSERT INTO last_ga_telegrams (timestamp, source_id, destination_id, telegramtype_id, direction_id) "
        "VALUES (?,?,?,?,?)",
        ("2026-01-15 12:00:00.000000", ids["source"], ids["destination"], ids["telegramtype"], ids["direction"]),
    )
    con.commit()
    con.close()

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    await store.initialize()
    await store.close()

    stored = sqlite3.connect(str(path)).execute("SELECT timestamp FROM last_ga_telegrams").fetchone()[0]
    assert stored.startswith("2026-01-15 11:00:00"), "outside the telegrams range, but still ours to convert"


async def test_last_values_are_converted_when_telegrams_is_empty(tmp_path):
    """Retention can empty telegrams entirely while last values remain."""
    path = tmp_path / "only-last.db"
    await _legacy_db(path, ["2026-07-15 12:00:00.000000"])

    con = sqlite3.connect(str(path))
    ids = {row[0]: row[1] for row in con.execute("SELECT category, id FROM string_lookup")}
    con.execute(
        "INSERT INTO last_ga_telegrams (timestamp, source_id, destination_id, telegramtype_id, direction_id) "
        "VALUES (?,?,?,?,?)",
        ("2026-07-15 12:00:00.000000", ids["source"], ids["destination"], ids["telegramtype"], ids["direction"]),
    )
    con.execute("DELETE FROM telegrams")
    con.commit()
    con.close()

    store = SqliteStore(str(path), legacy_timestamp_timezone=BERLIN)
    await store.initialize()
    await store.close()

    stored = sqlite3.connect(str(path)).execute("SELECT timestamp FROM last_ga_telegrams").fetchone()[0]
    assert stored.startswith("2026-07-15 10:00:00"), "an empty telegrams table is not an empty database"


async def test_transitions_that_do_not_fall_on_the_hour(tmp_path):
    """Pacific/Chatham moves at 03:45; rounding to 04:00 misconverts the gap."""
    path = tmp_path / "chatham.db"
    chatham = ZoneInfo("Pacific/Chatham")
    await _legacy_db(path, ["2026-04-05 03:30:00.000000", "2026-04-05 03:50:00.000000"])

    store = SqliteStore(str(path), legacy_timestamp_timezone=chatham)
    await store.initialize()

    stamps = sorted(t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams)
    assert stamps[0] == datetime(2026, 4, 5, 3, 30, tzinfo=chatham).astimezone(UTC), "before the 03:45 change"
    assert stamps[1] == datetime(2026, 4, 5, 3, 50, tzinfo=chatham).astimezone(UTC), "after it"
    await store.close()
