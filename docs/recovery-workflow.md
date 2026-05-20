# Broken-State Recovery Workflow

When an `otto v5 run` ends in a degraded state — `merge_blocked` children,
discarded child work, integration timed out mid-repair — you don't have to
start over. The recovery workflow lets you fix the underlying bug, retry only
the affected children, and resume the run for ~30% of the cost of a fresh
re-run.

This workflow was added in plan-checkpoint-resume-v2.md (Codex APPROVED at R5)
across Phases 0/1/2/3.

## The four commands

| Command | Mutates? | When to use |
|---|---|---|
| `otto v5 status` | No | "What state is this project in?" |
| `otto v5 plan-resume` | No | "What would `otto v5 run` do next?" |
| `otto v5 reset-verdict` | Graph only | Verdict-correction (rare; usually you want `retry-children`) |
| `otto v5 retry-children` | Atomic transaction | Re-execute specific children with the current otto code |

## The typical loop

```bash
# 1. See the broken state
otto v5 status

# 2. Predict what resume would do (cost + per-child action)
otto v5 plan-resume
# Use --json for scripting / MC integration:
otto v5 plan-resume --json | jq

# 3. If children are merge_blocked from an upstream bug you've fixed:
otto v5 retry-children --task v5-X --task v5-Y --dry-run    # validate
otto v5 retry-children --task v5-X --task v5-Y              # execute

# 4. Dispatch the retries — scheduler picks them up via
#    `verdict=None, review_state="approved", retry_count>0`
otto v5 run "<original intent>"

# 5. Verify outcome
otto v5 status
```

## What each command does

### `otto v5 status`

Read-only diagnostic. Shows:
- Project root + root intent preview
- Root verdict
- Per-child verdicts + intent previews
- Resume eligibility (RESUMABLE / NOT_RESUMABLE / FRESH_ONLY) via the
  canonical `compute_resume_plan` helper
- Suggested next command if blocked children exist
- `--verbose` adds per-child failure metadata (origin/reason/structured)

### `otto v5 plan-resume`

Read-only resume simulation. Shows:
- Phase resume would re-enter (integration / none)
- Per-child predicted action: `skip_pass` | `merge_unmerged` |
  `rebuild_via_retry` | `stays_merge_blocked` | `stays_unverified` |
  `pending_children` | `unknown_state`
- Cost estimate range (low/p50/high USD), calibrated from iTracker runs:
  - Opus: $15 / $35 / $60 per child rebuild
  - Sonnet: $3 / $8 / $15 per child rebuild
  - Integration phase: $5 / $15 / $40
- Wall time range (low/p50/high minutes)
- Concerns surfaced as advisory
- Suggested next commands
- `--json` for structured output (`schema_version: 1`)
- `--model opus|sonnet` to switch cost basis
- `--intent <text>` to enforce the persisted intent matches (refuses on drift)

### `otto v5 reset-verdict`

Verdict-only correction. Use when:
- A run incorrectly recorded a verdict and you want to fix it
- You want to flip a wrongly-merge_blocked task to `pass` (when you've
  verified manually)

**Does NOT make a task runnable** — `unverified` is settled-but-no-evidence,
not runnable. Use `retry-children` for re-execution.

```
otto v5 reset-verdict --task v5-X --to unverified [--dry-run]
```

### `otto v5 retry-children`

Atomic targeted retry. The transaction:
1. **Validation gate (full set, no mutations):** leaf check, non-foundation,
   branch exists, worktree clean (or `--continue`), no live PIDs
2. **Dependency closure:** with `--cascade-dependents`, recursively pull in
   downstream `pass` tasks that depend on targets (iterative BFS, cycle-safe)
3. **Stable lock:** `otto_logs/.locks/retry-children.lock` (NOT timestamped —
   serializes concurrent invocations) + TOCTOU revalidation under the lock
4. **Archive prior sessions:** rename to `.archived-<ts>`
5. **Snapshot graph:** deep-copy for rollback
6. **Atomic graph reset:** `clear_task_for_retry()` clears verdict +
   completed_at + all blocker metadata + sets retry_count, review_state=None
7. **Pending rewrite:** synthesizes new pending entry if missing; supersedes
   older entries (kept for audit); writes via atomic tmp+rename;
   `review_state="approved"` so `take_ready()` picks it up
8. **Rollback on any failure:** restore graph snapshot + un-archive sessions

```
otto v5 retry-children
    --task v5-X --task v5-Y         # targets (required)
    [--cascade-dependents]           # recursively include downstream
    [--continue]                     # OK if worktree dirty (commit first)
    [--force]                        # override pass-verdict refusal
    [--dry-run]                      # validate without mutating
```

**Refused inputs (exit 2):**
- Task doesn't exist
- Task is a non-leaf (has children or `decomposition=emit`)
- Task is `task_role=foundation` (foundation requires `--fresh`)
- Task's branch `i2p/build/<id>` is missing
- Worktree on wrong branch / dirty (use `--continue`)
- Live PIDs in worktree or session
- Pass-verdict task without `--force`
- Downstream pass tasks would become stale (use `--cascade-dependents`)
- Cycles in dependency graph (e.g., A→B→C→B)

## How resume actually re-dispatches retried children

After `retry-children` resets state:
- Graph entry: `verdict=None, retry_count>0, retry_reason="cli_retry_children"`
- Pending entry: `verdict=None, review_state="approved", retry_count>0`

`otto v5 run` (no `--fresh`):
1. `_resume_root_from_checkpoint` detects partial root + child branches → skip
   compile + decompose + child rebuild (most), re-enter at integration
2. Scheduler's `take_ready()` finds entries with `verdict=None,
   review_state="approved"` → dispatches them
3. Children rebuild with the current otto code (today's fixes apply)
4. Integration runs after all children complete

## Phase 3 checkpoint artifacts

Each session under `otto_logs/sessions/<sid>/` now writes:

- `checkpoint.events.jsonl` — durable append-only log; one line per
  phase boundary (compile_done / decompose_done / integration_done / etc.)
- `checkpoint.json` — materialized snapshot/cache with sha256 checksum +
  `last_event_seq` (corrupt or stale → rebuilt from events.jsonl)

Read via `otto.v5_checkpoint.get_or_rebuild_snapshot(session_dir)`.

Forward-compat: unknown event kinds are appended to events.jsonl for
later consumers; current snapshot materialization is `schema_version: 1`.

## Worked example: the iTracker Opus 2026-05-20 case

Original run: $205, 96min, 2/5 children merged, root `merge_blocked` —
3 features discarded via foundation_contract_write_gate + verdict-evidence
schema mismatch + chokepoint routing miss.

After fixing the otto bugs (5 commits) and shipping retry-children (Phase
0+1), the same broken state was recovered via:

```bash
otto v5 status                # showed 3 merge_blocked children
otto v5 retry-children \
    --task v5-83da4b4ba629 \
    --task v5-133534052888 \
    --task v5-f353f8ea8602 \
    --dry-run                 # validated atomic plan
otto v5 retry-children ...    # executed transaction
otto v5 run "<intent>"        # dispatched retries
```

Result: $87.64 / 49.5min / 5-of-5 children merged / verdict=partial /
product boots cleanly. **57% cost savings, 48% time savings, $117 of
work retained that would have been discarded.**

See `review.md` for the full Codex Implementation Gate trail (3 rounds,
APPROVED at R3).
