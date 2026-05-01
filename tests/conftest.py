
import pytest

from knx_telegram_store.backends.memory import MemoryStore
from knx_telegram_store.backends.sqlite import SqliteStore


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    """Parametrized fixture for TelegramStore implementations."""
    if request.param == "memory":
        store = MemoryStore(max_telegrams=100)
    elif request.param == "sqlite":
        db_file = tmp_path / "test.db"
        store = SqliteStore(db_file, max_telegrams=100)
    
    await store.initialize()
    yield store
    await store.close()
