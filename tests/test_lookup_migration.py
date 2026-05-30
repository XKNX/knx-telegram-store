from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import create_async_engine

from knx_telegram_store import TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore


@pytest.fixture
def old_schema_db(tmp_path):
    """Create a SQLite DB with the old (pre-normalization) schema."""
    db_path = tmp_path / "old_telegrams.db"

    metadata = MetaData()
    telegrams = Table(
        "telegrams",
        metadata,
        Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
        Column("source", String(20), nullable=False),
        Column("destination", String(20), nullable=False, index=True),
        Column("telegramtype", String(50), nullable=False),
        Column("direction", String(20), nullable=False, server_default=""),
        Column("payload", JSON, nullable=True),
        Column("dpt_main", Integer, nullable=True),
        Column("dpt_sub", Integer, nullable=True),
        Column("dpt_name", String(100), nullable=True),
        Column("unit", String(20), nullable=True),
        Column("value", JSON, nullable=True),
        Column("value_numeric", Double, nullable=True),
        Column("raw_data", Text, nullable=True),
        Column("data_secure", Boolean, nullable=True),
        Column("source_name", String(255), server_default=""),
        Column("destination_name", String(255), server_default=""),
    )

    return db_path, telegrams, metadata


@pytest.mark.asyncio
async def test_sqlite_migration(old_schema_db):
    db_path, old_table, metadata = old_schema_db
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url)

    now = datetime.now(UTC).replace(microsecond=0)

    # 1. Create old schema and insert data
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(
            insert(old_table).values(
                timestamp=now,
                source="1.1.1",
                destination="1/1/1",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                dpt_name="Temperature",
                unit="°C",
                value=22.5,
                value_numeric=22.5,
                source_name="Sensor 1",
                destination_name="Living Room Temp",
            )
        )
        await conn.execute(
            insert(old_table).values(
                timestamp=now - timedelta(seconds=10),
                source="1.1.2",
                destination="1/1/1",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                dpt_name="Temperature",
                unit="°C",
                value=21.0,
                value_numeric=21.0,
                source_name="Sensor 2",
                destination_name="Living Room Temp",
            )
        )
        await conn.execute(
            insert(old_table).values(
                timestamp=now - timedelta(seconds=20),
                source="1.1.3",
                destination="1/1/2",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                dpt_name="String",
                unit=None,
                value={"value": "M1 S0 A305 E00"},
                payload={"value": [12, 101]},
                value_numeric=None,
                source_name="Sensor 3",
                destination_name="Color/String Status",
            )
        )
        await conn.execute(
            insert(old_table).values(
                timestamp=now - timedelta(seconds=30),
                source="1.1.4",
                destination="1/1/3",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                dpt_name="Counter",
                unit=None,
                value={"value": 0},
                payload={"value": 0},
                value_numeric=0.0,
                source_name="Sensor 4",
                destination_name="Counter Status",
            )
        )
        await conn.execute(
            insert(old_table).values(
                timestamp=now - timedelta(seconds=40),
                source="1.1.5",
                destination="1/1/4",
                telegramtype="GroupValueWrite",
                direction="Incoming",
                dpt_name="Binary",
                unit=None,
                value=False,
                payload={"value": [10]},
                value_numeric=0.0,
                source_name="Sensor 5",
                destination_name="Binary Status",
            )
        )

    await engine.dispose()

    # 2. Use new SqliteStore to initialize and migrate
    store = SqliteStore(db_path)
    assert await store.needs_migration() is True
    await store.initialize()
    assert await store.needs_migration() is False

    # 3. Verify data via query (transparent)
    result = await store.query(TelegramQuery(order_descending=False))
    assert len(result.telegrams) == 5

    t0 = result.telegrams[0]
    assert t0.source == "1.1.5"
    assert t0.destination == "1/1/4"
    assert t0.value is False
    assert t0.payload == [10]

    t1 = result.telegrams[1]
    assert t1.source == "1.1.4"
    assert t1.destination == "1/1/3"
    assert t1.value == 0
    assert t1.payload == 0

    t2 = result.telegrams[2]
    assert t2.source == "1.1.3"
    assert t2.destination == "1/1/2"
    assert t2.telegramtype == "GroupValueWrite"
    assert t2.value == "M1 S0 A305 E00"
    assert t2.payload == [12, 101]

    t3 = result.telegrams[3]
    assert t3.source == "1.1.2"
    assert t3.destination == "1/1/1"
    assert t3.telegramtype == "GroupValueWrite"
    assert t3.source_name == "Sensor 2"
    assert t3.destination_name == "Living Room Temp"

    t4 = result.telegrams[4]
    assert t4.source == "1.1.1"
    assert t4.source_name == "Sensor 1"

    # 4. Verify internal normalization
    async with store.engine.connect() as conn:
        # Check string_lookup
        lookup_result = await conn.execute(select(store.string_lookup))
        lookups = lookup_result.fetchall()
        # source: 1.1.1, 1.1.2, 1.1.3, 1.1.4, 1.1.5 (5)
        # destination: 1/1/1, 1/1/2, 1/1/3, 1/1/4 (4)
        # telegramtype: GroupValueWrite (1)
        # direction: Incoming (1)
        # source_name: Sensor 1, Sensor 2, Sensor 3, Sensor 4, Sensor 5 (5)
        # destination_name: Living Room Temp, Color/String Status, Counter Status, Binary Status (4)
        # Total unique (cat, val) pairs: 5+4+1+1+5+4 = 20
        assert len(lookups) == 20

        # Check telegrams table columns
        from sqlalchemy import inspect

        def get_cols(conn):
            return {col["name"] for col in inspect(conn).get_columns("telegrams")}

        cols = await conn.run_sync(get_cols)
        assert "source_id" in cols
        assert "source" not in cols

    await store.close()
