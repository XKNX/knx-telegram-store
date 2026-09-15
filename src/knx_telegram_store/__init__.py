from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

from .backends.memory import MemoryStore
from .buffered import BufferedMemoryStore
from .connection import ConnectionCheckResult, ConnectionErrorKind
from .model import StoredTelegram
from .query import TelegramQuery, TelegramQueryResult
from .store import KnxTelegramStoreException, StoreCapabilities, StoreStats, TelegramStore

if TYPE_CHECKING:
    from .buffered_sql import BufferedPostgresStore as BufferedPostgresStore
    from .buffered_sql import BufferedSqliteStore as BufferedSqliteStore

_SQL_BUFFERED_EXPORTS = {
    "BufferedPostgresStore",
    "BufferedSqliteStore",
}

try:
    _SQLALCHEMY_AVAILABLE = find_spec("sqlalchemy") is not None
except ModuleNotFoundError:
    _SQLALCHEMY_AVAILABLE = False


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

if _SQLALCHEMY_AVAILABLE:
    __all__.extend(sorted(_SQL_BUFFERED_EXPORTS))
