from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import timedelta

from ..model import StoredTelegram
from ..query import TelegramQuery, TelegramQueryResult
from ..store import StoreCapabilities, TelegramStore


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

    async def initialize(self) -> None:
        """Set up the store. Idempotent."""

    async def close(self) -> None:
        """Tear down the store."""

    async def store(self, telegram: StoredTelegram) -> None:
        """Persist a single telegram."""
        self._telegrams.append(telegram)

    async def store_many(self, telegrams: Sequence[StoredTelegram]) -> None:
        """Persist multiple telegrams in a single batch."""
        self._telegrams.extend(telegrams)

    async def query(self, query: TelegramQuery) -> TelegramQueryResult:
        """Retrieve telegrams matching the given query."""
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
        if query.dpt_mains:
            results = [t for t in results if t.dpt_main in query.dpt_mains]

        # 2. Time range
        if query.start_time:
            results = [t for t in results if t.timestamp >= query.start_time]
        if query.end_time:
            results = [t for t in results if t.timestamp <= query.end_time]

        # 3. Time-delta context window
        if query.delta_before_ms > 0 or query.delta_after_ms > 0:
            pivot_timestamps = [t.timestamp for t in results]
            
            delta_before = timedelta(milliseconds=query.delta_before_ms)
            delta_after = timedelta(milliseconds=query.delta_after_ms)
            
            # Re-collect all telegrams within any pivot's window
            # This implementation is O(N*M) but N is small for MemoryStore (500)
            context_results = set()
            for t in self._telegrams:
                for pivot_ts in pivot_timestamps:
                    if (pivot_ts - delta_before) <= t.timestamp <= (pivot_ts + delta_after):
                        context_results.add(t)
                        break
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

    async def count(self) -> int:
        """Return the total number of stored telegrams."""
        return len(self._telegrams)

    async def clear(self) -> None:
        """Remove all stored telegrams."""
        self._telegrams.clear()
