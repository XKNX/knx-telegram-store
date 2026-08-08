from __future__ import annotations

import os
import socket

import pytest

from knx_telegram_store import ConnectionErrorKind
from knx_telegram_store.backends.memory import MemoryStore
from knx_telegram_store.backends.sqlite import SqliteStore
from knx_telegram_store.connection import (
    classify_postgres_error,
    evaluate_sqlite_path,
    probe_timescaledb,
)

# --- SQLite static check_config (pure filesystem) ---------------------------


def test_sqlite_check_config_memory():
    result = SqliteStore.check_config(":memory:")
    assert result.ok
    assert result.kind is ConnectionErrorKind.OK


def test_sqlite_check_config_existing_writeable_file(tmp_path):
    db_file = tmp_path / "exists.db"
    db_file.write_text("")  # create it
    result = SqliteStore.check_config(db_file)
    assert result.ok


def test_sqlite_check_config_creatable_nested_path(tmp_path):
    # Parent dirs do not exist yet but the nearest ancestor (tmp_path) is writeable.
    db_file = tmp_path / "a" / "b" / "telegrams.db"
    result = SqliteStore.check_config(db_file)
    assert result.ok
    # The check must not create anything on disk.
    assert not db_file.exists()
    assert not (tmp_path / "a").exists()


def test_sqlite_check_config_readonly_dir(tmp_path):
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    os.chmod(ro_dir, 0o500)
    try:
        result = SqliteStore.check_config(ro_dir / "telegrams.db")
        assert not result.ok
        assert result.kind is ConnectionErrorKind.PERMISSION
    finally:
        os.chmod(ro_dir, 0o700)  # restore so tmp cleanup works


def test_sqlite_check_config_path_is_directory(tmp_path):
    result = SqliteStore.check_config(tmp_path)
    assert not result.ok
    assert result.kind is ConnectionErrorKind.PERMISSION


def test_evaluate_sqlite_path_no_side_effects(tmp_path):
    target = tmp_path / "nope" / "x.db"
    evaluate_sqlite_path(target)
    assert not target.exists()
    assert not target.parent.exists()


# --- SQLite live check_connection -------------------------------------------


async def test_sqlite_check_connection_ok(tmp_path):
    store = SqliteStore(tmp_path / "x.db")
    try:
        result = await store.check_connection()
        assert result.ok
        assert result.kind is ConnectionErrorKind.OK
    finally:
        await store.close()


# --- Memory backend ----------------------------------------------------------


async def test_memory_check_connection_ok():
    store = MemoryStore(max_telegrams=10)
    result = await store.check_connection()
    assert result.ok


# --- Postgres error classification (pure, no DB) ----------------------------


def test_classify_postgres_timeout():
    assert classify_postgres_error(TimeoutError()) is ConnectionErrorKind.TIMEOUT


def test_classify_postgres_host_unreachable():
    assert classify_postgres_error(ConnectionRefusedError()) is ConnectionErrorKind.HOST_UNREACHABLE
    assert classify_postgres_error(socket.gaierror()) is ConnectionErrorKind.HOST_UNREACHABLE


def test_classify_postgres_missing_dependency():
    assert classify_postgres_error(ModuleNotFoundError("asyncpg")) is ConnectionErrorKind.MISSING_DEPENDENCY


def test_classify_postgres_unwraps_orig():
    asyncpg = pytest.importorskip("asyncpg")

    class FakeWrapped(Exception):
        def __init__(self, orig):
            self.orig = orig

    assert classify_postgres_error(FakeWrapped(asyncpg.InvalidPasswordError("bad"))) is ConnectionErrorKind.AUTH
    assert (
        classify_postgres_error(FakeWrapped(asyncpg.InvalidCatalogNameError("nope")))
        is ConnectionErrorKind.DATABASE_MISSING
    )
    assert (
        classify_postgres_error(FakeWrapped(asyncpg.InsufficientPrivilegeError("denied")))
        is ConnectionErrorKind.PERMISSION
    )


def test_classify_postgres_unknown():
    assert classify_postgres_error(ValueError("weird")) is ConnectionErrorKind.UNKNOWN


# --- TimescaleDB availability probe (fake engine, no DB) ---------------------


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConn:
    def __init__(self, row=None, error=None):
        self._row = row
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return _FakeResult(self._row)


class _FakeEngine:
    def __init__(self, row=None, error=None):
        self._row = row
        self._error = error

    def connect(self):
        return _FakeConn(self._row, self._error)


async def test_probe_timescaledb_available():
    result = await probe_timescaledb(_FakeEngine(row=(1,)), timeout=1.0)
    assert result.ok
    assert result.kind is ConnectionErrorKind.OK


async def test_probe_timescaledb_missing():
    result = await probe_timescaledb(_FakeEngine(row=None), timeout=1.0)
    assert not result.ok
    assert result.kind is ConnectionErrorKind.MISSING_TIMESCALEDB


async def test_probe_timescaledb_query_error_is_classified():
    result = await probe_timescaledb(_FakeEngine(error=ValueError("boom")), timeout=1.0)
    assert not result.ok
    assert result.kind is ConnectionErrorKind.UNKNOWN


# --- TimescaleDB is optional: missing extension is an advisory, not an error --


def test_timescale_advisory_available():
    from knx_telegram_store.backends.postgres import _timescale_advisory
    from knx_telegram_store.connection import ConnectionCheckResult

    result = _timescale_advisory(ConnectionCheckResult.success())
    assert result.ok
    assert "TimescaleDB available" in result.message


def test_timescale_advisory_missing_is_success():
    from knx_telegram_store.backends.postgres import _timescale_advisory
    from knx_telegram_store.connection import ConnectionCheckResult

    missing = ConnectionCheckResult.failure(
        ConnectionErrorKind.MISSING_TIMESCALEDB,
        "The TimescaleDB extension is not available on the database server",
    )
    result = _timescale_advisory(missing)
    assert result.ok
    assert result.kind is ConnectionErrorKind.OK
    assert "plain PostgreSQL" in result.message


def test_timescale_advisory_other_failures_pass_through():
    from knx_telegram_store.backends.postgres import _timescale_advisory
    from knx_telegram_store.connection import ConnectionCheckResult

    failure = ConnectionCheckResult.failure(ConnectionErrorKind.TIMEOUT, "Connection timed out")
    assert _timescale_advisory(failure) is failure


async def test_postgres_check_connection_without_timescale_is_ok():
    pytest.importorskip("asyncpg")
    from knx_telegram_store.backends.postgres import PostgresStore

    store = PostgresStore("postgresql://user:pw@localhost/db")
    # Fake engine: SELECT 1 succeeds, extension query returns no row.
    store.engine = _FakeEngine(row=None)
    result = await store.check_connection()
    assert result.ok
    assert result.kind is ConnectionErrorKind.OK
    assert "plain PostgreSQL" in result.message


async def test_postgres_check_connection_with_timescale_is_ok():
    pytest.importorskip("asyncpg")
    from knx_telegram_store.backends.postgres import PostgresStore

    store = PostgresStore("postgresql://user:pw@localhost/db")
    store.engine = _FakeEngine(row=(1,))
    result = await store.check_connection()
    assert result.ok
    assert "TimescaleDB available" in result.message


# --- Postgres live check (opt-in via env var) -------------------------------


@pytest.mark.skipif(not os.environ.get("KNX_TEST_PG_DSN"), reason="KNX_TEST_PG_DSN not set")
async def test_postgres_check_config_live_ok():
    from knx_telegram_store.backends.postgres import PostgresStore

    dsn = os.environ["KNX_TEST_PG_DSN"]
    result = await PostgresStore.check_config(dsn)
    assert result.ok, result.message


def test_postgres_build_engine_decodes_url_components():
    pytest.importorskip("asyncpg")
    from knx_telegram_store.backends.postgres import _build_engine

    engine = _build_engine("postgresql://u%40x:p%40ss@host:5432/knx%3Fquery%23hash")
    assert engine.url.username == "u@x"
    assert engine.url.password == "p@ss"
    assert engine.url.database == "knx?query#hash"


def test_postgres_build_engine_translates_sslmode():
    pytest.importorskip("asyncpg")
    from knx_telegram_store.backends.postgres import _build_engine

    # libpq's sslmode query parameter must not survive into the URL: the
    # asyncpg dialect forwards leftover query keys verbatim to
    # asyncpg.connect(), which rejects sslmode with a TypeError.
    engine = _build_engine("postgresql://u:p@host:5432/db?sslmode=require")
    assert "sslmode" not in engine.url.query
    _, connect_kwargs = engine.dialect.create_connect_args(engine.url)
    assert "sslmode" not in connect_kwargs


async def test_postgres_check_config_tls_unreachable():
    pytest.importorskip("asyncpg")
    from knx_telegram_store.backends.postgres import PostgresStore

    # A TLS DSN must fail at the network layer (unreachable host), not with a
    # TypeError from an untranslated sslmode argument (classified UNKNOWN).
    result = await PostgresStore.check_config(
        "postgresql://user:pw@127.0.0.1:1/nodb?sslmode=require",
        timeout=3.0,
    )
    assert not result.ok
    assert result.kind in {ConnectionErrorKind.HOST_UNREACHABLE, ConnectionErrorKind.TIMEOUT}


async def test_postgres_check_config_unreachable():
    pytest.importorskip("asyncpg")
    from knx_telegram_store.backends.postgres import PostgresStore

    # Reserved-as-unreachable: port 1 on localhost should refuse quickly.
    result = await PostgresStore.check_config(
        "postgresql://user:pw@127.0.0.1:1/nodb",
        timeout=3.0,
    )
    assert not result.ok
    assert result.kind in {ConnectionErrorKind.HOST_UNREACHABLE, ConnectionErrorKind.TIMEOUT}


def test_evaluate_sqlite_path_memory():
    # Test string and Path variants of ":memory:"
    from pathlib import Path

    # read_only=False
    res1 = evaluate_sqlite_path(":memory:")
    assert res1.ok
    assert res1.kind is ConnectionErrorKind.OK

    res2 = evaluate_sqlite_path(Path(":memory:"))
    assert res2.ok
    assert res2.kind is ConnectionErrorKind.OK

    # read_only=True
    res3 = evaluate_sqlite_path(":memory:", read_only=True)
    assert not res3.ok
    assert res3.kind is ConnectionErrorKind.UNKNOWN

    res4 = evaluate_sqlite_path(Path(":memory:"), read_only=True)
    assert not res4.ok
    assert res4.kind is ConnectionErrorKind.UNKNOWN


def test_evaluate_sqlite_path_read_only_edge_cases(tmp_path):
    # Non-existing path
    missing_file = tmp_path / "missing.db"
    res = evaluate_sqlite_path(missing_file, read_only=True)
    assert not res.ok
    assert res.kind is ConnectionErrorKind.PERMISSION

    # Existing directory
    res = evaluate_sqlite_path(tmp_path, read_only=True)
    assert not res.ok
    assert res.kind is ConnectionErrorKind.PERMISSION

    # Readable file
    readable_file = tmp_path / "readable.db"
    readable_file.write_text("")
    res = evaluate_sqlite_path(readable_file, read_only=True)
    assert res.ok

    # Unreadable file
    unreadable_file = tmp_path / "unreadable.db"
    unreadable_file.write_text("")
    os.chmod(unreadable_file, 0o000)
    try:
        # Check if we're running as root which bypasses permissions
        if not os.access(unreadable_file, os.R_OK):
            res = evaluate_sqlite_path(unreadable_file, read_only=True)
            assert not res.ok
            assert res.kind is ConnectionErrorKind.PERMISSION
    finally:
        os.chmod(unreadable_file, 0o644)


def test_evaluate_sqlite_path_write_edge_cases(tmp_path):
    # Existing directory
    res = evaluate_sqlite_path(tmp_path)
    assert not res.ok
    assert res.kind is ConnectionErrorKind.PERMISSION

    # Writeable file
    writeable_file = tmp_path / "writeable.db"
    writeable_file.write_text("")
    res = evaluate_sqlite_path(str(writeable_file))  # passing as string to test string handling
    assert res.ok

    # Unwriteable file
    unwriteable_file = tmp_path / "unwriteable.db"
    unwriteable_file.write_text("")
    os.chmod(unwriteable_file, 0o400)
    try:
        if not os.access(unwriteable_file, os.W_OK):
            res = evaluate_sqlite_path(unwriteable_file)
            assert not res.ok
            assert res.kind is ConnectionErrorKind.PERMISSION
    finally:
        os.chmod(unwriteable_file, 0o644)

    # Missing file in unwriteable dir
    ro_dir = tmp_path / "ro_dir"
    ro_dir.mkdir()
    os.chmod(ro_dir, 0o500)
    try:
        res = evaluate_sqlite_path(ro_dir / "new.db")
        assert not res.ok
        assert res.kind is ConnectionErrorKind.PERMISSION
    finally:
        os.chmod(ro_dir, 0o700)


def test_evaluate_sqlite_path_no_existing_parent(monkeypatch, tmp_path):
    from pathlib import Path

    # Test the fallback where we reach the root and nothing exists.
    target = tmp_path / "a" / "b" / "c.db"

    def mocked_exists(self):
        # Make everything look non-existent for this test
        return False

    monkeypatch.setattr(Path, "exists", mocked_exists)

    res = evaluate_sqlite_path(target)
    assert not res.ok
    assert res.kind is ConnectionErrorKind.PERMISSION
    assert "No existing parent directory found" in res.message


def test_evaluate_sqlite_path_os_error(monkeypatch, tmp_path):
    from pathlib import Path

    target = tmp_path / "error.db"

    def mocked_exists(self):
        raise OSError("Permission denied test")

    monkeypatch.setattr(Path, "exists", mocked_exists)

    res = evaluate_sqlite_path(target)
    assert not res.ok
    assert res.kind is ConnectionErrorKind.PERMISSION
    assert res.detail == "Permission denied test"
