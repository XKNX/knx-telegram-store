from .backends.postgres import PostgresStore
from .backends.sqlite import SqliteStore
from .buffered_memory import BufferedMemoryStore as BufferedMemoryStore
from .buffered_memory import _BufferMixin


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
