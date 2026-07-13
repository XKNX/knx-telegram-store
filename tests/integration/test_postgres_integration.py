"""Integration tests against live PostgreSQL and TimescaleDB instances.

These tests exercise the Postgres backend end-to-end against two real servers:
a TimescaleDB container (hypertable + compression path) and a stock PostgreSQL
container (plain fallback path). They are skipped unless the corresponding DSN
environment variables are set.

Run everything with Docker via the helper script:

    ./scripts/run_integration_tests.sh

Or manually:

    docker compose -f docker-compose.test.yml up -d --wait
    export KNX_TEST_TIMESCALE_DSN=postgresql://knx:knxtest@localhost:5433/knx
    export KNX_TEST_PG_DSN=postgresql://knx:knxtest@localhost:5434/knx
    pytest -m integration tests/integration -v
    docker compose -f docker-compose.test.yml down -v
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("asyncpg")

from sqlalchemy import text  # noqa: E402

from knx_telegram_store import BufferedPostgresStore, StoredTelegram, TelegramQuery  # noqa: E402
from knx_telegram_store.backends.postgres import PostgresStore, _build_engine  # noqa: E402

pytestmark = pytest.mark.integration

TIMESCALE_DSN = os.environ.get("KNX_TEST_TIMESCALE_DSN")
PLAIN_PG_DSN = os.environ.get("KNX_TEST_PG_DSN")

BACKENDS = [
    pytest.param(
        "timescale",
        marks=pytest.mark.skipif(not TIMESCALE_DSN, reason="KNX_TEST_TIMESCALE_DSN not set"),
    ),
    pytest.param(
        "plain",
        marks=pytest.mark.skipif(not PLAIN_PG_DSN, reason="KNX_TEST_PG_DSN not set"),
    ),
]

TABLES = ("telegrams", "last_ga_telegrams", "string_lookup", "store_metadata")


def _dsn(backend_name: str) -> str:
    dsn = TIMESCALE_DSN if backend_name == "timescale" else PLAIN_PG_DSN
    assert dsn is not None
    return dsn


async def _reset_database(dsn: str) -> None:
    """Drop all store tables so every test starts from an empty database."""
    engine = _build_engine(dsn)
    try:
        async with engine.begin() as conn:
            for table in TABLES:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    finally:
        await engine.dispose()


async def _scalar(dsn: str, sql: str) -> object:
    """Run a single scalar query on a throwaway AUTOCOMMIT connection."""
    engine = _build_engine(dsn)
    try:
        autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
        async with autocommit.connect() as conn:
            return await conn.scalar(text(sql))
    finally:
        await engine.dispose()


async def _is_hypertable(dsn: str) -> bool:
    return (
        await _scalar(
            dsn,
            "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'telegrams'",
        )
        == 1
    )


def make_telegram(
    base: datetime,
    offset_s: float = 0,
    *,
    destination: str = "1/2/3",
    source: str = "1.1.1",
    value: float = 21.5,
) -> StoredTelegram:
    return StoredTelegram(
        timestamp=base + timedelta(seconds=offset_s),
        source=source,
        destination=destination,
        telegramtype="GroupValueWrite",
        direction="Incoming",
        payload=(12, 154),
        dpt_main=9,
        dpt_sub=1,
        value=value,
        value_numeric=value,
        raw_data="0c9a",
        data_secure=False,
        source_name="Sensor",
        destination_name="Temperature",
    )


@pytest.fixture(params=BACKENDS)
async def backend(request):
    """(name, dsn) for each reachable database, reset to an empty state."""
    name = request.param
    dsn = _dsn(name)
    await _reset_database(dsn)
    return name, dsn


@pytest.fixture
async def store(backend):
    """An initialized PostgresStore against the parametrized backend."""
    name, dsn = backend
    store = PostgresStore(dsn, retention_days=30)
    await store.initialize()
    yield name, store
    await store.close()


@pytest.fixture
async def timescale_dsn():
    """DSN of the TimescaleDB container, reset to an empty state."""
    if not TIMESCALE_DSN:
        pytest.skip("KNX_TEST_TIMESCALE_DSN not set")
    await _reset_database(TIMESCALE_DSN)
    return TIMESCALE_DSN


# --- Connection checks --------------------------------------------------------


async def test_check_config_reports_mode(backend):
    name, dsn = backend
    result = await PostgresStore.check_config(dsn)
    assert result.ok, result.message
    if name == "timescale":
        assert "TimescaleDB available" in result.message
    else:
        assert "plain PostgreSQL" in result.message


async def test_check_connection_reports_mode(store):
    name, pg_store = store
    result = await pg_store.check_connection()
    assert result.ok, result.message
    expected = "TimescaleDB available" if name == "timescale" else "plain PostgreSQL"
    assert expected in result.message


# --- Initialization / mode detection ------------------------------------------


async def test_initialize_idempotent_and_detects_mode(store):
    name, pg_store = store
    assert pg_store.timescale_enabled is (name == "timescale")

    # A second initialize must be a no-op (idempotency).
    await pg_store.initialize()
    assert pg_store.timescale_enabled is (name == "timescale")

    dsn = _dsn(name)
    if name == "timescale":
        assert await _is_hypertable(dsn)
    else:
        # Stock Postgres: the extension is not even installable.
        installed = await _scalar(dsn, "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
        assert installed == 0


async def test_timescale_enabled_unknown_before_initialize(backend):
    _, dsn = backend
    pg_store = PostgresStore(dsn)
    try:
        assert pg_store.timescale_enabled is None
    finally:
        await pg_store.close()


# --- Write / read round-trip ---------------------------------------------------


async def test_roundtrip_time_range_pagination_ordering(store):
    _, pg_store = store
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    telegrams = [
        make_telegram(base, offset_s=60 * i, destination="1/2/3" if i % 2 else "4/5/6", value=20.0 + i)
        for i in range(50)
    ]
    await pg_store.store_many(telegrams)

    # Full query, newest first.
    result = await pg_store.query(TelegramQuery())
    assert result.total_count == 50
    assert len(result.telegrams) == 50
    timestamps = [t.timestamp for t in result.telegrams]
    assert timestamps == sorted(timestamps, reverse=True)
    assert result.telegrams[0].destination in {"1/2/3", "4/5/6"}
    assert result.telegrams[0].source_name == "Sensor"

    # Time-range filter: minutes 10..19 inclusive.
    result = await pg_store.query(
        TelegramQuery(
            start_time=base + timedelta(minutes=10),
            end_time=base + timedelta(minutes=19),
        )
    )
    assert result.total_count == 10

    # Destination filter.
    result = await pg_store.query(TelegramQuery(destinations=["4/5/6"]))
    assert result.total_count == 25

    # Pagination.
    result = await pg_store.query(TelegramQuery(limit=10, offset=45))
    assert result.total_count == 50
    assert len(result.telegrams) == 5
    assert not result.limit_reached
    result = await pg_store.query(TelegramQuery(limit=10, offset=0))
    assert len(result.telegrams) == 10
    assert result.limit_reached

    assert await pg_store.count() == 50


async def test_time_delta_context_window(store):
    _, pg_store = store
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    await pg_store.store_many(
        [
            make_telegram(base, 0, destination="1/1/1"),  # pivot
            make_telegram(base, 2, destination="2/2/2"),  # within +3s of pivot
            make_telegram(base, -2, destination="3/3/3"),  # within -3s of pivot
            make_telegram(base, 60, destination="4/4/4"),  # far away
        ]
    )
    result = await pg_store.query(
        TelegramQuery(destinations=["1/1/1"], delta_before_ms=3000, delta_after_ms=3000, order_descending=False)
    )
    assert [t.destination for t in result.telegrams] == ["3/3/3", "1/1/1", "2/2/2"]


async def test_last_unique_telegrams(store):
    _, pg_store = store
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    await pg_store.store_many(
        [
            make_telegram(base, 0, destination="1/2/3", value=1.0),
            make_telegram(base, 10, destination="1/2/3", value=2.0),
            make_telegram(base, 5, destination="4/5/6", value=3.0),
        ]
    )
    last = await pg_store.get_last_unique_telegrams()
    by_destination = {t.destination: t for t in last}
    assert set(by_destination) == {"1/2/3", "4/5/6"}
    assert by_destination["1/2/3"].value_numeric == 2.0
    assert by_destination["4/5/6"].value_numeric == 3.0


# --- Stats ---------------------------------------------------------------------


async def test_stats_and_size(store):
    _, pg_store = store
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    await pg_store.store_many([make_telegram(base, i) for i in range(10)])

    stats = await pg_store.get_stats()
    assert stats.telegram_count == 10
    assert stats.oldest_timestamp == base
    assert stats.newest_timestamp == base + timedelta(seconds=9)
    assert stats.backend == "postgresql"
    assert stats.retention_days == 30
    # Exercises hypertable_size() on TimescaleDB and pg_total_relation_size() on plain PG.
    assert stats.size_bytes is not None and stats.size_bytes > 0


# --- Retention -----------------------------------------------------------------


async def test_evict_older_than(store):
    _, pg_store = store
    now = datetime.now(UTC)
    old = [make_telegram(now - timedelta(days=10), i) for i in range(10)]
    new = [make_telegram(now, i, destination="4/5/6") for i in range(5)]
    await pg_store.store_many(old + new)

    cutoff = now - timedelta(days=1)
    assert await pg_store.evict_older_than(cutoff, dry_run=True) == 10
    assert await pg_store.count() == 15  # dry run must not delete

    assert await pg_store.evict_older_than(cutoff) == 10
    assert await pg_store.count() == 5


# --- Buffered store --------------------------------------------------------------


async def test_buffered_store_flushes(backend):
    _, dsn = backend
    buffered = BufferedPostgresStore(dsn, flush_interval=60.0)
    await buffered.initialize()
    try:
        buffered.start()
        base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        for i in range(5):
            await buffered.store(make_telegram(base, i))
        # Nothing flushed yet (long interval); flush_first forces visibility.
        result = await buffered.query(TelegramQuery(), flush_first=True)
        assert result.total_count == 5
    finally:
        await buffered.stop()


# --- TimescaleDB-specific behavior ----------------------------------------------


async def test_plain_table_migrates_to_hypertable(timescale_dsn):
    """A store that ran on plain PostgreSQL is upgraded in place once Timescale is available."""
    # Simulate a plain-PostgreSQL deployment by forcing detection to fail.
    plain_store = PostgresStore(timescale_dsn)

    async def _no_timescale() -> bool:
        return False

    plain_store._detect_and_enable_timescale = _no_timescale  # type: ignore[method-assign]
    await plain_store.initialize()
    assert plain_store.timescale_enabled is False
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await plain_store.store_many([make_telegram(base, i) for i in range(20)])
    assert not await _is_hypertable(timescale_dsn)
    await plain_store.close()

    # A normal initialize converts the populated table (migrate_data => TRUE).
    upgraded = PostgresStore(timescale_dsn)
    await upgraded.initialize()
    try:
        assert upgraded.timescale_enabled is True
        assert await _is_hypertable(timescale_dsn)
        assert await upgraded.count() == 20
    finally:
        await upgraded.close()


async def test_compression_configured_and_queryable(timescale_dsn):
    """Compression is set up, chunks compress, and query/eviction still work on compressed data."""
    pg_store = PostgresStore(timescale_dsn, retention_days=365)
    await pg_store.initialize()
    try:
        # The compression policy job must exist.
        jobs = await _scalar(
            timescale_dsn,
            "SELECT count(*) FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_name = 'telegrams'",
        )
        assert jobs == 1

        # Old data (past the 7-day compress threshold) plus recent data.
        now = datetime.now(UTC)
        old_base = now - timedelta(days=30)
        await pg_store.store_many(
            [make_telegram(old_base, 60 * i, value=1.0 + i) for i in range(10)]
            + [make_telegram(now, i, destination="4/5/6") for i in range(5)]
        )

        # Compress the old chunks now instead of waiting for the policy job.
        engine = pg_store.engine.execution_options(isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT compress_chunk(c, if_not_compressed => TRUE) "
                    "FROM show_chunks('telegrams', older_than => INTERVAL '14 days') c"
                )
            )
        compressed = await _scalar(
            timescale_dsn,
            "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'telegrams' AND is_compressed",
        )
        assert compressed >= 1

        # Queries read transparently across compressed and uncompressed chunks.
        result = await pg_store.query(TelegramQuery(destinations=["1/2/3"], order_descending=False))
        assert result.total_count == 10
        assert result.telegrams[0].value_numeric == 1.0

        # Exact DELETE-based eviction works inside compressed chunks.
        assert await pg_store.evict_older_than(now - timedelta(days=1)) == 10
        assert await pg_store.count() == 5
    finally:
        await pg_store.close()
