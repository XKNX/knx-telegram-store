from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from urllib.parse import unquote

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..connection import (
    ConnectionCheckResult,
    ConnectionErrorKind,
    classify_postgres_error,
    probe_engine,
    probe_timescaledb,
)
from ..model import StoredTelegram
from ..store import wrap_store_errors
from .base_sql import BaseSQLStore

_NOTIFY_CHANNEL = "telegram_inserted"

_LOGGER = logging.getLogger(__name__)


def _build_engine(dsn: str) -> AsyncEngine:
    """Build an asyncpg engine from a DSN, normalizing the driver and SSL default.

    Shared by ``__init__`` and ``check_config`` so connection handling stays consistent.
    """
    # Ensure we use asyncpg
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    url = make_url(dsn)
    query = dict(url.query)
    # asyncpg takes an ``ssl`` connect argument instead of libpq's ``sslmode``
    # query parameter; a leftover query key would be forwarded verbatim to
    # asyncpg.connect() and raise TypeError. Translate and strip it here.
    # Default to no SSL when not requested, to avoid blocking cert loading.
    ssl_value = query.pop("sslmode", None) or query.pop("ssl", None) or False
    url = url.set(query=query)
    if url.database:
        # libpq/asyncpg URIs percent-decode the database component, but
        # SQLAlchemy's make_url leaves it encoded — decode it here so
        # percent-encoded database names target the right database.
        url = url.set(database=unquote(url.database))
    return create_async_engine(url, connect_args={"ssl": ssl_value})


def _timescale_advisory(result: ConnectionCheckResult) -> ConnectionCheckResult:
    """Demote a missing-TimescaleDB probe result to an informative success.

    TimescaleDB is optional: the store falls back to plain PostgreSQL when the
    extension is not available, so its absence must not fail a connection
    check. Any other probe failure (auth, timeout, ...) is passed through.
    """
    if result.ok:
        return ConnectionCheckResult.success("Connection OK (TimescaleDB available)")
    if result.kind is ConnectionErrorKind.MISSING_TIMESCALEDB:
        return ConnectionCheckResult.success("Connection OK (TimescaleDB not available — using plain PostgreSQL)")
    return result


def _telegram_from_notify_payload(payload: str) -> StoredTelegram:
    """Parse a NOTIFY payload (row_to_json of the resolved trigger query) into a StoredTelegram."""
    data = json.loads(payload)
    return StoredTelegram(
        timestamp=datetime.fromisoformat(data["timestamp"]),
        source=data["source"],
        destination=data["destination"],
        telegramtype=data["telegramtype"],
        direction=data["direction"],
        payload=data["payload"],
        dpt_main=data["dpt_main"],
        dpt_sub=data["dpt_sub"],
        value=data["value"],
        value_numeric=data["value_numeric"],
        raw_data=data["raw_data"],
        data_secure=data["data_secure"],
        source_name=data["source_name"] or "",
        destination_name=data["destination_name"] or "",
    )


class PostgresStore(BaseSQLStore):
    """PostgreSQL implementation of TelegramStore.

    TimescaleDB is used automatically when the extension is available on the
    server: the telegrams table becomes a hypertable and native compression is
    enabled. Without the extension the store runs on plain PostgreSQL with
    identical semantics.
    """

    def __init__(
        self,
        dsn: str,
        retention_days: int | None = None,
        *,
        compress_after_days: int | None = 7,
        read_only: bool = False,
    ) -> None:
        """Initialize the Postgres store.

        Args:
            dsn: PostgreSQL connection string.
            retention_days: Optional retention period in days.
            compress_after_days: When TimescaleDB is available, compress chunks
                older than this many days (None disables compression setup).
                Ignored on plain PostgreSQL.
            read_only: With read_only=True the store never runs DDL/migrations
                and rejects all mutating operations; only querying, stats and
                ``listen_for_new_telegrams`` are available. Intended for
                reading a database owned and written by another process (e.g.
                Home Assistant's KNX integration).
        """
        engine = _build_engine(dsn)
        super().__init__(engine, retention_days, read_only=read_only)
        self._compress_after_days = compress_after_days
        self._timescale_enabled: bool | None = None
        # Plain VACUUM only marks dead tuples reusable — it does not return
        # disk space to the OS, so an optimize run is not observable in the
        # reported size (VACUUM FULL would be, but takes an ACCESS EXCLUSIVE
        # lock and doubles disk usage while it runs). Autovacuum handles dead
        # tuples; don't advertise a no-op to UIs.
        self._capabilities = replace(self._capabilities, supports_optimize=False)

    @property
    def timescale_enabled(self) -> bool | None:
        """Whether TimescaleDB is in use, or None before initialize() has run."""
        return self._timescale_enabled

    @staticmethod
    async def check_config(dsn: str, *, timeout: float = 5.0) -> ConnectionCheckResult:
        """Validate a Postgres DSN by attempting a real connection.

        Asynchronous — connecting requires network I/O. Builds a throwaway engine,
        runs ``SELECT 1``, and disposes the engine. Distinguishes auth, host, and
        missing-database failures via the returned result's ``kind``. TimescaleDB
        availability is probed but only reflected in the success message — the
        store falls back to plain PostgreSQL when the extension is missing.
        """
        engine = _build_engine(dsn)
        try:
            result = await probe_engine(engine, timeout=timeout, classify=classify_postgres_error)
            if not result.ok:
                return result
            return _timescale_advisory(await probe_timescaledb(engine, timeout=timeout))
        finally:
            await engine.dispose()

    async def check_connection(self, *, timeout: float = 5.0) -> ConnectionCheckResult:
        """Probe the live Postgres engine (no migrations, no schema changes).

        TimescaleDB availability only affects the success message; its absence
        is not an error since the store falls back to plain PostgreSQL.
        """
        result = await probe_engine(self.engine, timeout=timeout, classify=classify_postgres_error)
        if not result.ok:
            return result
        return _timescale_advisory(await probe_timescaledb(self.engine, timeout=timeout))

    async def _size_bytes(self) -> int | None:
        """Return the total size of the store's tables in bytes.

        Uses TimescaleDB's hypertable_size() for the telegrams table (chunks
        live in a separate schema, so pg_total_relation_size would undercount)
        and falls back to pg_total_relation_size if telegrams is not a
        hypertable. Runs in AUTOCOMMIT so the fallback probe does not abort a
        transaction.
        """
        engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            telegrams_size = None
            # Skip the hypertable probe only when we know Timescale is off;
            # before initialize() the mode is unknown, so try it.
            if self._timescale_enabled is not False:
                try:
                    telegrams_size = await conn.scalar(text("SELECT hypertable_size('telegrams')"))
                except Exception:
                    telegrams_size = None
            if telegrams_size is None:
                telegrams_size = await conn.scalar(text("SELECT pg_total_relation_size('telegrams')"))
            aux_size = await conn.scalar(
                text("SELECT pg_total_relation_size('string_lookup') + pg_total_relation_size('last_ga_telegrams')")
            )
        return int(telegrams_size or 0) + int(aux_size or 0)

    @wrap_store_errors
    async def optimize(self) -> None:
        """Mark dead tuples reusable after deletions (plain VACUUM).

        Not advertised via capabilities: plain VACUUM does not shrink the
        database files, so the reported size will not drop. Kept for callers
        that want to make dead-tuple space reusable ahead of autovacuum.
        VACUUM cannot run inside a transaction, hence AUTOCOMMIT.
        """
        self._ensure_writable()
        engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text("VACUUM telegrams"))
            await conn.execute(text("VACUUM string_lookup"))
            await conn.execute(text("VACUUM last_ga_telegrams"))

    async def initialize(self) -> None:
        """Set up the database schema and perform upgrades.

        TimescaleDB is detected at runtime: when available, the telegrams table
        is converted to a hypertable (migrating existing rows in place) and
        native compression is configured; otherwise the store runs on plain
        PostgreSQL tables.

        With read_only=True, no DDL/migrations run at all — another process
        (the writer) owns the schema, including the notify trigger set up
        below. Hosts should check needs_migration() and surface a version-skew
        error instead of relying on this store to upgrade anything.
        """
        if self._read_only:
            await super().initialize()
            return

        # 1. Detect (and if needed enable) the TimescaleDB extension. Runs on a
        # separate AUTOCOMMIT connection so a failed CREATE EXTENSION cannot
        # abort the schema transaction below.
        timescale = await self._detect_and_enable_timescale()

        async with self.engine.begin() as conn:
            # 2. Create tables if not exists
            await conn.run_sync(self._metadata.create_all)

            # 3. Perform column-level upgrades
            await conn.run_sync(self._upgrade_schema)

            # 3.5. Notify trigger so a read-only listener learns about new
            # rows via LISTEN/NOTIFY instead of polling. Always created,
            # regardless of whether anything is listening.
            await self._setup_notify_trigger(conn)

        # 4. Hypertable conversion + compression (idempotent)
        if timescale:
            await self._setup_hypertable()

        # 5. Legacy data backfills — separate transaction and never fatal:
        # they only improve rows written by pre-library schemas, and on
        # TimescaleDB they may be blocked by compressed chunks (DML
        # restrictions / tuple decompression limits). A failure here must
        # not prevent the host application from starting.
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(lambda c: self._backfill_legacy_data(c, timescale=timescale))
        except Exception as err:
            _LOGGER.warning(
                "Skipping legacy telegram data backfill (rows written by old schemas "
                "may miss decoded values until it succeeds): %s",
                err,
            )

        self._timescale_enabled = timescale
        _LOGGER.info(
            "Postgres store initialized (%s)",
            "TimescaleDB: hypertable + compression" if timescale else "plain PostgreSQL",
        )

        # 5. Warm the cache
        await super().initialize()

    async def _detect_and_enable_timescale(self) -> bool:
        """Return True if the TimescaleDB extension is installed or could be installed."""
        engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            available = await conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"))
            if available.first() is None:
                return False
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))
            except Exception as err:
                # e.g. insufficient privileges — usable anyway if already installed
                installed = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"))
                if installed.first() is None:
                    _LOGGER.warning(
                        "TimescaleDB is available but could not be enabled (%s); falling back to plain PostgreSQL",
                        err,
                    )
                    return False
        return True

    async def _setup_hypertable(self) -> None:
        """Convert telegrams to a hypertable and configure compression.

        migrate_data => TRUE also converts a table that already holds rows
        (e.g. a store that previously ran on plain PostgreSQL). Compression
        segments by destination GA — the dominant query filter — and orders by
        time; a background policy compresses chunks once they age past
        compress_after_days.
        """
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT create_hypertable('telegrams', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE)")
            )
            if self._compress_after_days is not None:
                # Only configure compression once: re-running the ALTER after
                # chunks have been compressed fails on older TimescaleDB with
                # "cannot change configuration on already compressed chunks".
                already_enabled = await conn.scalar(
                    text(
                        "SELECT compression_enabled FROM timescaledb_information.hypertables "
                        "WHERE hypertable_name = 'telegrams'"
                    )
                )
                if not already_enabled:
                    await conn.execute(
                        text(
                            "ALTER TABLE telegrams SET ("
                            "timescaledb.compress, "
                            "timescaledb.compress_orderby = 'timestamp DESC', "
                            "timescaledb.compress_segmentby = 'destination_id')"
                        )
                    )
                await conn.execute(
                    text(
                        f"SELECT add_compression_policy('telegrams', "
                        f"INTERVAL '{int(self._compress_after_days)} days', if_not_exists => TRUE)"
                    )
                )

    async def _setup_notify_trigger(self, conn: AsyncConnection) -> None:
        """Create the NOTIFY trigger that lets a read-only store LISTEN for new rows.

        A statement-level trigger with a transition table resolves the whole
        inserted batch against string_lookup in a single join (one insert from
        store_many can hold many rows), then emits one pg_notify per row so
        each payload stays well under Postgres's 8000-byte limit. The payload
        mirrors query()'s decoded shape (string source/destination/etc., not
        the raw lookup ids), so a listener can build a StoredTelegram directly
        without a follow-up query.
        """
        await conn.execute(
            text(f"""
                CREATE OR REPLACE FUNCTION knx_telegram_store_notify_insert() RETURNS trigger AS $$
                DECLARE
                  rec RECORD;
                BEGIN
                  FOR rec IN
                    SELECT
                      t.timestamp,
                      s.value AS source,
                      d.value AS destination,
                      tt.value AS telegramtype,
                      dir.value AS direction,
                      sn.value AS source_name,
                      dn.value AS destination_name,
                      t.payload,
                      t.dpt_main,
                      t.dpt_sub,
                      t.value,
                      t.value_numeric,
                      t.raw_data,
                      t.data_secure
                    FROM new_rows t
                    JOIN string_lookup s ON s.id = t.source_id AND s.category = 'source'
                    JOIN string_lookup d ON d.id = t.destination_id AND d.category = 'destination'
                    JOIN string_lookup tt ON tt.id = t.telegramtype_id AND tt.category = 'telegramtype'
                    JOIN string_lookup dir ON dir.id = t.direction_id AND dir.category = 'direction'
                    LEFT JOIN string_lookup sn ON sn.id = t.source_name_id AND sn.category = 'source_name'
                    LEFT JOIN string_lookup dn ON dn.id = t.destination_name_id AND dn.category = 'destination_name'
                  LOOP
                    PERFORM pg_notify('{_NOTIFY_CHANNEL}', row_to_json(rec)::text);
                  END LOOP;
                  RETURN NULL;
                END;
                $$ LANGUAGE plpgsql;
            """)
        )
        await conn.execute(text("DROP TRIGGER IF EXISTS knx_telegram_store_notify ON telegrams"))
        await conn.execute(
            text("""
                CREATE TRIGGER knx_telegram_store_notify
                  AFTER INSERT ON telegrams
                  REFERENCING NEW TABLE AS new_rows
                  FOR EACH STATEMENT
                  EXECUTE FUNCTION knx_telegram_store_notify_insert()
            """)
        )

    async def listen_for_new_telegrams(self) -> AsyncIterator[StoredTelegram]:
        """Yield telegrams as they are inserted by any writer, via LISTEN/NOTIFY.

        Requires the writer-side schema migration to have created the notify
        trigger (done automatically by a non-read-only PostgresStore's
        initialize()) — this works whether or not *this* store is read_only.
        Runs for as long as the returned async generator is iterated; breaking
        out of (or closing) the loop releases the dedicated listen connection.
        """
        queue: asyncio.Queue[str] = asyncio.Queue()

        def _on_notify(_connection: object, _pid: int, _channel: str, payload: str) -> None:
            queue.put_nowait(payload)

        async with self.engine.connect() as conn:
            raw = await conn.get_raw_connection()
            asyncpg_conn = raw.driver_connection
            assert asyncpg_conn is not None
            await asyncpg_conn.add_listener(_NOTIFY_CHANNEL, _on_notify)
            try:
                while True:
                    payload = await queue.get()
                    yield _telegram_from_notify_payload(payload)
            finally:
                await asyncpg_conn.remove_listener(_NOTIFY_CHANNEL, _on_notify)

    def _upgrade_schema(self, connection) -> None:
        """Synchronous part of schema upgrade (run via run_sync)."""
        inspector = inspect(connection)
        try:
            columns = inspector.get_columns("telegrams")
        except Exception:
            # Table might not exist yet
            return
        existing_columns = {col["name"] for col in columns}

        # 1. Handle renames from legacy SpectrumKNX schema
        renames = {
            "source_address": "source",
            "target_address": "destination",
            "telegram_type": "telegramtype",
            "value_json": "payload",
            "value": "value_numeric",  # Legacy value was FLOAT, library value is JSONB
        }
        for old, new in renames.items():
            if old in existing_columns:
                if new not in existing_columns:
                    connection.execute(text(f'ALTER TABLE telegrams RENAME COLUMN "{old}" TO "{new}"'))
                    existing_columns.remove(old)
                    existing_columns.add(new)
                elif old == "value":
                    # Special case: 'value' (float) and 'value_numeric' (float) both exist.
                    # We must move 'value' out of the way so it can be recreated as JSONB.
                    is_float = any(c["name"] == "value" and "double" in str(c["type"]).lower() for c in columns)
                    if is_float:
                        connection.execute(text('ALTER TABLE telegrams RENAME COLUMN "value" TO "value_legacy_float"'))
                        existing_columns.remove("value")
                        existing_columns.add("value_legacy_float")

        # Migrate raw_data from bytea to text (hex encoded)
        if "raw_data" in existing_columns:
            for col in columns:
                if col["name"] == "raw_data" and "bytea" in str(col["type"]).lower():
                    connection.execute(
                        text("ALTER TABLE telegrams ALTER COLUMN raw_data TYPE TEXT USING encode(raw_data, 'hex')")
                    )

        cols_to_migrate = {
            "source": "source",
            "destination": "destination",
            "telegramtype": "telegramtype",
            "direction": "direction",
            "source_name": "source_name",
            "destination_name": "destination_name",
        }

        # 2. Handle normalization to string_lookup
        if "source" in existing_columns:
            # Populate string_lookup table
            for cat, old_col in cols_to_migrate.items():
                if old_col in existing_columns:
                    connection.execute(
                        text(
                            f"INSERT INTO string_lookup (category, value) "
                            f"SELECT DISTINCT '{cat}', {old_col} FROM telegrams WHERE {old_col} IS NOT NULL "
                            f"ON CONFLICT DO NOTHING"
                        )
                    )

            # Add *_id columns
            for cat in cols_to_migrate:
                id_col = f"{cat}_id"
                if id_col not in existing_columns:
                    connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {id_col} INTEGER"))

            # Update IDs using JOIN
            for cat, old_col in cols_to_migrate.items():
                connection.execute(
                    text(
                        f"UPDATE telegrams SET {cat}_id = sl.id "
                        f"FROM string_lookup sl WHERE sl.category='{cat}' AND sl.value=telegrams.{old_col}"
                    )
                )

            # Drop old columns
            for old_col in list(cols_to_migrate.values()) + ["dpt_name", "unit"]:
                if old_col in existing_columns:
                    connection.execute(text(f'ALTER TABLE telegrams DROP COLUMN "{old_col}"'))
                    existing_columns.remove(old_col)

            # Re-fetch existing columns after drops
            columns = inspector.get_columns("telegrams")
            existing_columns = {col["name"] for col in columns}

        # Drop legacy normalized columns if present
        for col in ["dpt_name_id", "unit_id", "dpt_name", "unit"]:
            if col in existing_columns and col not in cols_to_migrate:
                try:
                    connection.execute(text(f'ALTER TABLE telegrams DROP COLUMN "{col}"'))
                except Exception:
                    pass

        # 3. Ensure all non-normalized library columns exist
        expected_columns = {
            "value": "JSONB",
            "value_numeric": "FLOAT",
            "payload": "JSONB",
            "data_secure": "BOOLEAN",
            "dpt_main": "INTEGER",
            "dpt_sub": "INTEGER",
        }

        for col_name, col_type in expected_columns.items():
            if col_name not in existing_columns and f"{col_name}_id" not in existing_columns:
                connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {col_name} {col_type}"))
                existing_columns.add(col_name)

    def _backfill_legacy_data(self, connection, *, timescale: bool) -> None:
        """Best-effort data backfills for rows written by pre-library schemas.

        Runs in its own transaction, after DDL upgrades and hypertable setup.
        Every pass is guarded so it only touches the database when there is
        actually something to fix — on TimescaleDB, UPDATEs on compressed
        chunks decompress data and are subject to DML restrictions, so the
        common no-op case must stay read-only.
        """
        inspector = inspect(connection)
        try:
            columns = inspector.get_columns("telegrams")
        except Exception:
            return
        existing_columns = {col["name"] for col in columns}

        def _pending(where: str) -> bool:
            return bool(connection.execute(text(f"SELECT EXISTS (SELECT 1 FROM telegrams WHERE {where})")).scalar())

        def _lift_decompression_limit() -> None:
            # TimescaleDB caps how many tuples one DML transaction may
            # decompress (default 100k) — a large backfill over compressed
            # chunks exceeds it. Lift the cap for this transaction only.
            if not timescale:
                return
            guc = "timescaledb.max_tuples_decompressed_per_dml_transaction"
            if connection.execute(text(f"SELECT 1 FROM pg_settings WHERE name = '{guc}'")).scalar():
                connection.execute(text(f"SET LOCAL {guc} = 0"))

        # Old schema had value_numeric (FLOAT) and value_json (now payload),
        # but no value (JSONB) column. Populate value from value_numeric
        # so the library's query returns it correctly. The store_metadata flag
        # marks completion so the unindexed _pending probe doesn't scan the
        # whole telegrams table on every startup.
        if not self._metadata_flag_set(connection, "nulls_recovered"):
            if (
                "value" in existing_columns
                and "value_numeric" in existing_columns
                and _pending("(value IS NULL OR value::text = 'null') AND value_numeric IS NOT NULL")
            ):
                _lift_decompression_limit()
                connection.execute(
                    text(
                        "UPDATE telegrams SET value = to_jsonb(value_numeric) "
                        "WHERE (value IS NULL OR value::text = 'null') AND value_numeric IS NOT NULL"
                    )
                )
            connection.execute(
                text(
                    "INSERT INTO store_metadata (key, value) VALUES ('nulls_recovered', 'true') "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                )
            )

        # Handle edge case from intermediate migrations where value was
        # a FLOAT column renamed to value_legacy_float
        if (
            "value_legacy_float" in existing_columns
            and "value_numeric" in existing_columns
            and _pending("value_numeric IS NULL AND value_legacy_float IS NOT NULL")
        ):
            _lift_decompression_limit()
            connection.execute(
                text(
                    "UPDATE telegrams SET value_numeric = value_legacy_float "
                    "WHERE value_numeric IS NULL AND value_legacy_float IS NOT NULL"
                )
            )

        # Data unwrapping pass for legacy {"value": ...} wrapped structures.
        # The store_metadata flag marks completion so the full-table scan
        # doesn't run on every startup.
        already_unwrapped = connection.execute(
            text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")
        ).scalar()
        if already_unwrapped == "true":
            return
        try:
            # Postgres supports casting JSONB to text, so we can cast value::text or payload::text
            rows = connection.execute(
                text(
                    "SELECT timestamp, source_id, destination_id, value::text, payload::text FROM telegrams "
                    "WHERE (value::text LIKE '{\"value\":%' AND value IS NOT NULL) "
                    "OR (payload::text LIKE '{\"value\":%' AND payload IS NOT NULL)"
                )
            ).fetchall()

            if rows:
                import json

                _lift_decompression_limit()
                for row in rows:
                    timestamp = row[0]
                    source_id = row[1]
                    destination_id = row[2]
                    val_str = row[3]
                    pay_str = row[4]

                    new_val = None
                    new_pay = None
                    needs_update = False

                    def unwrap(s):
                        if s is None:
                            return None, False
                        try:
                            if isinstance(s, dict):
                                d = s
                            else:
                                d = json.loads(s)
                            if isinstance(d, dict) and "value" in d and len(d) == 1:
                                return d["value"], True
                        except Exception:
                            pass
                        return s, False

                    if val_str is not None:
                        unwrapped_val, unwrapped = unwrap(val_str)
                        if unwrapped:
                            new_val = unwrapped_val
                            needs_update = True
                        else:
                            new_val = val_str

                    if pay_str is not None:
                        unwrapped_pay, unwrapped = unwrap(pay_str)
                        if unwrapped:
                            new_pay = unwrapped_pay
                            needs_update = True
                        else:
                            new_pay = pay_str

                    if needs_update:

                        def to_json_str(orig_val, new_val_unwrapped, did_unwrap):
                            if did_unwrap:
                                return json.dumps(new_val_unwrapped)
                            if orig_val is None:
                                return None
                            if isinstance(orig_val, dict | list | int | float | bool):
                                return json.dumps(orig_val)
                            try:
                                json.loads(orig_val)
                                return orig_val
                            except Exception:
                                return json.dumps(orig_val)

                        json_val = to_json_str(val_str, new_val, val_str != new_val)
                        json_pay = to_json_str(pay_str, new_pay, pay_str != new_pay)

                        connection.execute(
                            text(
                                "UPDATE telegrams SET value = :value, payload = :payload "
                                "WHERE timestamp = :timestamp AND source_id = :source_id AND destination_id = :destination_id"
                            ),
                            {
                                "value": json_val,
                                "payload": json_pay,
                                "timestamp": timestamp,
                                "source_id": source_id,
                                "destination_id": destination_id,
                            },
                        )

            # Record successful migration state in store_metadata
            connection.execute(
                text(
                    "INSERT INTO store_metadata (key, value) VALUES ('data_unwrapped', 'true') "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                )
            )
        except Exception:
            pass

    def _needs_migration_sync(self, connection) -> bool:
        """Synchronously check if legacy Postgres schema migration is required."""
        inspector = inspect(connection)
        try:
            columns = inspector.get_columns("telegrams")
        except Exception:
            return False
        existing_columns = {col["name"] for col in columns}

        # 1. Handle renames from legacy SpectrumKNX schema
        renames = {
            "source_address",
            "target_address",
            "telegram_type",
            "value_json",
        }
        for old in renames:
            if old in existing_columns:
                return True

        # Special value float rename check
        if "value" in existing_columns:
            is_float = any(c["name"] == "value" and "double" in str(c["type"]).lower() for c in columns)
            if is_float:
                return True

        # raw_data bytea check
        if "raw_data" in existing_columns:
            for col in columns:
                if col["name"] == "raw_data" and "bytea" in str(col["type"]).lower():
                    return True

        # 2. Handle normalization to string_lookup
        if "source" in existing_columns:
            return True

        # Add *_id columns
        cols_to_migrate = [
            "source_id",
            "destination_id",
            "telegramtype_id",
            "direction_id",
            "source_name_id",
            "destination_name_id",
        ]
        for col_id in cols_to_migrate:
            if col_id not in existing_columns:
                return True

        # Missing columns
        expected_columns = {
            "payload",
            "dpt_main",
            "dpt_sub",
            "value",
            "value_numeric",
            "data_secure",
        }
        for col_name in expected_columns:
            if col_name not in existing_columns:
                return True

        # 4.5. Check if there are any legacy 'null' values to recover from value_numeric.
        # Skip this scan entirely once the nulls_recovered flag is set — with no matching
        # rows (the common case) the unindexed LIMIT 1 probe scans the whole table.
        if (
            not self._metadata_flag_set(connection, "nulls_recovered")
            and "value" in existing_columns
            and "value_numeric" in existing_columns
        ):
            try:
                row = connection.execute(
                    text(
                        "SELECT 1 FROM telegrams WHERE (value IS NULL OR value::text = 'null') AND value_numeric IS NOT NULL LIMIT 1"
                    )
                ).fetchone()
                if row:
                    return True
            except Exception:
                pass

        # 5. Check if any rows contain legacy {"value": ...} wrapped values
        # Skip this scan entirely if the metadata table indicates we already unwrapped
        if not self._metadata_flag_set(connection, "data_unwrapped"):
            try:
                row = connection.execute(
                    text(
                        "SELECT 1 FROM telegrams WHERE (value::text LIKE '{\"value\":%' AND value IS NOT NULL) "
                        "OR (payload::text LIKE '{\"value\":%' AND payload IS NOT NULL) LIMIT 1"
                    )
                ).fetchone()
                if row:
                    return True
            except Exception:
                pass

        return False
