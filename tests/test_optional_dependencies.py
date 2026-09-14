from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_base_imports_without_sqlalchemy() -> None:
    project_root = Path(__file__).parents[1]
    script = """
import importlib.abc
import sys

class BlockSQLAlchemy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlalchemy" or fullname.startswith("sqlalchemy."):
            raise ModuleNotFoundError("sqlalchemy intentionally unavailable", name=fullname)
        return None

sys.meta_path.insert(0, BlockSQLAlchemy())

from knx_telegram_store import BufferedMemoryStore, MemoryStore, StoredTelegram, TelegramQuery
from knx_telegram_store.formats import RawTelegramRecord

assert MemoryStore.__name__ == "MemoryStore"
assert BufferedMemoryStore.__name__ == "BufferedMemoryStore"
assert StoredTelegram.__name__ == "StoredTelegram"
assert TelegramQuery.__name__ == "TelegramQuery"
assert RawTelegramRecord.__name__ == "RawTelegramRecord"
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
