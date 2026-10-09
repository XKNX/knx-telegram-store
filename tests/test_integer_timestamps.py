"""SQLite stores timestamps as integer microseconds (XKNX/knx-telegram-store#78).

SQLAlchemy's default SQLite datetime is a 26-character string, which costs about
a quarter of the file once the timestamp index is counted in. The column is now
a BIGINT of microseconds since the Unix epoch, UTC. Microseconds rather than
milliseconds because that is the precision the strings carried, so an existing
database converts without loss. PostgreSQL keeps ``timestamptz``.
"""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.base_sql import datetime_to_micros, micros_to_datetime
from knx_telegram_store.backends.sqlite import _TEXT_TO_MICROS, SqliteStore

PLUS_TWO = timezone(timedelta(hours=2))
BASE = datetime(2026, 9, 28, 1, 0, 1, 667641, tzinfo=UTC)
TEXT_STAMPS = ["2026-01-15 12:00:00.123456", "2026-07-15 12:00:00.654321"]


def _telegram(ts: datetime, source: str = "1.1.1", destination: str = "1/1/1") -> StoredTelegram:
    return StoredTelegram(
        timestamp=ts,
        source=source,
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=1.0,
    )


def _raw(path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _flag_set(path: Path, key: str) -> bool:
    return _raw(path, f"SELECT value FROM store_metadata WHERE key = '{key}'") == [("true",)]  # noqa: S608


async def _pre_0_15_db(path: Path, telegrams: list[str], last_values: list[str]) -> None:
    """A database as 0.14 left it: DATETIME columns holding SQLAlchemy's text format.

    The schema is the current one with the timestamp columns typed as the old
    code declared them, so the NUMERIC affinity of a real old file is covered,
    and every other one-off pass is already flagged done.
    """
    template = path.with_name("template.db")
    store = SqliteStore(str(template))
    await store.initialize()
    await store.close()
    # Not the sqlite_* internals (sqlite_stat1 once the store is analysed): they
    # cannot be created by hand and belong to the engine, not the schema.
    ddl = [
        sql
        for (sql,) in _raw(template, "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
    ]

    con = sqlite3.connect(str(path))
    for statement in ddl:
        con.execute(statement.replace("timestamp BIGINT", "timestamp DATETIME"))
    con.execute(
        "INSERT INTO store_metadata (key, value) VALUES "
        "('nulls_recovered','true'),('data_unwrapped','true'),"
        "('timestamps_utc','true'),('last_ga_newest_reconciled','true')"
    )
    con.execute(
        "INSERT INTO string_lookup (category, value) VALUES "
        "('source','1.1.1'),('destination','1/1/1'),"
        "('telegramtype','GroupValueWrite'),('direction','Incoming')"
    )
    ids = {row[0]: row[1] for row in con.execute("SELECT category, id FROM string_lookup")}
    for stamp in telegrams:
        con.execute(
            "INSERT INTO telegrams (timestamp, source_id, destination_id, telegramtype_id, direction_id) "
            "VALUES (?,?,?,?,?)",
            (stamp, ids["source"], ids["destination"], ids["telegramtype"], ids["direction"]),
        )
    for stamp in last_values:
        con.execute(
            "INSERT INTO last_ga_telegrams (destination_id, timestamp, source_id, telegramtype_id, direction_id) "
            "VALUES (?,?,?,?,?)",
            (ids["destination"], stamp, ids["source"], ids["telegramtype"], ids["direction"]),
        )
    con.commit()
    con.close()


async def test_round_trip_is_exact_and_stored_as_an_integer(tmp_path: Path) -> None:
    """Microsecond precision survives, and the file holds the microsecond count."""
    path = tmp_path / "ints.db"
    original = datetime(2026, 9, 28, 3, 0, 1, 667641, tzinfo=PLUS_TWO)  # 01:00:01.667641 UTC
    store = SqliteStore(str(path))
    await store.initialize()
    await store.store_many([_telegram(original)])

    (stored,) = (await store.query(TelegramQuery(limit=1))).telegrams
    assert stored.timestamp == original
    assert stored.timestamp.tzinfo is UTC
    await store.close()

    expected = ((original - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1),)
    assert _raw(path, "SELECT timestamp FROM telegrams") == [expected]
    assert _raw(path, "SELECT timestamp FROM last_ga_telegrams") == [expected]
    assert _raw(path, "SELECT typeof(timestamp) FROM telegrams") == [("integer",)]


@pytest.mark.parametrize(
    ("text_value", "expected"),
    [
        pytest.param("2026-09-28 01:00:01.667641", BASE, id="six-digit-fraction"),
        pytest.param("2026-09-28 01:00:01", BASE.replace(microsecond=0), id="no-fraction"),
        pytest.param("2026-09-28T01:00:01.667641", BASE, id="T-separator"),
        pytest.param("2026-09-28 01:00:01.5", BASE.replace(microsecond=500000), id="short-fraction"),
        pytest.param("2026-09-28 01:00:01.999641", BASE.replace(microsecond=999641), id="fraction-strftime-rounds-up"),
        pytest.param(
            "1969-12-31 23:59:59.999999", datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), id="pre-epoch"
        ),
    ],
)
def test_conversion_expression_is_lossless(text_value: str, expected: datetime) -> None:
    """The SQL rewrite must reproduce the Python conversion to the microsecond.

    julianday() would be the obvious one-liner, but it is a double and loses
    about 40 µs, which would reorder a busy second. And strftime('%s') on the
    full string rounds .9995 and above into the next second, because SQLite
    keeps milliseconds internally.
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE telegrams (timestamp DATETIME NOT NULL)")
    con.execute("INSERT INTO telegrams VALUES (?)", (text_value,))
    con.execute(_TEXT_TO_MICROS.format(table="telegrams"))
    (stored,) = con.execute("SELECT timestamp FROM telegrams").fetchone()

    assert stored == datetime_to_micros(expected)
    assert micros_to_datetime(stored) == expected


async def test_an_existing_text_database_is_converted_on_first_start(tmp_path: Path, monkeypatch) -> None:
    """Both tables are rewritten, exactly, with the index back and the file flagged.

    Home Assistant runs initialize() under a 10 s timeout unless
    needs_migration() says otherwise, so the rewrite has to be announced there.
    """
    path = tmp_path / "old.db"
    await _pre_0_15_db(path, TEXT_STAMPS, [TEXT_STAMPS[1]])
    vacuums: list[str] = []
    original_optimize = SqliteStore.optimize

    async def counting_optimize(self: SqliteStore) -> None:
        vacuums.append("VACUUM")
        await original_optimize(self)

    monkeypatch.setattr(SqliteStore, "optimize", counting_optimize)

    store = SqliteStore(str(path))
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False
    assert vacuums == ["VACUUM"]

    stamps = [t.timestamp for t in (await store.query(TelegramQuery(limit=10, order_descending=False))).telegrams]
    assert stamps == [datetime.fromisoformat(s).replace(tzinfo=UTC) for s in TEXT_STAMPS]
    (last,) = await store.get_last_unique_telegrams()
    assert last.timestamp == datetime.fromisoformat(TEXT_STAMPS[1]).replace(tzinfo=UTC)
    await store.close()

    assert _flag_set(path, "timestamps_integer")
    assert _raw(path, "SELECT DISTINCT typeof(timestamp) FROM telegrams") == [("integer",)]
    assert _raw(path, "SELECT DISTINCT typeof(timestamp) FROM last_ga_telegrams") == [("integer",)]
    assert _raw(path, "SELECT count(*) FROM sqlite_master WHERE name = 'ix_telegrams_timestamp'") == [(1,)]
    assert "timestamp DATETIME" in _raw(path, "SELECT sql FROM sqlite_master WHERE name = 'telegrams'")[0][0], (
        "the table must not have been rebuilt"
    )

    # A second start finds the flag and does nothing, VACUUM included.
    store = SqliteStore(str(path))
    await store.initialize()
    assert vacuums == ["VACUUM"]
    await store.close()


async def test_a_fresh_database_is_flagged_without_a_vacuum(tmp_path: Path, monkeypatch) -> None:
    vacuums: list[str] = []

    async def counting_optimize(self: SqliteStore) -> None:
        vacuums.append("VACUUM")

    monkeypatch.setattr(SqliteStore, "optimize", counting_optimize)
    path = tmp_path / "fresh.db"
    store = SqliteStore(str(path))
    await store.initialize()
    await store.close()

    assert vacuums == []
    assert _flag_set(path, "timestamps_integer")


async def test_a_read_only_store_leaves_an_unconverted_file_alone(tmp_path: Path) -> None:
    """It reports the pending conversion but never runs it, and still reads datetimes."""
    path = tmp_path / "old.db"
    await _pre_0_15_db(path, TEXT_STAMPS, [TEXT_STAMPS[1]])

    store = SqliteStore(str(path), read_only=True)
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is True

    stamps = sorted(t.timestamp for t in (await store.query(TelegramQuery(limit=10))).telegrams)
    assert stamps == [datetime.fromisoformat(s).replace(tzinfo=UTC) for s in TEXT_STAMPS]
    (last,) = await store.get_last_unique_telegrams()
    assert last.timestamp == datetime.fromisoformat(TEXT_STAMPS[1]).replace(tzinfo=UTC)
    stats = await store.get_stats()
    assert stats.oldest_timestamp == stamps[0]
    assert stats.newest_timestamp == stamps[1]
    await store.close()

    assert not _flag_set(path, "timestamps_integer")
    assert _raw(path, "SELECT DISTINCT typeof(timestamp) FROM telegrams") == [("text",)]
    assert _raw(path, "SELECT DISTINCT typeof(timestamp) FROM last_ga_telegrams") == [("text",)]


@pytest.fixture
async def populated(tmp_path: Path):
    """Three telegrams one second apart, microsecond fractions and all."""
    store = SqliteStore(str(tmp_path / "ops.db"))
    await store.initialize()
    await store.store_many(
        [
            _telegram(BASE - timedelta(seconds=1), "1.1.1", "1/1/1"),
            _telegram(BASE, "1.1.2", "1/1/2"),
            _telegram(BASE + timedelta(seconds=1), "1.1.3", "1/1/3"),
        ]
    )
    yield store
    await store.close()


@pytest.mark.parametrize(
    ("query", "expected_sources"),
    [
        pytest.param(TelegramQuery(limit=10, order_descending=True), ["1.1.3", "1.1.2", "1.1.1"], id="descending"),
        pytest.param(TelegramQuery(limit=10, order_descending=False), ["1.1.1", "1.1.2", "1.1.3"], id="ascending"),
        pytest.param(
            TelegramQuery(start_time=BASE, end_time=BASE + timedelta(microseconds=1), limit=10),
            ["1.1.2"],
            id="range-to-the-microsecond",
        ),
        pytest.param(
            TelegramQuery(start_time=BASE - timedelta(microseconds=1), limit=10, order_descending=False),
            ["1.1.2", "1.1.3"],
            id="open-ended-range",
        ),
        pytest.param(
            TelegramQuery(destinations=["1/1/2"], delta_before_ms=1000, delta_after_ms=999, limit=10),
            ["1.1.2", "1.1.1"],
            id="delta-window-before-only",
        ),
        pytest.param(
            TelegramQuery(
                destinations=["1/1/2"], delta_before_ms=1000, delta_after_ms=1000, limit=10, order_descending=False
            ),
            ["1.1.1", "1.1.2", "1.1.3"],
            id="delta-window-both-sides-inclusive",
        ),
    ],
)
async def test_queries_work_on_integer_timestamps(
    populated: SqliteStore, query: TelegramQuery, expected_sources: list[str]
) -> None:
    result = await populated.query(query)
    assert [t.source for t in result.telegrams] == expected_sources
    assert result.total_count == len(expected_sources)


async def test_eviction_and_stats_use_the_same_integers(populated: SqliteStore) -> None:
    stats = await populated.get_stats()
    assert stats.oldest_timestamp == BASE - timedelta(seconds=1)
    assert stats.newest_timestamp == BASE + timedelta(seconds=1)

    assert await populated.evict_older_than(BASE, dry_run=True) == 1
    assert await populated.evict_older_than(BASE + timedelta(microseconds=1)) == 2
    remaining = (await populated.query(TelegramQuery(limit=10))).telegrams
    assert [t.source for t in remaining] == ["1.1.3"]
    assert (await populated.get_stats()).oldest_timestamp == BASE + timedelta(seconds=1)
