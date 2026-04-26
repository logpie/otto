# Mission Control Audit — Cherry-Pick Guide

**Branch:** `worktree-i2p` → `origin/i2p`
**Range:** `6b275a72b..c69ddc683` (38 commits, ~26 hours autonomous work)
**Final state:** 1189 default + 198 browser tests passing; 29/29 CRITICAL closed; ~113/132 IMPORTANT closed.

For each commit below, `category` indicates whether it's **doc** (no code changes), **infra** (test/build harness), **fix-server** (Python), **fix-client** (TypeScript/React/CSS), **fix-mixed** (both), or **test-only** (live findings, no production fix).

---

## Quick navigation by priority

### 🔴 MUST cherry-pick (safety / correctness fixes — every CRITICAL closure)

| Commit | What it fixes | Files |
|---|---|---|
| `c69ddc683` | Missing `useCrossTabChannel.ts` source (companion to 4b5f35e41) | `otto/web/client/src/hooks/useCrossTabChannel.ts` |
| `4b5f35e41` | W7-W10 CRITICAL: keyboard cancel hits wrong button, polling burst on visibility-restore, cross-tab state propagation broken both directions | `App.tsx`, `app.py`, `styles.css`, 3 browser tests |
| `94bbffaae` | **W5-CRITICAL: merge preflight ignored untracked files in project root** (silent dirty-tree merge — real safety bug); + 5 IMPORTANT provenance/evidence (proof drawer cache invalidation, certification round history, binary artifact MIME detection, ArtifactRef hashing, visual evidence manifest) | `service.py`, `serializers.py`, `app.py`, `setup_gitignore.py`, `App.tsx`, `types.ts`, `certifier/__init__.py`, 5 test files |
| `49add4102` | W3-CRITICAL: improve forks from main not prior run (real merge collision); W3-CRITICAL: app-ready marker for external probes; W3-IMPORTANT: WARN renders as PASS in improvement-report (false PASS) | `pipeline.py`, `cli_improve.py`, `App.tsx`, `mission_control/service.py`, `queue/{schema,enqueue,runner}.py`, `worktree.py`, 4 test files |
| `86f005b88` | W13-CRITICAL: inspector overlay buried shortcut buttons (Playwright clicks landed under modal) | `App.tsx`, `styles.css`, `tests/browser/test_outage_recovery_clickable.py` |
| `41fa5ba7c` | W2-CRITICAL: cancelled queue tasks vanished from /api/state | `otto/queue/runner.py`, `tests/test_queue_cancel_history.py` |
| `f6aded58a` | W1-CRITICAL: inspector tab buttons z-index hardening; W11-CRITICAL: dirty-target loop after Otto auto-dirties; W11-CRITICAL: modal backdrop click-dismiss; cluster G accessibility (modal isolation via `inert`, tablist arrow-nav, skip link, aria-live, focus contrast); cluster H test scaffolding | `App.tsx`, `styles.css`, `app.py`, `serializers.py`, 5 test files |

### 🟠 STRONGLY RECOMMENDED (high-leverage IMPORTANT clusters)

| Commit | What it fixes | Files |
|---|---|---|
| `c7264c41c` | Cluster E (CRITICAL diff freshness contract: SHA-stamped diff + merge SHA validation) + cluster F (CRITICAL boot-loading gate, pre-submit advanced summary, +12 first-run clarity items) | `service.py`, `App.tsx`, `api.ts`, `types.ts`, 3 test files |
| `dca7cc11d` | Cluster C (CRITICAL bundle integrity: source-hash freshness check + cache headers + recursive package_data) + cluster D (CRITICAL history pagination: URL-persisted page+size) | `app.py`, `bundle.py` (new), `App.tsx`, `model.py`, `serializers.py`, `pyproject.toml`, `package.json`, `vite.config.ts`, 3 test files |
| `8c1ce66e9` | Cluster B (CRITICAL bounded log buffer: 10MB log no longer locks browser) + Live vs Final indicator + polling backoff | `App.tsx`, `service.py`, `types.ts`, `tests/browser/test_log_buffering.py` |
| `6bd46c34c` | Cluster A (CRITICAL async-action discipline: useInFlight hook + Spinner + :active state, kills duplicate-POST class of bug across all action sites) | `App.tsx`, `styles.css`, new `hooks/useInFlight.ts`, `hooks/useDebouncedValue.ts`, `components/Spinner.tsx`, `tests/browser/test_async_actions.py` |

### 🟡 NICE-TO-HAVE (UX polish + product gaps closed)

| Commit | What it fixes | Files |
|---|---|---|
| `7abaa1b95` | iPhone submit reachable, Cmd+Enter shortcut, live/history dedupe, managed-root explanation, lost-connection banner | `App.tsx`, `styles.css`, 5 test files |
| `11db3b2b8` | Drawer chevron animation, sidebar first-run collapse, diagnostics user-facing labels, recent-activity copy, responsive layout breaks, typed task-card chips | `App.tsx`, `styles.css`, 6 test files |
| `c414a949a` | W3-IMPORTANT batch (improve loop hardening: task slug uses focus, single-round writes build-journal, splits PASS/WARN/FAIL count, improve runs in improve/ not build/, Start watcher tooltip fix, internal mode flag no longer in event log) | `enqueue.py`, `cli_improve.py`, `pipeline.py`, `runner.py`, `adapters/queue.py`, `App.tsx`, 6 test files |
| `771090ee1` | Visual coherence: design tokens, status badge tones with icons (color-blind safety), typography scale, 8px grid, hover/disabled consistency | `styles.css` (major), `App.tsx`, `tests/browser/test_visual_coherence_tokens.py` |
| `3073dd94d` | Heavy-user power-tools: history table column sort, log search, background-tab notification, Cmd-K command palette, filters in URL | `App.tsx`, `styles.css`, 5 test files |
| `0165986a7` | Long-string overflow: toast wrap, banner clamp, dialog status, confirm dialog, review packet, file lists, diff toolbar | `styles.css`, `App.tsx`, `tests/browser/test_long_string_overflow.py` |
| `1dbf1fdda` | W2-IMPORTANT-3 watcher cancel race + Mission Focus active headline + status badge tones + filter-blind empty + queued-card clickable | `App.tsx`, `styles.css`, `queue/runner.py`, 5 test files |
| `4d3a41c5e` | W1/W11 IMPORTANT batch: JobDialog validation hint, sidebar TASKS counter, atomic→build naming, Start watcher feedback, event log lifecycle | `App.tsx`, `service.py`, 5 test files |
| `22069c694` | session-dir resolution for queue/merge runs + queue-compat URL routing + orphan watchers SIGTERM cleanup | `paths.py`, `service.py`, `app.py`, `App.tsx`, `api.ts`, `tests/browser/_helpers/server.py`, 3 test files |
| `a821737a5` | Cluster H finishing — graduates 5 xfails to green (cleanup-queued copy, cancel SIGTERM copy, action error keeps dialog open, JobDialog 3s grace window) | `App.tsx`, `styles.css`, `tests/browser/test_destructive_actions.py` |

### 🔵 INFRASTRUCTURE (foundation; required if adopting any test commits)

| Commit | What it adds | Files |
|---|---|---|
| `e388d242e` | Migrates W-scenario harness to durable `[data-mc-shell=ready]` probe (replaces 13 scenario-specific waits with one helper) | `scripts/web_as_user.py`, `docs/mc-audit/live-findings.md` |
| `0cd3c207a` | `scripts/web_as_user.py` Phase 5 live harness scaffold + `scripts/web_record_fixture.py` Phase 3.5 capture orchestrator | `scripts/web_as_user.py` (new), `scripts/web_record_fixture.py` (new), `tests/test_web_as_user_scaffolding.py`, `pyproject.toml` |
| `f6373ad78` | Phase 3 Playwright + pytest infrastructure: `tests/browser/conftest.py` with 10+ fixtures, build-bundle, atomic-port server launcher, monkeypatched watcher subprocess, frozen clock, viewport profiles, console/network assertion helpers | `tests/browser/` (new dir), `pyproject.toml`, `tests/browser/test_smoke.py` |

### ⚪ DOC ONLY / TEST DISCOVERY (no production code; safe but no functional impact)

| Commit | What it documents | Files |
|---|---|---|
| `c8b1630d6` | Phase 0 (coverage migration) + Phase 1 (4 discovery docs: server endpoints, ~120 client product states, 40 user flows, test-coverage matrix) | `docs/mc-audit/{coverage-migration,server-endpoints,client-views,user-flows,test-coverage-matrix}.md` |
| `42093a121` | Phase 2 adversarial UX hunt — 228 findings from 13 parallel hunters, severity-gated triage | `docs/mc-audit/{findings,deferred}.md` + 13 per-hunter files under `_hunter-findings/` |
| `2aa772116` | W7+W8+W9+W10 live findings (raw bug discovery; the actual fixes are in 4b5f35e41) | `docs/mc-audit/live-findings.md` |
| `4a554fa43` | W3 live findings (improve loop discovery) | `docs/mc-audit/live-findings.md` |
| `27fcb60cf` | W2/W12a/W12b/W13 live findings + W2-W13 harness implementation | `scripts/web_as_user.py`, `docs/mc-audit/live-findings.md` |
| `490920291` | W1+W11 live findings + initial W1/W11 harness implementation | `scripts/web_as_user.py`, `docs/mc-audit/live-findings.md` |
| `9a24d8825` | W4+W5 live findings (turned out to be harness regressions; later fix in `e388d242e`) | `docs/mc-audit/live-findings.md` |

---

## Recommended cherry-pick orders

### Minimum viable: just the CRITICALs (12 commits)

If you only want to ship safety + correctness fixes:

```bash
# Foundation
git cherry-pick f6373ad78  # Playwright infra (required for tests)
git cherry-pick 0cd3c207a  # web-as-user harness scaffolding (optional but recommended)

# CRITICAL fixes (in dependency order)
git cherry-pick 6bd46c34c  # cluster A — async-action discipline (blocks 4 CRITICAL)
git cherry-pick 8c1ce66e9  # cluster B — log buffer (1 CRITICAL)
git cherry-pick dca7cc11d  # clusters C+D — bundle integrity + history pagination (2 CRITICAL)
git cherry-pick c7264c41c  # clusters E+F — diff freshness + boot-loading gate (3 CRITICAL)
git cherry-pick f6aded58a  # cluster G — accessibility blockers + W1/W11 CRITICAL
git cherry-pick a821737a5  # cluster H finishing
git cherry-pick 41fa5ba7c  # W2-CRITICAL queue cancel
git cherry-pick 86f005b88  # W13-CRITICAL inspector overlay
git cherry-pick 49add4102  # W3-CRITICAL improve from prior run + app-ready
git cherry-pick 94bbffaae  # W5-CRITICAL merge preflight + provenance
git cherry-pick 4b5f35e41  # W7-W10 CRITICAL (keyboard/visibility/cross-tab)
git cherry-pick c69ddc683  # missing useCrossTabChannel hook source
```

Result: all 29 CRITICAL findings closed.

### Add high-leverage IMPORTANT clusters (+11 commits)

After the above, add to also close ~80 IMPORTANT items:

```bash
git cherry-pick e388d242e  # harness probe migration (needed if you want live W-scenarios)
git cherry-pick 22069c694  # session-dir + queue-compat URL + orphan watchers
git cherry-pick 4d3a41c5e  # W1/W11 IMPORTANT batch
git cherry-pick c414a949a  # W3-IMPORTANT batch (improve loop hardening)
git cherry-pick 1dbf1fdda  # queue race + density
git cherry-pick 0165986a7  # long-string overflow
git cherry-pick 3073dd94d  # heavy-user power-tools
git cherry-pick 771090ee1  # visual coherence design tokens
git cherry-pick 11db3b2b8  # microinteractions polish
git cherry-pick 7abaa1b95  # final IMPORTANT batch
```

### Full cherry-pick (everything) — 38 commits

```bash
git log --reverse --pretty=format:'git cherry-pick %h  # %s' 6b275a72b..c69ddc683
```

---

## Per-commit detail (chronological)

| # | Commit | Category | Bugs closed | Test delta |
|---:|---|---|---|---|
| 1 | `6b275a72b` | doc | — | — |
| 2 | `c8b1630d6` | doc | — | — |
| 3 | `42093a121` | doc | — | — |
| 4 | `f6373ad78` | infra | — | +1 smoke (Playwright infra) |
| 5 | `0cd3c207a` | infra | — | +8 (harness scaffolding) |
| 6 | `6bd46c34c` | fix-client | 4 CRITICAL + 4 IMPORTANT | +7 browser |
| 7 | `8c1ce66e9` | fix-mixed | 1 CRITICAL + 2 IMPORTANT | +7 browser |
| 8 | `dca7cc11d` | fix-mixed | 2 CRITICAL + 6 IMPORTANT | +15 server + 10 browser |
| 9 | `c7264c41c` | fix-mixed | 3 CRITICAL + 13 IMPORTANT | +6 server + 18 browser |
| 10 | `490920291` | test-only (live W1+W11) | — | — (15 bugs found) |
| 11 | `f6aded58a` | fix-mixed | 3 live CRITICAL + cluster G a11y | +5 test files |
| 12 | `a821737a5` | fix-client | 4 IMPORTANT (cluster H) | 5 xfails → green |
| 13 | `27fcb60cf` | test-only (live W2-W13) | — | — (14 bugs found) |
| 14 | `41fa5ba7c` | fix-server | W2-CRITICAL | +5 server |
| 15 | `86f005b88` | fix-client | W13-CRITICAL | +4 browser |
| 16 | `22069c694` | fix-mixed | 3 IMPORTANT (session-dir, URL, orphans) | +12 server + 3 browser |
| 17 | `4d3a41c5e` | fix-mixed | 5 IMPORTANT | +5 test files |
| 18 | `1dbf1fdda` | fix-mixed | 5 IMPORTANT | +14 browser + 3 server |
| 19 | `4a554fa43` | test-only (live W3) | — | — (11 bugs found) |
| 20 | `49add4102` | fix-mixed | 2 W3-CRITICAL + W3-IMPORTANT-3 + 4 microinteractions | +15 test files |
| 21 | `c414a949a` | fix-mixed | 6 W3-IMPORTANT (improve loop hardening) | +14 server + 2 browser |
| 22 | `771090ee1` | fix-client | 7 IMPORTANT (visual coherence) | +6 browser |
| 23 | `9a24d8825` | doc (W4+W5 found harness regression) | — | — |
| 24 | `e388d242e` | infra (harness migration) | — | (W4+W5 re-runs) |
| 25 | `94bbffaae` | fix-mixed | W5-CRITICAL + 5 IMPORTANT (provenance) | +9 server + 6 browser |
| 26 | `2aa772116` | test-only (live W7-W10) | — | — (18 bugs found) |
| 27 | `11db3b2b8` | fix-client | 6 IMPORTANT (microinteractions polish) | +12 browser |
| 28 | `4b5f35e41` | fix-mixed | 4 W7-W10 CRITICAL | +12 browser |
| 29 | `7abaa1b95` | fix-client | 5 IMPORTANT (iPhone, Cmd+Enter, dedupe, etc.) | +14 browser |
| 30 | `c69ddc683` | fix-client | (companion to 4b5f35e41) | — |

---

## Notes for the Codex session

- **All commits land on `worktree-i2p`** in the i2p worktree at `/Users/yuxuan/work/cc-autonomous/.claude/worktrees/i2p` (not in the main worktree). The `origin/i2p` ref tracks them.
- **Bundle assets in `otto/web/static/assets/`** are committed binary; cherry-picks that touch React source must be followed by `npm run web:build` to keep the bundle in sync (or accept that the next CI build will rebuild them anyway). The cluster C bundle freshness check will refuse to start `otto web` if source ≠ bundle.
- **Pyproject changes** in `dca7cc11d` add `pytest-playwright`, `playwright`, `freezegun` to dev deps. If cherry-picking only a subset, you may need to manually add these.
- **`tests/browser/`** package requires `pytest-playwright` plugin. Add `[tool.pytest.ini_options] addopts = ["-m", "not browser", "-p", "no:playwright"]` to keep default suite green; run browser tests via `pytest -m browser -p playwright`.
- **`scripts/web_as_user.py` + `scripts/web_record_fixture.py`** require `OTTO_ALLOW_REAL_COST=1` env to run (they can spend real LLM budget).
- **`docs/mc-audit/`** docs are durable reference material — not strictly needed if cherry-picking only fixes, but useful for future audits.
- **Worktree pollution**: a separate process during this session left untracked files at the repo root (`architecture.md`, `tasks.json`, `plan-*.md`, `weather2/`, etc.). I never staged those — they're not in any commit.
