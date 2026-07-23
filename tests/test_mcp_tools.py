from datetime import UTC, datetime, timedelta

import pytest

from knx_telegram_store import StoredTelegram
from knx_telegram_store.mcp import (
    CountResult,
    LastValuesInput,
    QueryTelegramsInput,
    QueryTelegramsResult,
    StoreCapabilitiesResult,
    StoreStatsResult,
    count_telegrams,
    get_last_values,
    get_store_capabilities,
    get_store_stats,
    query_telegrams,
)
from knx_telegram_store.mcp.tools import _format_dpt, _normalize_type, _parse_dpt, _parse_dt

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _telegrams() -> list[StoredTelegram]:
    return [
        StoredTelegram(
            timestamp=NOW - timedelta(minutes=5),
            source="1.1.1",
            destination="1/1/1",
            telegramtype="GroupValueWrite",
            direction="Incoming",
            value=20.0,
            value_numeric=20.0,
            dpt_main=9,
            dpt_sub=1,
            destination_name="Living Room Temp",
        ),
        StoredTelegram(
            timestamp=NOW - timedelta(minutes=3),
            source="1.1.2",
            destination="1/1/1",
            telegramtype="GroupValueWrite",
            direction="Incoming",
            value=21.5,
            value_numeric=21.5,
            dpt_main=9,
            dpt_sub=1,
        ),
        StoredTelegram(
            timestamp=NOW - timedelta(minutes=1),
            source="1.1.3",
            destination="1/2/2",
            telegramtype="GroupValueWrite",
            direction="Outgoing",
            value=True,
            dpt_main=1,
            dpt_sub=1,
        ),
    ]


async def _seed(store) -> None:
    await store.store_many(_telegrams())


async def test_query_telegrams_returns_summaries(store):
    await _seed(store)
    result = await query_telegrams(store, QueryTelegramsInput(destinations=["1/1/1"]))

    assert isinstance(result, QueryTelegramsResult)
    assert len(result.telegrams) == 2
    assert all(t.destination == "1/1/1" for t in result.telegrams)
    # Newest first by default.
    assert result.telegrams[0].value_numeric == 21.5
    # DPT rendered as main.sub and a friendly name carried through.
    assert result.telegrams[0].dpt == "9.001"
    assert result.telegrams[1].destination_name == "Living Room Temp"
    # Timestamp is an ISO-8601 string, not a datetime.
    assert result.telegrams[0].timestamp == (NOW - timedelta(minutes=3)).isoformat()


async def test_query_telegrams_time_range_and_z_suffix(store):
    await _seed(store)
    start = (NOW - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    result = await query_telegrams(store, QueryTelegramsInput(start_time=start))
    # Only the telegram from 1 minute ago falls in [-2 min, now].
    assert len(result.telegrams) == 1
    assert result.telegrams[0].destination == "1/2/2"


async def test_query_telegrams_dpt_subtype_filter(store):
    await _seed(store)
    # Specific subtype "9.001" matches only the two temperature telegrams.
    result = await query_telegrams(store, QueryTelegramsInput(dpts=["9.001"]))
    assert {t.destination for t in result.telegrams} == {"1/1/1"}
    assert len(result.telegrams) == 2
    # A bare main "1" matches every 1.x subtype.
    result = await query_telegrams(store, QueryTelegramsInput(dpts=["1"]))
    assert [t.destination for t in result.telegrams] == ["1/2/2"]


async def test_query_telegrams_type_aliases(store):
    await _seed(store)
    # "Write" is an alias for GroupValueWrite (all three seeded rows are writes)...
    writes = await query_telegrams(store, QueryTelegramsInput(telegram_types=["Write"]))
    assert len(writes.telegrams) == 3
    # ...and "Read" (GroupValueRead) matches none.
    reads = await query_telegrams(store, QueryTelegramsInput(telegram_types=["Read"]))
    assert reads.telegrams == []


async def test_query_telegrams_delta_window(store):
    await _seed(store)
    # Pivot on the 1/2/2 telegram (1 min ago); a 2.5-min window pulls in the
    # 1/1/1 telegram from 3 min ago (context), but not the one from 5 min ago.
    result = await query_telegrams(
        store,
        QueryTelegramsInput(destinations=["1/2/2"], delta_before_ms=150_000, delta_after_ms=150_000),
    )
    assert {t.destination for t in result.telegrams} == {"1/1/1", "1/2/2"}
    assert len(result.telegrams) == 2


async def test_query_telegrams_limit_reached(store):
    await _seed(store)
    result = await query_telegrams(store, QueryTelegramsInput(limit=1))
    assert len(result.telegrams) == 1
    assert result.limit_reached is True


async def test_get_last_values(store):
    await _seed(store)
    last = await get_last_values(store, LastValuesInput())
    by_dest = {t.destination: t for t in last}
    assert set(by_dest) == {"1/1/1", "1/2/2"}
    # Most recent value for the repeated GA.
    assert by_dest["1/1/1"].value_numeric == 21.5

    filtered = await get_last_values(store, LastValuesInput(destinations=["1/2/2"]))
    assert [t.destination for t in filtered] == ["1/2/2"]


async def test_get_store_stats(store):
    await _seed(store)
    stats = await get_store_stats(store)
    assert isinstance(stats, StoreStatsResult)
    assert stats.telegram_count == 3
    assert isinstance(stats.backend, str) and stats.backend
    assert stats.oldest_timestamp == (NOW - timedelta(minutes=5)).isoformat()
    assert stats.newest_timestamp == (NOW - timedelta(minutes=1)).isoformat()


async def test_get_store_capabilities(store):
    caps = await get_store_capabilities(store)
    assert isinstance(caps, StoreCapabilitiesResult)
    # Every declared capability is a bool.
    assert isinstance(caps.read_only, bool)
    assert isinstance(caps.supports_count, bool)


async def test_count_telegrams(store):
    await _seed(store)
    result = await count_telegrams(store)
    assert isinstance(result, CountResult)
    assert result.count == 3


@pytest.mark.parametrize(
    ("main", "sub", "expected"),
    [(9, 1, "9.001"), (9, None, "9"), (None, None, None), (1, 1, "1.001")],
)
def test_format_dpt(main, sub, expected):
    assert _format_dpt(main, sub) == expected


def test_parse_dt_invalid():
    with pytest.raises(ValueError, match="Invalid ISO-8601"):
        _parse_dt("not-a-timestamp")
    assert _parse_dt(None) is None


@pytest.mark.parametrize(
    ("entry", "expected"),
    [("9", (9, None)), ("9.001", (9, 1)), ("1.0", (1, 0)), (" 14.056 ", (14, 56))],
)
def test_parse_dpt(entry, expected):
    assert _parse_dpt(entry) == expected


@pytest.mark.parametrize("entry", ["", "9.", ".1", "9.x", "abc"])
def test_parse_dpt_invalid(entry):
    with pytest.raises(ValueError, match="Invalid DPT"):
        _parse_dpt(entry)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Write", "GroupValueWrite"),
        ("read", "GroupValueRead"),
        ("RESPONSE", "GroupValueResponse"),
        ("GroupValueWrite", "GroupValueWrite"),
        ("Something", "Something"),
    ],
)
def test_normalize_type(value, expected):
    assert _normalize_type(value) == expected
