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

    offset = 0
    total_migrated = 0
    while True:
        # We query in ascending order to migrate oldest first
        result = await source.query(TelegramQuery(limit=batch_size, offset=offset, order_descending=False))
        if not result.telegrams:
            break

        new_telegrams = [t for t in result.telegrams if t.timestamp not in existing_timestamps]

        if new_telegrams:
            await dest.store_many(new_telegrams)
            total_migrated += len(new_telegrams)
            # Update local cache to avoid duplicates if source has any
            for t in new_telegrams:
                existing_timestamps.add(t.timestamp)

        offset += batch_size
        print(
            f"Processed {min(offset, source_count)}/{source_count} telegrams, migrated {total_migrated} new entries..."
        )

        if not result.limit_reached:
            break

    print("\nMigration completed!")
    print(f"Total telegrams processed: {min(offset, source_count)}")
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

    args = parser.parse_args()

    source = get_store(args.src_type, args.src_uri)
    dest = get_store(args.dest_type, args.dest_uri)

    try:
        await migrate(source, dest, batch_size=args.batch_size)
    finally:
        await source.close()
        await dest.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
