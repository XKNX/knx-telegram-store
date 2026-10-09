"""Rows are paginated before the lookup joins, not after (#75).

`query()` resolves source/destination/type/direction to strings through six
`string_lookup` joins. Joined ahead of `ORDER BY … LIMIT`, the inner joins look
to the planner as if they could drop rows, so it cannot take the first n rows
off the timestamp index: it joins the whole table and sorts. A `limit=1` query
took 3.9 s on 114k telegrams, against 5 ms once the page is cut from
`telegrams` first and only that page is joined.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from knx_telegram_store import StoredTelegram, TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _telegram(minute: int, source: str, destination: str, telegramtype: str = "GroupValueWrite", **extra):
    return StoredTelegram(
        timestamp=BASE + timedelta(minutes=minute),
        source=source,
        destination=destination,
        telegramtype=telegramtype,
        direction="Incoming",
        value=float(minute),
        dpt_main=9,
        dpt_sub=1,
        **extra,
    )


TELEGRAMS = [
    _telegram(0, "1.1.1", "1/1/1", source_name="Sensor", destination_name="Temperature"),
    _telegram(1, "1.1.2", "1/1/1"),
    _telegram(2, "1.1.1", "1/1/2", destination_name="Humidity"),
    _telegram(3, "1.1.3", "1/1/2", telegramtype="GroupValueRead"),
    _telegram(4, "1.1.1", "1/1/3", source_name="Sensor"),
    _telegram(5, "1.1.2", "1/1/3"),
    _telegram(6, "1.1.3", "1/1/1", telegramtype="GroupValueResponse"),
]


@pytest.fixture
async def store(tmp_path):
    store = SqliteStore(str(tmp_path / "page.db"))
    await store.initialize()
    await store.store_many(TELEGRAMS)
    yield store
    await store.close()


def _capture_statements(engine, sink):
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        sink.append(" ".join(statement.split()))


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(TelegramQuery(limit=1), id="newest-one"),
        pytest.param(TelegramQuery(limit=3, offset=2, order_descending=False), id="offset-ascending"),
        pytest.param(TelegramQuery(sources=["1.1.1"], limit=2), id="source-filter"),
        pytest.param(
            TelegramQuery(sources=["1.1.3"], delta_before_ms=90_000, delta_after_ms=90_000, limit=2),
            id="delta-context",
        ),
    ],
)
async def test_limit_is_applied_before_the_lookup_joins(store, query):
    statements: list[str] = []
    _capture_statements(store.engine, statements)

    await store.query(query)

    rows = [s for s in statements if "count(" not in s.lower() and "FROM (SELECT" in s]
    assert len(rows) == 1, f"expected one paginated row statement: {statements}"
    statement = rows[0]
    limit_at = statement.index("LIMIT")
    join_at = statement.index("JOIN string_lookup")
    assert limit_at < join_at, f"lookup joins run before the limit again: {statement}"
    # Nothing may paginate a second time outside the subquery.
    assert statement.count("LIMIT") == 1


def _key(t: StoredTelegram):
    return (t.timestamp, t.source, t.destination, t.telegramtype, t.source_name, t.destination_name, t.value)


@pytest.mark.parametrize("descending", [True, False])
@pytest.mark.parametrize(
    "base",
    [
        pytest.param(TelegramQuery(), id="unfiltered"),
        pytest.param(TelegramQuery(sources=["1.1.1"]), id="source"),
        pytest.param(TelegramQuery(destinations=["1/1/1", "1/1/3"]), id="destinations"),
        pytest.param(TelegramQuery(telegram_types=["GroupValueWrite"]), id="type"),
        pytest.param(TelegramQuery(start_time=BASE + timedelta(minutes=2)), id="time-range"),
    ],
)
async def test_pages_tile_the_full_result_in_order(store, base, descending):
    """Walking the pages yields exactly the unpaginated result, in order, with
    every lookup resolved — including the optional names."""
    base = replace(base, order_descending=descending)
    everything = (await store.query(replace(base, limit=1000))).telegrams

    stamps = [t.timestamp for t in everything]
    assert stamps == sorted(stamps, reverse=descending)

    walked: list[StoredTelegram] = []
    for offset in range(0, len(everything) + 2, 2):
        page = await store.query(replace(base, limit=2, offset=offset))
        assert page.total_count == len(everything)
        assert page.limit_reached is (offset + 2 < len(everything))
        walked.extend(page.telegrams)

    assert [_key(t) for t in walked] == [_key(t) for t in everything]


async def test_page_rows_carry_every_resolved_field(store):
    newest, second = (await store.query(TelegramQuery(limit=2))).telegrams

    assert (newest.source, newest.destination, newest.telegramtype) == ("1.1.3", "1/1/1", "GroupValueResponse")
    assert newest.direction == "Incoming"
    assert (newest.source_name, newest.destination_name) == ("", "")
    assert (newest.dpt_main, newest.dpt_sub, newest.value) == (9, 1, 6.0)
    assert second.source == "1.1.2"

    oldest = (await store.query(TelegramQuery(limit=1, order_descending=False))).telegrams[0]
    assert (oldest.source_name, oldest.destination_name) == ("Sensor", "Temperature")


async def test_offset_past_the_end_is_empty(store):
    result = await store.query(TelegramQuery(limit=5, offset=100))
    assert result.telegrams == []
    assert result.total_count == len(TELEGRAMS)
    assert result.limit_reached is False
