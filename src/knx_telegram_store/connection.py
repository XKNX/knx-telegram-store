from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class ConnectionErrorKind(StrEnum):
    """Categorizes the outcome of a store connection / config check."""

    OK = "ok"
    AUTH = "auth"  # bad user / password
    HOST_UNREACHABLE = "host_unreachable"  # DNS / refused / network
    DATABASE_MISSING = "database_missing"  # catalog does not exist
    PERMISSION = "permission"  # fs not writeable / insufficient privilege
    TIMEOUT = "timeout"
    MISSING_DEPENDENCY = "missing_dependency"  # driver not installed
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConnectionCheckResult:
    """Structured result of a store connection / config validation."""

    ok: bool
    kind: ConnectionErrorKind
    message: str  # human-readable, safe to show in a UI
    detail: str | None = None  # underlying exception text (for logs)

    @classmethod
    def success(cls, message: str = "Connection OK") -> ConnectionCheckResult:
        """Build a successful result."""
        return cls(ok=True, kind=ConnectionErrorKind.OK, message=message)

    @classmethod
    def failure(
        cls,
        kind: ConnectionErrorKind,
        message: str,
        detail: str | None = None,
    ) -> ConnectionCheckResult:
        """Build a failed result."""
        return cls(ok=False, kind=kind, message=message, detail=detail)


def evaluate_sqlite_path(db_path: str | Path) -> ConnectionCheckResult:
    """Validate that a SQLite file is writeable or can be created.

    Pure filesystem check — performs no mutation (no mkdir, no file creation).
    """
    if str(db_path) == ":memory:":
        return ConnectionCheckResult.success("In-memory SQLite database")

    path = Path(db_path)

    # A non-traversable ancestor (e.g. an unreadable parent dir) makes stat()
    # raise rather than return False — that is itself a permission failure.
    try:
        if path.exists():
            if path.is_dir():
                return ConnectionCheckResult.failure(
                    ConnectionErrorKind.PERMISSION,
                    f"SQLite path '{path}' is a directory, not a file",
                )
            if os.access(path, os.W_OK):
                return ConnectionCheckResult.success(f"SQLite file '{path}' is writeable")
            return ConnectionCheckResult.failure(
                ConnectionErrorKind.PERMISSION,
                f"SQLite file '{path}' exists but is not writeable",
            )

        # File does not exist yet — find the nearest existing ancestor directory.
        # initialize() creates intermediate dirs via mkdir(parents=True), so the
        # config is valid as long as that nearest ancestor is writeable.
        ancestor = path.parent
        while not ancestor.exists():
            parent = ancestor.parent
            if parent == ancestor:  # reached filesystem root
                break
            ancestor = parent

        if not ancestor.exists():
            return ConnectionCheckResult.failure(
                ConnectionErrorKind.PERMISSION,
                f"No existing parent directory found for SQLite file '{path}'",
            )
        if ancestor.is_dir() and os.access(ancestor, os.W_OK):
            return ConnectionCheckResult.success(
                f"SQLite file '{path}' can be created (directory '{ancestor}' is writeable)"
            )
    except OSError as err:
        return ConnectionCheckResult.failure(
            ConnectionErrorKind.PERMISSION,
            f"Cannot access SQLite path '{path}'",
            detail=str(err),
        )

    return ConnectionCheckResult.failure(
        ConnectionErrorKind.PERMISSION,
        f"Cannot create SQLite file '{path}': directory '{ancestor}' is not writeable",
    )


def classify_postgres_error(exc: BaseException) -> ConnectionErrorKind:
    """Map a connection exception to a ConnectionErrorKind.

    Unwraps SQLAlchemy's DBAPIError wrapping (``.orig``) to inspect the
    underlying asyncpg / OS exception. asyncpg is imported lazily so the core
    library keeps zero runtime dependencies.
    """
    orig = getattr(exc, "orig", None) or exc

    if isinstance(orig, TimeoutError):
        return ConnectionErrorKind.TIMEOUT
    if isinstance(orig, ModuleNotFoundError | ImportError):
        return ConnectionErrorKind.MISSING_DEPENDENCY

    try:
        import asyncpg  # type: ignore[import-not-found]
    except ImportError:
        asyncpg = None  # type: ignore[assignment]

    if asyncpg is not None:
        if isinstance(orig, asyncpg.InvalidPasswordError | asyncpg.InvalidAuthorizationSpecificationError):
            return ConnectionErrorKind.AUTH
        if isinstance(orig, asyncpg.InvalidCatalogNameError):
            return ConnectionErrorKind.DATABASE_MISSING
        if isinstance(orig, asyncpg.InsufficientPrivilegeError):
            return ConnectionErrorKind.PERMISSION

    # Network-level failures (DNS resolution, refused connections, sockets).
    if isinstance(orig, socket.gaierror | ConnectionRefusedError | OSError):
        return ConnectionErrorKind.HOST_UNREACHABLE

    return ConnectionErrorKind.UNKNOWN


async def probe_engine(
    engine: AsyncEngine,
    *,
    timeout: float,
    classify: Callable[[BaseException], ConnectionErrorKind],
) -> ConnectionCheckResult:
    """Open a connection and run ``SELECT 1`` to verify connectivity.

    Does not create tables or run migrations. The engine is not disposed —
    callers owning a throwaway engine are responsible for disposing it.
    """
    from sqlalchemy import text

    async def _probe() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_probe(), timeout=timeout)
    except TimeoutError as err:
        return ConnectionCheckResult.failure(
            ConnectionErrorKind.TIMEOUT,
            f"Connection timed out after {timeout:g}s",
            detail=str(err),
        )
    except Exception as err:
        kind = classify(err)
        return ConnectionCheckResult.failure(kind, _message_for_kind(kind), detail=str(err))

    return ConnectionCheckResult.success()


_KIND_MESSAGES: dict[ConnectionErrorKind, str] = {
    ConnectionErrorKind.AUTH: "Authentication failed (check user and password)",
    ConnectionErrorKind.HOST_UNREACHABLE: "Could not reach the database host (check host and port)",
    ConnectionErrorKind.DATABASE_MISSING: "The specified database does not exist",
    ConnectionErrorKind.PERMISSION: "Insufficient privileges for this operation",
    ConnectionErrorKind.TIMEOUT: "Connection timed out",
    ConnectionErrorKind.MISSING_DEPENDENCY: "Required database driver is not installed",
    ConnectionErrorKind.UNKNOWN: "Connection failed",
}


def _message_for_kind(kind: ConnectionErrorKind) -> str:
    """Return a human-readable message for an error kind."""
    return _KIND_MESSAGES.get(kind, "Connection failed")
