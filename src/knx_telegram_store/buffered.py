from __future__ import annotations

import asyncio
import logging
from typing import Any

from .backends.postgres import PostgresStore
from .backends.sqlite import SqliteStore
from .model import StoredTelegram
from .query import TelegramQuery, TelegramQueryResult
from .store import StoreStats, wrap_store_errors

_LOGGER = logging.getLogger(__name__)


class _BufferMixin:
    """Write-buffering mixin for SQL-backed TelegramStore subclasses.

    Place this before the concrete store in the MRO:

        class BufferedSqliteStore(_BufferMixin, SqliteStore): ...

    The mixin intercepts store() / store_sync() and accumulates telegrams in an
    in-memory list.  A periodic background task (start/stop) drains the buffer
    by calling self.store_many(), which resolves to the SQL backend via MRO.

    Behaviour:
    - store() / store_sync() are O(1) and never touch the database.
    - flush() atomically drains the buffer and delegates to store_many().
    - On flush failure the batch is re-prepended so no writes are lost.
    - query() accepts flush_first=True to guarantee read-your-writes consistency.
    - clear() wipes both the in-memory buffer and the underlying table.
    """

    def __init__(self, *args: Any, flush_interval: float = 1.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._buffer: list[StoredTelegram] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._closing = False
        self.flush_interval = flush_interval

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the periodic flush task."""
        if self._flush_task is not None:
            return
        self._flush_task = asyncio.create_task(self._flush_loop())

    @wrap_store_errors
    async def stop(self) -> None:
        """Stop the periodic flush task, perform a final flush, then close."""
        self._closing = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        await self._flush()
        await self.close()  # type: ignore[attr-defined]

    async def _flush_loop(self) -> None:
        """Periodic flush loop."""
        while not self._closing:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.exception("Unexpected error in flush loop: %s", err)

    # --- Write path ---

    @wrap_store_errors
    async def store(self, telegram: StoredTelegram) -> None:
        """Append a telegram to the buffer (non-blocking, no I/O)."""
        if self._closing:
            _LOGGER.warning("Store is closing, dropping telegram")
            return
        self._buffer.append(telegram)

    def store_sync(self, telegram: StoredTelegram) -> None:
        """Synchronous alias for store(), for use inside sync callbacks."""
        if self._closing:
            _LOGGER.warning("Store is closing, dropping telegram")
            return
        self._buffer.append(telegram)

    async def _flush(self) -> None:
        """Drain the buffer into the backing store (private implementation)."""
        if not self._buffer:
            return

        batch = self._buffer.copy()
        self._buffer.clear()

        try:
            await self.store_many(batch)  # type: ignore[attr-defined]
        except Exception as err:
            _LOGGER.error("Error flushing telegram buffer: %s", err)
            # Re-prepend the batch so it's retried before any newer items
            self._buffer[0:0] = batch

    @wrap_store_errors
    async def flush(self) -> None:
        """Flush the buffer now and reset the periodic timer.

        Resets the flush_interval countdown so the next automatic flush is
        flush_interval seconds from now, avoiding a near-immediate double-flush
        when flush() is called close to a scheduled tick.
        """
        await self._flush()
        # Reset the periodic timer so the next auto-flush starts fresh
        if self._flush_task is not None and not self._closing:
            self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._flush_loop())

    # --- Read path override (flush_first support) ---

    @wrap_store_errors
    async def query(self, query: TelegramQuery, *, flush_first: bool = False) -> TelegramQueryResult:
        """Query the store.

        Args:
            query: The query to execute.
            flush_first: If True, flush buffered writes before querying so that
                all previously stored telegrams are visible in the result.
                Defaults to False; set to True when read-your-writes consistency
                is required (e.g. after a bulk import).
        """
        if flush_first:
            await self.flush()
        return await super().query(query)  # type: ignore[misc, no-any-return]

    @wrap_store_errors
    async def get_last_unique_telegrams(self) -> list[StoredTelegram]:
        """Retrieve the latest unique telegram for each destination group address."""
        await self.flush()
        return await super().get_last_unique_telegrams()  # type: ignore[misc, no-any-return]

    @wrap_store_errors
    async def get_stats(self) -> StoreStats:
        """Return store stats, flushing buffered writes first so counts are current."""
        await self.flush()
        return await super().get_stats()  # type: ignore[misc, no-any-return]

    @wrap_store_errors
    async def optimize(self) -> None:
        """Flush buffered writes, then reclaim space in the backing store."""
        await self.flush()
        await super().optimize()  # type: ignore[misc]

    # --- Clear overrride (wipe buffer + table) ---

    @wrap_store_errors
    async def clear(self) -> None:
        """Clear both the in-memory buffer and the underlying table."""
        self._buffer.clear()
        await super().clear()  # type: ignore[misc]


class BufferedSqliteStore(_BufferMixin, SqliteStore):
    """SqliteStore with transparent write-buffering.

    Args:
        db_path: Path to the SQLite database file, or ``:memory:``.
        retention_days: Optional retention period in days.
        flush_interval: Seconds between automatic buffer flushes (default 1.0).
    """


class BufferedPostgresStore(_BufferMixin, PostgresStore):
    """PostgresStore with transparent write-buffering.

    Args:
        dsn: PostgreSQL connection string.
        retention_days: Optional retention period in days.
        flush_interval: Seconds between automatic buffer flushes (default 1.0).
    """
