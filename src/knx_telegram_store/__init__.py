from typing import Any

from .backends.memory import MemoryStore
from .buffered import BufferedMemoryStore
from .connection import ConnectionCheckResult, ConnectionErrorKind
from .model import StoredTelegram
from .query import TelegramQuery, TelegramQueryResult
from .store import KnxTelegramStoreException, StoreCapabilities, StoreStats, TelegramStore

_SQL_BUFFERED_EXPORTS = {
    "BufferedPostgresStore",
    "BufferedSqliteStore",
}


def __getattr__(name: str) -> Any:
    if name in _SQL_BUFFERED_EXPORTS:
        from . import buffered_sql

        return getattr(buffered_sql, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "StoredTelegram",
    "TelegramQuery",
    "TelegramQueryResult",
    "StoreCapabilities",
    "StoreStats",
    "TelegramStore",
    "MemoryStore",
    "BufferedMemoryStore",
    "KnxTelegramStoreException",
    "ConnectionCheckResult",
    "ConnectionErrorKind",
]
