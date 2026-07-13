from __future__ import annotations

import logging
from dataclasses import replace
from urllib.parse import unquote

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..connection import (
    ConnectionCheckResult,
    ConnectionErrorKind,
    classify_postgres_error,
    probe_engine,
    probe_timescaledb,
)
from ..store import wrap_store_errors
from .base_sql import BaseSQLStore

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
    ) -> None:
        """Initialize the Postgres store.

        Args:
            dsn: PostgreSQL connection string.
            retention_days: Optional retention period in days.
            compress_after_days: When TimescaleDB is available, compress chunks
                older than this many days (None disables compression setup).
                Ignored on plain PostgreSQL.
        """
        engine = _build_engine(dsn)
        super().__init__(engine, retention_days)
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
        """
        # 1. Detect (and if needed enable) the TimescaleDB extension. Runs on a
        # separate AUTOCOMMIT connection so a failed CREATE EXTENSION cannot
        # abort the schema transaction below.
        timescale = await self._detect_and_enable_timescale()

        async with self.engine.begin() as conn:
            # 2. Create tables if not exists
            await conn.run_sync(self._metadata.create_all)

            # 3. Perform column-level upgrades
            await conn.run_sync(self._upgrade_schema)

        # 4. Hypertable conversion + compression (idempotent)
        if timescale:
            await self._setup_hypertable()

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

        # 4. Data migrations for old SpectrumKNX rows
        # Old schema had value_numeric (FLOAT) and value_json (now payload),
        # but no value (JSONB) column. Populate value from value_numeric
        # so the library's query returns it correctly.
        if "value" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(
                text(
                    "UPDATE telegrams SET value = to_jsonb(value_numeric) "
                    "WHERE (value IS NULL OR value::text = 'null') AND value_numeric IS NOT NULL"
                )
            )

        # Handle edge case from intermediate migrations where value was
        # a FLOAT column renamed to value_legacy_float
        if "value_legacy_float" in existing_columns and "value_numeric" in existing_columns:
            connection.execute(
                text(
                    "UPDATE telegrams SET value_numeric = value_legacy_float "
                    "WHERE value_numeric IS NULL AND value_legacy_float IS NOT NULL"
                )
            )

        # 5. Data unwrapping pass for legacy {"value": ...} wrapped structures
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

        # 4.5. Check if there are any legacy 'null' values to recover from value_numeric
        if "value" in existing_columns and "value_numeric" in existing_columns:
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
        is_unwrapped = False
        try:
            if inspector.has_table("store_metadata"):
                row = connection.execute(
                    text("SELECT value FROM store_metadata WHERE key = 'data_unwrapped'")
                ).fetchone()
                if row and row[0] == "true":
                    is_unwrapped = True
        except Exception:
            pass

        if not is_unwrapped:
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
