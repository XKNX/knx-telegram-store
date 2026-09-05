"""The paginated count must not join the lookup tables (SpectrumKNX#450).

`query()` returns rows with source/destination/type/direction resolved to
strings, which needs six `string_lookup` joins. The total count used for
pagination does not: every filter is expressed against `telegrams` columns via
id subqueries, so the joins cannot change which rows match — they only project
names onto the output.

Counting through them was measured at roughly 44x slower on a 2M-row
TimescaleDB hypertable (1559 ms against 36 ms), and a client that gave up
mid-query left the backend burning CPU.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore


def _telegram(minutes_ago: int, source: str, destination: str, telegramtype: str = "GroupValueWrite"):
    return StoredTelegram(
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        source=source,
        destination=destination,
        telegramtype=telegramtype,
        direction="Incoming",
        value=float(minutes_ago),
        dpt_main=9,
    )


@pytest.fixture
async def populated_store(tmp_path):
    store = SqliteStore(str(tmp_path / "count.db"))
    await store.initialize()
    await store.store_many(
        [
            _telegram(5, "1.1.1", "1/1/1"),
            _telegram(4, "1.1.2", "1/1/1"),
            _telegram(3, "1.1.1", "1/1/2"),
            _telegram(2, "1.1.3", "1/1/2", telegramtype="GroupValueRead"),
            _telegram(1, "1.1.1", "1/1/3"),
        ]
    )
    yield store
    await store.close()


def _capture_statements(engine, sink):
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        sink.append(" ".join(statement.split()))


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(TelegramQuery(limit=2), id="unfiltered"),
        pytest.param(TelegramQuery(sources=["1.1.1"], limit=2), id="source-filter"),
        pytest.param(TelegramQuery(destinations=["1/1/2"], limit=2), id="destination-filter"),
        pytest.param(TelegramQuery(telegram_types=["GroupValueWrite"], limit=2), id="type-filter"),
    ],
)
async def test_count_statement_does_not_join_the_lookup_tables(populated_store, query):
    statements: list[str] = []
    _capture_statements(populated_store.engine, statements)

    await populated_store.query(query)

    counts = [s for s in statements if "count(" in s.lower()]
    assert counts, f"no count statement was issued: {statements}"
    for statement in counts:
        assert "JOIN" not in statement.upper(), f"count re-acquired a join: {statement}"
        # The filters legitimately reference string_lookup in an IN subquery;
        # what must not come back is joining it to project names.
        assert "string_lookup AS" not in statement, f"count aliases string_lookup: {statement}"


@pytest.mark.parametrize(
    ("query", "expected_total"),
    [
        (TelegramQuery(limit=2), 5),
        (TelegramQuery(sources=["1.1.1"], limit=2), 3),
        (TelegramQuery(destinations=["1/1/2"], limit=2), 2),
        (TelegramQuery(telegram_types=["GroupValueRead"], limit=2), 1),
        (TelegramQuery(sources=["1.1.1"], destinations=["1/1/2"], limit=2), 1),
        (TelegramQuery(sources=["nope"], limit=2), 0),
    ],
)
async def test_total_count_is_unchanged_by_dropping_the_joins(populated_store, query, expected_total):
    """The count must stay exact, not merely fast.

    The four inner joins could in principle have excluded rows; they cannot,
    because every id is interned under its own category when written.
    """
    result = await populated_store.query(query)
    assert result.total_count == expected_total
    # And it still agrees with what pagination actually yields.
    all_rows = await populated_store.query(replace(query, limit=1000, offset=0))
    assert len(all_rows.telegrams) == expected_total


async def test_count_matches_paginated_rows_with_time_delta_context(populated_store):
    """The delta-context branch builds its own EXISTS clause; the count has to
    carry the same one rather than the joined statement."""
    query = TelegramQuery(sources=["1.1.3"], delta_before_ms=120_000, delta_after_ms=120_000, limit=100)
    result = await populated_store.query(query)
    assert result.total_count == len(result.telegrams)
    assert result.total_count >= 1


def test_declared_indexes_cover_the_filterable_columns(tmp_path):
    """Every column TelegramQuery can filter on should be indexed.

    Audited by EXPLAIN against a 2M-row hypertable: without these, dpt filters
    were the only ones still doing a sequential scan.
    """
    store = SqliteStore(str(tmp_path / "idx.db"))
    indexed = {tuple(c.name for c in index.columns) for index in store.telegrams.indexes}

    for column in ("timestamp", "source_id", "destination_id", "telegramtype_id", "direction_id"):
        assert (column,) in indexed, f"{column} is filterable but not indexed"
    # Composite so a dpt_main-only filter uses the leading column too.
    assert ("dpt_main", "dpt_sub") in indexed


def test_last_ga_telegrams_is_not_over_indexed(tmp_path):
    """It holds one row per group address and is upserted on every telegram.

    It is only ever read whole or by primary key, so extra indexes on it would
    be write cost for no read benefit.
    """
    store = SqliteStore(str(tmp_path / "idx2.db"))
    extra = {tuple(c.name for c in index.columns) for index in store.last_ga_telegrams.indexes}
    assert extra == set(), f"unexpected indexes on the upsert hot path: {extra}"
