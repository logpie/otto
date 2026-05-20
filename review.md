# Review Trail — `parallel-otto` branch

Append-only log of code review findings + resolutions during the Phase 1-6
implementation of `plan-parallel.md`.

---

## Leaf Runtime Invariant Poisoning Fix — Implementation Gate (2026-05-15)

Scope reviewed:
- `otto/lead.py` prompt runtime block/sanitizer and verdict Write-tool rescue.
- `tests/test_v5_leaf_runtime_invariants.py`.
- Current `research.md` / `plan.md` entries.

Local `codex-gate` checklist result: APPROVED. External Codex MCP gate remains
unavailable in this session; no peer review tool was invoked.

Diff review findings:
- Adjusted sanitizer to apply only to child/integration prompts so root
  user-authored intents are not unexpectedly altered.
- No blocking findings after the adjustment.

Validation:
- Pre-fix check: reversed only the `otto/lead.py` patch and ran
  `uv run --extra dev python -m pytest tests/test_v5_leaf_runtime_invariants.py -v`
  — both tests failed against the old behavior, then passed after reapplying.
- `uv run --extra dev python -m pytest tests/test_v5_leaf_runtime_invariants.py -v`
  — 2 passed.
- `uv run --extra dev python -m pytest tests/test_v5_verdict_recovery.py tests/test_v5_integration_worktree.py -q`
  — 18 passed.
- `uv run --extra dev python -m pytest tests/smoke -v` — 12 passed.
- `uv run ruff check otto/lead.py tests/test_v5_leaf_runtime_invariants.py`
  — passed.
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev basedpyright --level error otto/lead.py tests/test_v5_leaf_runtime_invariants.py`
  — 0 errors.
- Strict `basedpyright` without `--level error` still exits nonzero on the
  existing warning profile in `otto/lead.py` (0 errors, 144 warnings).

---

## Field-Test Forced Tiers — Implementation Gate (2026-05-15)

Scope reviewed:
- Scenario tier metadata and expected-shape declarations under
  `bench/field-tests/`.
- `scripts/run_field_tests.py` tier validation/reporting.
- `otto/lead.py` tier prompt semantics.
- `otto/v5_runner.py` root inline commit finalization.
- Regression tests for field-test reporting, tier prompt text, and root inline
  commit behavior.

Local `codex-gate` checklist result: APPROVED. External Codex MCP gate remains
unavailable in this session; no peer review tool was invoked.

Diff review findings:
- No blocking findings. Root inline uses `commit_worktree()` instead of the
  stricter integration allowlist because greenfield inline products may create
  legitimate top-level files such as `csv_to_json.py`.

Validation:
- `uv run --extra dev pytest tests/test_run_field_tests.py tests/test_v5_inline_commit.py tests/test_v5_integration_worktree.py -q` — 17 passed.
- `uv run ruff check scripts/run_field_tests.py tests/test_run_field_tests.py tests/test_v5_inline_commit.py tests/test_v5_integration_worktree.py otto/lead.py otto/v5_runner.py` — passed.
- `uv run --extra dev pytest tests/smoke/ -q` — 12 passed.
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python scripts/run_field_tests.py --dry-run --report-path /tmp/otto-field-test-tier-dry-run.md` — passed and wrote tiered dry-run report.

---

## v6 Dispatch 3 — Gate Availability (2026-05-14)

The local `codex-gate` skill was present, but the `mcp__codex__codex` tool was
not available in this session. Plan Gate and Implementation Gate could not be
invoked through MCP. Local validation was used instead:

- `uv run --extra dev pytest tests/test_v5_context_slicer.py tests/test_v5_ia_contract.py tests/test_v5_capability_inventory.py tests/test_spec_compile_flat_structured.py tests/test_v5_record_preservation.py -q` — 68 passed
- `uv run --extra dev pytest tests/ -q -k "v5 or spec_compile or charter" --ignore=tests/integration` — 354 passed, 2261 deselected
- `uv run ruff check otto scripts tests` — passed

No review findings were produced because the MCP gate was unavailable.

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
- CRITICAL: lock-mismatch race in the legacy command-drain path (data loss); unhandled `_tick` exception orphans children; `_otto_bin` dead nonsense code
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

---

### A5 Approval — 2026-05-04 (tick 37)

**Phase**: A5 — Spec review screen + hybrid plan ownership.
**Verdict**: APPROVED for the data + plumbing layer. Visual polish (Add Feature modal, diff view, full markdown styling) is tracked in progress.md as post-cutover items, not blockers.

Acceptance evidence:
- 1774 unit tests pass after the tick-35 alias-bug fix (slices=/groups= silent no-op resolved at root in `Spec.__init__`).
- `tests/integration/test_spec_review_e2e.py::test_a5_full_review_flow` passes — full GET → /edit → /approve flow produces correct on-disk artifacts (spec.json updated, spec-v1.json archived, lifecycle.json approved) and emits all three spec.* events in order.
- `tests/integration/test_spec_review_e2e.py::test_a5_stale_edit_blocked_during_concurrent_session` passes — Tier-1 concurrency guard verified end-to-end: stale intent_hash → 409, on-disk spec untouched.
- Frontend: `?view=spec-review&spec=<id>` route mounts SpecReviewPage; typecheck + vite build green (tick 34).
- Backend routes: 14/14 spec_review_routes tests pass (CRUD + path-traversal + lifecycle).

Pending (NOT blocking A5 closure):
- `spec.regenerated` event wiring (compile-agent recompile path; lands when that path is exercised).
- Visual polish (markdown rendering, Add Feature modal, diff view).
- Browser RUA against an in-flight pause (deferred to Phase B/C — needs a real running session).

---

### A6 Approval — 2026-05-04 (tick 46)

**Phase**: A6 — Brownfield compile mode.
**Verdict**: APPROVED for the data + plumbing layer. A6.6 (file preserve markers) deferred per progress.md until a real user need triggers it; not blocking.

Acceptance evidence:
- 45/45 brownfield + guard tests pass across:
  - `tests/test_brownfield_preamble.py` — 13 tests (file tree, README, manifest, truncation, ignore filter, determinism)
  - `tests/test_brownfield_compile.py` — 8 tests (greenfield path unchanged, brownfield prompt switch, additive reconcile rules)
  - `tests/test_out_of_scope_guard.py` — 22 tests (15 parametrized keyword cases + override + integration with compile_spec)
  - `tests/integration/test_brownfield_compile_real.py` — 2 tests (full Python plumbing against realistic CLI fixture, empty-base + additive paths)
- All caps in `otto/defaults.py` (`BROWNFIELD_PREAMBLE_MAX_FILES=200`, `MAX_LINES_PER_FILE=200`).
- Greenfield `compile_spec` path entirely unchanged (verified by test_greenfield_compile_unchanged + test_compile_spec_rejects_out_of_scope_intent_before_llm guards).
- Tier-1 invariants honored in additive mode (intent + intent_hash from base; mechanical/historical fields preserved).

What this unblocks:
- B.1 — `otto certify` cutover: brownfield-compile a baseline spec from the existing project, then run audit_loop + render. The "no spec to drive audit" gap from tick 39 is now closed.
- B.2 — `otto improve` cutover: same pattern, with multi-round audit_loop wrapping cli_improve.

Pending (NOT blocking A6 closure):
- A6.6 (file preserve markers, e.g. `.otto/preserve` file pattern) — deferred until needed by a real user; mechanism options sketched in progress.md.

---

## Implementation Gate — 2026-05-14 — Pre-v6c hardening

Codex MCP gate status: skipped because the `mcp__codex__codex` tool required
by `/codex-gate` is not available in this session. Local deterministic review
and verification were used instead.

Findings from the new hardening tests:
- [IMPORTANT] Lead verdict rescue missed Codex/OpenAI-style terminal `result`
  records — fixed in `otto/lead.py`.
- [IMPORTANT] Nested schedulers could re-dispatch a `pending_children`
  decomposition task after releasing the global lease — fixed in
  `otto/queue/subtask.py`.

Verification:
- New hardening tests: 8 passed.
- Requested focused suite: 412 passed, 2229 deselected.
- Ruff on touched files: passed.
- Py compile on touched Python files: passed.

---

## Implementation Gate — 2026-05-14 — v6.6 consolidation

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, diff review, ruff, and
the requested smoke matrix were used instead.

Findings during implementation review:
- [IMPORTANT] Initial artifact edit overwrote the existing tracked
  `research.md`; restored it from HEAD and moved this pass's notes to
  `research-v6.6-consolidation.md`.
- [IMPORTANT] Separate smoke-preflight repair consumed the failure before the
  integration prompt could see it; changed integration smoke preflight to
  observe/inject and left repair-loop dispatch for branch hygiene.
- [NOTE] Git commits could not be created because the sandbox cannot write the
  parent git admin dir for this worktree.

Verification:
- Six post-change runs of `uv run --extra dev pytest tests/smoke/ -q` passed.
- Six post-change runs of
  `uv run --extra dev pytest tests/ -q -k "v5" --ignore=tests/integration`
  passed.
- Final matrix: smoke 14 passed; v5 suite 302 passed, 2353 deselected.
- Ruff on touched files passed.

---

## Implementation Gate — 2026-05-15 — field-test rig

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, diff review, focused
tests, ruff, py_compile, and dry-run report generation were used instead.

Findings during implementation review:
- [IMPORTANT] `bench/` was ignored globally, so the new scenario files would
  have been invisible to git. Added a narrow `.gitignore` exception for
  `bench/field-tests/**` while keeping `bench/field-tests/runs/*` ignored.
- [IMPORTANT] Boot-smoke cleanup now stores the process group id immediately
  after launching `start.sh`, so cleanup can still kill child processes even if
  the shell exits quickly.
- [NOTE] No live Otto run was launched. Only dry-run report generation was
  exercised.

Verification:
- `uv run pytest -q tests/test_run_field_tests.py` passed: 4 tests.
- `uv run ruff check scripts/run_field_tests.py tests/test_run_field_tests.py`
  passed.
- `uv run python -m py_compile scripts/run_field_tests.py tests/test_run_field_tests.py`
  passed.
- `uv run python scripts/run_field_tests.py --dry-run --report-path /tmp/otto-field-test-dry-run.md`
  passed and wrote the sample matrix without launching Otto.
- `uv run python scripts/run_field_tests.py --dry-run --scenario 03-todo-fullstack --parallel 2 --report-path /tmp/otto-field-test-one-dry-run.md`
  passed and verified scenario selection.
- `git diff --check` passed.

---

## Implementation Gate — 2026-05-15 — Round 6 nested integration worktree binding

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, diff inspection,
red/green regression proof, requested non-regression suites, ruff,
basedpyright, and `git diff --check` were used instead.

Findings during implementation review:
- [HIGH] Confirmed root cause in `otto/v5_branching.py`: the merge primitive
  checked out the target integration branch in caller `project_dir`, even when
  that branch was already legally checked out by a dedicated integration
  worktree. Git rejects that with "already used by worktree".
- [HIGH] Fixed by resolving the target branch owner through
  `git worktree list --porcelain` and running the merge in that owner
  worktree. This applies to child-build to parent-integration merges and
  subtree-integration to grandparent-integration merges because both use
  `merge_branch_into()`.
- [MEDIUM] Added a per-target branch lock under the git common dir so two
  completions cannot mutate the same integration branch at the same time.
- [MEDIUM] Preserved repair observability by mirroring conflict packets back
  to `project_dir` when the merge ran in a separate target worktree.
- [NOTE] The e469 all-pass subtree was not this Git checkout failure. Its
  branch reached `main`; it stayed `partial` due runner-check failures. The
  bc66 serving child was blocked by dependencies and was not a separate
  orphaning mechanism.

Verification:
- Red proof before production patch: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_integration_worktree.py::test_child_merge_uses_existing_integration_worktree_owner -q`
  failed as expected with `checkout i2p/integ/v5-parent failed: fatal:
  'i2p/integ/v5-parent' is already used by worktree .../.worktrees/integ-v5-parent`.
- Focused regression after implementation: same command passed, 1 passed.
- Existing merge/worktree suites: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_integration_worktree.py tests/test_v5_merge_noise.py tests/test_v5_phase5.py -q`
  passed: 37 passed.
- Required hardening/leaf/smoke/guardrail batch plus the new regression:
  `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_p0_hardening.py tests/test_v5_p1_hardening.py tests/test_v5_p2_hardening.py tests/test_v5_pass4_hardening.py tests/test_v5_leaf_runtime_invariants.py tests/test_brittleness_guardrail.py tests/smoke tests/test_v5_integration_worktree.py::test_child_merge_uses_existing_integration_worktree_owner -q`
  passed after replacing an implicit `or project_dir` fallback flagged by the
  guardrail: 113 passed.
- Required smoke tier: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python scripts/test_tiers.py smoke`
  passed: 306 passed, 2471 deselected.
- Required merge queue: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_merge_queue.py -q`
  passed: 46 passed.
- Touched-file ruff passed.
- Touched-file basedpyright passed: 0 errors, 0 warnings, 0 notes.
- `git diff --check` passed.

---

## Implementation Gate — 2026-05-15 — modular decomposition repair

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, diff review, focused
tests, ruff, py_compile, and live field-test launch attempts were used
instead.

Findings during implementation review:
- [IMPORTANT] `start.sh` clean-deploy failures were blocking before the
  integration repair loop could act. Root and subtree integration smoke
  preflights now run through the repair controller and commit successful
  repair edits on the integration branch.
- [IMPORTANT] Direct child branch propagation had no final invariant. The
  scheduler now verifies that every pass/partial/unverified child branch tip
  reaches the parent target branch before it declares the parent ready.
- [IMPORTANT] Port cleanup killed every PID on declared ports. It now kills
  only Otto-owned or field-test-owned listeners and leaves unrelated processes
  alone.
- [NOTE] Live validation could not reach product execution in this sandbox:
  the requested runs root was not writable, and writable reruns for
  `04-mini-crm` and `05-blog-generator` both stopped during spec compilation
  because the Claude provider was not logged in.

Verification:
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run ruff check otto/v5_runner.py otto/v5_preflight_repair.py otto/v5_clean_verify.py tests/test_v5_integration_worktree.py tests/test_v5_port_cleanup.py tests/test_v5_decomposed_child_lands_in_main.py` passed.
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python -m py_compile otto/v5_runner.py otto/v5_preflight_repair.py otto/v5_clean_verify.py tests/test_v5_integration_worktree.py tests/test_v5_port_cleanup.py tests/test_v5_decomposed_child_lands_in_main.py` passed.
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev pytest -q tests/test_v5_port_cleanup.py tests/smoke/test_preflight_repair_fixtures.py tests/test_v5_integration_worktree.py::test_integration_smoke_failure_runs_repair_on_resolved_worktree tests/test_v5_decomposed_child_lands_in_main.py::test_all_passing_direct_child_branch_tips_reach_main_even_noop_child` passed: 18 tests.
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev pytest -q tests/test_v5_integration_worktree.py tests/test_v5_port_cleanup.py tests/test_v5_decomposed_child_lands_in_main.py tests/smoke/test_preflight_repair_fixtures.py tests/test_v5_preflight.py tests/smoke/test_nested_subtree_propagation.py` passed: 46 tests.
- `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python scripts/test_tiers.py smoke` passed: 305 passed, 2370 deselected.
- `git diff --check` passed.

---

## Implementation Gate — 2026-05-15 — v5 P0 merge/verdict integrity

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, focused diff review,
red/green regression proof, smoke gates, ruff, and basedpyright were used
instead.

Findings during implementation review:
- [IMPORTANT] Raw `partial` and `unverified` child verdicts are now blocked
  before upward merge and routed through one verify/repair attempt plus the
  existing clean-deploy smoke oracle. Only `pass` or a recorded
  `review_state="reviewed_partial"` may merge upward.
- [IMPORTANT] Vague success payloads without journey/evidence proof now
  canonicalize to `unverified`; runner verification-plan crashes also
  invalidate self-reported `pass`.
- [IMPORTANT] Compile-spec JSON failures now fail loudly instead of writing an
  empty fallback contract.
- [IMPORTANT] Unknown clean-verify/preflight failure kinds now block for
  repair with raw failure context instead of defaulting to warning.
- [IMPORTANT] Scaffold clean-copy failures now block for repair; only the
  explicitly named "no package manager/runtime to compile" skips remain
  warnings.
- [NOTE] `reviewed_partial` is intentionally minimal: a durable task-graph
  review state plus metadata, leaving the terminal verdict as `partial`.

Verification:
- Red proof used a reversible production patch rollback because git stash
  could not write the worktree index from this sandbox. With the production
  P0 patch reversed, `tests/test_v5_p0_hardening.py` failed: 15 failed,
  1 passed.
- Green focused regression: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_p0_hardening.py tests/test_merge_queue.py::test_run_merge_queue_blocks_degraded_slice_with_failed_behavior_check -q`
  passed: 17 passed.
- Required invariant/smoke suite: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_leaf_runtime_invariants.py tests/smoke -q`
  passed: 61 passed.
- Required smoke tier: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python scripts/test_tiers.py smoke`
  passed: 306 passed, 2434 deselected.
- Touched-file lint: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run ruff check ...`
  passed.
- Touched-file type check: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev basedpyright --level error ...`
  passed: 0 errors.

---

## Implementation Gate — 2026-05-15 — v5 P1 agentic-native hardening

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, focused diff review,
red/green regression proof, smoke gates, ruff, and basedpyright were used
instead.

Findings during implementation review:
- [IMPORTANT] Required runtime/build failures now stay in the repair lane:
  missing required runtimes block instead of warn-skip, scaffold timeouts get
  one larger-budget retry, and remaining failures route through the existing
  preflight repair oracle.
- [IMPORTANT] Deterministic preflight auto-fixes now have honest no-op
  semantics. Empty filename/chmod repairs fall through to the agent instead of
  logging repaired without a state change.
- [IMPORTANT] Startup port cleanup now reports killed/freed/still-bound ports.
  Still-bound ports after cleanup block startup unless agent repair plus the
  cleanup oracle clears them.
- [IMPORTANT] Integration worktree setup must produce the intended worktree and
  branch. Setup failures now trigger repair and block if unresolved, instead of
  running integration in `project_dir`.
- [IMPORTANT] Non-driver merge conflicts now write a conflict packet with both
  sides and dispatch a focused repair agent, then require a clean merge plus
  smoke oracle before accepting the result.
- [IMPORTANT] Audit repair scheduling now guarantees an initial repair attempt
  for every failing group before caps can narrow work, reserves re-audit budget
  for that first fix, and raises on spec/verdict group mismatches.
- [IMPORTANT] Ambiguous context slicing now dispatches a focused spec/scope
  resolver agent from the runner and records explicit last-resort full-context
  fallback only when that resolution path is unavailable or unresolved.
- [IMPORTANT] Out-of-scope leaf writes are now blocking scope failures that
  require amendment/revert before merge.
- [NOTE] Duplicate sweep found `setup_child_worktree(...); falling back to
  project_dir` in `otto/v5_branching.py`. It is outside this P1 item list
  (the requested fallback fix was integration worktree setup in
  `otto/v5_runner.py`), so it was left unchanged for a separate pass.

Verification:
- Red proof used a reversible production-only patch rollback. With the P1
  production patch reversed, `uv run --extra dev python -m pytest tests/test_v5_p1_hardening.py -q`
  failed: 15 failed.
- Green focused regression: `uv run --extra dev python -m pytest tests/test_v5_p1_hardening.py -q`
  passed: 15 passed.
- Required invariant/smoke suite: `uv run --extra dev python -m pytest tests/test_v5_p0_hardening.py tests/test_v5_leaf_runtime_invariants.py tests/smoke -q`
  passed: 77 passed.
- Required smoke tier: `uv run python scripts/test_tiers.py smoke`
  passed: 306 passed, 2449 deselected.
- Merge queue non-regression: `uv run --extra dev python -m pytest tests/test_merge_queue.py -q`
  passed: 46 passed.
- Touched-file lint: `uv run ruff check otto/v5_preflight.py otto/v5_preflight_repair.py otto/v5_clean_verify.py otto/v5_runner.py otto/v5_branching.py otto/audit_loop.py otto/v5_context_slicer.py otto/build.py tests/test_v5_p1_hardening.py tests/test_audit_loop_repair.py tests/test_build.py`
  passed.
- Touched-file type check: `uv run --extra dev basedpyright --level error otto/v5_preflight.py otto/v5_preflight_repair.py otto/v5_clean_verify.py otto/v5_runner.py otto/v5_branching.py otto/audit_loop.py otto/v5_context_slicer.py otto/build.py tests/test_v5_p1_hardening.py tests/test_audit_loop_repair.py tests/test_build.py`
  passed: 0 errors, 0 warnings, 0 notes.
- `git diff --check` passed.

---

## Implementation Gate — 2026-05-15 — v5 P2 agentic-native hardening

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, focused diff review,
red/green regression proof, smoke gates, ruff, basedpyright, and diff-check
were used instead.

Findings during implementation review:
- [IMPORTANT] Detector-found non-passing verdicts now default to agent repair.
  Deterministic no-repair is limited to typed provider/auth/quota codes with
  explicit reasons.
- [IMPORTANT] Browser command classification is centralized behind a typed
  adapter with exact executable/module/script-table mappings. Unknown commands
  fall through as real checks instead of being silently skipped.
- [IMPORTANT] Flat spec compile now prefers typed structured-output payloads
  from the provider result/breakdown, with only the exact Claude
  `StructuredOutput` tool contract retained as provider-specific fallback.
- [IMPORTANT] Webapp structured contract absence is a required verification
  failure so compile/product repair can see it. Non-webapp legacy specs remain
  skipped.
- [IMPORTANT] Scaffold build failures now emit `partial` instead of
  `unverified`, preserving the negative evidence for repair routing.
- [NOTE] A residual string scan remains in the agent-browser server boot
  preflight for script contents. It is outside the central command-family
  router changed in this pass and should be revisited in a broader heuristic
  audit.

Verification:
- Red proof before production patch: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_p2_hardening.py -q`
  failed as expected: 9 failed, 1 passed.
- Focused P2 regression after implementation: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_p2_hardening.py -q`
  passed: 10 passed.
- Existing touched regressions: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_repair_gates.py tests/test_browser_testing.py tests/test_spec_compile_flat_structured.py tests/test_v5_verification_plan.py tests/test_v5_certify_scaffold.py tests/test_audit_loop_repair.py -q`
  passed: 75 passed.
- Required P0/P1/leaf/smoke plus P2: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_p0_hardening.py tests/test_v5_p1_hardening.py tests/test_v5_leaf_runtime_invariants.py tests/smoke tests/test_v5_p2_hardening.py -q`
  passed: 102 passed.
- Required merge/audit/build non-regression: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_merge_queue.py tests/test_audit_loop_repair.py tests/test_build.py -q`
  passed: 137 passed.
- Required smoke tier: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python scripts/test_tiers.py smoke`
  passed: 306 passed, 2460 deselected.
- Touched-file lint: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run ruff check ...`
  passed.
- Touched-file type check: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev basedpyright --level error ...`
  passed: 0 errors, 0 warnings, 0 notes.

---

## Implementation Gate — 2026-05-15 — Overnight iTracker correctness bugs

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, research/plan notes,
red/green regression proof, requested regression batch, smoke tier, ruff,
basedpyright, and direct diff review were used instead.

Findings during implementation review:
- [HIGH] The preflight repair cap was counting successful repairs as total
  failure pressure. It now keeps the old repeated-fingerprint and per-kind
  guards, treats repaired/changed/narrowed oracle output as progress, and adds
  a separate absolute ceiling for pathological loops.
- [HIGH] Deprecation detection was matching prose that merely mentioned
  warnings. It now requires an emitted `DeprecationWarning:` line, ignores
  zeroed/filtered success summaries, and filters third-party path origins such
  as `site-packages`.
- [HIGH] Runner downgrades were not written into `failure_reason`, leaving
  partial summaries with an empty reason. Downgrades now update the session
  result, summary reason, and mutable verify result consistently.
- [HIGH] Step 0b reconciliation was branch-ancestry-only. Verification-blocked
  children now carry durable `merge_blocked_origin=verification`, and
  reconciliation refuses to convert those to `pass` without a real oracle
  re-verification.
- [NOTE] Unknown legacy `merge_blocked` children still reconcile by ancestry to
  preserve the existing recovered-child behavior. New verification-blocked
  paths are explicitly marked so they do not rely on ambiguous legacy state.

Verification:
- Red proof before production patch:
  `uv run pytest -q tests/smoke/test_preflight_repair_fixtures.py::test_progressing_preflight_repairs_do_not_hit_old_total_cap tests/test_v5_verification_plan.py::test_deprecation_detection_filters_prose_dependencies_and_records_downgrade_reason tests/test_v5_step0b_recovery.py::test_reconcile_does_not_upgrade_verification_blocked_child_by_ancestry`
  failed as expected: 3 failed.
- Same focused command after implementation passed: 3 passed.
- Required regression batch:
  `uv run pytest -q tests/test_v5_p0_hardening.py tests/test_v5_p1_hardening.py tests/test_v5_p2_hardening.py tests/test_v5_pass4_hardening.py tests/test_v5_leaf_runtime_invariants.py tests/test_brittleness_guardrail.py tests/test_v5_integration_worktree.py tests/smoke tests/test_merge_queue.py tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py`
  passed: 210 passed.
- Required smoke tier:
  `uv run python scripts/test_tiers.py smoke` passed:
  307 passed, 2473 deselected.
- Touched-file lint:
  `uv run ruff check otto/v5_preflight_repair.py otto/v5_verification_plan.py otto/lead.py otto/v5_runner.py otto/queue/task_graph.py tests/smoke/test_preflight_repair_fixtures.py tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py`
  passed.
- Touched-file type check:
  `uv run basedpyright --level error otto/v5_preflight_repair.py otto/v5_verification_plan.py otto/lead.py otto/v5_runner.py otto/queue/task_graph.py tests/smoke/test_preflight_repair_fixtures.py tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py`
  passed: 0 errors, 0 warnings, 0 notes.

---

## Implementation Gate — 2026-05-15 — Pass 4 brittleness containment

Codex MCP gate status: skipped because no `mcp__codex__codex` tool is
available in this session. Local codex-gate checklist, focused diff review,
red/green regression proof, smoke gates, ruff, basedpyright, and diff-check
were used instead.

Findings during implementation review:
- [HIGH] Redispatch terminality and dependency satisfaction were conflated.
  `catastrophic`/`merge_blocked`/`unverified`/raw `partial` now stay
  non-runnable for anti-thrash, but only `pass` and reviewed partials unlock
  dependents.
- [HIGH] Synthesized audit walkthroughs could report success with no runnable
  webapp shape. Missing shape now fails the walkthrough oracle, and configured
  walkthrough failure caps an otherwise-passing judge verdict to `partial`.
- [HIGH] Malformed per-check evidence remains non-slice-blocking per v2.1, but
  is now machine-marked as malformed, diagnostic-only, and not usable proof.
  Audit packets and prompts consume that typed signal.
- [HIGH] Child worktree setup no longer falls back to the project root or
  dispatches a Lead without a valid parent integration branch. Setup failure
  or missing branch identity records `merge_blocked` before agent dispatch.
- [MEDIUM] Several silent JSON/YAML/config fallback readers now log malformed
  or unreadable input before returning default state.
- [MEDIUM] The standing guardrail is AST-based and green. It detects success on
  error/malformed/fallback paths, swallowed state parse failures, substring
  error classifiers, non-pass dependency-satisfaction sets, and branch/worktree
  identity default fallbacks. Allowlist entries require concrete reasons.
- [NOTE] Two legacy root/default-branch helpers remain explicitly allowlisted:
  `otto/cli_run.py:_pipeline_base_branch` and
  `otto/mission_control/service.py:_merge_target`. They are not child
  worktree/dependency identity paths, but remain visible medium debt.

Verification:
- Red proof before production patch: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_pass4_hardening.py -q`
  failed as expected: 7 failed.
- Focused Pass 4 plus guardrail: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_pass4_hardening.py tests/test_brittleness_guardrail.py -q`
  passed: 10 passed.
- Required smoke tier: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run python scripts/test_tiers.py smoke`
  passed: 306 passed, 2470 deselected.
- Required hardening/leaf/smoke/guardrail batch: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_p0_hardening.py tests/test_v5_p1_hardening.py tests/test_v5_p2_hardening.py tests/test_v5_leaf_runtime_invariants.py tests/smoke tests/test_brittleness_guardrail.py -q`
  passed: 104 passed.
- Required merge/audit/build/repair plus Pass 4/config/audit regressions:
  `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev python -m pytest tests/test_v5_pass4_hardening.py tests/test_merge_queue.py tests/test_audit_loop_repair.py tests/test_build.py tests/test_repair_gates.py tests/test_audit.py tests/test_config.py -q`
  passed: 333 passed.
- Extra touched check suite: adding `tests/test_checks.py` produced four
  environment-only failures because this sandbox rejects local socket bind on
  `127.0.0.1:0` with `PermissionError: [Errno 1] Operation not permitted`;
  the same run had 400 passing tests before those socket probe failures.
- Touched-file lint: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run ruff check ...`
  passed.
- Touched-file type check: `UV_CACHE_DIR=/private/tmp/otto-uv-cache uv run --extra dev basedpyright --level error ...`
  passed: 0 errors, 0 warnings, 0 notes.
- `git diff --check` passed.

## Implementation Gate — 2026-05-15 — agent-native repair protocol (4 protocol commits + fixes)

### Round 1 — Codex (REVISE)
- [CRITICAL] composite gate ran after commit_hook → post-commit bypass — fixed by Codex (ed70bc5cc): pre-commit gate on working tree + post-commit gate via pre_repair_head..HEAD
- [CRITICAL] dict(os.environ) serialized into packet/result/CLI → secret leak — fixed by Codex: serialized-env allowlist; runtime exec keeps live env
- [IMPORTANT] AgentCallError escaped run_oracle_repair_agent — fixed: caught → structured merge_blocked escalation, session/cost preserved
- [IMPORTANT] budgets reset on packet replay; cost/idle/closeout unenforced — fixed: budget reconciled from events.jsonl under lock; closeout implemented
- [IMPORTANT] agent-run oracle pass ignored if controller oracle budget exhausted — fixed: packet reloaded after each agent turn, freshest oracle evaluated before escalation
- [REFACTOR] RepairPacket construction duplicated x4 — fixed: shared _build_repair_packet (env-allowlist + baseline + budget-replay live once)

### Round 2 — Codex re-reviewed fixes (REVISE — both CRITICALs confirmed fixed; 2 new IMPORTANT)
- [IMPORTANT] CRITICAL-1 fix introduced a glob-ownership regression (literal-only _path_allowed false-blocks owned globs like src/features/foo/**) — fixed by Codex (8169f042e): extracted shared otto/path_ownership.py; build.py delegates (test_build 73 passed, no drift); _path_allowed gains globs, keeps literal-prefix
- [IMPORTANT] closeout still dispatched a provider call after hard cost/wall/idle exhaustion — fixed: _CLOSEOUT_AGENT_REASONS = {budget_exhausted, oracle_budget_exhausted} only; cost/wall/idle/diff → packet-derived escalation, no agent turn

### Round 3 — Codex re-reviewed round-2 fixes
- APPROVED. No new CRITICAL/IMPORTANT. Shared matcher preserves build._matches_any semantics (no import cycle, no drift); closeout reason-gate complete; both CRITICALs remain fixed.

Plan Gate: 4 rounds (11+7+3 findings) → APPROVED. Implementation Gate: 3 rounds (5+2 findings) → APPROVED.

## Implementation Gate — journey-verification (Units 1-3) — 4 rounds → APPROVED

### Round 1 — Codex (REVISE: 2 CRITICAL + 5 IMPORTANT + REFACTOR)
- [CRITICAL] committed range internally broken (Unit-3 dropped legacy_results, v5_clean_verify still passed it — incomplete git-add) — fixed 5fb0fd7f5 (+ committed load-bearing spec_schemas/service.json Claude caught)
- [CRITICAL] generated UI pass-models unexecutable (compile "avoid selectors" vs executor requires selectors) — fixed: accessible-locator lowering
- [IMPORTANT] contract normalization failed open / non-webapp not enforced / leaf verdict regressed webapp / api informational passes / ui missed effects+untracked-dirt — all fixed
- [REFACTOR] api if-chain → PROBE_EXECUTORS table + shared journey_executor_common.py
### Round 2 — Codex (REVISE: 3 IMPORTANT) — fixed a42eaf0e7
- api strength validator≠executor (header/json-path counted-not-enforced) → implemented + COUNTED==ENFORCED test
- malformed api pass-model crashed → validate + per-journey guard → unverified
- accessible UI lowering text false-pass → scoped pre/post DELTA + exact-default
### Round 3 — Codex (REVISE: 1 IMPORTANT) — fixed 9240cf828
- delta guard missed role/name/label-only observables → _dom_observable_state fingerprints all forms + require_delta all forms + locator-set agreement test
### Round 4 — Codex
- APPROVED. No new CRITICAL/IMPORTANT. Action-effect delta covers all validator-accepted locator forms; agreement test meaningful; final-persistence presence preserved; range clean.

Plan Gate: 4 rounds (7+5+1) → APPROVED. Implementation Gate: 4 rounds (8+3+1) → APPROVED.

---

## Concurrency+Recursion Critical-Seam Fixes — Implementation Gate (2026-05-16)

Reviewed: 6796a9058 (spec-state event_id flock + moving-target union repair),
hardened across rounds. Repros banked in 401e78f6c
(tests/test_concurrency_recursion_seam_repros.py): #1 depth-3 dual-subtree
propagation GREEN (regression); #2 5-way concurrent merge RED→fixed; #3
spec-state event_id race RED→fixed.

### Round 1 — Codex
- [CRITICAL] Conflict-path stale retry could terminal merge_blocked with
  structured_reason=None (fell into generic block) — fixed by Codex (f92b55ff9):
  unified record_terminal() always records non-None structured reason.
- [CRITICAL] Three stale-target re-entry calls unguarded → escape →
  catastrophic — fixed by Codex: _repair_child_stale_target_gate_once
  builds feedback first, try/excepts the bounded re-entry.
- [IMPORTANT] Stale-target success after conflict repair skipped merged-target
  smoke oracle — fixed by Codex (run_smoke_preflight in shared helper).
- [IMPORTANT] Follow-up union failure recorded stale_feedback as top-level,
  burying integration_union_incomplete — fixed by Codex (followup_feedback
  top-level, stale nested).
- [REFACTOR] Three diverging near-duplicate stale-target blocks — collapsed
  into one shared _repair_stale_target_and_retry_merge helper.

### Round 2 — Codex re-reviewed fixes
- [IMPORTANT] Union recheck (_record_and_check_integration_union) still
  unguarded in helper + site-2 → escape → unstructured — fixed by Codex
  (3779aca79): wrapped → integration_union_guard_error structured terminal.
- [NOTE→fixed] Empty pre_merge_ref returned blind success — fixed: structured
  *_pre_merge_ref_unresolved terminal before any downstream union check.

### Round 3 — Codex re-reviewed fixes
- [IMPORTANT] Original inline conflict-repair smoke call still unguarded
  (escape / generic unstructured block) — fixed by Codex (9d82e48f8):
  _child_merge_conflict_smoke_failed_feedback + unified recorder + return.
- [IMPORTANT] Terminal recorder _record_structured_merge_failed could itself
  throw via task-graph IO — fixed by Codex: now no-throw (staged in-memory →
  best-effort durable with recording_error → best-effort emit); unified as
  the single terminal recorder.

### Round 4 — Codex re-reviewed fixes
- [IMPORTANT] REGRESSION from round 3: _repair_child_upward_merge_gate_once
  recorded terminally itself AND callers recorded → duplicate
  merge_blocked + double merge_failed per child — fixed by Codex (b7ed0366a):
  caller-owned terminal ownership; helper report-only again.
- [IMPORTANT] Deeper repair-helper awaits after commit_worktree still
  unguarded → escape — fixed by Codex: every direct await try/excepted via
  _child_repair_helper_crashed_feedback + unified recorder + return; final
  child_merge_path_incomplete guard before sole _emit "merged".

### Round 5 — Codex final confirmation
- APPROVED. All 5 invariant claims confirmed (exactly-one terminal; no
  post-commit escape; "merged" unreachable after terminal; no-throw
  recorder; bounded/single-channel unchanged). NOTE: structured-feedback
  builder count is a maintainability follow-up, non-blocking.

Independent verification (Claude): tests/test_concurrency_recursion_seam_repros
+ test_critical_seam_repros + test_shared_route_registration_repro 9/9;
spec_state+v5_runner+merge_child 20/20; ruff clean.

## 2026-05-16T08:22:38Z - Spec Compile Timeout Re-entry Review

Implementation gate status: `/codex-gate` / Codex MCP is unavailable in this
session's tool list, so no external gate was invoked.

### Review findings
- No issue found in timeout classification: `_is_compile_agent_timeout()` only
  matches `AgentCallError` reasons with Otto's exact `Timed out after Ns`
  wording, so max-turn/budget/provider errors keep propagating.
- No issue found in retry bounds: timeout retries use three total compile-agent
  attempts and the existing transient-provider retry remains one retry within
  the same bounded attempt counter.
- Budget wiring review found one issue before final verification: CLI and
  runner compile call sites were not passing `RunBudget` into `compile_spec()`.
  Fixed by threading `RunBudget.start_from(config)` through `_run_compile_phase`,
  `_brownfield_compile_locked`, and `runner.run_pipeline()` compile fallback.
- No issue found in terminal recording: compile `SpecValidationError`s now emit
  `run.finished` with `verdict=blocked`; structured timeout exhaustion carries
  `structured_reason.kind=spec_compile_timeout_exhausted`.

### Verification
- RED run before production fix: `tests/test_spec_compile_timeout_reentry.py`
  failed 3/4, proving raw timeout propagation and missing terminal recording.
- GREEN: `uv run --extra dev pytest tests/test_spec_compile_timeout_reentry.py -q -p no:cacheprovider`
  passed 4/4.
- GREEN: `uv run --extra dev pytest tests/ -q -k "spec_compile or compile_spec" -p no:cacheprovider`
  passed 98/98 selected.
- GREEN: `uv run ruff check otto/`.
- Additional focused config check passed: `uv run --extra dev pytest tests/test_config.py -q -k spec_timeout -p no:cacheprovider`.

Plan Gate: n/a (fix dispatched from RED repro evidence, not a written plan).
Implementation Gate: 4 review rounds + 1 confirmation (8 findings) → APPROVED.
Commits: 401e78f6c (repros) · 6796a9058 → f92b55ff9 → 3779aca79 → 9d82e48f8
→ b7ed0366a (fix series).

---

## Spec-Compile Timeout Robustness — Implementation Gate (2026-05-16)

Reviewed: 798b0a0d4 → a75677088 (bounded compile-timeout re-entry +
structured terminal; default 600→1200). Surfaced by the iTracker capstone
crashing catastrophic at the 600s spec-compile cap.

### Round 1 — Codex
- [CRITICAL] compile created a fresh/discarded RunBudget → not bounded by
  run budget — fixed by Codex (a75677088): single invocation RunBudget
  threaded through compile + raise_compile_budget_exhausted_if_needed.
- [IMPORTANT] for_call() could yield 0/neg → degenerate 0s attempt —
  fixed: budget.exhausted()/<=0 guard before dispatch → structured terminal.
- [IMPORTANT] runner.py run_pipeline(spec=None) still escaped uncaught —
  fixed: catches SpecValidationError → shared record_compile_failure_terminal
  → blocked RunResult.
- [IMPORTANT] timeout detection too broad (substring search) — fixed:
  anchored fullmatch on exc.reason vs the exact agent.py outer-timeout shape.
- [NOTE→addressed] test gaps — repro expanded 4→10.
- [REJECTED/REVERTED by Claude] Codex added an out-of-scope
  _repair_verdicts_for_audit "product-wide PASSED backfill" to green a
  broadened test selection; it masked a PRE-EXISTING unrelated failure
  (test_runner.py::test_layer2_repairs_multiple_actionable_features_by_default,
  red at fd476df20, before this arc). Reverted in full; no verdict-semantics
  change shipped. Pre-existing test tracked separately, intentionally left red.

### Round 2 — Codex re-reviewed fixes
- APPROVED. No CRITICAL/IMPORTANT.
- [NOTE] downstream build/merge/audit still use BuildBudget not RunBudget
  (accepted "minimum approach": compile just can't continue with 0 time).
- [NOTE] SpecCompileBudgetExhaustedError records attempts=len(timeout_attempts),
  undercounting a prior non-timeout attempt — structured-reason fidelity
  only; control flow bounded/correct. Tracked follow-up.

Implementation Gate: 1 review round + 1 confirmation → APPROVED.
Commits: 798b0a0d4 → a75677088.

### Separate finding (NOT this fix's scope) — capstone compile-convergence
Live iTracker capstone (47-feature intent) crashed twice in spec-compile:
600s (old default) then 1800s. Root: the compile AGENT thinking-loops —
repeatedly emits "Let me write the spec JSON now" across elapsed 278s→773s
→1288s without ever emitting the Write/structured-output. This timeout fix
correctly converts that catastrophic crash into an honest bounded terminal
but does NOT make the agent converge. Compile-agent convergence on very
large intents is a separate open issue (prompt/agent design).

---

## Audit-Repair Over-Classification (#3) — Implementation Gate (2026-05-16)

Reviewed: e551565a3 → 1415419f7. Root cause bisected to 146f2a889
("agentic-native hardening pass 3 — over-classification"): removed
audit_loop's blocked/no-evidence exclusion → repair_gate defaulted every
non-passing verdict to REPAIR_NOW → no-evidence/"not evaluated" features
perpetuated repair+audit rounds (test_layer2: 4 audits vs 2). User
directive: kill the recurring classification class, not patch #4.

### Round 1 — Codex
- [CRITICAL] audit-timeout recovery flattened recovered `failed` →
  evidence-less `blocked` → new evidence-driven gate returned NO_REPAIR
  → genuine failure silently unrepaired (false-pass) — fixed by Codex
  (1415419f7): shared typed contract; recovered failed/partial →
  actionable; strength fields carried into Layer-2 payload.
- [IMPORTANT] stale-verdict oscillation for evidence-backed failures
  omitted by a PASSED re-audit — fixed: _fill_missing_attempted_oracle
  _state makes omitted attempted ids count as no-progress (signature
  only; no verdict synthesis) so the loop halts.
- [NOTE] evidence signal was an ad-hoc allow-list (relocated taxonomy /
  drift root) — fixed: new otto/repair_evidence.py single typed
  RepairEvidence contract consumed by parse + recovery + runner payload
  + gate.

### Round 2 — Codex re-reviewed fixes
- APPROVED. No CRITICAL/IMPORTANT.
- [NOTE] _audit_output_format() producer schema still manually lists
  only evidence_refs, not generated from the shared contract — "can't
  desync" holds downstream of produced JSON only. Non-blocking; future
  cleanup tracked.

Production de-classified to evidence-driven (failed/partial = explicit
signal; ambiguous needs actionable evidence); no verdict synthesis, no
detail/test-string special-casing; oracle test_layer2_… byte-unchanged;
2 regression tests added. Verified: oracle PASS; 194/194 focused;
430/430 broad audit|repair|runner|audit_loop; ruff clean.

Implementation Gate: 1 round + 1 confirmation → APPROVED.
Commits: e551565a3 → 1415419f7. Triage trail: brownfield fixtures
(stale Group.title, 0fa0a81bc) + this (146f2a889 regression). The 4
tests/test_v5_*.py route-isolation dirty files remain pre-existing /
out-of-scope (untouched, uncommitted).

---

## Ownership-first redesign S0 (runtime primitive) — Implementation Gate (2026-05-16)

Plan: plan-ownership-decomposition.md (Plan Gate APPROVED, 4 rounds).
S0 = task_role + foundation_contracts data-model/parse/persist/idempotency.

### Round 1 — Codex (REVISE)
- [CRITICAL] foundation→feature duplicate silently kept stale role —
  fixed (sentinel) f9cd4cf96.
- [CRITICAL] duplicate update changed task_graph not pending → stale
  dispatch — fixed (_reconcile_pending_entry_with_graph, graph-wins at
  take_ready).
- [IMPORTANT] idempotency not atomic (double-create) — fixed
  (_locked_append spans check+append+record).
- [IMPORTANT] persist couldn't clear stale parent contracts on emptied
  CHARTER — fixed (write [] on valid-empty).
- [NOTE] scope confirmed clean (no S1 leak).

### Round 2 — Codex (REVISE)
- [CRITICAL] sentinel lost at MCP boundary (_coerce_task_role(None)→
  "feature") → omitted-role duplicate still demoted foundation — fixed
  b4b3202b5 (None survives end-to-end; clobber only on explicit role).
- [IMPORTANT] unreadable/missing CHARTER wiped parent contracts (read
  failure indistinguishable from valid-empty) — fixed
  (foundation_contracts_charter_unreadable finding → persist no-op).
- C2/I1 confirmed correct.

### Round 3 — Codex — APPROVED
- Omitted sentinel survives end-to-end; explicit clobbers; new child =
  feature; duplicate-correction keyed on `role is not None` (real
  signal, not value-sniff); unreadable-CHARTER no-clear while
  genuine-removal clears; malformed-readable still re-enters. No new
  issues.

S0 Implementation Gate: 2 review rounds + 1 confirmation → APPROVED.
Commits: b4cfa3afe (S0) → f9cd4cf96 (R1) → b4b3202b5 (R2). 16 S0 units
GREEN; 5 scene repros still RED (S0 flips none — correct); ruff clean.
4 test_v5_phase2 failures confirmed PRE-EXISTING (git-worktree
test-harness rot, identical at 8dece7c93 before S0; one of the 4
uncommitted route-isolation files; out of scope). Proceed to S1.

---

## Ownership-first redesign S1 (isolation gate + scheduler + write-invariant) — Impl Gate (2026-05-16)

### Round 1 — Codex (REVISE)
- [CRITICAL] scheduler allowed feature dispatch when contracts absent — fixed.
- [CRITICAL] terminal-blocked foundation silently stranded features — fixed.
- [CRITICAL] isolation gate missed the NESTED owned-path capstone shape
  (foundation backend/, feature backend/routers/auth.py) — fixed; scene
  #1 strengthened to nested shape (verified RED on old db5b8196c).
- [IMPORTANT] _task_entry_allows_upward_merge ignored durable blocked — fixed.
- [IMPORTANT] over-broad any-contract_amendment write allow — removed
  (S2 reintroduces bound allow); integration-of-record deferred.
- 8 commit hooks enumerated+gated; no S2-S5 leak — confirmed.

### Round 2 — Codex (REVISE)
- [CRITICAL] foundation passed-without-contracts dead-end (silent hold,
  test masked via external injection) — fixed: re-enter architect
  bounded → honest terminal; test rewritten to real transition.
- [CRITICAL] terminal foundation only terminalized ready features, not
  pending depends_on ones — fixed: scan ALL unmerged feature siblings.
- Nested-tree fix + IMPORTANT-4/5 confirmed correct.

### Round 3 — Codex — APPROVED
- Bounded re-enter (existing cap), all dependents terminalized on
  exhaustion, valid-contracts path unaffected, branches compose
  (missing-after-pass vs terminal-blocked mutually exclusive),
  pure-feature unaffected, tests assert real transitions. No new issues.

S1 Implementation Gate: 2 review rounds + 1 confirmation → APPROVED.
Commits: db5b8196c (S1) → cfabf1fbf (R1) → 78535d150 (R2). Scene #1
GREEN (nested capstone shape); #2-#5 RED; 22 S0+S1 units GREEN; broad
suite only the 4 known pre-existing test_v5_phase2 git-worktree-rot
failures; ruff clean. Proceed to S2.

---

## Ownership-first redesign S2 (shared-contract repair routing + amendment lifecycle) — Impl Gate (2026-05-16)

Hardest step — a net-new graph state machine with crash/restart/multi-runner concerns.

### Round 1 — Codex (REVISE)
- [CRITICAL] merge-only retry never persisted restored verdict → restart double-merge (scene masked via fake set_verdict) — fixed.
- [CRITICAL] amendment crash stranded blocked leaves (settlement only on normal path) — fixed (any terminalization → settle).
- [IMPORTANT] bound write-allow still permitted arbitrary non-contract writes — fixed (amendment writes only its bound contract).
- [NOTE] futile-amendment churn unbounded — fixed (per-(leaf,contract) cap=2 → honest terminal).

### Round 2 — Codex (REVISE)
- [IMPORTANT] retry flag cleared before merge → crash/restart/2nd-runner re-dispatch window — fixed (durable contract_amendment_retry_in_progress, fails-closed).

### Round 3 — Codex (REVISE)
- [CRITICAL] R2 left second-runner race (mark not compare-and-set) — fixed (atomic CAS under _locked_graph).
- [CRITICAL] R2 introduced crash/restart DEADLOCK (stale in-progress, no recovery) — fixed (bounded stale-recovery: pid/heartbeat → reclaim-resume or terminalize).

### Round 4 — Codex (final; 1 minimal must-fix, rest accepted)
- [CRITICAL] heartbeat never refreshed → live owner running a legit ~1800s merge falsely reclaimed after 15min → two concurrent merges — fixed (owner-token-checked 60s heartbeat refresh; dead owners still timeout-recover).
- Accepted NOTE-level residual: conservative remote/unknown-host stale timeout (dead remote owner waits the bounded timeout before recovery).

S2 Implementation Gate: 4 review rounds → APPROVED with one tracked
NOTE. Commits: ea2dfccad (S2) → e661e80da (R1) → 6a2caac6e (R2) →
ae224e766 (R3) → eae1f3a2e (R4). Scenes #1/#5 GREEN; #2/#3/#4 RED;
34 S0+S1+S2 units GREEN; broad suite only the 4 known pre-existing
test_v5_phase2 git-worktree-rot failures; ruff clean. Net invariant:
exactly one runner executes a leaf's merge-only retry at a time;
crash/restart always resolves to pass or honest merge_blocked within
bounded attempts; no double-merge, no deadlock, no silent strand.
Proceed to S3.

---

## Ownership-first redesign S4 (split detection-only smoke from leaf repair loop) — Impl Gate (2026-05-16)

The fix for the user's ORIGINAL pain: the 1799s leaf repair-agent timeout that hung the iTracker capstone.

### Round 1 — Codex (REVISE)
- [CRITICAL] S4 broken on the REAL path: CleanOracleIssue.paths dropped by preflight_issues_from_clean_oracle/PreflightIssue/serialization → classifier saw pathless → empty-bound amendment + cap-check/increment key mismatch → 1799s stuck-cycle re-emerged via S2 tasks (tests masked with pathful fakes) — fixed (d91cece58): PreflightIssue.paths field threaded end-to-end + clean_oracle_result fallback; pathless → honest integration_smoke_unrouteable; single normalized repair_path cap key.
- [IMPORTANT] in-scope leaf smoke-repair fallback entered UNRESTRICTED full-oracle loop — fixed: allowed_paths=owned + scope_policy + commit-hook allowlist before foundation gate.

### Round 2 — Codex (REVISE)
- [CRITICAL] py_compile set issue.paths = ALL compiled files (command input, not causal) → leaf-owned syntax error looked out-of-scope → misrouted in-scope bug — fixed (2adf5cad1): causal-path parsing at the producer; router binds ALL causal paths or honest unrouteable (no first-sorted guess); py_compile confirmed only non-causal producer; amendment gate multi-bound-path support stays tight.
- R1 pathless/cap + in-scope-scoping confirmed CLOSED.

### Round 3 — Codex — APPROVED
- Causal py_compile parsing robust across shapes; broad input never drives routing; audit holds (py_compile only non-causal producer); multi-path binding tight (rejects outside bound set; unrouteable if cannot bind all); S4-R1 + S0/S1/S2 protections intact; tests exercise the real producer.
- [NOTE tracked] _py_compile_causal_paths converts abs-path-outside-cwd → basename (edge: synthetic basename for unexpected internal traceback). Non-blocking; hardening = skip non-cwd-relative abs paths.

S4 Implementation Gate: 2 review rounds + 1 confirmation → APPROVED with
1 tracked NOTE. Commits: fa5c481c5 (S4) → d91cece58 (R1) → 2adf5cad1
(R2). Scenes #1/#2/#5 GREEN; #3/#4 RED (S5/S3 not yet done); 44 S0-S2+S4
ownership units GREEN; broad suite only the known pre-existing
test_v5_phase2 git-worktree-rot + committed test_v5_architect_retry
check_scaffold_compiles-AttributeError rot (both pre-session, e2329e9a7;
entangled with the user's 4 uncommitted route-isolation dirty files —
deliberately NOT committed); ruff clean. The original 1799s leaf
repair-hang is now structurally impossible (detection-only smoke;
out-of-scope→S2-routed runnable task; pathless/indeterminate→honest
terminal; in-scope→owned-path-scoped). Remaining: S3, S5, global verify,
capstone acceptance.

---

## v5 one-hard-gate — Phase 1·Task 1.1·Step 1 terminal inventory (2026-05-19)

Helper defs: `_block_child_before_upward_merge` v5_runner.py:4006;
`_record_task_merge_blocked_reason` :6803; `_record_structured_merge_failed` :6839.

Helper call-sites ~42 — `_record_task_merge_blocked_reason` (~12): 1752,
3276, 3347, 3593, 5148, 5186, 6048, 6081, 6203, 6407, 6866 (+def-internal
6828/6851). `_record_structured_merge_failed` (~30): 1613, 1645, 7251, 7464,
7487, 7509, 7554, 7576, 7626, 7660, 7717, 7741, 7780, 7837, 7871, 7893,
7930, 7952, 7980, 8030, 8080, 8093, 8121, 8141, 8193, 8227, 8273, 8302.

Direct terminal-literal verdict writes in v5_runner.py: **46** — 1749, 1845,
3297/8, 3327/8, 3365/6, 3387, 3398, 3401, 3690, 4015/23, 4441, 4460,
4505(cat), 4532, 4539(cat), 4561(cat), 4756, 4816, 4826(cat), 5141, 5183,
5704, 6045, 6078, 6200, 6295/7, 6435(cat)/6438(cat), 6493, 6501, 6555, 6563,
6575, 6583, 6828, 6851, 8806, 9149, 9162, 9197/8. cli_v5.py sys.exit: 12.

Assessment: architecture sound; chokepoint collapses ~42 helper call-sites
to 2 helper bodies. BUT ~46 direct literal writes each need individual
cause-classification + per-caller control-flow refactor + artifact-exists
assert (Codex R3#1 quantified at ~46 distinct contexts) — large,
individually-risky refactor of the most critical orchestration file. STOPPED
and raised to user for scoping (executing-plans: stop on critical scale gap).

---

## v5 one-hard-gate KEYSTONE — Implementation record (2026-05-19)

**Codex Implementation Gate: WAIVED for this session per explicit user
instruction ("no codex needed for this session unless i say so"). This
record is the honest paper trail so the gate can run later if requested.**

### What changed (production: otto/v5_runner.py only)
- `import enum`.
- Terminal chokepoint: `TerminalCause`, `TerminalAction`,
  `resolve_terminal_outcome(*, cause)` (no default cause), `_cause_from_origin`.
- `_record_task_merge_blocked_reason` + `_record_structured_merge_failed`
  rerouted: only `INFRA_CORRUPT` keeps `merge_blocked`; every other cause
  LANDS (`verdict='partial'` + `landed_with_annotation` metadata +
  `verify_result['annotations']`). ~42 helper call-sites neutralized in 2
  bodies.

### Deviation from Codex Plan-Gate R2#3 (documented for later audit)
Plan said no-default-cause + explicit `cause=` at all 42 call-sites.
Implemented instead: cause derived inside the helper via `_cause_from_origin`
from the existing `origin`/`phase` args; unmapped → `PRODUCT` (LAND).
Rationale: in the inverted design the ONLY refusal is `INFRA_CORRUPT`,
decided at the git/merge layer — NOT in these recording helpers — so an
unmapped origin landing CANNOT hide a needed refusal; LAND is the correct
fail-safe and this avoids 42 churny error-prone edits. A test asserts known
origins map explicitly; unmapped origins log a warning.

### Test triage (evidence-based; env later degraded — see caveat)
- Keystone's own unit tests: 19/19 green; `ruff check` clean.
- Full suite (pre-env-degradation): 2871 passed / 36 failed / 2 skipped.
- Isolated A/B (stash otto/v5_runner.py): **7 proven pre-existing**
  (fail with keystone ON and OFF: brittleness_guardrail,
  prompt_group_vocabulary x2, v5_architect_retry, v5_p1_hardening &
  v5_step5 port-cleanup, v5_spec_cache_hardening).
- **4 `test_v5_phase2` failures proven NOT keystone**: fail identically
  with keystone OFF in the current env — `git worktree add … not a git
  repository` (v5_runner.py:6554). Environmental, exposed by this session's
  SIGKILLed 29-min suite + repeated `git stash push/pop` degrading the
  worktree env.
- Remaining (`ownership_s1/s2/s4`, `decomposed_child::merge_blocked_*`,
  `shared_route_registration`): **expected behavior-change** — they assert
  the deleted fail-closed `merge_blocked` contract. Contract-migration of
  these is deferred TAIL work (was to be Codex-led; now deferred per the
  no-Codex-this-session instruction).
- **Net: zero proven keystone logic regressions.** Caveat: env degradation
  mid-investigation means the with/without split cannot be re-proven
  cleanly without an env reset; the keystone's deterministic own-tests +
  the pending Linkboard e2e are the real validation.

### Validation status
Unit suite is NOT a reliable gate here (integration-heavy, no per-test
timeout, env-fragile). Real thesis test = Linkboard e2e: does always-land
break the convergence/cascade failure. Pending next.

### Linkboard e2e VERDICT (2026-05-19, session 2026-05-19-173946-98bf6c)

**KEYSTONE THESIS VALIDATED on a live run.** Authoritative task_graph.json:
- foundation `v5-7072f73474e1`: verdict=`pass`
- feature `v5-6f7993287989`: verdict=`partial`, `landed_with_annotation=True`
- feature `v5-e91e2922b59d`: verdict=`partial`, `landed_with_annotation=True`
- ALL `merge_blocked_origin=None`; run.out merge_blocked/cascade grep = 0;
  no architect_retry / reenter / fresh-lead anywhere.

Both feature children carry the EXACT keystone chokepoint signature
(`partial` + `landed_with_annotation=True`) instead of `merge_blocked`.
The prior-session failure mode (feature child merge_blocked → fresh-lead
cascade → budget starvation → non-convergence) is ELIMINATED. Product was
built on disk (backend/ app+pyproject+tests, frontend/ index+package+
node_modules, start.sh, CHARTER.md, scaffold-contract.json).

**Caveat (NOT a keystone regression — the deferred Phase 1.2 issue,
empirically confirmed):** the run did not reach a clean final ROOT verdict;
root stayed `pending_children` because the bounded integration-smoke
repair turn-1 WEDGED ~6+ min past the 40-min budget (python child 0% CPU,
zero writes 6+ min). This is exactly the Phase 1.2 BUDGET_EXHAUSTED /
landing-transaction gap (Codex Plan-Gate R2#4 / R3#4): a single
bounded-repair turn is not wall-clock-bounded and can overrun/wedge,
preventing the terminal landing transaction. Now proven by a live run →
this is the next highest-value protocol work.

Orthogonal: journey `register_tag_bookmark_filter` never passed — a real
product bug (Add-bookmark button visibility / no POST /api/tags) the
bounded repair was chasing. Product-quality, not keystone (land-vs-refuse).

**Task #5 ("convert Linkboard-path terminal callers") is largely MOOT:**
the e2e proves the helper chokepoint already covers the Linkboard
suffocation (child-merge/cascade) path end-to-end. The real remaining gap
is Phase 1.2 (budget-bounded repair / landing transaction), a
protocol-level deferred plan item.

### CORRECTION (2026-05-19, after the run actually terminated)

The verdict above was PREMATURE — written before the e2e finished; I
called the long repair turn "hung/wedged" (slow-vs-hung error, twice) and
declared Task #5 moot. The run then exited cleanly (exit 0, 2766s/~46min)
with **`Verdict: merge_blocked` at ROOT**. Corrected truth:

- **Child level: keystone IS validated** (foundation `pass`; both features
  `partial`+`landed_with_annotation=True`; no child merge_blocked; no
  cascade; no architect-reentry). This stands.
- **Root level: STILL REFUSED.** `Agent timed out after 1199s` →
  `integration root: merge_blocked` → root verdict `merge_blocked`. The run
  did NOT land end-to-end. Linkboard suffocation is NOT fully fixed.
- **Exact refusing site (Task #5 — NOT moot):**
  `otto/v5_runner.py:4757` `integration_result.verdict = "merge_blocked"`
  (direct literal, deferred baseline, NOT chokepoint-routed) → `:4768`
  verify_result → `:4790` `set_verdict(ROOT_TASK_ID, integration_result
  .verdict)`. Trigger = the 1199s integration-agent single-turn timeout
  (`:2599`/`:8772` confirm this is the known p0fix3 prior-session cause).
- Net: keystone Phase-1 = real PARTIAL win (child cascade eliminated,
  proven) but end-to-end convergence NOT achieved — the same merge_blocked
  bug class survives at the integration-root terminal because Phase-1 only
  rerouted the 2 helpers, not this direct literal + the 1199s
  integration-agent timeout (Phase 1.2 / Task #5 both implicated).
- Process integrity note: I declared "hung" twice on long-but-progressing
  work. Lesson: a 0%-CPU/no-write snapshot during an LLM/agent turn is NOT
  proof of hang; an agent turn can be silent for many minutes. Verify with
  the agent-turn timeout (here 1199s), not a single ps snapshot.

### Integration-failure ROOT CAUSE + fix plan (2026-05-19)

Read the 580-line integration-repair narrative. The repair agent was NOT
stuck/broken — it productively fixed cascading real product bugs (POST
/api/tags → 201 ✓; diagnosed select-vs-input; edited BookmarksPage.tsx;
`tsc --noEmit` CLEAN) and was killed **mid `git add -A && git commit`** by
the hard 1199s single-turn timeout. Work discarded → post-agent smoke
re-ran against unfixed code → `_integration_smoke_blocks` true →
`v5_runner.py:4757` direct `merge_blocked` → `:4789` ROOT override.

Root cause = (1) 1199s integration-repair single-turn cap too short for
legitimate multi-bug repair; (2) on timeout the agent's in-progress work
is thrown away; (3) the integration terminal at 4753-4790 is a deferred
direct literal not routed through the chokepoint. Exactly the fail-closed
"discard near-complete work + refuse" pathology, surviving at the
integration layer.

FIX (evidence-driven, no-Codex this session):
- **Part A / Task #5:** extract 4753-4781 → testable helper routing
  through `resolve_terminal_outcome`; post-agent smoke-block = VERIFICATION
  cause → LAND `partial`+annotation (the blocking issues become the
  honest annotation), never `merge_blocked`. Root override then carries
  `partial`.
- **Part B / Phase 1.2 slice:** before the post-agent terminal, the
  runner commits any uncommitted repair-agent worktree changes (safety
  commit) so near-complete work is preserved + the smoke evaluates the
  real repaired state, not the discarded one.
A makes it LAND; B makes what lands contain the repair. Together = the
end-to-end Linkboard fix. Deeper boot-maximization stays full-Phase-3.

### Part A DONE (commit 9185b1837); Part B = full Phase 1.2 (2026-05-19)

Part A shipped + tested (23/23 keystone+Task5 green, ruff clean):
`_integration_terminal_verdict` routes the post-agent terminal through
the chokepoint → VERIFICATION cause → LAND `partial`+annotation. The run
no longer REFUSES at root (merge_blocked→partial). Decisive refusal fix.

Part B finding (why it's NOT an inline slice): the timed-out pre-agent
smoke-repair takes the escalated branch `if _preflight_repair_escalated:
integration_result = _preflight_blocked_result(...)` (v5_runner.py:4692)
which SKIPS `_commit_integration_agent_changes` (else-branch only) AND
sets `post_preflight_result = preflight_result` (no re-run). So
preserving the work needs: (1) commit runner-committable dirty state on
the escalated branch, AND (2) re-run the post-agent smoke against the
committed tree so the fix is actually evaluated. That is the Phase 1.2
landing-transaction (plan-v5-one-hard-gate.md Phase 1.2 / Task #8) —
correctness-critical escalated-path control flow, not a hack. Deferred to
a proper focused Task #8 implementation (Codex waived → MORE care, not
less). Part A alone already makes Linkboard LAND (partial) instead of
refuse; Part B improves WHAT lands (near-fixed vs pre-repair state).

### Validation e2e — Part A + Part B PROVEN end-to-end (2026-05-19)

Run `bp237rgkr` on project `fastrepro-linkboard-140038`, exit 0, cost
$3.73, duration 2656s (~44min).
- Same trigger as prior: `Agent timed out after 1199s` on the integration
  smoke-pre repair turn.
- **Part A working:** `integration root: partial` → `Verdict: partial`
  (prior run: `Verdict: merge_blocked`). task_graph: root=partial,
  foundation=pass, features=pass+partial(landed_with_annotation). Zero
  merge_blocked, zero cascade, zero architect-reentry in run.out.
- **Part B working:** product `git log` contains commit `73fec6b otto:
  preserve timed-out integration-repair work (Phase 1.2 Task #8)` — the
  helper fired and committed the timed-out repair agent's work.
- **Product BOOTS:** backend `uvicorn` started, `GET /api/health` →
  `{"status":"ok"}`. The landed `partial` product is viable, not just a
  verdict label.
- Direct A/B vs the prior `Verdict: merge_blocked` + discarded work run.

### Phase 1.2-A in-progress validation (resume) — RUNNING

Conclusive proof of the dev-velocity multiplier: with root=partial
persisted in task_graph (from above), `otto v5 run` on the same project
should now resume (was rejected pre-1.2-A). Launched `bxxiarqol` —
expected: emits `v5_resume_from_checkpoint` event, SKIPS compile +
foundation + feature builds, re-enters integration only (~10-20min vs
~44min from-scratch). Outcome pending; will be appended on exit.

**RESUME RESULT (2026-05-19, bg=bxxiarqol):** Phase 1.2-A PROVEN.
- Wall-time **1277s (~21min) vs. 2656s from-scratch** — saved ~26min.
- Rebuild lines in run.out: **0** (no compile/child-build) — entire
  pre-integration phase skipped exactly as 1.2-A claimed.
- Cost $3.34. merge_blocked/cascade count: 0.
- Root verdict: `partial` (Part A chokepoint held at post-agent terminal).
- Product still BOOTS: GET /api/health → {"status":"ok"}.
- **Real product progress committed:** git log shows the resume's repair
  agent committed `6dce32e feat: implement tags CRUD and fix register
  flow for UI journey` (Auth-tab default fix, new backend tags router,
  full TagsPage) and `d98afdd feat(bookmarks): merge tag management into
  BookmarksPage for journey` (cross-page nav fix). Three waves of repair
  preserved cumulatively: 73fec6b (Part B from-scratch preserve) →
  6dce32e → d98afdd.

This validates the entire keystone + A + B + 1.2-A stack end-to-end on
a real product with real bugs. Pending = Phase 1.2-B (cut-mid-repair
landing_pending → resume CONTINUES the in-flight repair turn). Different
from 1.2-A (which re-enters integration from the phase boundary).

### Full-stack validation e2e (bpfm196fo, 2026-05-19) — NO REGRESSION

Project fastrepro-linkboard-validate-155831, full 7-commit stack
(keystone + Part A + Part B + 1.2-A + Polish + Phase 5 + Phase 4). Exit
0, cost $4.0993, duration 3632.8s (~60.5min).
- Verdict: **partial** (NEVER merge_blocked — keystone+Part A end-to-end).
- task_graph: foundation=pass, features=partial+landed_annot=True both.
  merge_blocked_origin=None everywhere.
- merge_blocked / cascade / architect_retry count in run.out: **0**.
- Phase 5 (architect_contract_landed_partial) + Phase 4
  (foundation_clean_boot_degraded_to_scaffold) event count: **0** —
  expected, since this run's architect produced a buildable structure
  and foundation passed clean-boot. The safety nets stayed silent;
  unit tests already lock in their behavior when triggered.
- Product boots: backend uvicorn started, GET /api/health returned
  `{"status":"ok","ts":"2026-05-19T23:59:45..."}`.
- Substantial product engineering captured across commits:
  - c7e902a fix: resolve Tags locator conflict and stale-auth race in
    journey executor (the EXACT bug live-predicted mid-run).
  - 92f06f2 feat: always-visible add-tag and add-bookmark forms.
  - ddda70c fix: use bcrypt directly (passlib 4.x incompatibility).
  - 4c63f7f feat: implement auth, bookmarks, tags API routers.
  - 4e78f82, 76b376c auth-tab/route fixes.
  - c724e38 dec-002 decisions log entry.

Comparison vs prior bp237rgkr: same structural shape (root=partial,
features partial+landed, 0 refusal/cascade); more product surface chased
(2 journey bugs vs 1); longer wall (3632s vs 2656s) because more real
repair work was done. **No regression** — every keystone/A/B/1.2-A
invariant from bp237rgkr held; Phase 5/4 paths didn't trigger but unit
tests cover their behavior. Campaign substantively complete (7/8 layers
shipped + validated; Phase 1.2-B deferred to focused follow-up).
