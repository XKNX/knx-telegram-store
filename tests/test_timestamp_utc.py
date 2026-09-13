"""Timestamps must round-trip as UTC-aware on every backend.

StoredTelegram.timestamp is documented as timezone-aware UTC. SQLite has no
native datetime, and the plain DateTime(timezone=True) type wrote the naive
digits while *discarding the offset without converting* — so noon at +02:00 and
noon at UTC, two hours apart, were stored identically and read back naive. That
is what gave the same telegram two different serializations depending on whether
it arrived live or from history (XKNX/knx-frontend#459).
"""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore

PLUS_TWO = timezone(timedelta(hours=2))
MINUS_FIVE = timezone(timedelta(hours=-5))


def _telegram(ts: datetime, source: str = "1.1.1", destination: str = "1/1/1"):
    return StoredTelegram(
        timestamp=ts,
        source=source,
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=1.0,
    )


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(str(tmp_path / "ts.db"))
    await s.initialize()
    yield s
    await s.close()


async def test_distinct_instants_stay_distinct(store):
    """The regression that caused the duplicates: these are two hours apart."""
    noon_plus_two = datetime(2026, 9, 12, 12, 0, 0, tzinfo=PLUS_TWO)  # 10:00 UTC
    noon_utc = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)

    await store.store_many([_telegram(noon_plus_two, "1.1.1"), _telegram(noon_utc, "1.1.2")])
    result = await store.query(TelegramQuery(limit=10, order_descending=False))

    stamps = [t.timestamp for t in result.telegrams]
    assert len(set(stamps)) == 2, f"two instants collapsed into one: {stamps}"
    assert stamps[0] == noon_plus_two
    assert stamps[1] == noon_utc
    assert stamps[1] - stamps[0] == timedelta(hours=2)


@pytest.mark.parametrize(
    "tz",
    [pytest.param(UTC, id="utc"), pytest.param(PLUS_TWO, id="+02:00"), pytest.param(MINUS_FIVE, id="-05:00")],
)
async def test_round_trip_preserves_the_instant(store, tz):
    original = datetime(2026, 9, 12, 12, 34, 56, 789012, tzinfo=tz)
    await store.store_many([_telegram(original)])

    (stored,) = (await store.query(TelegramQuery(limit=1))).telegrams
    assert stored.timestamp == original, "the instant changed"
    assert stored.timestamp.tzinfo is not None, "contract says timezone-aware"
    assert stored.timestamp.utcoffset() == timedelta(0), "contract says UTC"


async def test_a_naive_input_is_taken_as_utc(store):
    """Guessing the writer's timezone is not possible here, so naive means UTC."""
    await store.store_many([_telegram(datetime(2026, 9, 12, 12, 0, 0))])

    (stored,) = (await store.query(TelegramQuery(limit=1))).telegrams
    assert stored.timestamp == datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)


async def test_values_are_stored_as_utc_on_disk(tmp_path):
    """Checked at the file level: the stored digits are UTC, not local."""
    path = tmp_path / "raw.db"
    store = SqliteStore(str(path))
    await store.initialize()
    await store.store_many([_telegram(datetime(2026, 9, 12, 12, 0, 0, tzinfo=PLUS_TWO))])
    await store.close()

    (raw,) = next(iter(sqlite3.connect(str(path)).execute("SELECT timestamp FROM telegrams")))
    assert raw.startswith("2026-09-12 10:00:00"), f"stored local time instead of UTC: {raw}"


async def test_ordering_is_by_instant_not_by_wall_clock(store):
    """Mixed offsets must sort by the moment they happened.

    Wall-clock digits would put the +02:00 telegram first; by instant it is
    second.
    """
    later_digits_earlier_instant = datetime(2026, 9, 12, 12, 0, 0, tzinfo=PLUS_TWO)  # 10:00 UTC
    earlier_digits_later_instant = datetime(2026, 9, 12, 11, 0, 0, tzinfo=UTC)

    await store.store_many(
        [_telegram(later_digits_earlier_instant, "1.1.1"), _telegram(earlier_digits_later_instant, "1.1.2")]
    )
    result = await store.query(TelegramQuery(limit=10, order_descending=False))
    assert [t.source for t in result.telegrams] == ["1.1.1", "1.1.2"]


async def test_time_delta_context_still_builds_valid_sql(store):
    """Regression guard for the type decorator.

    The delta-context window binds a timedelta against the timestamp column.
    A TypeDecorator types every compared value as itself unless it delegates
    coerce_compared_value, which made PostgreSQL reject
    "timestamptz >= interval" — caught only by the integration tests.
    """
    base = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
    await store.store_many(
        [
            _telegram(base - timedelta(seconds=5), "1.1.9", "1/1/9"),
            _telegram(base, "1.1.1", "1/1/1"),
            _telegram(base + timedelta(seconds=5), "1.1.8", "1/1/8"),
        ]
    )

    result = await store.query(
        TelegramQuery(destinations=["1/1/1"], delta_before_ms=10_000, delta_after_ms=10_000, limit=100)
    )
    assert result.total_count == len(result.telegrams)
    assert len(result.telegrams) == 3, "the context window should pull in the neighbours"


async def test_time_range_filters_use_the_instant(store):
    """A range given in one offset must match data written in another."""
    await store.store_many([_telegram(datetime(2026, 9, 12, 12, 0, 0, tzinfo=PLUS_TWO))])  # 10:00 UTC

    hit = await store.query(
        TelegramQuery(
            start_time=datetime(2026, 9, 12, 9, 30, 0, tzinfo=UTC),
            end_time=datetime(2026, 9, 12, 10, 30, 0, tzinfo=UTC),
            limit=10,
        )
    )
    assert hit.total_count == 1

    miss = await store.query(
        TelegramQuery(
            start_time=datetime(2026, 9, 12, 11, 30, 0, tzinfo=UTC),
            end_time=datetime(2026, 9, 12, 12, 30, 0, tzinfo=UTC),
            limit=10,
        )
    )
    assert miss.total_count == 0, "matched on wall-clock digits rather than the instant"


async def test_last_values_timestamps_are_also_utc_aware(store):
    """last_ga_telegrams carries its own timestamp column."""
    await store.store_many([_telegram(datetime(2026, 9, 12, 12, 0, 0, tzinfo=PLUS_TWO))])

    (last,) = await store.get_last_unique_telegrams()
    assert last.timestamp.tzinfo is not None
    assert last.timestamp == datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)


async def test_query_bounds_carrying_an_offset_are_normalised(store):
    """The read side of the same bug: bounds must be compared by instant.

    A TypeDecorator that delegates coerce_compared_value for datetimes hands
    query bounds to the plain DateTime, which drops the offset on SQLite — so
    the range misses the very row it was written to match.
    """
    await store.store_many([_telegram(datetime(2026, 9, 12, 12, 0, 0, tzinfo=PLUS_TWO))])  # 10:00 UTC

    result = await store.query(
        TelegramQuery(
            start_time=datetime(2026, 9, 12, 11, 0, 0, tzinfo=PLUS_TWO),  # 09:00 UTC
            end_time=datetime(2026, 9, 12, 13, 0, 0, tzinfo=PLUS_TWO),  # 11:00 UTC
            limit=10,
        )
    )
    assert len(result.telegrams) == 1, "a bound written in +02:00 must match data stored from +02:00"

    outside = await store.query(
        TelegramQuery(
            start_time=datetime(2026, 9, 12, 13, 0, 0, tzinfo=PLUS_TWO),  # 11:00 UTC
            end_time=datetime(2026, 9, 12, 15, 0, 0, tzinfo=PLUS_TWO),  # 13:00 UTC
            limit=10,
        )
    )
    assert outside.telegrams == [], "and must not match a window the instant falls outside"
