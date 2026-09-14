# Buffered Store Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent every buffered-store mutation from racing with final flush and backend disposal.

**Architecture:** Replace the two partially overlapping lifecycle locks with one task-reentrant mutation gate. Every backend mutation enters this gate; operations that also touch the buffer then acquire the existing flush lock in a single fixed order. `close()` marks shutdown first and holds the gate continuously across final flush and backend disposal.

**Tech Stack:** Python 3.12, asyncio, contextlib, pytest, pytest-asyncio, Ruff, mypy

**Spec:** `docs/superpowers/specs/2026-09-15-buffered-store-lifecycle-design.md`

## Global Constraints

- Start each behavior change with a failing deterministic regression test.
- Use asyncio events, not timing sleeps, to control the important race ordering.
- Do not add dependencies or change public method signatures.
- Preserve public `store_many()` dispatch from the flush path.
- Preserve buffered telegrams on failed or cancelled flushes.
- Keep read-only operations outside the mutation gate.
- Use the lock order mutation gate, then flush lock.
- Update PR #66's description before requesting the next Copilot review.

## File Structure

- Modify `src/knx_telegram_store/buffered.py`: central mutation gate and coverage of all buffered mutation paths.
- Modify `tests/test_buffered_store.py`: deterministic lifecycle, race, rejection, and retry regressions.
- Modify `docs/superpowers/specs/2026-09-15-buffered-store-lifecycle-design.md` only if implementation discovers a factual mismatch; do not silently diverge from it.

---

### Task 1: Close and eviction share one lifecycle boundary

**Files:**
- Modify: `tests/test_buffered_store.py`
- Modify: `src/knx_telegram_store/buffered.py`

**Interfaces:**
- Produces: `_BufferMixin._mutation(*, allow_closing: bool = False) -> AsyncIterator[None]`
- Produces: `_mutation_lock: asyncio.Lock`
- Produces: `_mutation_owner: asyncio.Task[Any] | None`
- Consumes: existing `_flush_lock`, `_closing`, `_flush()`, and public `store_many()`

- [ ] **Step 1: Add the deterministic close-versus-queued-eviction regression**

Add a test that pauses the final buffered backend write, starts eviction while close owns the lifecycle, then releases the write. It must assert that eviction raises and the backend eviction method was never entered:

```python
async def test_close_rejects_eviction_queued_during_final_flush(sample_telegram, monkeypatch):
    store = BufferedSqliteStore(":memory:", flush_interval=60)
    await store.initialize()
    await store.store(sample_telegram)
    original_store_many = SqliteStore.store_many
    final_write_started = asyncio.Event()
    release_final_write = asyncio.Event()
    backend_eviction_called = asyncio.Event()

    async def _pause_final_write(self, telegrams):
        final_write_started.set()
        await release_final_write.wait()
        await original_store_many(self, telegrams)

    async def _observe_eviction(self, cutoff, *, dry_run=False):
        backend_eviction_called.set()
        return 0

    monkeypatch.setattr(SqliteStore, "store_many", _pause_final_write)
    monkeypatch.setattr(SqliteStore, "evict_older_than", _observe_eviction)
    close_task = asyncio.create_task(store.close())
    await asyncio.wait_for(final_write_started.wait(), timeout=1)
    eviction_task = asyncio.create_task(store.evict_older_than(sample_telegram.timestamp))
    await asyncio.sleep(0)
    release_final_write.set()

    await close_task
    with pytest.raises(KnxTelegramStoreException, match="closing"):
        await eviction_task
    assert not backend_eviction_called.is_set()
```

- [ ] **Step 2: Run the regression and verify RED**

Run:

```bash
pytest -q tests/test_buffered_store.py::test_close_rejects_eviction_queued_during_final_flush
```

Expected: failure because the existing eviction reaches `SqliteStore.evict_older_than()` after the final flush instead of being rejected.

- [ ] **Step 3: Add the minimal task-reentrant mutation gate**

In `buffered.py`, import `AsyncIterator` and `asynccontextmanager`, replace `_store_lock` and `_buffer_flush_task` with `_mutation_lock` and `_mutation_owner`, and add:

```python
@asynccontextmanager
async def _mutation(self, *, allow_closing: bool = False) -> AsyncIterator[None]:
    task = asyncio.current_task()
    assert task is not None
    if task is self._mutation_owner:
        yield
        return

    async with self._mutation_lock:
        if self._closing and not allow_closing:
            raise RuntimeError("Store is closing")
        self._mutation_owner = task
        try:
            yield
        finally:
            self._mutation_owner = None
```

Wrap the complete `store_many()` backend call in `_mutation()`. Wrap `_flush()` in `_mutation()` before `_flush_lock`; retain its existing detach/store/restore logic and public `self.store_many(batch)` call. Wrap `_evict_older_than()` in `_mutation()` before `_flush_lock`.

Change `close()` so it enters `_mutation(allow_closing=True)`, calls `_flush()` reentrantly, and calls `super().close()` before releasing the gate:

```python
async with self._mutation(allow_closing=True):
    await self._flush(raise_on_error=True)
    await super().close()
```

- [ ] **Step 4: Run the new race test and focused existing lifecycle tests**

Run:

```bash
pytest -q tests/test_buffered_store.py::test_close_rejects_eviction_queued_during_final_flush tests/test_buffered_store.py::test_close_waits_for_concurrent_store_many tests/test_buffered_store.py::test_eviction_serializes_with_concurrent_flush tests/test_buffered_store.py::test_eviction_serializes_with_concurrent_store_many tests/test_buffered_store.py::test_cancellation_finishes_buffer_reconciliation_after_backend_eviction
```

Expected: all selected tests pass, including both cancellation parameter cases.

- [ ] **Step 5: Commit the core lifecycle boundary**

```bash
git add src/knx_telegram_store/buffered.py tests/test_buffered_store.py
git commit -m "fix: serialize buffered store shutdown"
```

---

### Task 2: Cover every sibling mutation and shutdown edge

**Files:**
- Modify: `tests/test_buffered_store.py`
- Modify: `src/knx_telegram_store/buffered.py`

**Interfaces:**
- Consumes: `_BufferMixin._mutation()` from Task 1
- Produces: consistent post-shutdown rejection for `store_many`, `flush`, eviction, `clear`, and `optimize`
- Produces: guarded `BufferedMemoryStore.evict_older_than()` and `start()`

- [ ] **Step 1: Add a parameterized post-shutdown mutation test**

Add an invocation helper and a test covering all sibling entry points:

```python
async def _invoke_mutation(store, operation, sample_telegram):
    if operation == "store_many":
        await store.store_many([sample_telegram])
    elif operation == "flush":
        await store.flush()
    elif operation == "evict":
        await store.evict_older_than(sample_telegram.timestamp)
    elif operation == "clear":
        await store.clear()
    else:
        await store.optimize()


@pytest.mark.parametrize("operation", ["store_many", "flush", "evict", "clear", "optimize"])
async def test_mutations_are_rejected_after_close(sample_telegram, operation):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.close()

    with pytest.raises(KnxTelegramStoreException, match="closing"):
        await _invoke_mutation(store, operation, sample_telegram)
```

Add a start regression:

```python
async def test_start_after_close_does_not_create_periodic_task():
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.close()

    store.start()

    assert store._flush_task is None
```

Pin the intentionally different synchronous behavior and cancellation preservation:

```python
async def test_single_stores_after_close_are_dropped(sample_telegram):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    await store.close()

    await store.store(sample_telegram)
    store.store_sync(sample_telegram)

    assert store._buffer == []


async def test_cancelled_flush_restores_detached_batch(sample_telegram, monkeypatch):
    store = BufferedMemoryStore(flush_interval=60)
    await store.initialize()
    write_started = asyncio.Event()
    never_release = asyncio.Event()

    async def _block_write(_telegrams):
        write_started.set()
        await never_release.wait()

    monkeypatch.setattr(store, "store_many", _block_write)
    await store.store(sample_telegram)
    flush_task = asyncio.create_task(store.flush())
    await asyncio.wait_for(write_started.wait(), timeout=1)
    flush_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await flush_task
    assert store._buffer == [sample_telegram]
```

- [ ] **Step 2: Run both additions and verify RED**

Run:

```bash
pytest -q tests/test_buffered_store.py::test_mutations_are_rejected_after_close tests/test_buffered_store.py::test_start_after_close_does_not_create_periodic_task
```

Expected: the mutation cases that currently bypass or silently ignore shutdown fail, and `start()` creates an unwanted periodic task.

- [ ] **Step 3: Route sibling operations through the gate**

Make only these changes:

```python
def start(self) -> None:
    if self._closing or self._flush_task is not None:
        return
    self._flush_task = asyncio.create_task(self._flush_loop())

async def optimize(self) -> None:
    async with self._mutation():
        await self.flush()
        await super().optimize()

async def clear(self) -> None:
    async with self._mutation():
        async with self._flush_lock:
            self._buffer.clear()
            await super().clear()
```

Keep `BufferedMemoryStore`'s max-size-only eviction behavior, but route its direct backend call through `_mutation()`:

```python
async with self._mutation():
    return await MemoryStore.evict_older_than(self, cutoff, dry_run=dry_run)
```

Do not add a second lifecycle abstraction or lock.

- [ ] **Step 4: Adapt the old silent-drop test to the explicit rejection contract**

Rename `test_close_drops_store_many_queued_during_shutdown` and replace its private-lock manipulation with this observable backend boundary:

```python
async def test_close_rejects_store_many_queued_during_shutdown(sample_telegram, monkeypatch, tmp_path):
    db_path = tmp_path / "telegrams.db"
    store = BufferedSqliteStore(db_path, flush_interval=60)
    await store.initialize()
    original_close = SqliteStore.close
    backend_close_started = asyncio.Event()
    release_backend_close = asyncio.Event()

    async def _pause_backend_close(self):
        backend_close_started.set()
        await release_backend_close.wait()
        await original_close(self)

    monkeypatch.setattr(SqliteStore, "close", _pause_backend_close)
    close_task = asyncio.create_task(store.close())
    await asyncio.wait_for(backend_close_started.wait(), timeout=1)
    queued_store_task = asyncio.create_task(store.store_many([sample_telegram]))
    release_backend_close.set()

    await close_task
    with pytest.raises(KnxTelegramStoreException, match="closing"):
        await queued_store_task

    reader = SqliteStore(db_path)
    await reader.initialize()
    try:
        assert await reader.count() == 0
    finally:
        await reader.close()
```

- [ ] **Step 5: Run the complete buffered-store suite**

Run:

```bash
pytest -q tests/test_buffered_store.py
```

Expected: all tests pass with no pending-task warnings.

- [ ] **Step 6: Commit complete mutation coverage**

```bash
git add src/knx_telegram_store/buffered.py tests/test_buffered_store.py
git commit -m "test: cover buffered mutation lifecycle"
```

---

### Task 3: Verify, update PR #66, and request an independent review

**Files:**
- Verify: `src/knx_telegram_store/buffered.py`
- Verify: `tests/test_buffered_store.py`
- Update remotely: PR #66 description

**Interfaces:**
- Consumes: completed Tasks 1 and 2
- Produces: pushed branch, accurate PR description, and a new Copilot review request

- [ ] **Step 1: Run formatting, linting, typing, and the non-integration test suite**

Run:

```bash
ruff format --check .
ruff check .
mypy src
pytest -q
```

Expected: every command exits zero. Do not claim success from partial output.

- [ ] **Step 2: Review the complete PR diff against its base**

Run:

```bash
git diff --check
git diff origin/fix/issue-43-buffered-close-flushes...HEAD -- src/knx_telegram_store/buffered.py tests/test_buffered_store.py
git status --short
```

Confirm the gate covers every mutation in the spec, lock ordering is uniform, no buffer batch can be lost, and the worktree is clean after any necessary fixes.

- [ ] **Step 3: Push the verified commits**

```bash
git push origin fix/issue-53-evict-buffered-telegrams
```

- [ ] **Step 4: Replace PR #66's description with the final scope**

Use this structure, filling the test count from actual verification:

```markdown
## Summary

- prune expired telegrams from the in-memory buffer together with persisted rows
- serialize every buffered-store mutation through one task-reentrant lifecycle gate
- keep final flush and backend disposal in one uninterrupted shutdown boundary
- reject queued writes, flushes, evictions, clears, and optimizations once shutdown begins
- preserve failed or cancelled flush batches and existing memory-store retention semantics

## Why

The original eviction/flush race exposed a broader lifecycle issue: mutations were protected by different locks, so a queued operation could resume after `close()` had disposed the backend. A single gate now defines the boundary for all mutating paths, with one lock order: lifecycle gate, then buffer lock.

## Tests

- deterministic close-versus-eviction and queued-mutation regressions
- flush failure, cancellation, retry, and repeated-close coverage
- full non-integration suite: `pytest -q` passed
- Ruff formatting/lint and mypy

Closes #53.

Identified and fixed with assistance from OpenAI Codex.
```

- [ ] **Step 5: Request the new Copilot review only after the remote head and description are current**

Request Copilot review on PR #66 using GitHub's review-request mechanism. Verify the request is visible on the PR; do not wait for CI or for the review result, per the user's instruction.
