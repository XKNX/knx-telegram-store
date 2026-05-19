#!/usr/bin/env python3
"""Migrate sample_telegrams.db to the normalized schema and verify a random subset.

Usage:
    cd knx-telegram-store
    .venv/bin/python tools/migrate_sample_db.py [--sample-size N]

The source file is read-only; it is copied to testdata/sample_telegrams_migrated.db
before the in-place schema migration runs.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from knx_telegram_store import TelegramQuery
from knx_telegram_store.backends.sqlite import SqliteStore

TESTDATA = Path(__file__).parent.parent / "testdata"
SRC = TESTDATA / "sample_telegrams.db"
DST = TESTDATA / "sample_telegrams_migrated.db"


# ---------------------------------------------------------------------------
# Read the OLD (flat-column) schema directly without touching it
# ---------------------------------------------------------------------------


@dataclass
class RawRow:
    timestamp: object
    source: str
    destination: str
    telegramtype: str
    direction: str
    dpt_name: str | None
    unit: str | None
    source_name: str
    destination_name: str
    payload: object
    dpt_main: int | None
    dpt_sub: int | None
    value: object
    value_numeric: float | None
    raw_data: str | None
    data_secure: bool | None


async def read_old_rows(db_path: Path) -> list[RawRow]:
    """Read every row from the source schema (detects if flat or normalized)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.connect() as conn:

            def _get_info(sync_conn):
                inspector = inspect(sync_conn)
                cols = {c["name"] for c in inspector.get_columns("telegrams")}
                tables = set(inspector.get_table_names())
                return cols, tables

            col_names, table_names = await conn.run_sync(_get_info)

            # If it's already normalized, we need to join string_lookup to read values
            if "source_id" in col_names and "string_lookup" in table_names:
                print("  (Detected already-normalized source schema)")
                # Minimal join logic to read the same fields as RawRow
                stmt = text("""
                    SELECT
                        t.timestamp,
                        s.value as source,
                        d.value as destination,
                        tt.value as telegramtype,
                        di.value as direction,
                        dn.value as dpt_name,
                        u.value as unit,
                        sn.value as source_name,
                        den.value as destination_name,
                        t.payload, t.dpt_main, t.dpt_sub, t.value, t.value_numeric, t.raw_data, t.data_secure
                    FROM telegrams t
                    JOIN string_lookup s ON t.source_id = s.id AND s.category = 'source'
                    JOIN string_lookup d ON t.destination_id = d.id AND d.category = 'destination'
                    JOIN string_lookup tt ON t.telegramtype_id = tt.id AND tt.category = 'telegramtype'
                    JOIN string_lookup di ON t.direction_id = di.id AND di.category = 'direction'
                    LEFT JOIN string_lookup dn ON t.dpt_name_id = dn.id AND dn.category = 'dpt_name'
                    LEFT JOIN string_lookup u ON t.unit_id = u.id AND u.category = 'unit'
                    LEFT JOIN string_lookup sn ON t.source_name_id = sn.id AND sn.category = 'source_name'
                    LEFT JOIN string_lookup den ON t.destination_name_id = den.id AND den.category = 'destination_name'
                    ORDER BY t.timestamp ASC
                """)
            else:
                print("  (Detected legacy flat source schema)")

                # Build column list defensively
                def _col(name: str, default="NULL"):
                    return name if name in col_names else f"{default} AS {name}"

                stmt = text(f"""
                    SELECT
                        timestamp,
                        {_col("source", "''")},
                        {_col("destination", "''")},
                        {_col("telegramtype", "''")},
                        {_col("direction", "''")},
                        {_col("dpt_name")},
                        {_col("unit")},
                        {_col("source_name", "''")},
                        {_col("destination_name", "''")},
                        {_col("payload")},
                        {_col("dpt_main")},
                        {_col("dpt_sub")},
                        {_col("value")},
                        {_col("value_numeric")},
                        {_col("raw_data")},
                        {_col("data_secure")}
                    FROM telegrams
                    ORDER BY timestamp ASC
                """)

            result = await conn.execute(stmt)
            rows = result.fetchall()

        parsed_rows = []
        for r in rows:
            # Parse timestamp if it's a string
            ts = r[0]
            if isinstance(ts, str):
                # SQLite datetime strings can vary; naive attempt
                from datetime import datetime

                try:
                    ts = datetime.fromisoformat(ts.replace(" ", "T"))
                except ValueError:
                    pass

            parsed_rows.append(
                RawRow(
                    timestamp=ts,
                    source=r[1] or "",
                    destination=r[2] or "",
                    telegramtype=r[3] or "",
                    direction=r[4] or "",
                    dpt_name=r[5],
                    unit=r[6],
                    source_name=r[7] or "",
                    destination_name=r[8] or "",
                    payload=r[9],
                    dpt_main=r[10],
                    dpt_sub=r[11],
                    value=r[12],
                    value_numeric=r[13],
                    raw_data=r[14],
                    data_secure=r[15],
                )
            )
        return parsed_rows
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def migrate(dst: Path) -> SqliteStore:
    """Copy source → dst, then run initialize() to migrate the schema in place."""
    print(f"Copying {SRC} → {dst} …")
    shutil.copy2(SRC, dst)

    store = SqliteStore(dst)
    print("Running SqliteStore.initialize() (schema migration) …")
    await store.initialize()
    print("  Done.")
    return store


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _norm(v):
    """Normalise a value for comparison (None/'' equivalence, float rounding)."""
    if v is None or v == "":
        return None
    if isinstance(v, float):
        return round(v, 6)
    return v


async def verify(old_rows: list[RawRow], store: SqliteStore, sample_size: int) -> bool:
    """Randomly sample rows and compare old vs new using offset-based lookup.

    Both the old read and the new store query use ORDER BY timestamp ASC, so
    row at index i in old_rows corresponds to offset=i in the new store.
    This avoids all datetime parsing / timezone-suffix comparison issues.
    """
    total = len(old_rows)
    if total == 0:
        print("Source DB is empty — nothing to compare.")
        return True

    new_count = await store.count()
    if new_count != total:
        print(f"WARNING: row count mismatch — old={total}, new={new_count}")
        print("         Offset-based comparison may be unreliable; continuing anyway.")

    n = min(sample_size, total)
    indices = sorted(random.sample(range(total), n))
    print(f"\nComparing {n} randomly sampled rows (out of {total}) …")

    failures = 0
    for i, idx in enumerate(indices):
        old = old_rows[idx]

        # Use offset to find the exact same row in the migrated DB
        result = await store.query(TelegramQuery(offset=idx, limit=1, order_descending=False))

        if not result.telegrams:
            print(f"  [{i + 1}/{n}] FAIL  idx={idx} ts={old.timestamp!r}  → no row at this offset in migrated DB")
            failures += 1
            continue

        match = result.telegrams[0]

        mismatches = []
        checks = [
            ("source", old.source or "", match.source),
            ("destination", old.destination or "", match.destination),
            ("telegramtype", old.telegramtype or "", match.telegramtype),
            ("direction", old.direction or "", match.direction),
            ("source_name", old.source_name or "", match.source_name),
            ("destination_name", old.destination_name or "", match.destination_name),
            ("dpt_main", old.dpt_main, match.dpt_main),
            ("dpt_sub", old.dpt_sub, match.dpt_sub),
            ("value_numeric", _norm(old.value_numeric), _norm(match.value_numeric)),
            ("raw_data", _norm(old.raw_data), _norm(match.raw_data)),
            ("data_secure", old.data_secure, match.data_secure),
        ]
        for field, oval, nval in checks:
            if oval != nval:
                mismatches.append(f"{field}: {oval!r} → {nval!r}")

        if mismatches:
            print(f"  [{i + 1}/{n}] FAIL  idx={idx} ts={old.timestamp}: " + "; ".join(mismatches))
            failures += 1
        else:
            print(f"  [{i + 1}/{n}] OK    idx={idx} ts={old.timestamp}")

    print()
    if failures:
        print(f"RESULT: {failures}/{n} mismatches found ✗")
        return False
    print(f"RESULT: all {n} samples match ✓")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(sample_size: int, force: bool) -> None:
    if not SRC.exists():
        print(f"ERROR: source DB not found at {SRC}", file=sys.stderr)
        sys.exit(1)

    if DST.exists():
        if force:
            DST.unlink()
            print(f"Removed existing {DST}")
        else:
            print(f"ERROR: {DST} already exists. Use --force to overwrite.", file=sys.stderr)
            sys.exit(1)

    print(f"Source: {SRC}")
    print(f"Target: {DST}\n")

    print("Reading old schema rows (read-only) …")
    old_rows = await read_old_rows(SRC)
    print(f"  Found {len(old_rows)} rows in old DB.\n")

    store = await migrate(DST)

    new_count = await store.count()
    print(f"\nNew store contains {new_count} telegrams.")

    if new_count != len(old_rows):
        print(f"WARNING: row count mismatch — old={len(old_rows)}, new={new_count}")

    ok = await verify(old_rows, store, sample_size)
    await store.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate and verify sample_telegrams.db")
    parser.add_argument("--sample-size", type=int, default=50, help="Number of rows to spot-check (default: 50)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing migrated DB")
    args = parser.parse_args()
    asyncio.run(main(args.sample_size, args.force))
