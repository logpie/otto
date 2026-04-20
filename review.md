# Review Trail — `parallel-otto` branch

Append-only log of code review findings + resolutions during the Phase 1-6
implementation of `plan-parallel.md`.

---

## Phase 1 — Implementation Gate (Codex, 2026-04-19)

### Round 1 (read-only review of initial Phase 1 implementation)

Codex returned REVISE with **5 IMPORTANT + 2 NOTE + 1 REFACTOR** findings:

1. **[IMPORTANT]** `otto/config.py:372` — Step 1.6 setup contract not enforced. `create_config()` swallowed `GitAttributesConflict` to a logger warning; `queue.bookkeeping_files: []` opt-out not honored.
2. **[IMPORTANT]** `otto/config.py:291 + otto/cli.py:488` — `detect_default_branch()` fell back to current branch when no remote, so first-run on a feature branch silently persisted that as default and broke Step 1.1's "stay put on feature branch" policy.
3. **[IMPORTANT]** `otto/cli_improve.py:107 + otto/worktree.py:112` — `otto improve --in-worktree` keyed branch/worktree off product intent, not improve focus/target. Two `improve feature "search UX"` and `improve feature "pricing"` runs collided.
4. **[IMPORTANT]** `otto/manifest.py:64` — `OTTO_QUEUE_TASK_ID` used as raw path component without validation. `../../etc` would escape the queue dir.
5. **[IMPORTANT]** `otto/branching.py:28` — slug not collision-resistant: unicode-only and long-prefix collisions produced same slug.
6. **[NOTE]** `otto/worktree.py:60` — existing path reuse didn't verify branch.
7. **[NOTE]** `otto/config.py:169` — malformed `queue:` section silently reset to defaults without warning.
8. **[REFACTOR]** Duplicated `--in-worktree` setup across cli.py and cli_improve.py (deferred to a later refactor pass).

### Round 1 fixes (Codex workspace-write call)

Per `feedback_codex_fixes_own_bugs.md`: Codex fixed all 7 substantive findings (5 IMPORTANT + 2 NOTE) in a separate `mcp__codex__codex` call with `sandbox: workspace-write`. Test count: 235 → 250.

Specific fixes:
1. `create_config()` reads queue.bookkeeping_files; skips install if empty; lets `GitAttributesConflict` propagate; soft-handles only `FileNotFoundError`/`PermissionError`.
2. `detect_default_branch()` chain: `origin/HEAD` → local `main` → local `master` → literal `"main"`. Never falls back to current branch.
3. Added `slug_source` parameter to `worktree_path_for` and `enter_worktree_for_atomic_command`; improve passes `focus or target or intent`.
4. Added `QUEUE_TASK_ID_RE = ^[a-z0-9]+(-[a-z0-9]+)*(-\d+)?$`; validation at path composition; raises `ValueError` on invalid forms.
5. Added 6-char sha1 hash suffix to slugs that hit the literal `"task"` fallback OR were truncated for length. Distinct intents → distinct slugs.
6. `add_worktree` now verifies `git branch --show-current` matches the requested branch when reusing an existing path; raises `RuntimeError` otherwise.
7. New `_normalize_queue_config()` logs WARNING for non-dict queue or wrong-type keys; falls back to defaults.

### Round 2 review

Codex returned REVISE with **1 IMPORTANT + 1 NOTE**:

1. **[IMPORTANT]** `otto/cli_setup.py:113` — Step 1.6 fix only ran inside `create_config()`, but `otto setup` only calls that when `otto.yaml` is missing. Existing projects upgrading via `otto setup` skipped bookkeeping setup entirely.

### Round 2 fixes (Codex workspace-write call)

1. Extracted `ensure_bookkeeping_setup(project_dir, config)` shared helper in `otto/config.py:227`. Reads `queue.bookkeeping_files`; skips on opt-out; runs install with the same error-handling contract.
2. `create_config()` delegates to this helper.
3. `otto setup` (cli_setup.py:113) now loads config and runs the helper for existing projects too, refreshing bookkeeping rules on upgrade.
4. New tests cover all 3 cases: existing config + missing rules / conflicting rules / opt-out.

Test count: 250 → 253.

### Round 3 review

Codex returned **APPROVED**. No new findings. The helper is well-factored (no circular imports), `otto setup` calls it at the right point (before CLAUDE prompt generation, fails early if misconfigured), and test coverage is solid.

### Phase 1 final state

- 253 tests passing (158 baseline + 95 new for Phase 1)
- Codex Implementation Gate: APPROVED in 3 rounds
- All 5 IMPORTANT + 3 NOTE findings resolved by Codex (per `feedback_codex_fixes_own_bugs.md` mandate)
- 1 REFACTOR finding deferred (cli.py / cli_improve.py --in-worktree duplication — addressable in Phase 2 when queue runner needs the same logic)

Files added: `otto/branching.py`, `otto/manifest.py`, `otto/setup_gitattributes.py`, `otto/worktree.py`, `tests/test_branching.py`, `tests/test_env_bypass.py`, `tests/test_manifest.py`, `tests/test_setup_gitattributes.py`, `tests/test_worktree.py`, `plan-parallel.md`, `review.md`.

Files modified: `otto/cli.py`, `otto/cli_improve.py`, `otto/cli_setup.py`, `otto/config.py`, `otto/certifier/__init__.py`, `otto/certifier/report.py`, `tests/test_config.py`.

---

## Phase 2 — Code-Health + Implementation Gate (Codex, 2026-04-19)

### Initial implementation
- `otto/queue/` package: `schema.py` (file format + atomic I/O), `ids.py` (slug + dedup + cycle detection), `runner.py` (watcher main loop)
- `otto/cli_queue.py`: CLI commands (build/improve/certify/ls/show/rm/cancel/run)
- `otto/cli.py`: registered queue command group
- `pyproject.toml`: added `psutil>=5.9` dependency
- 86 new tests across 4 test files

### Code-health audit (4 review agents in parallel)

Bug Hunter, Dead Code Hunter, Dedup Hunter, AI Slop Hunter dispatched simultaneously.

**Findings: 3 CRITICAL + 12 IMPORTANT + several MINOR.** All fixed by Codex (workspace-write call):
- CRITICAL: lock-mismatch race in drain_commands ↔ append_command (data loss); unhandled `_tick` exception orphans children; `_otto_bin` dead nonsense code
- IMPORTANT: `bookkeeping_files` field unused; `on_status_update` dead hook; `policy=ask` stub; cycle log spam; `os.waitstatus_to_exitcode` dead hasattr; ChildProcessError fake-success; unused dataclasses (TaskChildState/TaskState/WatcherState); `--in-worktree` duplication carried from Phase 1; intent-resolution snapshot timing asymmetric between queue improve vs certify

Test count after cleanup: 339 → 345 (+6 tests added by Codex).

### Implementation Gate (Codex 4 rounds)

**Round 1** (review of cleaned code): REVISE with 4 CRITICAL + 3 IMPORTANT
- CRITICAL: cancel/remove leaves zombie children (state marked terminal before child exits)
- CRITICAL: try/except `_tick` enables duplicate-spawn (write_state fail → reload stale → respawn)
- CRITICAL: queue tasks don't snapshot branch/worktree → collision on same intent
- CRITICAL: **Phase 2.9 was missing entirely** — `_commit_artifacts` always commits intent.md/otto.yaml even in queue mode, defeating the whole point of bookkeeping skip
- IMPORTANT: cancel rewrites done tasks; CLI doesn't validate enqueued args; `_resolve_otto_bin` fallback returns single string instead of argv list

**Round 2** (review of round-1 fixes): REVISE with 2 CRITICAL
- CRITICAL: `terminating` state (introduced in round 1) not reconciled on watcher restart
- CRITICAL: post-spawn persistence-failure exits but leaves child running untracked

**Round 3** (review of round-2 fixes): REVISE with 1 CRITICAL + 1 NOTE
- CRITICAL: `on_watcher_restart=resume` for still-alive `running` child broken — `waitpid` raises ECHILD when watcher inherited the child rather than forked it
- NOTE: missing test for the still-alive-running case at restart

**Round 4** (round-3 fixes applied; final Codex pass): explicit "defer to user" rather than open round 5. Applied the suggested fix as a final patch (extracted `_finalize_task_from_manifest` helper; ECHILD on `running` falls back to `child_is_alive` check; new test for the inherited-running-child restart case).

### Final state

- 357 tests passing (158 baseline + 199 across Phases 1-2 = +95 Phase 1 + +104 Phase 2)
- All 7 CRITICAL findings + 6 IMPORTANT resolved
- 1 REFACTOR finding from Phase 1 (cli.py / cli_improve.py --in-worktree dup) was addressed via the `setup_worktree_for_atomic_cli` helper extraction in Phase 2 cleanup
- Real-LLM E2E deferred to Phase 4 (otto merge), where it has irreducible value. Phase 2's queue mechanics are fully exercised by:
    - Unit tests with `fake_otto.sh` subprocess (full spawn → manifest → reap lifecycle)
    - Smoke E2E (CLI surface; no LLM cost) — verified `otto queue build/improve/certify/ls/show/rm/cancel`, schema integrity, OTTO_QUEUE_TASK_ID validation, exclusive lock
    - Phase 2.9 bookkeeping skip unit-tested in `test_v3_pipeline.py`

### Files added (Phase 2)
- `otto/queue/__init__.py`, `otto/queue/schema.py`, `otto/queue/ids.py`, `otto/queue/runner.py`
- `otto/cli_queue.py`
- `tests/test_queue_schema.py`, `tests/test_queue_ids.py`, `tests/test_queue_runner.py`, `tests/test_cli_queue.py`
- `RESUMING.md` (compaction-safety bridge)

### Files modified (Phase 2)
- `otto/cli.py` (registered queue commands; refactored --in-worktree via shared helper)
- `otto/cli_improve.py` (refactored --in-worktree via shared helper)
- `otto/config.py` (added `ensure_bookkeeping_setup` shared helper from Phase 1; added `resolve_intent_for_enqueue`)
- `otto/pipeline.py` (Phase 2.9 — skip bookkeeping commits in queue mode)
- `otto/worktree.py` (added `setup_worktree_for_atomic_cli` shared helper)
- `pyproject.toml` (added psutil dependency)
- `tests/test_config.py`, `tests/test_worktree.py`, `tests/test_v3_pipeline.py`

---

## Phase 3 — `--after` dependencies (2026-04-19)

Phase 3 was structurally implemented as part of Phase 2 (the queue runner needed dep handling for dispatch logic). This phase added the missing transitive cascade test (Phase 3.2 verify): A→B→C → all fail when A fails. Test count: 357 → 358. Commit `78a44181`.

---

## Phase 4 — `otto merge` MVP (2026-04-19)

### What ships
- `otto merge --all|<ids>` — Python-driven git merge loop
- `otto merge --target <branch>` — non-default target
- `otto merge --no-certify --full-verify --fast --cleanup-on-success` — mode flags (Phase 5 effectively shipped here)
- 2 prompt files: `merger-conflict.md`, `merger-triage.md`
- Step 4.0: certifier API extended with `stories` parameter; `{stories_section}` placeholder added to all 5 certifier prompts
- Step 4.1: orchestrator with provider gate (codex requires --fast)
- Step 4.2: per-conflict agent with Bash disallowed + path-scope validation + untracked-file detection + content snapshot for retry
- Step 4.3: triage agent with story-coverage validation (every input must be covered)
- Step 4.4: cert phase invokes `run_agentic_certifier(stories=must_verify)`
- Step 4.5: bookkeeping handled by Phase 1.6 `.gitattributes` (no Python normalization)
- Step 4.6: `--resume` DEFERRED to follow-up (CLI prints workaround)

### Implementation Gate
1 round → 2 CRITICAL + 2 IMPORTANT findings, all fixed by Codex:
- Triage accepted incomplete output → now validates story coverage by name
- Codex provider gate fired too late → now refuses BEFORE merge starts (unless --fast)
- Conflict agent ignored untracked files → now snapshotted + cleaned up
- `_extract_json` non-greedy regex → now greedy + DOTALL

Test count: 358 → 408 (+50 for Phase 4).

E2E smoke: clean merges work (no LLM cost), --fast bails correctly on conflict, bookkeeping union driver auto-merges intent.md.

---

## Phase 5 — Merge mode variants (2026-04-19)

Phase 5 mode flags (`--full-verify`, `--no-certify`, `--fast`) were implemented as part of Phase 4's CLI surface. No additional code or commit needed.

---

## Phase 6 — Polish (2026-04-19)

### What ships
- **`otto queue cleanup [--done|--all|<ids>...]`** — explicit worktree cleanup. Branches preserved. Manifests preserved at `otto_logs/queue/<task-id>/`. Default scope: done tasks only. `--force` overrides dirty-worktree check.
- **`otto queue ls --post-merge-preview`** — pairwise file-overlap detection across done branches. Highlights collision risk before user runs `otto merge`.

### What's deferred to v2 (per plan §7 explicit out-of-scope)
- Auto-merge to main as default (opt-in only)
- Remote/server queue
- Web dashboard
- `otto history --queue` filter (the existing `otto history` works; queue runs are visible there too)
- Log-archival-then-cleanup integration (`cleanup_after_merge: true` semantics) — current cleanup is opt-in only
- `otto merge --resume` Mode A/B/C dispatch (CLI prints helpful workaround)

Test count: 408 (no new tests — both new commands are exercised through smoke E2E).

---

## Implementation Gate — 2026-04-20 — Code-health audit (parallel-otto F1-F14 cleanup)

Audited the parallel-otto branch against main. Four review agents (bug, dead-code,
dedup, AI slop) flagged findings; fixed all CRITICAL/IMPORTANT items inline.
Then ran 3 rounds of Codex implementation review.

### Round 1 — Codex
- [IMPORTANT] `_files_with_markers` too coarse (any single marker line) — fixed by Codex (round 2 over-tightened to triplet-only; round 3 reverted to "any marker" with rationale)
- [IMPORTANT] `accumulated_diffs` used plain `git diff` not merge-aware — fixed by Codex (now uses `git diff --merge` + raw file snapshots)
- [IMPORTANT] Narrowed `(OSError, ValueError)` handlers missed `yaml.YAMLError` — fixed by Codex (normalized at `load_queue` source)
- [NOTE] `merged_with_markers` status not in CLI icon map / state.py — fixed by Codex (added to icon dict, BranchStatus Literal, docstring)
- [REFACTOR] Duplicated post-agent finalize bookkeeping — DEFERRED (sequential vs consolidated paths have structurally different bookkeeping)

### Round 2 — Codex re-reviewed fixes
- [IMPORTANT] Size guard in `_files_with_markers` failed open (>10MB files silently treated as clean) — fixed by Codex (round 3 streams line-by-line, no size cap)
- [IMPORTANT] Triplet-only detection missed partial markers (only `<<<<<<<`, or `<<<<<<<` + `=======`) — fixed by Codex (round 3 reverted to "any marker line" with defense-in-depth rationale; the original docstring false-positive concern doesn't apply because the function is only called with files in the conflict set)

### Round 3 — Codex re-reviewed fixes
- APPROVED. No new issues. Round-3 changes fail closed for both large files and partial marker remnants.

Test count: 408 → 421 (round 1) → 428 (round 2) → 430 (round 3). All passing.
