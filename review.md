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
