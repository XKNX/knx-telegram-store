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

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("asyncpg")

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from knx_telegram_store import (  # noqa: E402
    BufferedPostgresStore,
    KnxTelegramStoreException,
    StoredTelegram,
    TelegramQuery,
)
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


async def test_initialize_reconciles_legacy_last_value_summaries(backend):
    """Repair stale summaries and deterministically fill missing tied summaries."""
    _, dsn = backend
    original = PostgresStore(dsn)
    await original.initialize()

    timestamp = datetime(2026, 9, 14, 12, tzinfo=UTC)
    newest = make_telegram(timestamp, value=30.0)
    older = make_telegram(timestamp - timedelta(days=1), value=10.0)
    tie_first = make_telegram(timestamp, destination="4/5/6", value=40.0)
    tie_first = replace(tie_first, raw_data="ff")
    tie_canonical = replace(tie_first, raw_data="00")
    await original.store_many([newest, older, tie_first, tie_canonical])

    columns = [column.name for column in original.last_ga_telegrams.columns]
    older_row = select(*(original.telegrams.c[name] for name in columns)).where(
        original.telegrams.c.timestamp == older.timestamp,
        original.telegrams.c.destination_id
        == select(original.string_lookup.c.id)
        .where(
            original.string_lookup.c.category == "destination",
            original.string_lookup.c.value == newest.destination,
        )
        .scalar_subquery(),
    )
    async with original.engine.begin() as conn:
        await conn.execute(original.last_ga_telegrams.delete())
        await conn.execute(original.last_ga_telegrams.insert().from_select(columns, older_row))
        await conn.execute(
            original.store_metadata.delete().where(original.store_metadata.c.key == "last_ga_newest_reconciled")
        )
    await original.close()

    upgraded = PostgresStore(dsn)
    try:
        await upgraded.initialize()
        by_destination = {telegram.destination: telegram for telegram in await upgraded.get_last_unique_telegrams()}
        assert by_destination[newest.destination].value_numeric == 30.0
        assert by_destination[tie_first.destination].raw_data == "00"
    finally:
        await upgraded.close()


async def test_timescale_enabled_unknown_before_initialize(backend):
    _, dsn = backend
    pg_store = PostgresStore(dsn)
    try:
        assert pg_store.timescale_enabled is None
    finally:
        await pg_store.close()


# --- Write / read round-trip ---------------------------------------------------


async def test_failed_store_does_not_publish_rolled_back_lookup_ids(store):
    _, pg_store = store
    bad = replace(
        make_telegram(datetime.now(UTC)),
        source="9.9.9",
        destination="9/9/9",
        value=object(),
        value_numeric=None,
    )

    with pytest.raises(KnxTelegramStoreException):
        await pg_store.store(bad)

    await pg_store.store(replace(bad, value=1, value_numeric=1.0))
    result = await pg_store.query(TelegramQuery())

    assert await pg_store.count() == 1
    assert len(result.telegrams) == 1
    assert result.telegrams[0].destination == "9/9/9"
    assert result.telegrams[0].value == 1


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


async def test_oversized_notify_payload_does_not_abort_batch(store):
    _, pg_store = store
    timestamp = datetime.now(UTC)
    oversized_value = "x" * 9000
    oversized = StoredTelegram(
        timestamp=timestamp,
        source="1.1.1",
        destination="1/2/3",
        telegramtype="GroupValueWrite",
        direction="Incoming",
        value=oversized_value,
    )
    normal = make_telegram(timestamp, destination="4/5/6", value=21.5)
    listener = pg_store.listen_for_new_telegrams()
    notification_task = asyncio.create_task(anext(listener))
    await asyncio.sleep(0.2)

    try:
        await pg_store.store_many([oversized, normal])
        received = await asyncio.wait_for(notification_task, timeout=5)
        result = await pg_store.query(TelegramQuery())
    finally:
        await listener.aclose()

    assert received.destination == normal.destination
    assert received.value == normal.value
    assert result.total_count == 2
    assert {telegram.value for telegram in result.telegrams} == {
        oversized_value,
        21.5,
    }


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


async def test_last_unique_telegrams_ignores_older_and_equal_writes(store):
    _, pg_store = store
    timestamp = datetime(2026, 9, 14, 12, tzinfo=UTC)
    newest = make_telegram(timestamp, value=30.0)
    older = make_telegram(timestamp - timedelta(days=1), value=10.0)
    equal = make_telegram(timestamp, value=40.0)

    await pg_store.store(newest)
    await pg_store.store(older)
    result = await pg_store.get_last_unique_telegrams()
    assert result[0].value_numeric == 30.0

    await pg_store.store(equal)
    result = await pg_store.get_last_unique_telegrams()
    assert result[0].value_numeric == 30.0


# --- Legacy migration ----------------------------------------------------------


async def test_legacy_unwrap_preserves_duplicate_natural_keys(store):
    _, pg_store = store
    timestamp = datetime(2026, 7, 1, 12, tzinfo=UTC)
    await pg_store.store_many(
        [
            make_telegram(timestamp, value=1.0),
            make_telegram(timestamp, value=2.0),
            make_telegram(timestamp, value=3.0),
        ]
    )

    async with pg_store.engine.begin() as conn:
        await conn.execute(
            text("UPDATE telegrams SET value = jsonb_build_object('value', value) WHERE value_numeric = 3")
        )
        await conn.execute(
            text(
                "UPDATE telegrams "
                "SET value = jsonb_build_object('value', value), "
                "payload = jsonb_build_object('value', payload)"
            )
        )
        await conn.execute(text("DELETE FROM store_metadata WHERE key = 'data_unwrapped'"))

    await pg_store.initialize()
    result = await pg_store.query(TelegramQuery())

    assert sorted(telegram.value for telegram in result.telegrams if isinstance(telegram.value, float)) == [1.0, 2.0]
    assert [telegram.value for telegram in result.telegrams if isinstance(telegram.value, dict)] == [{"value": 3.0}]
    assert all(telegram.payload == [12, 154] for telegram in result.telegrams)
    async with pg_store.engine.connect() as conn:
        assert await conn.scalar(text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")) == "true"

    await pg_store.initialize()
    result = await pg_store.query(TelegramQuery())
    assert [telegram.value for telegram in result.telegrams if isinstance(telegram.value, dict)] == [{"value": 3.0}]


async def test_legacy_unwrap_preserves_duplicates_in_compressed_chunk(timescale_dsn):
    store = PostgresStore(timescale_dsn)
    await store.initialize()
    timestamp = datetime(2026, 7, 1, 12, tzinfo=UTC)
    try:
        await store.store_many(
            [
                make_telegram(timestamp, value=1.0),
                make_telegram(timestamp, value=2.0),
                make_telegram(timestamp, 1, value=3.0),
                make_telegram(timestamp, value=4.0),
            ]
        )
        async with store.engine.begin() as conn:
            await conn.execute(
                text("UPDATE telegrams SET value = jsonb_build_object('value', value) WHERE value_numeric = 4")
            )
            await conn.execute(
                text(
                    "UPDATE telegrams "
                    "SET value = jsonb_build_object('value', value), "
                    "payload = jsonb_build_object('value', payload) "
                    "WHERE timestamp = :timestamp"
                ),
                {"timestamp": timestamp},
            )
            await conn.execute(
                text("UPDATE telegrams SET value = jsonb_build_object('value', value) WHERE timestamp > :timestamp"),
                {"timestamp": timestamp},
            )
            await conn.execute(text("DELETE FROM store_metadata WHERE key = 'data_unwrapped'"))
            await conn.execute(
                text("SELECT compress_chunk(c, if_not_compressed => TRUE) FROM show_chunks('telegrams') c")
            )
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM timescaledb_information.chunks "
                        "WHERE hypertable_name = 'telegrams' AND is_compressed"
                    )
                )
                >= 1
            )

        await store.initialize()
        result = await store.query(TelegramQuery())

        assert sorted(telegram.value for telegram in result.telegrams if isinstance(telegram.value, float)) == [
            1.0,
            2.0,
            3.0,
        ]
        assert {"value": 4.0} in [telegram.value for telegram in result.telegrams]
        assert all(telegram.payload == [12, 154] for telegram in result.telegrams)
    finally:
        await store.close()


async def test_concurrent_legacy_backfills_unwrap_nested_value_once(store):
    _, pg_store = store
    await pg_store.store(make_telegram(datetime.now(UTC), value=3.0))
    async with pg_store.engine.begin() as conn:
        await conn.execute(text('UPDATE telegrams SET value = \'{"value": {"value": 3}}\'::jsonb'))
        await conn.execute(text("DELETE FROM store_metadata WHERE key = 'data_unwrapped'"))
        assert await conn.scalar(text("SELECT value FROM store_metadata WHERE key = 'nulls_recovered'")) == "true"

    first = await pg_store.engine.connect()
    second = await pg_store.engine.connect()
    first_transaction = await first.begin()

    def _backfill(connection):
        pg_store._backfill_legacy_data(connection, timescale=pg_store.timescale_enabled is True)

    try:
        # Keep both the decoded value and completion marker uncommitted while
        # the second transaction starts from the same pending migration state.
        await first.run_sync(_backfill)

        async def _second_backfill():
            async with second.begin():
                await second.run_sync(_backfill)

        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.wait_for(_second_backfill(), timeout=2)

        await first_transaction.commit()
    finally:
        if first_transaction.is_active:
            await first_transaction.rollback()
        await first.close()
        await second.close()

    result = await pg_store.query(TelegramQuery())
    assert [telegram.value for telegram in result.telegrams] == [{"value": 3}]
    async with pg_store.engine.connect() as conn:
        assert await conn.scalar(text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")) == "true"


@pytest.mark.parametrize("pending_backfill", ["unwrap", "nulls", "legacy_float"])
async def test_legacy_backfill_skips_concurrent_writer_and_retries(store, pending_backfill):
    _, pg_store = store
    await pg_store.store(make_telegram(datetime.now(UTC), value=1.0))

    async with pg_store.engine.begin() as conn:
        if pending_backfill == "unwrap":
            await conn.execute(text("UPDATE telegrams SET value = jsonb_build_object('value', value)"))
            await conn.execute(text("DELETE FROM store_metadata WHERE key = 'data_unwrapped'"))
        elif pending_backfill == "nulls":
            await conn.execute(text("DELETE FROM store_metadata WHERE key = 'nulls_recovered'"))
        else:
            await conn.execute(text("ALTER TABLE telegrams ADD COLUMN value_legacy_float FLOAT"))
            await conn.execute(text("UPDATE telegrams SET value_numeric = NULL, value_legacy_float = 1"))

    writer = await pg_store.engine.connect()
    backfill = await pg_store.engine.connect()
    writer_transaction = await writer.begin()
    try:
        if pending_backfill == "nulls":
            await writer.execute(text("UPDATE telegrams SET value = NULL"))
        else:
            await writer.execute(text("UPDATE telegrams SET data_secure = NOT data_secure"))

        async def _run_backfill():
            async with backfill.begin():
                await backfill.run_sync(
                    lambda sync_conn: pg_store._backfill_legacy_data(
                        sync_conn,
                        timescale=pg_store.timescale_enabled is True,
                    )
                )

        with pytest.raises(DBAPIError) as error:
            await asyncio.wait_for(_run_backfill(), timeout=2)
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
        async with pg_store.engine.connect() as observer:
            if pending_backfill == "unwrap":
                assert (
                    await observer.scalar(text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")) is None
                )
            elif pending_backfill == "nulls":
                assert (
                    await observer.scalar(text("SELECT value FROM store_metadata WHERE key = 'nulls_recovered'"))
                    is None
                )
            else:
                assert await observer.scalar(text("SELECT value_numeric FROM telegrams")) is None

        await writer_transaction.commit()
        await _run_backfill()
    finally:
        if writer_transaction.is_active:
            await writer_transaction.rollback()
        await writer.close()
        await backfill.close()

    result = await pg_store.query(TelegramQuery())
    assert [telegram.value for telegram in result.telegrams] == [1.0]
    assert result.telegrams[0].value_numeric == 1.0


async def test_legacy_backfill_skips_active_prechange_reader_and_retries(store):
    _, pg_store = store
    await pg_store.store(make_telegram(datetime.now(UTC), value=1.0))
    async with pg_store.engine.begin() as conn:
        await conn.execute(text("UPDATE telegrams SET value = jsonb_build_object('value', value)"))
        await conn.execute(text("DELETE FROM store_metadata WHERE key = 'data_unwrapped'"))

    old_migration = await pg_store.engine.connect()
    backfill = await pg_store.engine.connect()
    old_transaction = await old_migration.begin()
    try:
        await old_migration.execute(text("SELECT value FROM telegrams"))

        async def _run_backfill():
            async with backfill.begin():
                await backfill.run_sync(
                    lambda sync_conn: pg_store._backfill_legacy_data(
                        sync_conn,
                        timescale=pg_store.timescale_enabled is True,
                    )
                )

        with pytest.raises(DBAPIError) as error:
            await asyncio.wait_for(_run_backfill(), timeout=2)
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"

        await old_transaction.commit()
        await _run_backfill()
    finally:
        if old_transaction.is_active:
            await old_transaction.rollback()
        await old_migration.close()
        await backfill.close()

    result = await pg_store.query(TelegramQuery())
    assert [telegram.value for telegram in result.telegrams] == [1.0]


async def test_legacy_unwrap_records_completion_only_after_every_update(store):
    _, pg_store = store
    await pg_store.store(make_telegram(datetime.now(UTC), value=1.0))

    async with pg_store.engine.begin() as conn:
        await conn.execute(text("UPDATE telegrams SET value = jsonb_build_object('value', value)"))
        await conn.execute(text("DELETE FROM store_metadata WHERE key = 'data_unwrapped'"))
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION skip_legacy_unwrap() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER skip_legacy_unwrap BEFORE UPDATE OF value, payload ON telegrams "
                "FOR EACH ROW EXECUTE FUNCTION skip_legacy_unwrap()"
            )
        )

    await pg_store.initialize()
    result = await pg_store.query(TelegramQuery())
    assert [telegram.value for telegram in result.telegrams] == [{"value": 1.0}]
    async with pg_store.engine.connect() as conn:
        assert await conn.scalar(text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")) is None

    async with pg_store.engine.begin() as conn:
        await conn.execute(text("DROP TRIGGER skip_legacy_unwrap ON telegrams"))
        await conn.execute(text("DROP FUNCTION skip_legacy_unwrap()"))

    await pg_store.initialize()
    result = await pg_store.query(TelegramQuery())
    assert [telegram.value for telegram in result.telegrams] == [1.0]


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


# --- Read-only + LISTEN/NOTIFY ---------------------------------------------------


@pytest.fixture
async def reader(backend):
    """(name, writer, reader) - a read-only store against a DB a writer already initialized."""
    name, dsn = backend
    writer = PostgresStore(dsn, retention_days=30)
    await writer.initialize()
    pg_reader = PostgresStore(dsn, read_only=True)
    await pg_reader.initialize()
    yield name, writer, pg_reader
    await pg_reader.close()
    await writer.close()


async def test_read_only_capabilities(reader):
    _, _writer, pg_reader = reader
    assert pg_reader.capabilities.read_only is True
    assert pg_reader.capabilities.supports_optimize is False


async def test_read_only_sees_writer_data(reader):
    _, writer, pg_reader = reader
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    await writer.store_many([make_telegram(base, i) for i in range(5)])
    assert await pg_reader.count() == 5
    result = await pg_reader.query(TelegramQuery())
    assert result.total_count == 5


async def test_read_only_rejects_mutations(reader):
    _, _writer, pg_reader = reader
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(KnxTelegramStoreException, match="read-only"):
        await pg_reader.store(make_telegram(base))
    with pytest.raises(KnxTelegramStoreException, match="read-only"):
        await pg_reader.store_many([make_telegram(base)])
    with pytest.raises(KnxTelegramStoreException, match="read-only"):
        await pg_reader.evict_older_than(datetime.now(UTC))
    with pytest.raises(KnxTelegramStoreException, match="read-only"):
        await pg_reader.clear()
    with pytest.raises(KnxTelegramStoreException, match="read-only"):
        await pg_reader.optimize()
    # Nothing was written or deleted.
    assert await pg_reader.count() == 0


async def test_read_only_never_runs_ddl(backend):
    """A read-only store with no prior writer must fail, not create the schema."""
    _, dsn = backend
    pg_reader = PostgresStore(dsn, read_only=True)
    try:
        with pytest.raises(KnxTelegramStoreException):
            await pg_reader.initialize()
    finally:
        await pg_reader.close()

    engine = _build_engine(dsn)
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT to_regclass('telegrams')"))
        assert exists is None
    finally:
        await engine.dispose()


async def test_listen_for_new_telegrams(reader):
    _, writer, pg_reader = reader
    listener = pg_reader.listen_for_new_telegrams()

    task = asyncio.create_task(anext(listener))
    await asyncio.sleep(0.2)  # let the LISTEN attach before the write

    await writer.store(make_telegram(datetime.now(UTC), destination="7/7/7", value=12.5))

    received = await asyncio.wait_for(task, timeout=5)
    assert received.destination == "7/7/7"
    assert received.value_numeric == 12.5
    assert received.source_name == "Sensor"
    await listener.aclose()


async def test_listen_for_new_telegrams_batch(reader):
    """A single store_many() call fires one notification per row, not per statement."""
    _, writer, pg_reader = reader
    listener = pg_reader.listen_for_new_telegrams()
    received: list[StoredTelegram] = []

    async def _collect(n: int) -> None:
        async for t in listener:
            received.append(t)
            if len(received) >= n:
                return

    task = asyncio.create_task(_collect(3))
    await asyncio.sleep(0.2)

    base = datetime.now(UTC)
    batch = [make_telegram(base, i, destination=f"1/2/{i}") for i in range(3)]
    await writer.store_many(batch)

    await asyncio.wait_for(task, timeout=5)
    assert sorted(t.destination for t in received) == ["1/2/0", "1/2/1", "1/2/2"]
    await listener.aclose()


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


async def test_reinitialize_with_compressed_legacy_rows(timescale_dsn):
    """App restarts must survive the legacy-value backfill hitting compressed chunks.

    An install upgraded to >=0.9.0 gets the compression policy; once chunks age
    past the threshold and compress, the recurring value-backfill UPDATE used to
    exceed TimescaleDB's per-transaction tuple decompression limit on every
    subsequent startup and crash the host application
    (ConfigurationLimitExceededError: tuple decompression limit exceeded).
    """
    store = PostgresStore(timescale_dsn)
    await store.initialize()
    base = datetime.now(UTC) - timedelta(days=30)
    await store.store_many([make_telegram(base, i) for i in range(10)])
    await store.close()

    engine = _build_engine(timescale_dsn)
    autocommit = engine.execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with autocommit.connect() as conn:
            # Simulate rows that predate the JSONB value column (value NULL,
            # value_numeric set) — exactly what the startup backfill targets —
            # then compress the chunk like the background policy would. Such a
            # database also predates the nulls_recovered completion flag, so
            # clear it to make the backfill pending again.
            await conn.execute(text("UPDATE telegrams SET value = NULL"))
            await conn.execute(text("DELETE FROM store_metadata WHERE key = 'nulls_recovered'"))
            await conn.execute(
                text("SELECT compress_chunk(c, if_not_compressed => TRUE) FROM show_chunks('telegrams') c")
            )
            # Make the decompression cap smaller than the pending backfill,
            # like a large production history against the 100k default.
            await conn.execute(
                text("ALTER DATABASE knx SET timescaledb.max_tuples_decompressed_per_dml_transaction = 5")
            )

        # Re-initialize (= app restart): must not raise, and the backfill
        # must complete by lifting the cap for its own transaction.
        store2 = PostgresStore(timescale_dsn)
        await store2.initialize()
        result = await store2.query(TelegramQuery())
        assert result.total_count == 10
        assert all(t.value == 21.5 for t in result.telegrams)
        await store2.close()
    finally:
        async with autocommit.connect() as conn:
            await conn.execute(text("ALTER DATABASE knx RESET timescaledb.max_tuples_decompressed_per_dml_transaction"))
        await engine.dispose()
