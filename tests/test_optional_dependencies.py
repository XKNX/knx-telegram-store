from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parents[1]
_BLOCK_SQLALCHEMY = """
import importlib.abc
import sys

class BlockSQLAlchemy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlalchemy" or fullname.startswith("sqlalchemy."):
            raise ModuleNotFoundError("sqlalchemy intentionally unavailable", name=fullname)
        return None

sys.meta_path.insert(0, BlockSQLAlchemy())
"""


def _run_script(script: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_PROJECT_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _run_without_sqlalchemy(script: str) -> None:
    _run_script(_BLOCK_SQLALCHEMY + script)


def test_base_imports_without_sqlalchemy() -> None:
    _run_without_sqlalchemy(
        """

from knx_telegram_store import BufferedMemoryStore, MemoryStore, StoredTelegram, TelegramQuery
from knx_telegram_store.formats import RawTelegramRecord

assert MemoryStore.__name__ == "MemoryStore"
assert BufferedMemoryStore.__name__ == "BufferedMemoryStore"
assert StoredTelegram.__name__ == "StoredTelegram"
assert TelegramQuery.__name__ == "TelegramQuery"
assert RawTelegramRecord.__name__ == "RawTelegramRecord"
"""
    )


def test_wildcard_import_without_sqlalchemy() -> None:
    _run_without_sqlalchemy(
        """

from knx_telegram_store import *

assert MemoryStore.__name__ == "MemoryStore"
assert BufferedMemoryStore.__name__ == "BufferedMemoryStore"
assert "BufferedSqliteStore" not in globals()
assert "BufferedPostgresStore" not in globals()
"""
    )


def test_wildcard_import_with_sqlalchemy() -> None:
    _run_script(
        """

from knx_telegram_store import *
from knx_telegram_store import BufferedPostgresStore as ExplicitPostgresStore
from knx_telegram_store import BufferedSqliteStore as ExplicitSqliteStore

assert BufferedPostgresStore is ExplicitPostgresStore
assert BufferedSqliteStore is ExplicitSqliteStore
"""
    )


def test_legacy_buffered_memory_import_without_sqlalchemy() -> None:
    _run_without_sqlalchemy(
        """

from knx_telegram_store.buffered import BufferedMemoryStore

assert BufferedMemoryStore.__name__ == "BufferedMemoryStore"
"""
    )
