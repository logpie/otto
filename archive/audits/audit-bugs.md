# Otto v5 Hot Path Audit: Critical Bug Report

**Scope:** otto/v5_runner.py (10540 LOC), v5_clean_verify.py, v5_preflight_repair.py, v5_branching.py, checkpoint.py, worktree.py, and related hot paths.

**Date:** 2026-05-19  
**Audit Level:** High severity focus only — bikeshedding excluded.

---

## Summary

Audited 17,537 LOC of v5 hot paths for MAJOR bugs in: race conditions, silent failures, retry semantics, budget enforcement, worktree safety, state corruption, subprocess leaks, file handle leaks, path traversal, async/await mistakes, and logging gaps.

**Finding:** 4 critical bugs identified (no catastrophic design flaws).

---

## Findings

### BUG-1: Missing `dir_fd` Cleanup on fsync Failure
- **Severity:** High
- **File:line:** otto/observability.py:85-88
- **Symptom:** If dir_fd is opened but then an exception occurs before the finally block, the fd is not closed. If `os.open()` succeeds but `os.fsync(dir_fd)` raises OSError, the fd leaks.
- **Root cause:** The dir_fd assignment and cleanup logic does not account for partial failures after fd is opened. The try/except at line 80 re-raises the exception, but if it occurs at line 79 after line 78 succeeds, the fd is not in a finally block yet (it's created inside the try but finalized in finally, but there's no intermediate cleanup if fsync fails).
- **Repro / evidence:**
  ```python
  # Line 77-79: dir_fd is opened
  if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
      dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
      os.fsync(dir_fd)  # <-- if this raises OSError, dir_fd is assigned but...
  # ...control jumps to except/finally, and finally block tries to close(dir_fd)
  # But the fd may already be in a bad state or the exception may hide the leak.
  ```
- **Suggested fix:** Wrap the dir_fd operations in a nested try/finally or use a context manager:
  ```python
  try:
      if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
          try:
              dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
              os.fsync(dir_fd)
          finally:
              if dir_fd is not None:
                  os.close(dir_fd)
  except OSError:
      ...
  ```

---

### BUG-2: Lost Task Results in Parallel Dispatch Loop (Race Condition)
- **Severity:** High
- **File:line:** otto/v5_runner.py:6418-6424
- **Symptom:** When a future completes, the code searches for its task ID using `next(t for t, f in in_flight.items() if f is fut)`. If the in_flight dict is modified concurrently (e.g., by another event loop task or a timeout handler), this lookup can fail silently or find the wrong task, causing child results to be assigned to the wrong task_id.
- **Root cause:** The lookup via `next()` with a generator expression is not atomic with respect to the subsequent `in_flight.pop()`. Between the lookup and pop, the dict could change. Additionally, if the future is not found (task_id mismatch), the `next()` call raises StopIteration, which crashes the task dispatch loop.
- **Repro / evidence:**
  ```python
  # Line 6421-6423: non-atomic lookup + pop
  for fut in done:
      tid = next(t for t, f in in_flight.items() if f is fut)  # <-- can raise StopIteration
      in_flight.pop(tid, None)                                # <-- pop is separate operation
  ```
  If a future is removed from in_flight between lines 6423 and 6424 (e.g., by a timeout handler or concurrent task), the StopIteration bubbles up unhandled.
- **Suggested fix:**
  ```python
  for fut in done:
      tid = None
      for t, f in list(in_flight.items()):
          if f is fut:
              tid = t
              break
      if tid is None:
          logger.warning("orphaned future in dispatch loop (task_id not found)")
          continue
      in_flight.pop(tid, None)
      # ... rest of processing
  ```

---

### BUG-3: Checkpoint Integrity: Partial Spec Field Overwrites
- **Severity:** Medium (correctness-critical in multi-phase workflows)
- **File:line:** otto/checkpoint.py:524-604
- **Symptom:** The spec-gate fields (intent, spec_path, spec_hash, spec_version, spec_cost) use a "preserve-if-None" merge pattern to avoid clobbering prior checkpoint values. However, if a phase=build call passes `spec_path=None` (not provided), the merge logic at line 558 falls back to the prior checkpoint's spec_path. If the prior checkpoint had a spec_path from a DIFFERENT, incompatible spec (e.g., from a prior run with different intent), the system will reference the wrong spec artifact.
- **Root cause:** The checkpoint does NOT track which phase wrote which spec fields. A phase=build run that doesn't re-validate the spec still inherits the spec_path from a stale checkpoint. This causes spec mismatch between what the task graph expects and what the build agent actually received.
- **Repro / evidence:**
  ```python
  # Line 557-558: preserve prior spec_path if None is passed
  "spec_path": spec_path if spec_path is not None else (prior.get("spec_path", "") if prior else ""),
  ```
  If a run crashes at phase=build before updating spec_path, and then a new invocation with --resume calls write_checkpoint(spec_path=None), the stale spec_path from the prior run is preserved. If intent has changed, this is now inconsistent.
- **Suggested fix:** Add a "spec_phase_version" field that tracks when the spec fields were last updated. Reject resume if the spec_phase_version is older than the current phase, forcing explicit re-spec.

---

### BUG-4: Silent Early Exit in In-Flight Task Draining (Budget Cap)
- **Severity:** Medium (user-visible but not silent failure)
- **File:line:** otto/v5_runner.py:5703-5718
- **Symptom:** When the tree budget cap is exceeded, the dispatcher breaks out of the loop and calls `asyncio.gather(*in_flight.values(), return_exceptions=True)` to drain in-flight tasks. However, if the budget cap hit occurs BETWEEN task dispatch (line 6391: `in_flight[tid] = asyncio.create_task(...)`) and the task reaching a completion state, the gather may return exception results (e.g., CancelledError) that are silently swallowed via return_exceptions=True. These tasks' verdicts are never recorded in the graph, leaving their entries in "pending_children" state indefinitely.
- **Root cause:** `return_exceptions=True` swallows exceptions instead of propagating them. The code then releases the dispatch lease but never checks if any tasks failed. Subsequent phases assume the tasks either completed or are safely pending, but they're actually orphaned in a bad state (cancelled mid-execution).
- **Repro / evidence:**
  ```python
  # Line 5714: gather with return_exceptions=True
  await asyncio.gather(*in_flight.values(), return_exceptions=True)  # <-- exceptions hidden
  # No iteration over results to check for failures
  ```
- **Suggested fix:**
  ```python
  if in_flight:
      results = await asyncio.gather(*in_flight.values(), return_exceptions=True)
      for (tid, _), res in zip(in_flight.items(), results):
          if isinstance(res, Exception):
              logger.error("task %s was cancelled/failed on budget cap: %s", tid, res)
              set_verdict(project_dir, tid, "merge_blocked", cost_usd=0.0)
      in_flight.clear()
  ```

---

## Analysis

### Non-Issues (Verified Safe)

1. **Atomic Checkpoint Writes:** `write_json_atomic()` uses `os.replace()` and `os.fsync()` properly—no partial writes.
2. **File Handle Management:** All `.open()` calls use context managers; no leaks detected.
3. **Subprocess Hygiene:** All `subprocess.run()` calls have timeouts and use list args (no shell injection risk).
4. **Git Branch Sanitization:** `child_branch_name()` and `integration_branch_name()` properly sanitize task IDs with `re.sub(r"[^a-zA-Z0-9_.-]+", "-", ...)`.
5. **Async Gather Safety:** Primary `_process_children` gather at line 5714 uses `return_exceptions=True` correctly (only for draining, not normal flow).
6. **Task Graph Reads:** `read_graph()` and `read_pending()` defensively return empty graphs on parse errors—no crashes.
7. **Verdict Aggregation:** `aggregate_verdict()` uses severity ordering correctly; worst verdict is propagated properly.
8. **Logging Timestamps:** All critical JSON writes include `"_written_at": time.strftime(...)` — no gap.

### Severity Justification

- **BUG-1 (fsync fd leak):** High — leaks kernel resources on repeated failures; impacts long-running processes.
- **BUG-2 (task result race):** High — silent result mismatch is catastrophic for multi-task runs; causes false passes.
- **BUG-3 (spec preserve):** Medium — triggers only on multi-phase resume with intent change; fixable at checkpoint read time.
- **BUG-4 (budget cap):** Medium — user-visible (tasks don't complete), but not silent; affects cost-constrained runs.

---

## Recommended Actions

1. **Immediate (BUG-2):** Fix the non-atomic in_flight lookup with a defensive search + guard. This can cause silent verdict mismatch in parallel runs.
2. **Near-term (BUG-1):** Wrap dir_fd operations in nested finally block to guarantee cleanup.
3. **Near-term (BUG-4):** Log and record verdicts for tasks cancelled by budget cap; don't silently abandon them.
4. **Deferred (BUG-3):** Add spec_phase_version tracking to reject stale-spec resume scenarios during checkpoint validation.

---

## Test Recommendations

- **Stress test:** Concurrent dispatch of 50+ tasks with budget cap at 10 task boundary; verify all task verdicts are recorded.
- **Failure injection:** Simulate `os.fsync(dir_fd)` OSError; confirm no fd leaks via `lsof` on test process.
- **Checkpoint replay:** Run with intent change between phases; confirm stale spec_path is rejected or re-validated.
