from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import URL, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from ..connection import (
    ConnectionCheckResult,
    ConnectionErrorKind,
    evaluate_sqlite_path,
    probe_engine,
)
from ..store import wrap_store_errors
from .base_sql import BaseSQLStore


def _classify_sqlite_error(exc: BaseException) -> ConnectionErrorKind:
    """Map a SQLite connection exception to a ConnectionErrorKind."""
    orig = getattr(exc, "orig", None) or exc
    if isinstance(orig, PermissionError | OSError):
        return ConnectionErrorKind.PERMISSION
    return ConnectionErrorKind.UNKNOWN


_LOGGER = logging.getLogger(__name__)

# SQLite stores datetimes as text; these mirror that representation.
_SQL_TS = "%Y-%m-%d %H:%M:%S.%f"


def _as_naive(raw: str) -> datetime:
    """Parse a stored timestamp into a naive datetime.

    The bounds are read with a raw ``text()`` query, so they arrive as the
    driver's own type for a SQLite TEXT column rather than through the
    ``UtcDateTime`` decorator.
    """
    return datetime.fromisoformat(raw).replace(tzinfo=None)


def _sql_ts(value: datetime) -> str:
    return value.strftime(_SQL_TS)


def _offset_at(moment: datetime, zone: tzinfo) -> int:
    """The zone's UTC offset in seconds at a naive local wall-clock time.

    Ambiguous times (the repeated hour of a fall-back) resolve to the first
    pass, and times in a spring-forward gap — which no telegram can carry —
    to the offset still in force before it. Both follow from fold=0.
    """
    delta = moment.replace(tzinfo=zone).utcoffset() or timedelta(0)
    return int(delta.total_seconds())


def _transition_between(before: datetime, after: datetime, zone: tzinfo, offset: int) -> datetime:
    """Bisect for the first second in (before, after] that no longer has ``offset``.

    The coarse walk only tells us a transition happened somewhere inside an
    hour. Several zones do not move on the hour — Pacific/Chatham changes at
    03:45, Lord Howe at 02:00 by half an hour — so rounding the boundary to the
    next hour would convert everything in between with the wrong offset.
    """
    while after - before > timedelta(seconds=1):
        middle = before + (after - before) / 2
        if _offset_at(middle, zone) == offset:
            before = middle
        else:
            after = middle
    return after.replace(microsecond=0)


def _offset_intervals(
    oldest: datetime, newest: datetime, zone: tzinfo, *, step: timedelta = timedelta(hours=1)
) -> list[tuple[datetime, datetime, int]]:
    """Split [oldest, newest] into stretches of constant UTC offset.

    Walks the range and coalesces, so a zone with daylight saving yields a
    couple of intervals per year rather than one entry per row. The walk is
    hourly because no zone holds an offset for less than that, but each
    boundary it finds is then bisected to the exact second.

    Returns (start, end_exclusive, offset_seconds) with the last interval
    extended past ``newest`` so the final rows are included.
    """
    intervals: list[tuple[datetime, datetime, int]] = []
    cursor = oldest.replace(minute=0, second=0, microsecond=0)
    end = newest + step
    current_offset: int | None = None
    start = cursor
    previous = cursor

    while cursor <= end:
        offset = _offset_at(cursor, zone)
        if current_offset is None:
            current_offset, start = offset, cursor
        elif offset != current_offset:
            boundary = _transition_between(previous, cursor, zone, current_offset)
            intervals.append((start, boundary, current_offset))
            current_offset, start = offset, boundary
        previous = cursor
        cursor += step

    if current_offset is not None:
        # Open-ended tail so rows at the very end are not missed.
        intervals.append((start, end + step, current_offset))
    return intervals


class SqliteStore(BaseSQLStore):
    """Async SQLite implementation of TelegramStore."""

    def __init__(
        self,
        db_path: str | Path,
        retention_days: int | None = None,
        *,
        read_only: bool = False,
        legacy_timestamp_timezone: tzinfo | None = None,
    ) -> None:
        """Initialize the SQLite store.

        With read_only=True the file is opened with sqlite's ``mode=ro`` (writes
        are impossible at the driver level), no DDL/migrations are run, and all
        mutating operations raise. Intended for reading a database owned and
        written by another process (e.g. Home Assistant's telegram store).

        ``legacy_timestamp_timezone`` is the timezone whose wall clock wrote the
        rows of a database created before timestamps were normalised to UTC. Pass
        it and initialize() converts them automatically, once. Callers that
        always stored ``datetime.now(UTC)`` pass ``UTC``, which marks the
        database converted without touching a row. Leave it unset and the
        conversion is skipped and merely offered — see migrate_timestamps_to_utc().
        """
        self._is_memory = str(db_path) == ":memory:"
        if self._is_memory:
            if read_only:
                raise ValueError("read_only is not supported for in-memory databases")
            url = URL.create("sqlite+aiosqlite", database=":memory:")
        else:
            path = Path(db_path)
            if read_only:
                encoded_path = quote(str(path), safe="/:")
                url = URL.create(
                    "sqlite+aiosqlite",
                    database=f"file:{encoded_path}",
                    query={"mode": "ro", "uri": "true"},
                )
            else:
                # Ensure parent directory exists
                path.parent.mkdir(parents=True, exist_ok=True)
                url = URL.create("sqlite+aiosqlite", database=str(path))

        # timeout is sqlite's busy timeout: with a concurrent writer (WAL or
        # rollback journal) readers wait instead of failing with SQLITE_BUSY.
        engine = create_async_engine(url, connect_args={"timeout": 10})
        self._legacy_timestamp_timezone = legacy_timestamp_timezone
        super().__init__(engine, retention_days, read_only=read_only)

    @staticmethod
    def check_config(db_path: str | Path, *, read_only: bool = False) -> ConnectionCheckResult:
        """Validate a SQLite path without constructing a store or touching the disk.

        Synchronous — this is a pure filesystem check (no I/O await). Returns a
        structured result indicating whether the file is writeable or can be
        created (or, with read_only=True, whether it exists and is readable).
        """
        return evaluate_sqlite_path(db_path, read_only=read_only)

    async def check_connection(self, *, timeout: float = 5.0) -> ConnectionCheckResult:
        """Probe the SQLite database with ``SELECT 1`` (creates an empty file if missing)."""
        return await probe_engine(self.engine, timeout=timeout, classify=_classify_sqlite_error)

    async def _size_bytes(self) -> int | None:
        """Return the database size in bytes (page_count * page_size)."""
        async with self.engine.connect() as conn:
            page_count = await conn.scalar(text("PRAGMA page_count"))
            page_size = await conn.scalar(text("PRAGMA page_size"))
        if page_count is None or page_size is None:
            return None
        return int(page_count) * int(page_size)

    @wrap_store_errors
    async def optimize(self) -> None:
        """Reclaim disk space freed by deletions.

        VACUUM rewrites the database file; it cannot run inside a transaction,
        blocks concurrent writers and temporarily needs up to twice the
        database size in free disk space.
        """
        self._ensure_writable()
        engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text("VACUUM"))

    async def initialize(self) -> None:
        """Set up the database schema and perform upgrades."""
        if self._read_only:
            # Another process owns the schema — never run DDL/migrations here.
            # Hosts should check needs_migration() and surface a version-skew
            # error instead of relying on this store to upgrade anything.
            await super().initialize()
            return

        if not self._is_memory:
            # WAL makes concurrent single-writer/multi-reader access safe
            # (readers don't block the writer and vice versa). Persists in the
            # database file; cannot be set inside a transaction.
            wal_engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
            async with wal_engine.connect() as conn:
                await conn.execute(text("PRAGMA journal_mode=WAL"))

        async with self.engine.begin() as conn:
            # 1. Create table if not exists
            await conn.run_sync(self._metadata.create_all)

            # 2. Perform column-level upgrades
            await conn.run_sync(self._upgrade_schema)

            # 2.1 Indexes the model declares but an existing database may lack (SpectrumKNX#450)
            await conn.run_sync(self.ensure_indexes)

            # 2.2 Classify the timestamp convention (XKNX/knx-frontend#459)
            await conn.run_sync(self._classify_timestamp_convention)

        # 3. Convert pre-UTC timestamps, if the caller told us what wrote them.
        #    Ahead of the cache warm below, which reads timestamps back.
        if self._legacy_timestamp_timezone is not None:
            await self.migrate_timestamps_to_utc(self._legacy_timestamp_timezone)

        # 4. Warm the cache
        await super().initialize()

    # ── Timestamp convention (XKNX/knx-frontend#459) ─────────────────────────
    #
    # Databases written before timestamps were normalised to UTC hold whatever
    # wall clock the writer passed in, with no offset to say which. Reading
    # those back as UTC shifts them by that offset. Which offset is not
    # recoverable from the data, and it differs between hosts using this very
    # library: Home Assistant writes its *configured* timezone (which need not
    # match the host's), while SpectrumKNX writes datetime.now(UTC). Guessing
    # from the system timezone would therefore fix the first and corrupt the
    # second. So the host names the zone once, via the constructor, and the
    # conversion then runs automatically on the next start like any other
    # migration. Unset, it stays a no-op the host can trigger by hand.

    _UTC_FLAG = "timestamps_utc"
    _UTC_BOUNDARY = "timestamps_utc_from"
    _LEGACY_MAX_ROWID = "timestamps_legacy_max_rowid"
    _UTC_SOURCE_ZONE = "timestamps_utc_source_zone"

    def _classify_timestamp_convention(self, connection) -> None:
        """Record whether this database's timestamps are already UTC.

        A database created now is UTC by construction. An empty one has nothing
        to convert, so it counts as UTC too. Anything else keeps its rows and
        gets markers recorded, so a later migration knows what predates the
        upgrade and cannot shift the same row twice: ``telegrams`` by its last
        rowid, exact because the table is append-only, and ``last_ga_telegrams``
        - upserted in place, where no rowid helps - by the instant from which
        its rows are UTC.
        """
        if self._metadata_flag_set(connection, self._UTC_FLAG):
            return

        # Both tables, because retention can empty telegrams while last values
        # remain: treating that as an empty database would flag it converted
        # and strand the rows that are left.
        has_rows = connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM telegrams) OR EXISTS (SELECT 1 FROM last_ga_telegrams)")
        ).scalar()
        if not has_rows:
            self._set_metadata_value(connection, self._UTC_FLAG, "true")
            return

        if self._metadata_value(connection, self._UTC_BOUNDARY) is None:
            if self._legacy_timestamp_timezone is not None:
                # initialize() converts right after this, before the store has
                # written a single row, so there is nothing to protect from a
                # second shift. Recording a boundary would only do harm: it is a
                # UTC instant, while these rows still hold local wall-clock
                # digits, so east of UTC it excludes exactly the rows written in
                # the hours before the upgrade - and they would then be flagged
                # converted without having been.
                return
            boundary = datetime.now(UTC).isoformat()
            self._set_metadata_value(connection, self._UTC_BOUNDARY, boundary)
            last_rowid = connection.execute(text("SELECT max(rowid) FROM telegrams")).scalar()
            self._set_metadata_value(connection, self._LEGACY_MAX_ROWID, str(last_rowid or 0))
            _LOGGER.warning(
                "This database predates UTC timestamp normalisation, so existing rows hold local "
                "wall-clock times and will read as UTC until converted. Telegrams stored from now "
                "on are UTC. Pass legacy_timestamp_timezone=<the zone that wrote them> to convert "
                "the existing ones on the next start, or call migrate_timestamps_to_utc()."
            )

    async def needs_timestamp_migration(self) -> bool:
        """Whether this database still holds pre-UTC timestamps."""
        async with self.engine.connect() as conn:
            return not await conn.run_sync(lambda c: self._metadata_flag_set(c, self._UTC_FLAG))

    @wrap_store_errors
    async def migrate_timestamps_to_utc(self, source_timezone: tzinfo) -> int:
        """Convert pre-UTC timestamps, interpreting them in ``source_timezone``.

        Only rows older than the boundary recorded at first start after the
        upgrade are touched, so calling this after the store has already written
        UTC rows cannot shift them a second time. Idempotent: once finished the
        database is flagged and further calls do nothing.

        Each table is rewritten by a single statement whose CASE carries one
        branch per stretch of constant UTC offset, so daylight saving is handled
        correctly and every row is read once and written once. Rows falling in a
        repeated hour are inherently ambiguous and resolve to the first pass.
        """
        self._ensure_writable()
        if not await self.needs_timestamp_migration():
            return 0

        async with self.engine.begin() as conn:
            scopes = await self._legacy_row_scopes(conn, source_timezone)

            converted = 0
            # last_ga_telegrams keeps one row per group address and is not subject
            # to retention, so it can hold values older than anything left in
            # telegrams — and can be non-empty when telegrams is empty. Its range
            # is therefore measured on its own rather than borrowed.
            for table, count_rows in (("telegrams", True), ("last_ga_telegrams", False)):
                rows = await self._convert_table_to_utc(conn, table, source_timezone, *scopes[table])
                if count_rows:
                    converted += rows

            await conn.run_sync(lambda c: self._set_metadata_value(c, self._UTC_FLAG, "true"))
            # Which zone was assumed, so a conversion done with the wrong one
            # can be recognised afterwards rather than inferred from the damage.
            await conn.run_sync(lambda c: self._set_metadata_value(c, self._UTC_SOURCE_ZONE, str(source_timezone)))

        _LOGGER.info("Converted %d telegram timestamps from %s to UTC", converted, source_timezone)
        return converted

    async def _legacy_row_scopes(self, conn, zone: tzinfo) -> dict[str, tuple[str, dict[str, object]]]:
        """Which rows of each table still predate the upgrade.

        ``telegrams`` is append-only, so the rowid recorded at the first start
        after the upgrade separates the two populations exactly - which a
        timestamp cannot, because the legacy rows hold local wall-clock digits
        while the ones written since hold UTC, and east of UTC those two ranges
        overlap. A marker above the table's current maximum means retention
        emptied it in between and rowids restarted, so it is discarded.

        ``last_ga_telegrams`` is upserted per group address and carries no such
        order, leaving only the boundary instant. Comparing it with wall-clock
        digits is exactly the overlap above, so the bound stays conservative: a
        row still holding a local time from the last hours before the upgrade is
        left alone rather than risk shifting an already converted one. It
        corrects itself the next time that group address is seen.

        One corner stays open, and cannot be closed without marking the rows
        themselves: retention emptying ``telegrams`` completely between the
        upgrade and the migration, so that rowids restart under the marker. A
        marker above the current maximum catches most of that, and the boundary
        read in the legacy frame catches rows written more than the zone's
        offset afterwards - but a row written into the emptied table within that
        window is indistinguishable from a legacy one.
        """
        boundary_raw = await conn.run_sync(lambda c: self._metadata_value(c, self._UTC_BOUNDARY))
        if not boundary_raw:
            # No boundary: nothing was written since the upgrade, so every row
            # in the database is legacy.
            return {"telegrams": ("1 = 1", {}), "last_ga_telegrams": ("1 = 1", {})}

        boundary = datetime.fromisoformat(boundary_raw).astimezone(UTC).replace(tzinfo=None)
        conservative: tuple[str, dict[str, object]] = ("timestamp < :boundary", {"boundary": _sql_ts(boundary)})
        scopes: dict[str, tuple[str, dict[str, object]]] = {
            "telegrams": conservative,
            "last_ga_telegrams": conservative,
        }

        marker_raw = await conn.run_sync(lambda c: self._metadata_value(c, self._LEGACY_MAX_ROWID))
        if marker_raw is not None:
            marker = int(marker_raw)
            current = (await conn.execute(text("SELECT max(rowid) FROM telegrams"))).scalar() or 0
            if current >= marker:
                # The legacy rows hold local wall-clock digits, so the boundary
                # is read in that same frame: east of UTC a row written just
                # before the upgrade carries digits above the boundary instant.
                boundary_local = datetime.fromisoformat(boundary_raw).astimezone(zone).replace(tzinfo=None)
                scopes["telegrams"] = (
                    "rowid <= :max_rowid AND timestamp < :boundary_local",
                    {"max_rowid": marker, "boundary_local": _sql_ts(boundary_local)},
                )

        return scopes

    async def _convert_table_to_utc(self, conn, table: str, zone: tzinfo, where: str, scope: dict[str, object]) -> int:
        """Shift one table's legacy timestamps into UTC with a single UPDATE.

        One statement rather than one per offset interval, because consecutive
        statements read a column the previous one has already written: west of
        UTC the shift moves timestamps *forward*, into the range a later
        interval then matches, and the row is converted twice.
        """
        bounds = (
            await conn.execute(
                text(f"SELECT min(timestamp), max(timestamp) FROM {table} WHERE {where}"),  # noqa: S608 - fixed names
                scope,
            )
        ).fetchone()
        if bounds is None or bounds[0] is None:
            return 0

        oldest, newest = _as_naive(bounds[0]), _as_naive(bounds[1])
        intervals = _offset_intervals(oldest, newest, zone)
        if not any(offset for _, _, offset in intervals):
            return 0  # the whole range was already UTC

        # SQLite's datetime() drops fractional seconds, so the shifted whole
        # seconds are recombined with the original fraction. COALESCE keeps a
        # row unchanged rather than nulling it should strftime reject the input.
        params: dict[str, object] = dict(scope)

        def shift_expr(index: int, offset: int) -> str:
            if not offset:
                return "timestamp"
            params[f"shift_{index}"] = f"{-offset} seconds"
            return f"strftime('%Y-%m-%d %H:%M:%S', timestamp, :shift_{index}) || substr(timestamp, 20)"

        if len(intervals) == 1:
            # A range with no transition in it needs no CASE at all, and would
            # otherwise produce "CASE ELSE ... END", which SQLite rejects.
            expression = shift_expr(0, intervals[0][2])
        else:
            branches = []
            for index, (_, end_of, offset) in enumerate(intervals[:-1]):
                params[f"end_{index}"] = _sql_ts(end_of)
                branches.append(f"WHEN timestamp < :end_{index} THEN {shift_expr(index, offset)}")
            last = len(intervals) - 1
            branches.append(f"ELSE {shift_expr(last, intervals[last][2])}")  # the open-ended tail
            expression = f"CASE {' '.join(branches)} END"

        result = await conn.execute(
            text(
                f"UPDATE {table} SET timestamp = COALESCE({expression}, timestamp) "  # noqa: S608 - fixed names
                f"WHERE {where}"
            ),
            params,
        )
        return result.rowcount or 0

    def _upgrade_schema(self, connection) -> None:
        """Synchronous part of schema upgrade (run via run_sync)."""
        inspector = inspect(connection)
        columns = inspector.get_columns("telegrams")
        existing_columns = {col["name"] for col in columns}

        cols_to_migrate = {
            "source": "source",
            "destination": "destination",
            "telegramtype": "telegramtype",
            "direction": "direction",
            "source_name": "source_name",
            "destination_name": "destination_name",
        }

        # Legacy columns that should not exist in the final schema
        legacy_columns = set(cols_to_migrate.values()) | {"dpt_name", "unit", "dpt_name_id", "unit_id"}

        # Check if any legacy columns still exist (drop failed in a previous run or first run)
        has_legacy_columns = bool(existing_columns & legacy_columns)
        has_old_string_cols = "source" in existing_columns  # Pre-normalized schema

        if has_old_string_cols:
            # 1a. Populate string_lookup table from old string columns
            for cat, old_col in cols_to_migrate.items():
                if old_col in existing_columns:
                    connection.execute(
                        text(
                            f"INSERT OR IGNORE INTO string_lookup (category, value) "
                            f"SELECT DISTINCT '{cat}', CAST({old_col} AS TEXT) FROM telegrams WHERE {old_col} IS NOT NULL"
                        )
                    )

            # 1b. Add *_id columns (nullable for now — will be enforced via table rebuild)
            for cat in cols_to_migrate:
                id_col = f"{cat}_id"
                if id_col not in existing_columns:
                    connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {id_col} INTEGER"))
                    existing_columns.add(id_col)

            # 1c. Populate *_id values from string_lookup
            for cat, old_col in cols_to_migrate.items():
                connection.execute(
                    text(
                        f"UPDATE telegrams SET {cat}_id = ("
                        f"SELECT id FROM string_lookup WHERE category='{cat}' AND value=CAST(telegrams.{old_col} AS TEXT))"
                    )
                )

        # 2. Add any missing expected columns (intermediate schema versions)
        expected_columns = {
            "payload": "JSON",
            "dpt_main": "INTEGER",
            "dpt_sub": "INTEGER",
            "value": "JSON",
            "value_numeric": "DOUBLE",
            "data_secure": "BOOLEAN",
        }
        for col_name, col_type in expected_columns.items():
            if col_name not in existing_columns and f"{col_name}_id" not in existing_columns:
                connection.execute(text(f"ALTER TABLE telegrams ADD COLUMN {col_name} {col_type}"))
                existing_columns.add(col_name)

        # 3. Rebuild the table to drop legacy columns (works on all SQLite versions and
        #    handles indexed columns correctly, unlike ALTER TABLE DROP COLUMN).
        if has_legacy_columns:
            # Determine the final set of columns we want to keep
            keep_cols = [
                "timestamp",
                "source_id",
                "destination_id",
                "telegramtype_id",
                "direction_id",
                "source_name_id",
                "destination_name_id",
                "payload",
                "dpt_main",
                "dpt_sub",
                "value",
                "value_numeric",
                "raw_data",
                "data_secure",
            ]
            # Only copy columns that actually exist to avoid errors
            copy_cols = [c for c in keep_cols if c in existing_columns]
            cols_sql = ", ".join(copy_cols)

            # Drop all indexes on the telegrams table before renaming — SQLite
            # preserves index names when renaming a table, which would conflict
            # with the new table's indexes created by metadata.create().
            old_indexes = inspector.get_indexes("telegrams")
            for idx in old_indexes:
                connection.execute(text(f"DROP INDEX IF EXISTS {idx['name']}"))

            connection.execute(text("DROP TABLE IF EXISTS _telegrams_old"))
            connection.execute(text("ALTER TABLE telegrams RENAME TO _telegrams_old"))
            # Recreate from SQLAlchemy metadata (enforces correct NOT NULL / types)
            self._metadata.tables["telegrams"].create(connection)
            connection.execute(text(f"INSERT INTO telegrams ({cols_sql}) SELECT {cols_sql} FROM _telegrams_old"))
            connection.execute(text("DROP TABLE _telegrams_old"))

        # 3.5. Populate value from value_numeric if it is missing/null (legacy SpectrumKNX
        # schema). The store_metadata flag marks completion so the unindexed WHERE clause
        # doesn't scan the whole telegrams table on every startup.
        if not self._metadata_flag_set(connection, "nulls_recovered"):
            if "value" in existing_columns and "value_numeric" in existing_columns:
                connection.execute(
                    text(
                        "UPDATE telegrams SET value = CAST(value_numeric AS TEXT) "
                        "WHERE (value IS NULL OR value = 'null') AND value_numeric IS NOT NULL"
                    )
                )
            connection.execute(
                text("INSERT OR REPLACE INTO store_metadata (key, value) VALUES ('nulls_recovered', 'true')")
            )

        # 4. Data unwrapping pass for legacy {"value": ...} wrapped structures
        if self._metadata_flag_set(connection, "data_unwrapped"):
            return

        try:
            # Query rowid, value, payload from telegrams where they are legacy JSON wrapped
            rows = connection.execute(
                text(
                    "SELECT rowid, value, payload FROM telegrams WHERE value LIKE '{\"value\":%' OR payload LIKE '{\"value\":%'"
                )
            ).fetchall()

            if rows:
                import json

                for row in rows:
                    row_id = row[0]
                    val_str = row[1]
                    pay_str = row[2]

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
                            text("UPDATE telegrams SET value = :value, payload = :payload WHERE rowid = :rowid"),
                            {"value": json_val, "payload": json_pay, "rowid": row_id},
                        )

            # Record successful migration state in store_metadata
            connection.execute(
                text("INSERT OR REPLACE INTO store_metadata (key, value) VALUES ('data_unwrapped', 'true')")
            )
        except Exception:
            pass

    def _needs_migration_sync(self, connection) -> bool:
        """Synchronously check if legacy SQLite schema migration is required."""
        inspector = inspect(connection)
        if not inspector.has_table("telegrams"):
            return False
        columns = inspector.get_columns("telegrams")
        existing_columns = {col["name"] for col in columns}

        # 1. Pre-normalized legacy schema (string columns instead of *_id)
        if "source" in existing_columns:
            return True

        # 2. Partially-migrated: old string columns still present (DROP COLUMN
        #    failed silently on a previous run, e.g. due to indexed columns).
        legacy_string_cols = {"destination", "telegramtype", "direction", "source_name", "destination_name"}
        if existing_columns & legacy_string_cols:
            return True

        # 3. Legacy intermediate columns from earlier schema versions
        for col in ["dpt_name_id", "unit_id", "dpt_name", "unit"]:
            if col in existing_columns:
                return True

        # 4. Missing expected columns from intermediate versions
        expected_columns = {
            "payload",
            "dpt_main",
            "dpt_sub",
            "value",
            "value_numeric",
            "data_secure",
        }
        for col_name in expected_columns:
            if col_name not in existing_columns and f"{col_name}_id" not in existing_columns:
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
                        "SELECT 1 FROM telegrams WHERE (value IS NULL OR value = 'null') AND value_numeric IS NOT NULL LIMIT 1"
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
                        "SELECT 1 FROM telegrams WHERE value LIKE '{\"value\":%' OR payload LIKE '{\"value\":%' LIMIT 1"
                    )
                ).fetchone()
                if row:
                    return True
            except Exception:
                pass

        return False
