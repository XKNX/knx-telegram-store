#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from knx_telegram_store import TelegramQuery
from knx_telegram_store.backends.postgres import PostgresStore
from knx_telegram_store.backends.sqlite import SqliteStore

if TYPE_CHECKING:
    from knx_telegram_store.store import TelegramStore


async def migrate(
    source: TelegramStore,
    dest: TelegramStore,
    batch_size: int = 5000,
    limit_newest: int | None = None,
) -> None:
    """Migrate telegrams from source to destination."""
    print(f"Initializing source store ({type(source).__name__})... (may take a while)")
    await source.initialize()
    print(f"Initializing destination store ({type(dest).__name__})... (may take a while)")
    await dest.initialize()

    print("Fetching existing telegrams from destination to avoid duplicates...")
    # Fetch all to build a set of timestamps. We use a high limit.
    # If the destination is very large, this might need optimization.
    dest_count = await dest.count()
    existing_timestamps: set[datetime] = set()
    if dest_count > 0:
        offset = 0
        while True:
            result = await dest.query(TelegramQuery(limit=batch_size, offset=offset))
            for t in result.telegrams:
                existing_timestamps.add(t.timestamp)
            offset += batch_size
            print(f"  Loaded {len(existing_timestamps)} timestamps...")
            if not result.limit_reached:
                break
    print(f"Found {len(existing_timestamps)} existing telegrams in destination.")

    print("Migrating from source...")
    source_count = await source.count()
    print(f"Source contains {source_count} telegrams.")

    # Determine order and limits
    descending = limit_newest is not None
    limit_count = limit_newest if limit_newest is not None else source_count
    if limit_newest is not None:
        print(f"Limiting migration to the newest {limit_newest} telegrams.")

    offset = 0
    total_processed = 0
    total_migrated = 0
    while total_processed < limit_count:
        current_batch_size = min(batch_size, limit_count - total_processed)
        if current_batch_size <= 0:
            break

        # If limiting newest, query descending (newest first)
        result = await source.query(TelegramQuery(limit=current_batch_size, offset=offset, order_descending=descending))
        if not result.telegrams:
            break

        new_telegrams = [t for t in result.telegrams if t.timestamp not in existing_timestamps]

        if new_telegrams:
            await dest.store_many(new_telegrams)
            total_migrated += len(new_telegrams)
            # Update local cache to avoid duplicates if source has any
            for t in new_telegrams:
                existing_timestamps.add(t.timestamp)

        total_processed += len(result.telegrams)
        offset += len(result.telegrams)
        print(f"Processed {total_processed}/{limit_count} target telegrams, migrated {total_migrated} new entries...")

        if not result.limit_reached:
            break

    print("\nMigration completed!")
    print(f"Total telegrams processed: {total_processed}")
    print(f"New telegrams migrated:   {total_migrated}")


def get_store(backend_type: str, uri: str) -> TelegramStore:
    """Create a store instance based on type and URI."""
    if backend_type == "sqlite":
        return SqliteStore(Path(uri))
    if backend_type == "postgres":
        return PostgresStore(uri)
    raise ValueError(f"Unknown backend type: {backend_type}")


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Migrate KNX telegrams between backends.")
    parser.add_argument("--src-type", choices=["sqlite", "postgres"], required=True, help="Source backend type")
    parser.add_argument("--src-uri", required=True, help="Source URI (file path or DSN)")
    parser.add_argument(
        "--dest-type",
        choices=["sqlite", "postgres"],
        required=True,
        help="Destination backend type",
    )
    parser.add_argument("--dest-uri", required=True, help="Destination URI (file path or DSN)")
    parser.add_argument("--batch-size", type=int, default=5000, help="Batch size for migration")
    parser.add_argument(
        "--limit-newest", type=int, default=None, help="Limit migration to the newest N telegrams from source"
    )

    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only validate source/destination connections, then exit",
    )

    args = parser.parse_args()

    source = get_store(args.src_type, args.src_uri)
    dest = get_store(args.dest_type, args.dest_uri)

    try:
        # Pre-flight: verify both stores are reachable before doing any work.
        ok = True
        for label, store in (("Source", source), ("Destination", dest)):
            result = await store.check_connection()
            if result.ok:
                print(f"{label} connection OK: {result.message}")
            else:
                ok = False
                print(f"{label} connection FAILED [{result.kind.value}]: {result.message}")
                if result.detail:
                    print(f"  detail: {result.detail}")
        if not ok:
            sys.exit(1)

        if args.check_only:
            print("Connection checks passed.")
            return

        await migrate(source, dest, batch_size=args.batch_size, limit_newest=args.limit_newest)
    finally:
        await source.close()
        await dest.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
