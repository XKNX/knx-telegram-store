from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .connection import ConnectionCheckResult
from .model import StoredTelegram
from .query import TelegramQuery, TelegramQueryResult

P = ParamSpec("P")
T = TypeVar("T")


class KnxTelegramStoreException(Exception):
    """Base exception for all KNX telegram store operations."""


def wrap_store_errors(func: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:  # noqa: UP047
    """Wrap any exception thrown by a store operation in KnxTelegramStoreException."""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except KnxTelegramStoreException:
            raise
        except Exception as err:
            raise KnxTelegramStoreException(f"Database error during {func.__name__}: {err}") from err

    return wrapper


@dataclass(frozen=True, slots=True)
class StoreCapabilities:
    """Declares what a backend can do natively."""

    supports_time_range: bool = False
    supports_time_delta: bool = False
    supports_pagination: bool = False
    supports_count: bool = False
    supports_size_stats: bool = False
    supports_optimize: bool = False
    max_storage: int | None = None  # None = unlimited


@dataclass(frozen=True, slots=True)
class StoreStats:
    """Snapshot of the store's contents and storage footprint."""

    telegram_count: int
    oldest_timestamp: datetime | None
    newest_timestamp: datetime | None
    size_bytes: int | None  # None if the backend cannot report a size
    backend: str
    retention_days: int | None


class TelegramStore(ABC):
    """Abstract interface for KNX telegram persistence."""

    @property
    @abstractmethod
    def retention_days(self) -> int | None:
        """Return the configured retention period in days, or None if disabled."""

    @property
    @abstractmethod
    def max_telegrams(self) -> int | None:
        """Return the configured maximum number of telegrams, or None if unlimited."""

    @property
    @abstractmethod
    def capabilities(self) -> StoreCapabilities:
        """Return the capabilities of this backend."""

    async def needs_migration(self) -> bool:
        """Return True if the store requires a database schema migration/upgrade."""
        return False

    async def check_connection(self) -> ConnectionCheckResult:
        """Probe live connectivity without running migrations.

        Returns a structured result describing whether the store is reachable
        and, if not, why. Safe to call before initialize(). Default: assume
        reachable (in-memory backends).
        """
        return ConnectionCheckResult.success()

    @abstractmethod
    async def initialize(self) -> None:
        """Set up the store (create tables, open connections, etc.).

        Called once at startup. Must be idempotent.
        """

    @abstractmethod
    async def close(self) -> None:
        """Tear down the store (close connections, flush buffers).

        Called once at shutdown.
        """

    @abstractmethod
    async def store(self, telegram: StoredTelegram) -> None:
        """Persist a single telegram."""

    @abstractmethod
    async def store_many(self, telegrams: Sequence[StoredTelegram]) -> None:
        """Persist multiple telegrams in a single batch."""

    @abstractmethod
    async def query(self, query: TelegramQuery) -> TelegramQueryResult:
        """Retrieve telegrams matching the given query.

        All backends MUST implement full filtering as defined in TelegramQuery.
        """

    @abstractmethod
    async def count(self) -> int:
        """Return the total number of stored telegrams."""

    async def get_stats(self) -> StoreStats:
        """Return a snapshot of the store's contents and storage footprint.

        Default implementation only fills the count; backends should override
        to report timestamps and size where they can do so natively.
        """
        return StoreStats(
            telegram_count=await self.count(),
            oldest_timestamp=None,
            newest_timestamp=None,
            size_bytes=None,
            backend=type(self).__name__,
            retention_days=self.retention_days,
        )

    async def optimize(self) -> None:  # noqa: B027 — intentional no-op default, not abstract
        """Reclaim storage space after deletions (e.g. VACUUM).

        No-op by default; backends that support it set
        capabilities.supports_optimize and override. May block writers and
        take a long time on large databases.
        """

    @abstractmethod
    async def evict_older_than(self, cutoff: datetime, *, dry_run: bool = False) -> int:
        """Delete all telegrams with timestamp < cutoff.

        If dry_run is True, return the count that would be deleted without deleting.
        Returns count of (would-be) deleted rows.
        """

    @abstractmethod
    async def evict_expired(self, *, dry_run: bool = False) -> int:
        """Delete telegrams older than the configured retention period.

        If dry_run is True, return the count that would be deleted without deleting.
        Returns count of (would-be) deleted rows.
        """

    @abstractmethod
    async def get_last_unique_telegrams(self) -> list[StoredTelegram]:
        """Retrieve the latest unique telegram for each destination group address."""

    async def clear(self) -> None:
        """Remove all stored telegrams.

        Optional — backends may raise NotImplementedError.
        """
        raise NotImplementedError
