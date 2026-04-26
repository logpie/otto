# Mission Control Audit — Merge Guide

**Source branch:** `worktree-i2p` → `origin/i2p`
**Target:** `main` (or wherever you're integrating)
**Range vs main:** 33 commits, 145 files, ~46,387 insertions, ~892 deletions
**Final state:** 1189 default + 198 browser tests passing; 29/29 CRITICAL closed; ~113/132 IMPORTANT closed.

This is the merge plan for a Codex session integrating the audit work into `main`. The branch is shippable as a single squash-merge OR can be merged commit-by-commit with the priority groups below.

---

## TL;DR — recommended merge

```bash
# From main:
git fetch origin
git merge --no-ff origin/i2p
# Or, if you want to flatten into one big commit:
git merge --squash origin/i2p
git commit -m "Mission Control audit: 29 CRITICAL + 113 IMPORTANT closures"
```

Resolve conflicts (none expected vs the i2p divergence point — see below). Run:

```bash
npm install   # in case dev deps shifted
uv sync       # picks up new pytest-playwright/playwright/freezegun
npm run web:typecheck && npm run web:build
uv run pytest -q                                   # 1189 expected
uv run pytest -q -m browser -p playwright          # 198 expected
```

That's it. Branch is in releasable state at every commit.

---

## What changed (high level)

### Server (Python) — `otto/`
- **`mission_control/service.py`** — diff freshness contract (target/branch/merge-base SHAs), proof provenance metadata, certification round history, binary-artifact MIME detection, watcher orphan cleanup helper, run lifecycle event emission, merge-preflight untracked-file check
- **`mission_control/serializers.py`** — ArtifactRef gains size_bytes/mtime/sha256; `_project_is_user_dirty` filters Otto-owned untracked
- **`mission_control/adapters/queue.py`** + **`adapters/atomic.py`** — testid wiring for `advanced-action-*` keyboard targets; display_name no longer leaks "legacy queue mode" internal flag
- **`queue/runner.py`** — `_finalize_unstarted_queue_task` writes terminal history snapshot for cancelled-before-spawn tasks; cancel-vs-dispatch race fixed via `late_drain` callback
- **`queue/schema.py`** + **`queue/enqueue.py`** — `QueueTask.base_ref` field for improve-from-prior-run wiring; improve task slug uses focus not project name
- **`worktree.py`** — `add_worktree(base_ref=...)` so improve worktrees branch from the prior run's branch
- **`pipeline.py`** — improve runs now scaffold under `improve/` (was `build/`); `_stories_to_journeys` carries `verdict` for PASS/WARN/FAIL distinction
- **`cli_improve.py`** — improvement-report.md splits "2 PASS / 3 WARN / 0 FAIL" instead of misleading 5/5; WARN renders with `!` glyph not `✓` (was false-PASS)
- **`web/app.py`** — Cache-Control headers on `/api/*`, `_CacheHeaderStaticFiles` for asset caching, lifespan-managed watcher cleanup, raw-artifact endpoint, history_page_size threading
- **`web/bundle.py`** (NEW) — bundle freshness check on startup
- **`paths.py`** — `session_dir_for_record(record)` helper for queue/merge-domain runs
- **`setup_gitignore.py`** — `OTTO_PATTERNS` shared constant; `is_otto_owned_path` helper
- **`certifier/__init__.py`** — visual evidence manifest written per artifact
- **`config.py`** — `repo_preflight_issues` adds `untracked` category for merge gate

### Client (TypeScript/React) — `otto/web/client/`
- **`App.tsx`** (3198 → ~4500+ lines) — major surface:
  - Async-action discipline (useInFlight, latch on every POST)
  - Boot-loading tri-state gate + `data-mc-shell="ready"` marker + `window.__OTTO_MC_READY`
  - Diff pane freshness header + Refresh button + truncation banner with byte counts
  - History pagination + sortable column headers + URL-persisted filters
  - Log search box + log buffer bounded to 1MB + Live/Final indicator
  - Cmd-K command palette + per-row Cancel button (keyboard-safe)
  - JobDialog 3s grace window + Cmd+Enter shortcut + prior-run dropdown for improve
  - ConfirmDialog backdrop-dismiss + danger-tone cancel emphasis
  - ProofProvenance + CertificationRoundTabs + binary artifact preview routing
  - Optimistic cancel UI + dedupe live[]/history[] + lost-connection banner
  - Sidebar first-run collapse + typed task chips + diagnostics user-facing labels
  - Recovery action bar surfaces Retry/Resume/Cleanup as primary contextual buttons
  - Modal isolation via `inert` (cluster G a11y) + skip-link + aria-live region
  - useDocumentTitle hook + responsive media queries
- **`hooks/useInFlight.ts`** (NEW) — synchronous lock + reactive pending flag
- **`hooks/useDebouncedValue.ts`** (NEW) — 200ms debounce for search
- **`hooks/useCrossTabChannel.ts`** (NEW) — BroadcastChannel + storage-event fallback
- **`components/Spinner.tsx`** (NEW) — pure-CSS spinner
- **`api.ts`** — `runDetailUrl` (whitelisted params, no /api/state filter leak), `friendlyApiMessage` HTTP-code mapper
- **`types.ts`** — many new fields: ProofReportInfo, CertificationRound, ArtifactRef extended, etc.
- **`styles.css`** — design tokens (color/space/radius/text scales), :active state, responsive breakpoints, modal-backdrop-with-pointer-events

### Tests — `tests/`
- **38 new browser tests** under `tests/browser/` covering every closed bug
- **~25 new server tests** for queue race, session-dir resolution, dirty-target preflight, merge preflight, proof provenance, improve report verdict, event log lifecycle, etc.
- **`tests/browser/conftest.py`** — full Playwright fixture catalog (mc_backend, monkeypatch_watcher_subprocess, frozen_clock, viewport profiles, pages_two, console/network assertion helpers)

### Scripts — `scripts/`
- **`scripts/web_as_user.py`** (NEW, ~3000 lines) — Phase 5 live web-as-user harness with 14 W-scenarios
- **`scripts/web_record_fixture.py`** (NEW) — Phase 3.5 capture orchestrator (R1-R14 registry)

### Build / config
- **`pyproject.toml`** — `pytest-playwright`, `playwright`, `freezegun` dev deps; `[tool.pytest]` adds `browser` marker excluded by default; `[tool.pyright]` includes `tests/` and `scripts/`
- **`package.json`** — `web:typecheck`, `web:build` scripts (root-level)
- **`vite.config.ts`** — emits `build-stamp.json` with source-hash on every build
- **`tests/browser/_helpers/{server,build_bundle,seed}.py`** — fixture support modules

### Docs — `docs/mc-audit/`
- `coverage-migration.md` — Phase 0 mapping of legacy harness coverage
- `server-endpoints.md` — Phase 1A endpoint catalog (529 lines)
- `client-views.md` — Phase 1B product-state catalog (840 lines, ~120 states)
- `user-flows.md` — Phase 1C 40 user journeys (575 lines)
- `test-coverage-matrix.md` — Phase 1D coverage gap matrix
- `findings.md` — Phase 2 audit findings, severity-gated (228 → action list)
- `deferred.md` — NOTE-tier items deferred per policy
- `_hunter-findings/` — 13 per-hunter raw outputs
- `live-findings.md` — Phase 5 live-discovery bug catalog (60 real bugs, all CRITICALs fixed)
- `cherry-pick-guide.md` + `merge-guide.md` (this file)

---

## Merge logistics

### Conflicts to expect

The branch was rebased onto `origin/main` partway through the session. As of the last check, `main..i2p` is **0 commits unique-to-main**, so a fast-forward merge is possible:

```bash
git fetch origin
git merge --ff-only origin/i2p   # if main hasn't moved
```

If `main` has moved since the rebase, expect potential conflicts in:
- `otto/web/client/src/App.tsx` (large file, every cluster touched it)
- `otto/web/client/src/styles.css` (visual coherence + microinteractions)
- `otto/web/static/assets/` (binary bundle, prefer i2p version + rebuild)
- `otto/mission_control/service.py` (multiple clusters)
- `otto/queue/runner.py` (queue cancel race + base_ref + finalize_unstarted)
- `otto/cli_improve.py` (improve-loop hardening)

For binary bundle conflicts, always take `i2p`'s version then `npm run web:build` to re-stamp.

### Untracked files to NOT bring in

A separate process during this session left these in the worktree root — they're NOT in any commit:

```
architecture.md
otto_plan.json
plan-agent-driven-v1.md
plan-pipeline-refactor.md
plan-run-budget.md
plan-spec-gate.md
plan-unified-certifier.md
product-spec.md
research-agent-driven-architecture.md
research-spec-gate.md
research-unified-certifier.md
scripts/bench-certifier.py
scripts/sync-ubuntu.sh
tasks.json
tasks.lock
weather2/
```

These are unrelated to the audit. The merge should not pull them.

### Post-merge verification

```bash
# 1. Dependencies
uv sync
npm install

# 2. Type-check + build
npm run web:typecheck
npm run web:build

# 3. Default suite (excludes browser by design)
OTTO_BROWSER_SKIP_BUILD=1 uv run pytest -q
# Expected: 1189 passed, 19 deselected

# 4. Browser suite (requires playwright browsers — `playwright install chromium webkit` once)
OTTO_BROWSER_SKIP_BUILD=1 uv run pytest -q -m browser -p playwright
# Expected: 198 passed

# 5. Smoke the web app
uv run otto web --port 8800
# Or open the project launcher:
uv run otto web --project-launcher
```

If any test fails post-merge, it's a merge-conflict residue — diff the conflict resolution against the canonical i2p HEAD.

---

## What can be dropped (if Codex wants a smaller integration)

If the full 145-file merge feels too big, here's what's safe to exclude:

### Drop these and keep the rest functional
- **`docs/mc-audit/`** — pure documentation. Drop entirely if you don't want the audit trail in main. The `findings.md` + `live-findings.md` are valuable institutional memory but not load-bearing.
- **`scripts/web_record_fixture.py`** — Phase 3.5 capture script that's never been actually run (recordings still scaffolded only). Drop if you don't plan to capture R1-R14.
- **`scripts/web_as_user.py`** — only useful if you want to run live web-as-user dogfooding. Drop if you have a different harness.

### Don't drop these
- **All `otto/` Python changes** — these are the actual fixes
- **All `otto/web/client/` TypeScript/CSS** — these are the SPA fixes
- **`otto/web/static/`** — must match the source; if you exclude, also exclude all client changes
- **`tests/`** — every test corresponds to a closed bug; dropping any means that fix is unverified
- **`pyproject.toml`** — adds dev deps required for browser tests
- **`package.json`** — adds web:typecheck/web:build scripts

### High-risk drops (do at your peril)
- **`otto/web/bundle.py`** — bundle freshness check; without it, dev tree drifts from served bundle silently
- **`otto/setup_gitignore.py`** changes — without these, project root accumulates Otto-runtime files in `git status`
- **`tests/browser/conftest.py`** — the entire browser test infrastructure depends on it

---

## What's NOT in this merge (deferred work)

- **~19 NOTE-tier IMPORTANT items** — paper-cut polish: full dark mode, brand identity, server-side history sort, log grep server-side, export-to-CSV, compare-two-runs, intent templates, command-palette action extensions
- **76 NOTE items** — explicitly deferred per the audit's severity-gate policy from the start
- **Phase 3.5 R1-R14 actual recordings** — scaffolding committed; capture script ready; gated on real-LLM budget (~$70-140) + hours of wall time

---

## Verification of bug closure (per-commit highlights)

| Bug class | Closures | Verification |
|---|---|---|
| Async-action duplicate POSTs | 4 CRITICAL | `tests/browser/test_async_actions.py` (8 tests) |
| Long-log browser lockup | 1 CRITICAL | `tests/browser/test_log_buffering.py` (7 tests) |
| Bundle integrity / stale UI | 1 CRITICAL | `tests/test_web_bundle_freshness.py` + `test_web_cache_headers.py` |
| History pagination unrendered | 1 CRITICAL | `tests/browser/test_history_pagination.py` (10 tests) |
| Diff freshness contract | 1 CRITICAL | `tests/test_diff_freshness.py` (6) + browser (5) |
| Boot-loading gate / advanced summary | 2 CRITICAL | `tests/browser/test_first_run_clarity.py` (17) |
| Modal aria-modal isolation | 2 CRITICAL | `tests/browser/test_accessibility.py` (13) |
| Bulk-merge enumeration | 1 CRITICAL | `tests/browser/test_destructive_actions.py` (11) |
| Inspector tab buttons buried | 2 CRITICAL | `tests/browser/test_inspector_tabs.py` + `test_outage_recovery_clickable.py` |
| Modal backdrop linger | 1 CRITICAL | `tests/browser/test_modal_backdrop_cleanup.py` (5) |
| Dirty-target loop / merge preflight | 2 CRITICAL | `tests/test_dirty_target_no_otto_files.py` + `test_merge_preflight_dirty_tree.py` |
| Cancelled queue tasks vanish | 1 CRITICAL | `tests/test_queue_cancel_history.py` (5) |
| Improve from prior run | 1 CRITICAL | `tests/test_improve_branches_from_prior_run.py` (9) |
| App-ready external probe | 1 CRITICAL | `tests/browser/test_app_ready_marker.py` (4) |
| WARN-as-PASS in improve report | 1 IMPORTANT (severity-blurred CRITICAL-shaped) | `tests/test_improvement_report_warn_distinct_from_pass.py` (9) |
| Keyboard cancel hits wrong button | 1 CRITICAL | `tests/browser/test_keyboard_cancel_correct_target.py` (3) |
| Polling burst on visibility restore | 1 CRITICAL | `tests/browser/test_visibility_polling_behavior.py` (3) |
| Cross-tab state propagation | 2 CRITICAL | `tests/browser/test_two_tab_consistency.py` (6) |
| Provenance / evidence trustworthiness | 5 IMPORTANT | `tests/test_proof_provenance.py` (9) + 2 browser files |

(Full per-bug mapping in `docs/mc-audit/cherry-pick-guide.md`.)

---

## Operating notes

### Real-LLM env guard
`scripts/web_as_user.py` and `scripts/web_record_fixture.py` refuse to run without `OTTO_ALLOW_REAL_COST=1`. Don't put either in CI without that env scoped to the appropriate workflow.

### Browser-test runtime
Browser suite takes ~3-5 min on macOS. Plays well with xdist, but each test gets its own backend (no shared state).

### Pyright config
A new `[tool.pyright]` block in `pyproject.toml` includes `otto`, `tests`, `scripts` and adds `extraPaths = [".", "scripts"]` so cross-script imports (e.g. `real_cost_guard`) resolve cleanly. If your existing pyright config conflicts, merge thoughtfully.

### `data-mc-shell="ready"` marker
External automation (Playwright/MCP/smoke tests) should wait on this attribute before any interaction. Replaces ad-hoc `#root.children > 0` probes which raced the loading skeleton.
