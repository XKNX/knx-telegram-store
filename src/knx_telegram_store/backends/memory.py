from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Sequence
from datetime import datetime, timedelta

from ..model import StoredTelegram
from ..query import TelegramQuery, TelegramQueryResult
from ..store import StoreCapabilities, StoreStats, TelegramStore, wrap_store_errors


class MemoryStore(TelegramStore):
    """In-memory implementation of TelegramStore using a deque."""

    def __init__(self, max_telegrams: int = 500) -> None:
        """Initialize the memory store."""
        self._max_telegrams = max_telegrams
        self._telegrams: deque[StoredTelegram] = deque(maxlen=max_telegrams)
        self._capabilities = StoreCapabilities(
            supports_time_range=True,
            supports_time_delta=True,
            supports_pagination=True,
            supports_count=True,
            max_storage=max_telegrams,
        )

    @property
    def capabilities(self) -> StoreCapabilities:
        """Return the capabilities of this backend."""
        return self._capabilities

    @property
    def retention_days(self) -> int | None:
        """Memory store does not support time-based retention."""
        return None

    @property
    def max_telegrams(self) -> int | None:
        """Return the configured maximum number of telegrams."""
        return self._max_telegrams

    @wrap_store_errors
    async def initialize(self) -> None:
        """Set up the store. Idempotent."""

    @wrap_store_errors
    async def close(self) -> None:
        """Tear down the store."""

    @wrap_store_errors
    async def store(self, telegram: StoredTelegram) -> None:
        """Persist a single telegram."""
        self._telegrams.append(telegram)

    @wrap_store_errors
    async def store_many(self, telegrams: Sequence[StoredTelegram]) -> None:
        """Persist multiple telegrams in a single batch."""
        self._telegrams.extend(telegrams)

    @wrap_store_errors
    async def query(self, query: TelegramQuery, *, flush_first: bool = False) -> TelegramQueryResult:
        """Retrieve telegrams matching the given query.

        flush_first is accepted for signature compatibility with the buffered
        stores; there is no write buffer here, so it is a no-op.
        """
        results = list(self._telegrams)

        # 1. Multi-value filters (AND across, OR within)
        if query.sources:
            results = [t for t in results if t.source in query.sources]
        if query.destinations:
            results = [t for t in results if t.destination in query.destinations]
        if query.telegram_types:
            results = [t for t in results if t.telegramtype in query.telegram_types]
        if query.directions:
            results = [t for t in results if t.direction in query.directions]
        if query.dpt_mains or query.dpts:

            def dpt_match(t: StoredTelegram) -> bool:
                if t.dpt_main in query.dpt_mains:
                    return True
                return any(t.dpt_main == main and sub in (None, t.dpt_sub) for main, sub in query.dpts)

            results = [t for t in results if dpt_match(t)]

        # 2. Time range
        if query.start_time:
            results = [t for t in results if t.timestamp >= query.start_time]
        if query.end_time:
            results = [t for t in results if t.timestamp <= query.end_time]

        # 3. Time-delta context window
        if query.delta_before_ms > 0 or query.delta_after_ms > 0:
            pivot_timestamps = sorted(t.timestamp for t in results)

            delta_before = timedelta(milliseconds=query.delta_before_ms)
            delta_after = timedelta(milliseconds=query.delta_after_ms)

            # Re-collect all telegrams within any pivot's window
            context_results = set()
            for t in self._telegrams:
                low_bound = t.timestamp - delta_after
                high_bound = t.timestamp + delta_before

                idx = bisect.bisect_left(pivot_timestamps, low_bound)
                if idx < len(pivot_timestamps) and pivot_timestamps[idx] <= high_bound:
                    context_results.add(t)
            results = list(context_results)

        # 4. Ordering
        results.sort(key=lambda t: t.timestamp, reverse=query.order_descending)

        total_count = len(results)

        # 5. Pagination
        start = query.offset
        end = query.offset + query.limit
        paginated_results = results[start:end]

        limit_reached = len(results) > end

        return TelegramQueryResult(
            telegrams=paginated_results,
            total_count=total_count,
            limit_reached=limit_reached,
        )

    @wrap_store_errors
    async def count(self) -> int:
        """Return the total number of stored telegrams."""
        return len(self._telegrams)

    @wrap_store_errors
    async def get_stats(self) -> StoreStats:
        """Return a snapshot of the store's contents."""
        timestamps = [t.timestamp for t in self._telegrams]
        return StoreStats(
            telegram_count=len(self._telegrams),
            oldest_timestamp=min(timestamps) if timestamps else None,
            newest_timestamp=max(timestamps) if timestamps else None,
            size_bytes=None,
            backend="memory",
            retention_days=None,
        )

    @wrap_store_errors
    async def evict_older_than(self, cutoff: datetime, *, dry_run: bool = False) -> int:
        """Memory store uses max_telegrams for pruning."""
        return 0

    @wrap_store_errors
    async def evict_expired(self, *, dry_run: bool = False) -> int:
        """Memory store uses max_telegrams for pruning."""
        return 0

    @wrap_store_errors
    async def get_last_unique_telegrams(self) -> list[StoredTelegram]:
        """Retrieve the latest unique telegram for each destination group address."""
        last_ga: dict[str, StoredTelegram] = {}
        for t in self._telegrams:
            last_ga[t.destination] = t
        return list(last_ga.values())

    @wrap_store_errors
    async def clear(self) -> None:
        """Remove all stored telegrams."""
        self._telegrams.clear()
