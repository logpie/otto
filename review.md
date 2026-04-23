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

---

## Implementation Gate — 2026-04-20 — Test-suite code-health audit

Audited `tests/` (6,811 LOC, 430 tests) against the same 4-agent code-health protocol.
Tests are real code and accumulate the same slop as production.

### Round 1 — Codex
- APPROVED. One NOTE-level finding about midnight-rollover race in test_cli_queue
  being reduced but not eliminated (true freeze would require monkeypatching the
  clock source `otto.branching.compute_branch_name` calls). Acceptable for now.

### Audit results
- 8 unused imports + 1 stale local import removed
- 1 real bug: `tests/conftest.py` `tmp_git_repo` was missing `check=True` (silent failure)
- 4 redundant `TestV3PipelineFail` tests (each ran the same pipeline) merged to 1
- 2 redundant `TestV3SkipQA` tests merged to 1 parametrized
- 1 duplicate signature-only test in `test_certifier_stories.py` deleted
- 1 weak assertion (`out.count("\n") >= 3`) strengthened to specific structural elements
- F-number / Codex-round / P6 cruft removed from 6 test files (mirrors production cleanup)
- 3 "removed because" gravestone comments + 1 12-line bug-archeology block deleted
- 1 apologetic 6-line comment in test_branching replaced with explicit assertion
- New `tests/_helpers.py` factory replacing duplicated `_init_repo`/`_make_repo` across 9 files

Test count: 430 → 426 (−4 from merging redundant tests, +1 parametrize). LOC: 6,811 → 6,704 (−107).

---

## Implementation Gate — 2026-04-20 — Delete sequential merge mode

Removed per-conflict sequential merge entirely. Consolidated agent mode is
now the only conflict-resolution path. Driven by bench data: P6 measured
2.1× faster, 32% cheaper, more files resolved cleanly.

### Round 1 — Codex
- [IMPORTANT] Phase-1 `merged_with_markers` rows never upgraded after agent success →
  conflicted branches showed yellow warning icons in CLI summary even when resolved.
  The synthetic `(consolidated)` row had the success status; per-branch rows had stale state.
- [NOTE] Stale docstrings in orchestrator.py / state.py / cli_merge.py / conflict_agent.py
  referencing deleted sequential behavior, --resume continuation, Codex disallowed_tools.
- [NOTE] `_files_with_markers` fail-closed scope worth documenting (intentional false
  positives on literal markers in conflict-set files).

### Round 2 — Codex re-reviewed fixes
- New helper `_update_consolidated_conflict_outcomes` in orchestrator.py walks
  `state.outcomes` and rewrites `merged_with_markers` rows in place at every
  terminal point (success → `conflict_resolved`; failure paths → `agent_giveup`
  with the failure note). Synthetic `(consolidated)` row no longer appended.
- Docstrings cleaned: orchestrator.py / conflict_agent.py / state.py / cli_merge.py
  now describe single-mode reality and mark --resume as deferred bookkeeping.
- `_files_with_markers` docstring now explicit about intentional fail-closed
  bias on literal markers in conflict-set files.
- Regression test `test_consolidated_resolution_upgrades_per_branch_outcomes`
  added (success-path coverage).
- APPROVED. No new findings. Failure-path rewrites only have indirect coverage
  but no behavioral defect was found at the call sites.

Test count: 420 → 421. LOC: otto/ ~11,000 → ~10,500 (−500 lines net).

---

## Implementation Gate — 2026-04-20 — Delete triage agent, fold into cert prompt

Removed the per-merge triage agent. Cert agent now does inline story-pruning
via a `merge_context` preamble in the rendered stories section. Same
pruning logic, no extra LLM call, no new prompt file.

### Round 1 — Codex
- [IMPORTANT] state.json back-compat: removed `verification_plan_path` from MergeState
  but `load_state()` still does `MergeState(**data)` → old state files raise
  `TypeError: unexpected keyword`.
- [IMPORTANT] `--full-verify` semantic shift: setting merge_context=None disables
  BOTH SKIPPED instruction AND FLAG_FOR_HUMAN instruction. Old --full-verify only
  disabled skip_likely_safe pruning while keeping flagging.
- [IMPORTANT] PoW report renders from `passed` boolean → SKIPPED and FLAG_FOR_HUMAN
  show up as FAIL, lying to users.
- [NOTE] Stale triage references in 16+ sites (CLI docstrings, README, architecture,
  bench scripts).

### Round 2 — Codex re-reviewed fixes
- `load_state()` filters `data` through `dataclasses.fields(MergeState)` before
  construction; drops unknown keys silently. Future-proofs against further field
  removals. Regression test added.
- `merge_context` always passed; new `allow_skip` flag (False when --full-verify).
  Preamble conditionally renders SKIPPED block; FLAG_FOR_HUMAN block always renders.
- New helpers `_story_verdict()`, `_story_verdict_display()`, `_normalize_story_result()`.
  PoW renderers use verdict-aware icons (✓ ✗ – ⚠). Regression test verifies all
  four verdicts render distinctly.
- All 16+ stale references rewritten to describe the single-cert-call flow.
- APPROVED. No new findings. Remaining `verification_plan_path` mentions are in
  the back-compat test fixture, which is correct.

Test count: 421 → 429 (+8 net: deleted 3 triage tests, added 11 new). LOC delta:
otto/ ~10,500 → ~10,300 (−200 lines).
---

# Implementation Gate — 2026-04-20 — log-restructure (Phases 1, 5, 6)

Branch: `worktree-i2p`
Commits reviewed: `9fa6554be`, `abf9313cb`, `0e9d959ae`, plus fix commit.

Scope: per-session `otto_logs/sessions/<id>/` layout, `otto/paths.py`
choke point, streaming `messages.jsonl` + `narrative.log` replacing the
legacy `live.log/agent.log/agent-raw.log` trio.

## Round 1 — Codex

- [CRITICAL] Project lock check-then-write race + never called by CLI
  entrypoints — fixed by Codex (O_EXCL atomic create + wired into
  build/certify/improve, --break-lock flag added)
- [CRITICAL] Split/resume session threading incomplete, one invocation
  fanned into multiple session dirs — fixed by Codex (session_id threaded
  through inner build/certifier calls + split-mode spec data + improve
  --resume run_id)
- [IMPORTANT] Split-mode journal writes still global/legacy paths —
  fixed by Codex (session_id threaded through all journal helpers)
- [IMPORTANT] Stale `paused` pointer after successful build — fixed by
  Codex (write_checkpoint clears paused pointer on status=completed)
- [IMPORTANT] History/memory merge order wrong (legacy entries appeared
  newer than post-refactor) — fixed by Codex (sort by parsed timestamp)
- [NOTE] messages.jsonl dropped structured_output — fixed by Codex
  (serialized when non-None + regression test)
- [NOTE] summary.json half-implemented — fixed by Codex (written at end
  of every completed session, not just --force abandoned path)

## Round 2 — Codex

- [IMPORTANT] LockHandle.release() unlinks any .lock, not its own —
  fixed by Codex (nonce-based ownership check + regression test)
- [NOTE] summary.json written for paused/error runs too — fixed by
  Codex (gated on final_status == "completed" only)

## Round 3 — Codex

- [IMPORTANT] set_session_id() could overwrite a new holder's lock after
  --break-lock — fixed by Codex (same nonce check applied to
  _write_record + regression test)

## Round 4 — Codex

- [IMPORTANT] Check-then-mutate TOCTOU still present in _write_record
  and release — fixed by Codex (switched to kernel-level fcntl.flock;
  `.lock` is now immutable after acquire; set_session_id is a no-op)

## Round 5 — Codex

- [IMPORTANT] fcntl imported at module level broke Windows — fixed by
  Codex (platform guard + Windows best-effort fallback with one-time
  warning; fork-based test marked skip-on-Windows)

## Round 6 — Codex

- [IMPORTANT] Windows fallback kept .lock fd open, preventing
  --break-lock from succeeding — fixed by Codex (Windows branch closes
  fd immediately after acquire; regression test for stale-holder unlink
  of replacement lock)

## Round 7 — Codex

- [IMPORTANT] Windows-fallback release() has intrinsic TOCTOU between
  nonce check and unlink — documented as an accepted limitation in the
  module docstring and at the release site. Unix flock is the
  authoritative correctness path.

## Round 8 — Codex re-reviewed fixes

APPROVED. No remaining critical issues.

## Final state

- 200 tests pass (up from 189 pre-gate)
- Lock: kernel flock on Unix (authoritative); Windows best-effort with
  documented TOCTOU
- Session threading: one invocation = one session dir, end to end
- Journal/report routing: all artifacts under session tree
- summary.json: canonical post-run record for completed sessions
- messages.jsonl: truly lossless (structured_output included)
- history/memory: merged chronologically across new + legacy + archive

## Co-authored

Codex authored all fixes during the gate via `mcp__codex__codex`
workspace-write sessions, per CLAUDE.md's "Codex fixes Codex-found
bugs" rule.

## Implementation Gate — 2026-04-23 — Phase 1 (substrate) — TUI Mission Control

### Round 1 — Codex
- [CRITICAL] Dual-writer race on live/<run_id>.json (queue watcher + atomic child) — fixed by Codex (atomic skips registry when OTTO_INTERNAL_QUEUE_RUNNER=1)
- [CRITICAL] Cancel acks written before durable state mutation — fixed by Codex (drain → persist → ack ordering)
- [IMPORTANT] Pre-merge cancel finalized registry without writing merge state — fixed by Codex
- [IMPORTANT] Cancel polling missing in certify; merge only polled once — fixed by Codex
- [IMPORTANT] Mixed-version compat (Exit D) not implemented — fixed by Codex
- [IMPORTANT] History writes best-effort — fixed by Codex
- [IMPORTANT] terminal_outcome schema drift (failed vs failure) — fixed by Codex
- [IMPORTANT] RunPublisher heartbeat-finalize race — fixed by Codex
- [NOTE] Hardcoded otto_logs/sessions literal — fixed by Codex

### Round 2 — Codex
- [IMPORTANT] History repair appended before queue state durable — fixed by Codex
- [IMPORTANT] Atomic cancel polling cadence (20s vs 2s) — fixed by Codex

### Round 3 — Codex re-reviewed Round 2 fixes
- APPROVED. No new issues.

Final state: 790 tests passing. Commits 0ea657fb5, fb24488c5, 4f194f71e.

## Implementation Gate — 2026-04-23 — Phase 2 (universal viewer)

### Round 1 — Codex
- [CRITICAL] Scenario A (old watcher + new viewer) not implemented — fixed by Codex
- [IMPORTANT] History pane ignored legacy/archived sources — fixed by Codex
- [IMPORTANT] Enter/Esc origin pane tracking broken — fixed by Codex
- [IMPORTANT] Adapter boundary violated for actions — fixed by Codex
- [NOTE] `/` substring filter UI missing — fixed by Codex

### Round 2 — Codex
- [IMPORTANT] load_project_history_rows() dropped limit_hint — fixed by Codex
- [NOTE] Queue-specific compat logic still in shared model — fixed by Codex

### Round 3 — Codex
- [IMPORTANT] Adapter-owned compat introduced double registry read — fixed by Codex

### Round 4 — Codex re-reviewed Round 3 fix
- APPROVED. No new issues.

Final state: 786 tests passing. Commits b0251805d, 7fb53ce57, 03d41b2f6, 2ee8e7466.

## Implementation Gate — 2026-04-23 — Phase 3 (mutations)

### Round 1 — Codex
- [CRITICAL] SIGTERM fallback signaled dead/reused process — fixed by Codex
- [IMPORTANT] Cancel appended without checking current state — fixed by Codex
- [IMPORTANT] Cleanup didn't check writer is dead — fixed by Codex
- [IMPORTANT] Queue cancel enabled with missing task_id — fixed by Codex
- [NOTE] Requeue suppressed --as on collision — fixed by Codex
- [NOTE] Subprocess only reported spawn-window failure — fixed by Codex
- [NOTE] m was single-select — fixed by Codex (multi-select with space)

### Round 2 — Codex
- [IMPORTANT] Cancel preflight broke legacy queue compat rows — fixed by Codex
- [NOTE] M (merge-all) didn't surface late exits — fixed by Codex

### Round 3 — Codex re-reviewed Round 2 fixes
- APPROVED. No new issues.

Final state: 814 tests passing. Commits 7f8718742, 7b226b639, 7990b0c96.

## FINAL Implementation Gate — 2026-04-23 — Holistic TUI Mission Control review

Reviewed full diff 99a53ccfe..HEAD (15558 lines, 45 files) — entire 5-phase work + audit fix pass.

### Round 1 — Codex
- [IMPORTANT] Merge restart repair incomplete — fixed by Codex
- [IMPORTANT] Old terminal queue attempts resurrected after GC — fixed by Codex
- [IMPORTANT] Build/improve had no startup history repair (Exit E gap) — fixed by Codex (otto/runs/atomic_repair.py)
- [NOTE] _repair_standalone_certify_history early-returned on existing history — fixed by Codex

### Round 2 — Codex
- [IMPORTANT] Atomic repair invented history from abandoned/non-terminal sessions — fixed by Codex (gate on proved terminal truth)

### Round 3 — Codex re-reviewed Round 2 fix
- APPROVED. No remaining cross-phase regressions, Repair Precedence violations, or missing gate-exit blockers.

Final state: 844 tests passing. All 5 design gate exits (A/B/C/D/E) verified.
