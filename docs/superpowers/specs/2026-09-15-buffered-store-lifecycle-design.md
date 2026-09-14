# Buffered Store Lifecycle Design

## Context

PR #66 started as a fix for buffered eviction racing with a flush. Repeated review findings exposed a broader lifecycle problem: the buffered store has several mutating entry points, but no single protocol prevents them from reaching the backend while `close()` is flushing and disposing it.

Patching only the currently reported eviction interleaving would leave the same class of race possible through `store_many()`, `clear()`, or `optimize()`. This design therefore treats shutdown as one store-wide mutation boundary.

## Goals

- Let a mutation that already owns the backend finish before shutdown disposes it.
- Reject new or queued mutations after shutdown has begun.
- Keep the final buffered flush and backend disposal inside one uninterrupted lifecycle boundary.
- Apply the same rule to `store_many`, `flush`, eviction, `clear`, and `optimize`.
- Preserve the public `store_many()` path used by flushing so subclasses and instrumentation continue to observe buffered writes.
- Preserve buffered telegrams if a cancelled or failed flush did not store them.
- Keep synchronous `store()` calls non-blocking.

## Non-goals

- Serializing read-only queries with mutations or shutdown.
- Reopening a store after `close()` has begun.
- Changing retention calculations, query semantics, database schemas, or KNX telegram representation.
- Adding parallel write throughput inside `BufferedKnxTelegramStore`.

## Lifecycle Semantics

The first call to `close()` marks the store as closing before it awaits anything. From that point onward:

- A mutation already holding the mutation gate may complete.
- A mutation waiting for the gate, or started later, must fail after it acquires/checks the gate and must not access the backend.
- The closing task may re-enter the gate to perform the final flush.
- The backend is disposed before the closing task releases the gate.
- Further `close()` calls remain safe and serialized.

The awaited mutation APIs fail explicitly when shutdown has begun. Synchronous `store()` and `store_sync()` retain their callback-friendly behavior: they do not block and discard new telegrams with a warning once closing has started.

## Proposed Design

### One task-reentrant mutation gate

Use one asynchronous lock plus the identity of its owning asyncio task. A small internal async context manager provides the mutation boundary:

- The first entry acquires the lock and records the current task as owner.
- Re-entry by the owner succeeds without trying to acquire the non-reentrant asyncio lock again.
- A non-owner checks the closing state after acquiring the lock. If shutdown has begun, it releases the lock and raises without calling the backend.
- Only the outermost owner clears ownership and releases the lock.
- `close()` has a private, explicit permission to enter after setting the closing flag; ordinary operations do not.

Task identity is required rather than a process-wide depth counter: unrelated asyncio tasks must never inherit another task's permission to mutate.

### Operation coverage

All backend mutations use the same gate:

| Operation | Work protected by the gate |
| --- | --- |
| `store_many()` | The complete backend write |
| `flush()` / `_flush()` | Buffer detachment, public `store_many()` call, and restoration on failure |
| eviction | Buffer pruning and backend eviction |
| `clear()` | Buffered and persisted telegram removal |
| `optimize()` | Its prerequisite flush and backend optimization |
| `close()` | Final flush and backend disposal as one unit |

`start()` must not create a new periodic flush task after closing has begun.

### Lock order

Whenever both locks are required, the order is fixed:

1. Mutation gate
2. Buffer/flush lock

No code may wait for the mutation gate while holding the buffer lock. This removes the close-versus-eviction gap without introducing the inverse-lock deadlock that piecemeal locking could create.

### Flush behavior

Flushing enters the mutation gate and then the buffer lock. It detaches the current buffer into a batch and calls the public `store_many()` method. That call re-enters the mutation gate as the same task, preserving overrides and instrumentation without deadlocking.

If storage fails or the task is cancelled, the detached batch is restored ahead of telegrams buffered in the meantime. Repeated cancellation while cleanup is running must not lose the batch.

### Close behavior

`close()` performs these steps:

1. Set the closing flag synchronously.
2. Cancel and await the periodic flush task, allowing its cancellation cleanup to restore any detached batch.
3. Enter the mutation gate with closing-task permission.
4. Flush the restored/current buffer through the normal flush path.
5. Dispose the backend before releasing the mutation gate.

This makes it impossible for a queued eviction or other mutation to run between the final flush and disposal, or to resume against an already disposed backend.

If the final flush fails, the batch is restored, the error is propagated, and the backend is not disposed. A later `close()` call may retry the final flush and complete shutdown. This preserves the existing close contract.

### Clear behavior

`clear()` holds both the mutation gate and buffer lock while coordinating in-memory and persisted state. It preserves the existing ordering: the current buffer is cleared before the backend call. Telegrams accepted after that buffer-clear point are newer work and remain buffered. New telegrams arriving through synchronous `store()` after closing has begun are rejected before reaching the buffer.

## Error and Cancellation Rules

- A mutation rejected because shutdown started raises the store's public exception type through the existing exception translation layer.
- Rejection happens before any backend call.
- Cancellation of a task waiting for the gate has no side effects.
- Cancellation during a detached-buffer flush restores the batch exactly once.
- Cancellation of the periodic task during `close()` cannot escape before its batch-restoration cleanup has completed.
- The gate owner is always cleared in `finally`, including backend failures and cancellation.

## Test Strategy

Implementation starts with failing regression tests. The tests use events or controlled backend methods instead of timing sleeps so every interleaving is deterministic.

Required scenarios:

1. An eviction queued while `close()` performs its final flush is rejected and never reaches the disposed backend.
2. A direct `store_many()` already in progress completes before disposal.
3. A direct `store_many()` queued after closing begins is rejected without backend access.
4. `clear()` and `optimize()` obey the same lifecycle boundary.
5. The final close flush can re-enter the public `store_many()` path without deadlock.
6. Failed and singly or repeatedly cancelled flushes restore their batch in original order.
7. A periodic flush cancelled by `close()` restores its batch before the final flush.
8. Concurrent or repeated `close()` calls are safe.
9. `start()` after shutdown has begun does not create a periodic task.
10. A failed final flush restores the batch, does not dispose the backend, and can be retried by a later `close()` call.
11. Existing buffered-store and backend test suites remain green.

## PR Presentation

The PR description must be updated before requesting the next review. It should explain that the original eviction race revealed a shared mutation-versus-shutdown lifecycle issue, list every protected mutation path, describe the task-reentrant gate and lock order, and summarize the deterministic regression coverage. It must retain the issue link and the grammatically correct attribution:

> Identified and fixed with assistance from OpenAI Codex.
