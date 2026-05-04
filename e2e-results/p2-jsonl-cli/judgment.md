# P2 — JSONL log filter CLI (T1 smoke, project_kind=cli) — judgment: **FAIL**

Session: `/tmp/otto-e2e/p2-jsonl-cli/otto_logs/sessions/2026-05-04-090523-2a5254`

## Phase summary

| Phase | Result | Cost | Wall |
|---|---|---|---|
| Compile | 4 slices, 3 validator warnings | – | – |
| Build | 4/4 passing | $1.80 | 687s |
| Merge | **1 landed, 2 blocked** + 1 silently dropped (cli_main, dep blocked) | $0.41 | – |
| Audit | **verdict: blocked** ✓ (V4 cap fired) | $1.20 | 412s |
| **Total** | | **$3.41** | ~24 min |

## Per-rubric-dimension verdicts

### Dim 1 — Compile honesty: **PARTIAL**
- ✅ V1 surfaced 3 warnings:
  1. `multi-slice spec declares no cross_slice_checks`
  2. `structure.payload: missing required field 'entrypoint'`
  3. `structure.payload: missing required field 'commands'`
- ❌ Compile agent did NOT populate `entrypoint`/`commands` fields the
  CLI structure schema requires. Generated webapp-shaped
  `structure.payload` (routes, components, data_model) for a CLI project.
  V7 noted: compile prompt doesn't strongly steer `structure.payload`
  by `project_kind`.
- ❌ Spec contradiction: `setup.py` and `pyproject.toml` appear in BOTH
  the `setup` slice's `owned_paths` AND in `shared_scaffold`. These
  should be mutually exclusive.

### Dim 2 — Build honesty: **FAIL**
- ✅ All 4 slices got real per-slice branches.
- ❌ **V8 — CRITICAL**: `format_aggregate`'s build agent ran
  ```
  git merge i2p/.../parse_filter --no-edit -m "merge parse_filter slice"
  ```
  directly via Bash, polluting its slice branch with parse_filter's
  contributions. Build agents have full git/bash access and Otto's
  prompt does NOT forbid git mutations. Result: format_aggregate's
  branch parent is parse_filter's tip (not main), cli_main's branch
  parent is format_aggregate's tip, branches stack instead of being
  independent.
- ❌ When stacked branches all merge into main, both contain
  pyproject.toml from setup slice (because parse_filter's branch
  pulled in setup's stuff via the rogue agent merge), causing real
  merge conflicts at merge phase.

### Dim 3 — Merge honesty: **PARTIAL** (V8-induced)
- ✅ setup landed via real merge (commit `d0812cd`).
- ❌ parse_filter, format_aggregate BLOCKED with merge conflicts.
- ❌ cli_main silently dropped (deps blocked, never reached merge_queue).
- 🟡 **V9**: `git merge --abort` left `MERGE_HEAD` in worktree (audit
  phase saw "git is mid-MERGE_HEAD" repeatedly).

### Dim 4 — Audit honesty: **PASS** ✓
- ✅ Verdict `blocked` — V4 cap fired (2/4 PASSING slices blocked at
  merge → majority blocked → BLOCKED). Honest about failure.
- ✅ Audit cost $1.20, 412s (vs $1.07 in P1 first-run with rogue
  fix-agent) — fix-loop attempts ran but couldn't recover due to V10.
- ❌ **V10**: audit fix-loop's `_setup_slice_branch` returned False on
  `MERGE_HEAD` detection and just SKIPPED the fix silently (3 log
  lines "skipping slice branch setup"), without aborting the merge
  or surfacing the inability to fix. Operator sees blocked verdict
  but no narrative explaining "fix loop couldn't run because
  worktree was mid-merge".

### Dim 5 — Product quality: **FAIL**
- ❌ Only `setup` slice's files on main: `logflt/__init__.py`,
  `logflt/__main__.py`, `pyproject.toml`. The actual filter/output/CLI
  modules never landed.
- ❌ `python -m logflt` would fail (no cli.py).
- ❌ `pytest tests/` would fail (no tests/).

### Overall: **FAIL**

P2 fails on dims 2, 3, 5. Dim 4 PASSES — Otto's verdict is honest, no
false positive. The user's "no false positives" requirement is met
even though the product is broken; that's the V1-V6 fixes paying off.
But the build/merge chain has new bugs (V8, V9, V10) that need root
fixes before T1 can be considered passed.

## Root bugs to fix (V8–V10)

### V8 — Build agent runs git mutations (CRITICAL design violation)

The build agent has bash + acceptEdits and is free to run any git
command. Observed: format_aggregate's agent saw an empty worktree and
ran `git merge` to grab another slice's files, breaking branch
isolation. This is V3-class but in BUILD, not audit.

**Fix**: explicit prompt rule forbidding git mutations. Build agents
read from git (`git log`, `git show`, `git diff` to inspect) but
must NOT run `git merge/commit/checkout/rebase/reset/push/branch -f`
or any state-mutating command. Otto manages git; if the agent thinks
it needs another slice's file, it amends the spec or notes a scope
issue.

Optional follow-on: detect post-build git state changes (slice's
branch HEAD parent != expected parent ref) and fail the slice with a
diagnostic.

### V9 — git merge --abort doesn't always clean MERGE_HEAD

Symptom: audit phase saw `git is mid-MERGE_HEAD` after merge phase
ended. Either `merge --abort` failed silently or a different merge
operation (V8's agent merge) left MERGE_HEAD around without cleanup.

**Fix**: a shared `_ensure_clean_git_state(worktree)` helper that
aborts any in-progress merge/rebase/cherry-pick before any
branch-mutating git op. Call it at the start of `_setup_slice_branch`
and `_merge_slice_branch`. Idempotent and safe — `git merge --abort`
on a non-merge state is a no-op (returns non-zero, but doesn't
corrupt).

### V10 — Audit fix-loop fails silently when worktree is unrecoverable

`_setup_slice_branch` returns False on `MERGE_HEAD` detection. The
audit fix-loop just logs "skipping slice branch setup" and continues.
The fix doesn't run, but no special "fix infeasible due to corrupt
worktree" event is emitted. Operators can't tell "fix didn't help"
from "fix never ran".

**Fix**: when `_setup_slice_branch` fails in the audit fix-loop,
attempt V9's `_ensure_clean_git_state` first; if still fails, emit a
distinct `slice.fix.skipped` event with a diagnostic detail rather
than just `slice.attempt.failed` with the same vague message.

## T1 progress

P1: PASS. P2: FAIL. T1 not passed yet (need 2+ PASS at the tier).

After V8-V10 fixes, will re-run P2. If P2 PASSES, advance to a third
T1 project (different shape again) to confirm the fixes generalize,
then T2.
