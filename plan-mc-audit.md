# Plan: Mission Control — UI/UX Audit + Browser Test Automation

**Status:** Draft, pre-implementation. Round 2 (revised after Codex Plan-Gate round 1 — see Plan Review at bottom).
**Scope branch:** `worktree-i2p`
**Last updated:** 2026-04-25

## Goals

1. Comprehensively audit Otto's Mission Control web app for UX, usability, and overall design issues. Compare reality vs. expectation; produce a findings list with severity + effort + theme.
2. Build comprehensive browser-driven test automation that exercises real user behavior end-to-end against the live web app, replacing/supplementing the current agent-browser script harness.
3. Fix all CRITICAL/IMPORTANT findings + accessibility blockers regardless of severity tier; defer NOTE/MINOR with explicit acceptance.

## Why a plan first

The web app has accreted quickly — `otto/web/client/src/App.tsx` is 3198 lines, the FastAPI server exposes ~24 endpoints, and the only browser test harness today (`scripts/e2e_web_mission_control.py`) sits outside pytest, runs against a real browser via agent-browser, and tracks 11 scenarios via a `COVERAGE_MODEL`. Doing this ad-hoc would miss surface area. The plan exists to **enumerate every user-facing path** before we start implementing.

## In scope

- `otto/web/app.py` — FastAPI server (~24 routes)
- `otto/web/client/src/{App,api,types}.tsx,ts` — SPA + types
- `otto/web/client/src/styles.css` — visual design
- `otto/web/static/` — built bundle (served by FastAPI)
- `otto/mission_control/*` — server-side logic the web app consumes
- `scripts/e2e_web_mission_control.py` — existing browser harness (migrate or retire)
- The full user journey, end-to-end, from `otto web` start to a completed run

## Out of scope (this pass)

- `otto/tui/mission_control*` — terminal UI is a separate surface
- CLI surfaces (`otto build`, `otto certify`) — covered by other audits
- Server-layer business logic in `otto/mission_control/` *unless* a UX bug traces back to it
- Performance / load testing — separate concern

## Surface area inventory (preliminary — full catalog in Phase 1)

### Server endpoints (otto/web/app.py — 24 routes)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Serves SPA shell |
| GET | `/api/project` | Current project info |
| GET | `/api/projects` | Known-projects list |
| POST | `/api/projects/{create,select,clear}` | Project lifecycle |
| GET | `/api/state` | App state snapshot |
| GET | `/api/runs/{id}` | Run detail |
| GET | `/api/runs/{id}/{logs,artifacts,proof-report,diff}` | Run drilldowns |
| GET | `/api/runs/{id}/artifacts/{i}/content` | Artifact content |
| POST | `/api/runs/{id}/actions/{action}` | Run-scoped actions (cancel/resume/retry/cleanup/merge) |
| POST | `/api/actions/merge-all` | Bulk merge |
| GET | `/api/watcher` | Watcher status |
| POST | `/api/watcher/{start,stop}` | Watcher lifecycle (real subprocess for `otto queue run`) |
| GET | `/api/runtime` | Runtime metadata |
| GET | `/api/events` | **JSON polling** (NOT SSE) — tails events JSONL with `limit` query param |
| POST | `/api/queue/{command}` | Queue **job submission** (`build` / `improve` / `certify`) |

### Client surface (otto/web/client/src/App.tsx, 3198 lines)

Preliminary list of named UI regions/states (fuller catalog in Phase 1B):
- Project launcher (no-project, project-list, create, invalid-path errors)
- Mission focus header
- Task board (live + history tables)
- Task cards (More/Less, status badges, action buttons)
- Recent activity panel
- Diagnostics view (runtime, backlog rows, malformed-row counts)
- Event timeline
- Inspector with tabs: Logs, Diff, Proof, Artifacts
- Diff viewer (file selector, empty/error/truncated states)
- Proof pane (evidence drawers: Checks / Changed files / Evidence)
- Artifact pane (text vs binary viewer, large-content handling)
- Run-action menu (cancel/resume/retry/cleanup/merge — destructive-confirm flow)
- Job dialog matrix (build/improve/certify; dirty-target preflight; advanced options)
- Confirm dialogs (destructive-action safety)
- Filters / search / no-match empty state
- Watcher controls (start/stop, stale/unverified PID surfacing)
- Two top-level view modes (`tasks` / `diagnostics`) with URL persistence
- Deep-link state for `selectedRunId` + `viewMode`

### Existing test coverage

- **Server-layer pytest:** `test_web_mission_control.py`, `test_mission_control_*.py`, `test_queue_dashboard.py` (HTTP + mocked filesystem; no real browser)
- **Existing browser harness:** `scripts/e2e_web_mission_control.py` — outside pytest; agent-browser-driven; 11 scenarios via `COVERAGE_MODEL`. Tracks states + actions covered.
- **Zero pytest browser tests.**

This is the primary gap.

---

## Phase 0 — Existing harness migration inventory (NEW, doc only)

Before introducing Playwright, audit what `scripts/e2e_web_mission_control.py` already covers.

- Read the script's `COVERAGE_MODEL`, list every state/action it claims to verify, list every scenario that maps to which.
- Map each existing scenario → planned Playwright test (Phase 3C).
- Mark gaps in both directions: (a) coverage model items with no scenario today, (b) Playwright tests we propose that have no agent-browser equivalent.
- Produce `docs/mc-audit/coverage-migration.md` listing: scenario name → existing harness assertions → new Playwright test(s) → migration status (parity / superset / replaced / retired).

**Retirement policy:** the agent-browser script stays in repo until Phase 4 closeout, when each scenario has at least parity in the new pytest suite. Then mark it `[deprecated]` in the file header.

**Verify:** every entry in the script's `COVERAGE_MODEL` appears in `coverage-migration.md`.

---

## Phase 1 — Discovery & Inventory (docs only, no code)

### 1A. Server endpoint catalog → `docs/mc-audit/server-endpoints.md`

For every route in `otto/web/app.py`:
- Method + path
- Query/body params + types
- Response shape (success + error)
- Side effects (filesystem writes, queue/watcher state, subprocess spawn, etc.)
- Auth / state preconditions
- Known failure modes

**Verify:** diff the doc against `grep '@app\.' otto/web/app.py` — no route missing.

### 1B. Client product-state catalog (NOT just named components) → `docs/mc-audit/client-views.md`

Walk the `App.tsx` render tree starting from the root. For every product state and every control:
- What route URL renders it
- What data does it depend on (which API calls)
- What actions does it expose (and what side effects each fires)
- Loading / error / empty / disabled / truncated / partial-data states
- Keyboard interactions (focus order, hotkeys, escape behavior)
- Focus management on open/close/dialog dismiss
- Animation / transition behavior

This is product surface, not a component list. Components are means to an end; states are the audit unit.

**Verify:** open the running app in headed Chrome with React DevTools, walk every region of every state, confirm doc matches. Cross-check against the surface area list at the top of this plan and the existing `scripts/e2e_web_mission_control.py` `COVERAGE_MODEL`.

### 1C. User-journey catalog → `docs/mc-audit/user-flows.md`

Enumerate end-to-end user journeys + describe expected behavior in one sentence each. **Now expanded post-round-1 review:**

#### Project / launcher
1. **Cold start, no project** — empty state actionable, "create" CTA visible
2. **Launch invalid path** — server rejects with clear error, no partial mount
3. **Launch duplicate project** — handled gracefully, no double-mount
4. **Launch outside repo root / non-git** — clear error
5. **Switch project** — old state cleared, runs/events flush

#### Build/run lifecycle
6. **Submit build job** (build / improve / certify each) via JobDialog — happy path
7. **Submit on dirty target** — preflight warning, user confirms or cancels
8. **Submit with invalid input** (zero-length, very long, weird chars)
9. **Watch run live** — events stream populates inspector
10. **Resume paused run** — discoverable, action works
11. **Cancel running run** — confirm dialog, state transitions cleanly
12. **Retry failed run** — preserves intent, new run id
13. **Cleanup completed run** — destructive-confirm UX
14. **Merge run** — single, then bulk merge-all; success + failure paths

#### Read paths
15. **Browse run history** — pagination, filtering, sorting
16. **Filter / search / no-match** — empty-state copy
17. **Open run detail** — inspector tab routing (Logs / Diff / Proof / Artifacts)
18. **Diff viewer** — file selector, large-diff truncation, no-changes state
19. **Proof drawers** — Checks / Changed files / Evidence each open & close
20. **Artifact viewer** — text vs binary, large content, missing artifact

#### Diagnostics / watcher
21. **Diagnostics view** — runtime data, backlog counts, malformed-row count surfacing
22. **Watcher start** — subprocess launches; UI reflects running PID
23. **Watcher stop** — process terminates, UI reflects stopped state
24. **Stale watcher PID / unverified PID** — UI surfaces honestly, doesn't claim "running"
25. **Watcher start failure** — error surfaced

#### Resilience / state
26. **Server restart mid-session** — UI reconnects/polls and recovers (NOT SSE; check polling cadence + retry)
27. **Tab backgrounded → return** — no stale data, polling continues
28. **Two tabs open** — mutation in tab A reflects in tab B within poll interval
29. **Long-running run + slow network** — graceful degraded UX
30. **Action error 4xx/5xx** — surfaced inline at the right element

#### Navigation / URL
31. **Tasks ↔ Diagnostics + URL push/replace** — back/forward works
32. **Deep link** — `?run=X&view=tasks` lands on right state
33. **Invalid deep link** (missing run, deleted run, malformed id) — graceful fallback
34. **Deep link to selected run that gets deleted mid-session** — state recovers

#### Keyboard / accessibility
35. **Tab through whole UI** — no traps, correct focus order
36. **Operate critical flows keyboard-only** — submit job, open run, cancel, merge — all without mouse
37. **Screen-reader landmarks** — main / nav / status regions present
38. **Reduced motion** — animations honored

#### Visual / layout
39. **Resize between mini / MBA / iPhone** — no layout breaks
40. **Long strings** — intent / error / URL — no overflow/truncation surprises

**Verify:** for every flow, written expected outcome before Phase 2.

### 1D. Coverage matrix → `docs/mc-audit/test-coverage-matrix.md`

For each user flow above × each test layer (server unit / server integration / agent-browser script / Playwright). Today's matrix should show big gaps — that's the implementation backlog.

---

## Phase 2 — Reality vs Expectation Audit (parallel adversarial)

### 2A. UI/UX adversarial review (parallel hunters)

Run the live app, capture state of each region, dispatch parallel reviewers (Claude + Codex):

- **First-time user (Codex):** "Land here cold. What is this? What do I do? What's confusing in 60 seconds?"
- **Heavy user (Claude):** Daily-driver paper cuts, missing power features
- **Accessibility (Claude):** keyboard order, focus traps, ARIA landmarks, contrast, screen-reader labels, motion sensitivity
- **Information density (Codex):** signal/noise per panel
- **Error & empty states (Codex):** every loading/error/empty path; actionable copy?
- **Visual coherence (Claude):** typography hierarchy, spacing rhythm, color semantics, dark/light parity
- **Long-string / overflow (Codex):** intents, URLs, error messages, large run history
- **State management (Codex):** two-tab consistency, deep-link reliability, history nav, polling resilience
- **Microinteractions (Claude):** hover/focus/click affordances, disabled-state clarity, loading spinners
- **Destructive-action safety (NEW — Codex):** cancel / cleanup / stop / merge — undo paths, confirm copy, accidental-trigger guard
- **Evidence trustworthiness (NEW — Codex):** proof / diff / log panels — provenance, freshness, tampering surface
- **Packaging / static-bundle integrity (NEW — Codex):** stale bundle? hash-asset rotation? dev vs prod parity?
- **Large-data ergonomics (NEW — Codex):** 1000-row history, 10MB log, 100-file diff — does it stay responsive
- **Keyboard-only operation (NEW — Claude):** can a power user run a full session without touching the mouse?

Output: `docs/mc-audit/findings.md` — each finding tagged severity (CRITICAL / IMPORTANT / NOTE) + effort (S / M / L) + theme; file:line where applicable; concrete fix.

### 2B. Functional gap audit

For each user flow from 1C, run it manually + try break-paths. Append findings to `findings.md`.

### 2C. Severity-and-effort gate (revised)

The "fix CRITICAL+IMPORTANT, defer NOTE" rule from `/code-health` is bug-shaped and doesn't transfer 1:1 to UX. Replace with:

| Tier | Action |
|---|---|
| CRITICAL | Always fix |
| IMPORTANT | Always fix |
| Accessibility blocker (any severity) | Always fix |
| NOTE/MINOR with effort=S, same theme as a fix being made | Bundle-fix opportunistically |
| NOTE/MINOR isolated | Defer to `deferred.md` only with explicit acceptance criteria (why not now) |

Rationale: many low-severity UX paper cuts compound. Cheap copy/layout tweaks bundled with a related fix are net wins; deferring them has near-zero benefit.

---

## Phase 3 — Browser Test Plan

### 3A. Tooling: Playwright

- **Library:** `pytest-playwright` + `playwright` (Python)
- **Browsers:** chromium (default), webkit (for iPhone-Safari emulation)
- **Dependency:** add to `pyproject.toml` `[project.optional-dependencies] dev` section: `pytest-playwright`, `playwright`. Pin versions.
- **Browser binaries:** `playwright install chromium webkit` — document in `CONTRIBUTING.md` and the test docstring; cache in CI via path key `~/.cache/ms-playwright`
- **CI:** GitHub Actions matrix job that runs the browser suite separately from unit tests so unit failures aren't masked by browser flakes

### 3B. Test fixture architecture (revised for parallel safety)

- **One backend per test.** No shared FastAPI app. Each test spins up `otto.web.app.create_app(project_dir=tmp_path)` on a free port. Shared backend only used in the explicit two-tab consistency test.
- **Atomic free-port:** bind socket, then hand fd to uvicorn; avoid TOCTOU under xdist.
- **Seed fixtures:** `tests/browser/_helpers/seed.py` — pre-creates project state on disk: a project, N past runs (success/fail/paused), an in-flight run, queue tasks, merge-state, optional events JSONL. Each scenario calls a dedicated builder (no test-cross-pollination).
- **Watcher isolation:** watcher tests **must** monkeypatch `subprocess.Popen` or use a fake `otto queue run` script that exits immediately. Real subprocess spawning is forbidden in browser tests. Teardown asserts no orphan watcher process survived.
- **Frozen time/locale:** `freezegun` for deterministic timestamps; `TZ=UTC` env; system locale fixed; `prefers-reduced-motion: reduce` set per page; animations disabled via injected CSS.
- **Network discipline:** Playwright's `page.route()` available for stubbing outbound calls (e.g., LLM provider). Internal `/api/*` not mocked — those are the unit under test.
- **Static bundle build:** every browser-test run must `npm run web:typecheck && npm run web:build` first (or use a "browser-build" pytest marker that depends on a build-bundle fixture). Stale bundles = false-pass tests. CI sequence: typecheck → build → assert no leftover hashed assets in `static/assets/` other than the new build → run browser tests.
- **Failure artifacts:** on test failure, capture screenshot + Playwright trace (`page.context.tracing.start/stop`) + relevant API state dump. Attach to CI artifact bundle.
- **Console + network noise:** every test fails on unexpected console errors or 4xx/5xx network responses unless explicitly allowed.

### 3C. Test inventory (revised — t-numbers reset)

#### Project / launcher
- `t01` Cold start, no project → empty state with create CTA
- `t02` Launch invalid path → error displayed, no partial mount
- `t03` Launch duplicate project → no double-mount
- `t04` Launch outside-repo / non-git → error
- `t05` Switch project → old state flushed

#### Job submission
- `t06` Build job submit (happy path)
- `t07` Improve job submit
- `t08` Certify job submit
- `t09` Job submit on dirty target → preflight warning, confirm flow
- `t10` Job submit with invalid input (empty, too-long, weird chars)

#### Run lifecycle
- `t11` Resume paused run → action works
- `t12` Cancel running run → confirm dialog → state transitions
- `t13` Retry failed run → preserves intent, fresh run id
- `t14` Cleanup completed run → destructive confirm UX
- `t15` Merge single run → success + failure paths
- `t16` Bulk merge-all → confirm + per-row outcome
- `t17` Action API returns 4xx/5xx → error surfaced inline

#### Read paths
- `t18` Browse history (pagination, sort)
- `t19` Filter / search / no-match empty state
- `t20` Inspector tab routing (Logs / Diff / Proof / Artifacts)
- `t21` Diff viewer — file selector + truncation + no-changes
- `t22` Proof drawers expand/collapse (Checks / Files / Evidence)
- `t23` Artifact viewer — text + binary + large content + missing
- `t24` Run detail with no proof report — graceful

#### Diagnostics / watcher
- `t25` Diagnostics shows runtime + backlog + malformed counts
- `t26` Watcher start (with monkeypatched subprocess) → UI reflects PID
- `t27` Watcher stop → UI reflects stopped
- `t28` Stale / unverified watcher PID → UI surfaces honestly
- `t29` Watcher start failure → error surfaced

#### Resilience / state
- `t30` Server outage mid-session → UI polling retries, reconnects
- `t31` Tab backgrounded → return → polling resumes, no stale data
- `t32` Two tabs same project → mutation in A visible in B within poll interval
- `t33` Long-running run + slow network — graceful degraded UX

#### Navigation / URL
- `t34` Tasks ↔ Diagnostics push/replace; back-forward works
- `t35` Deep link `?run=X&view=tasks` → lands on right state
- `t36` Invalid deep link (missing/deleted run) → graceful fallback
- `t37` Selected run deleted mid-session → state recovers

#### Keyboard / accessibility
- `t38` Tab order across whole UI; no traps
- `t39` Submit-job → run-detail flow keyboard-only
- `t40` Cancel-confirm flow keyboard-only

#### Visual regression (curated, fewer + masked)
- `tv01` Empty launcher — 3 viewports
- `tv02` Task board with seeded runs — 3 viewports
- `tv03` Run detail Logs tab — 3 viewports
- `tv04` Run detail Proof tab — 3 viewports
- `tv05` Job dialog open — 3 viewports
- `tv06` Confirm dialog — 1 viewport (modal layout same)
- `tv07` Diagnostics empty + populated — 1 viewport each
- `tv08` 1000-row history density — 1 viewport (desktop only)

Visual regression discipline:
- Frozen seed timestamps (every test uses fixed `2026-04-25T12:00:00Z`)
- Mask dynamic regions (durations, "now" indicators, run IDs) via Playwright `mask=[]`
- Disabled animations + transitions via injected CSS at fixture
- Fixed system fonts (bundle test fonts in `tests/browser/fonts/` and inject CSS-load)
- Baselines per browser (chromium / webkit) AND per OS (linux for CI / darwin for dev) — committed under `tests/browser/__snapshots__/<browser>-<os>/`
- Tolerance: pixel diff threshold `0.1%` per shot; mask-region budget tracked in PR review

### 3D. Test anti-patterns the suite must avoid

- No `assert x is not None` when None is the failure mode
- No `with pytest.raises(Exception):` (too broad)
- No mocks so loose the production code path never runs
- No "exists" assertions standing in for "behaves correctly"
- No bare `expect(locator).to_be_visible()` without checking the surrounding state
- Each test: setup fresh fixture → exact actions → behavioral assertions → fixture cleaned up
- Tests that depend on prior tests are forbidden

---

## Phase 3.5 — Recorded-from-reality fixtures (NEW)

**Problem:** Hand-authored seed JSON encodes my belief about what the pipeline emits. If reality drifts (added field, renamed key, encoding change), seeded tests pass while production breaks.

**Solution:** Record real otto activity once, snapshot **the full project runtime state**, replay in Playwright tests.

### 3.5A. Capture boundary (revised — generated from path helpers, not handwritten)

**Hardcoded path strings in this plan would drift from reality.** Verified against the real codebase: live runs live at `otto_logs/cross-sessions/runs/live/` (NOT `otto_logs/runs/registry/`); queue state lives at project-root `.otto-queue-state.json` + `.otto-queue-commands*.jsonl` (NOT `otto_logs/queue/`); events live at `otto_logs/mission-control/events.jsonl`; watcher state at `otto_logs/web/watcher-supervisor.json`.

To avoid drift between this plan's prose and real layout, the capture script **must enumerate paths via `otto/paths.py` helpers** at recording time:

```python
# scripts/web_record_fixture.py — pseudo
from otto import paths
manifest = {
    "logs_dir": paths.logs_dir(project),
    "session_dirs": list(paths.sessions_root(project).iterdir()),  # helper is sessions_root
    "cross_sessions": paths.cross_sessions_dir(project),
    "live_runs": paths.live_runs_dir(project),
    "merge_dir": paths.merge_dir(project),
    "queue_dir": paths.queue_dir(project),
    "queue_state_root": project / ".otto-queue-state.json",
    "queue_commands": list(project.glob(".otto-queue-commands*.jsonl*")),
    "events_jsonl": <mission-control events path helper from otto.mission_control.events>,
    "watcher_state": <web watcher supervisor path helper from otto.mission_control.supervisor>,
    "project_root_inputs": [project / "intent.md", project / "otto.yaml"],
    "git_state": "captured-as-bundle",  # see 3.5A-ter
}
```

If a helper doesn't exist for a path the recording needs, the missing helper is the bug to fix first — no string-literal capture in the plan or script.

Captures **everything** the `_service()` reads. Recording emits a `state-contract.json` mirroring what each **GET** endpoint returns at recording time: `/api/state`, `/api/project`, `/api/runs/{id}`, `/api/runs/{id}/{logs,artifacts,proof-report,diff}`, `/api/runs/{id}/artifacts/{i}/content`, `/api/watcher`, `/api/runtime`, `/api/events`. (Note: `/api/queue/{command}` is POST-only — excluded.) Manifest describes intent, provider, outcome, included artifacts, recording date, source commit, otto version.

**`/api/projects` and `projects_root` isolation:** `create_app()`'s default `projects_root` is `~/otto-projects` (`app.py:31`), which would leak the recorder's local managed projects into the contract. Recording therefore **always sets `projects_root` to a fixture-owned isolated dir** (e.g., `<recording-dir>/managed-projects/`). `/api/projects` is included in the contract **only for launcher-specific recordings** (a future R-launcher fixture if launcher tests need it); for typical recordings it is **excluded**, since launcher state is irrelevant to a single-project run.

### 3.5A-bis. Sanitization with referential-integrity validator

Volatile fields are scrubbed:
- Timestamps → frozen `2026-04-25T12:00:00Z`
- Run IDs → deterministic
- Costs → rounded
- Absolute paths → **`$PROJECT_ROOT/...` placeholder** (NOT bare repo-relative — `service.py:678` resolves stored paths and verifies they're under `project_dir`, so a relative path resolves against process cwd and breaks). Restore step **hydrates** placeholders to absolute paths under the test's tmp project dir.
- Branch names → deterministic

After scrub, an **invariant pass** runs to ensure the fixture is internally consistent:

- Every artifact path referenced from any JSON file resolves to an existing file in the snapshot (after `$PROJECT_ROOT` hydration)
- Every run_id in `cross-sessions/history.jsonl` has a matching `sessions/<id>/` dir (or is explicitly documented as deliberately missing)
- Every queue task ID matches its manifest under `queue_dir`
- Every audit branch in merge state exists in the captured git bundle
- Booting the FastAPI app against the sanitized fixture passes the **endpoint-status contract** below — not a blanket 200

#### Endpoint-status contract (per recording)

Each recording's manifest declares **expected** status + shape per endpoint, because some recordings (e.g. R8 minimal/edge) intentionally lack proof or artifacts and so should 404 there:

| Endpoint | Expected for typical R | Expected for R8 (minimal/edge) |
|---|---|---|
| `GET /api/state` | 200 | 200 |
| `GET /api/runs/{id}` | 200 | 200 |
| `GET /api/runs/{id}/logs` | 200 | 200 (may return empty entries) |
| `GET /api/runs/{id}/artifacts` | 200 with non-empty list | 200 with empty list |
| `GET /api/runs/{id}/artifacts/{i}/content` | 200 | n/a (no artifacts to drill into) |
| `GET /api/runs/{id}/proof-report` | 200 | **404** |
| `GET /api/runs/{id}/diff` | 200 | 200 (empty) or 404 — recording declares which |
| `GET /api/watcher` | 200 | 200 |
| `GET /api/runtime` | 200 | 200 |
| `GET /api/events` | 200 | 200 (empty list) |

Recording rejected on contract miss. Fixture-vs-reality mismatch is a deal-breaker.

### 3.5A-ter. Git state via bundle, not raw `.git/`

Raw `.git/` capture is unsafe: hooks, reflogs, abs paths, large objects, remote URLs. Recording uses a **minimal git bundle** + reconstruction script.

**Restore order matters.** `git clone fixture.bundle <project_dir>` fails if `<project_dir>` already exists with files in it (and our test fixture restore puts `otto_logs/` files there before git). Correct order:

```bash
# capture
git bundle create fixture.bundle --all
cat > restore-git.sh <<'EOF'
# Restore order: init empty repo first, fetch from bundle, then overlay non-git fixture files
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
git init -q
git remote add fixture "$FIXTURE_DIR/fixture.bundle"
git fetch -q fixture
git checkout -q <recorded-branch>
git remote remove fixture
EOF

# replay flow (in pytest fixture):
#   1. tmp_path mkdir empty
#   2. bash restore-git.sh (sets up .git/ + worktree)
#   3. copy/overlay sanitized otto_logs/, .otto-queue-state.json, etc.
#   4. hydrate $PROJECT_ROOT placeholders → tmp_path absolutes
```

Stored under `tests/browser/_fixtures/recorded-runs/<scenario>/git/`. Scrubbed: no hooks copied, no remotes, no reflog beyond what's needed for branches the test exercises.

### 3.5B. Scenarios to record (revised — covers queue/watcher/merge surfaces)

| ID | Scenario | Why |
|---|---|---|
| R1 | Successful kanban build | events JSONL, full proof, screenshots, recording.webm |
| R2 | Failed build (deterministic — known-bad fixture) | failure trace, retry-eligible state |
| R3 | Paused/resumable build (checkpoint mid-build) | resume action coverage |
| R4 | Improve loop with 2 cert rounds | build-journal, fix-loop history |
| R5 | Successful build merged (audit branch state) | merge happy path |
| R6 | Codex provider build | token-usage rendering vs cost |
| R7 | Large run (1000-event JSONL, 100-file diff, 10MB log) | rendering pressure |
| R8 | Minimal/edge run (no proof, no artifacts) | robustness floor |
| **R9** | **Queue with mixed rows** (queued/running/done/failed/cancelled simultaneously) | queue UI surfaces |
| **R10** | **Active watcher** (running PID, command backlog, acks captured) | watcher UI |
| **R11** | **Stale watcher** (PID file present, process dead) | watcher honesty |
| **R12** | **Blocked merge** (state shows blocking reason) | merge guardrail UI |
| **R13** | **Merge with conflicts** | merge failure surface |
| **R14** | **Large run history** (200+ runs in cross-sessions/history.jsonl) | history rendering at scale |

### 3.5B-bis. W → R fixture mapping (drift-gate target table)

Each W scenario declares which R recordings are its drift-gate target(s). Composite scenarios (W11, W12b) compare against multiple recordings since the operator-day touches several state surfaces.

| W | Target R fixture(s) | What's compared |
|---|---|---|
| W1 first-time user | R1 | `state-contract.json` of completed kanban build |
| W2 multi-job operator | R9 (mixed-row queue), R5 (merged) | queue rows + post-merge state |
| W3 improve | R4 (improve loop) | build-journal + final improvement-report |
| W4 merge happy | R5 | post-merge state-contract |
| W5 merge blocking | R12 (blocked merge) | merge state showing block reason |
| W6 deterministic failure | R2 (failed) + post-correction success contract (separate snapshot) | failed contract + corrected-success contract |
| W7 iPhone | R1 | same as W1 (mobile is layout-only diff) |
| W8 keyboard-only | R9 + R5 | same as W2 |
| W9 backgrounded tab | R1.mid | mid-build polling contract |
| W10 two-tab | R1.mid + delta | mid-build state + observable delta after mutation |
| W11 operator day | R5 + R9 + R10 (active watcher) + R14 (large history) | full operator-day contract; composite |
| W12a CLI atomic→web | R3 (paused, then cancel/retry) | atomic record contract |
| W12b CLI-queued→web | R5 + R9 | queued task → merged state |
| W13 outage recovery | R1.mid (pre-outage) + R1.post (post-recovery) | continuity contract across server restart |

**Multi-phase recordings** (R1 specifically; others as needed): a single live recording session captures three contract snapshots — `<recording>.pre.state-contract.json` (just before the LLM build kicks off), `<recording>.mid.state-contract.json` (mid-build, agent active, events flowing), `<recording>.post.state-contract.json` (terminal). The drift gate compares against the phase named in the W→R mapping. Without phases, scenarios about active runs (polling, heartbeat, recovery) would only validate against terminal state — wrong.

Composite mapping is fine; what's not fine is "no mapping at all." Implementation enforces this table at drift-gate run time.

### 3.5C. Recording rerun + freshness policy (revised — enforced, not advisory)

- **Drift gate:** every Phase 5 live run emits a sanitized `state-contract.json` per scenario. CI compares against the matching recorded fixture's contract. Mismatch → fail; manual refresh required.
- **Freshness enforcement (not warning):**
  - PR CI: blocks if PR touches `otto/web/`, `otto/mission_control/`, `otto/paths.py`, or any schema-defining file AND any consumed recording is older than 14 days OR sanitized contract differs. Override only by re-recording or a maintainer-applied `recording-waiver` label with rationale.
  - Scheduled `main` job: fails (not warns) if any recording is older than 30 days. Posts an alert/issue automatically.
  - Pre-release: blocks tagged release if any recording is older than 14 days OR any drift-gate check is red.
- **Schema-snapshot diff:** CI step that diffs the JSON-schema of recorded `state-contract.json` against the live service's current shape; mismatch fails CI.

### 3.5D. Functional Playwright tests (`t01–t40`) consume recorded fixtures

`tests/browser/_helpers/seed.py` copies a recorded scenario tree into `tmp_path`, optionally mutates targeted fields (small surgical edits documented per test), and points the FastAPI app at it. **Aggressive mutation defeats the recorded-from-reality claim** — keep mutations minimal and document each.

**Verify:**
- Every recorded scenario loads cleanly into the FastAPI app's project view (no 500s, no UI errors)
- One Playwright smoke test per recording proves the UI renders the real data
- Mutations log + reviewer approves before merging mutation helpers

---

## Phase 4 — Implementation order

1. **Phase 0 + 1 docs** (migration inventory + endpoint/view/flow/coverage catalogs)
2. **Phase 2 findings** (parallel hunters, severity+effort+theme triage, user reviews CRITICAL/IMPORTANT)
3. **Phase 3 fixture infrastructure** (Playwright deps, build-bundle fixture, seeding helpers)
4. **Phase 3.5 recordings** (capture R1–R14, run sanitization invariant pass, commit, point Phase 3 fixtures at them; closeout gate fails if any recording lacks a smoke-render Playwright test)
5. **Critical UX fixes** + paired browser tests (TDD: write failing test, fix, verify; commit per pair)
6. **Important UX fixes** + paired tests
7. **Accessibility-blocker fixes** + paired tests
8. **Remaining Playwright tests** for already-correct flows (regression coverage)
9. **Visual regression** baseline capture (last, so churn from earlier fixes is settled)
10. **Phase 5 live harness lands**: `scripts/web_as_user.py` ships, all W-scenarios green at least once
11. **Retire `scripts/e2e_web_mission_control.py`** once parity confirmed; mark `[deprecated]`

---

## Phase 5 — Live web-as-user simulation harness (NEW)

**Problem:** Even recorded fixtures can't catch live-pipeline bugs — real subprocess lifecycle, polling timing, artifact production during a real build, real merge against real audit branches. Hand-recorded fixtures freeze a moment in time; they don't exercise the moving system.

**Solution:** Live harness analogous to `scripts/otto_as_user.py` (TUI) but for the web — `scripts/web_as_user.py`. Runs **real otto builds** against throwaway projects with **real LLM providers**, driven via Playwright against the actual web UI.

### 5A. Architecture
- One throwaway git repo + `otto web` server per scenario
- Playwright drives a real browser against `http://127.0.0.1:<port>/`
- Real provider (`--provider claude` or `codex`); real subprocess; real artifacts
- Per-scenario asciinema-equivalent capture (Playwright trace + video + screenshots + network log)
- Output: `bench-results/web-as-user/<run-id>/<scenario>/` mirroring otto-as-user's layout

### 5B. Scenarios (real-user simulations) — revised

W11 is the **nightly core signal** — modeled on `otto-as-user-nightly` N9. It's not a feature check, it's a real operator day.

| ID | Scenario | Tier | Cost | Time |
|---|---|---|---|---|
| **W11** | **Operator day (mandatory nightly core)** — open `otto web`, create project, submit one build that's already running standalone via CLI (tests CLI/web interop), enqueue 2 more jobs from web, start watcher, inspect heartbeat/log mid-flight, cancel one queue task via UI, open History and inspect cancelled snapshot, merge the succeeded queue row, verify final state. Adapted from N9. | nightly core | ~$5 | 25m |
| W1 | First-time user — open `otto web`, create project, submit kanban build, walk inspector tabs after completion. **Includes product verification:** browser-load the built app, run its tests, confirm at least one acceptance criterion. | nightly | ~$1 | 8m |
| W2 | Multi-job operator — submit 3 jobs (build/improve-bugs/certify), start watcher, queue drain, cancel one mid-run, others complete, history reflects all 3. | weekly | ~$3 | 15m |
| W3 | Improve loop (via JobDialog "Improve" command, NOT a row-level action — the latter doesn't exist; tracked as P2 product gap, see findings.md) — submit Improve job referencing prior run, watch build-journal update round by round, final improvement-report.md visible, **product verification: tests pass after improvement**. | weekly | ~$2 | 12m |
| W4 | Merge happy path — successful run → Merge → confirm → **inspect actual audit branch landed in target branch + verify file diff applied**. | weekly | ~$1 | 6m |
| W5 | Merge blocking — failed/incomplete/dirty-target run → Merge blocked with clear reason. | weekly | ~$1 | 6m |
| W6 | Deterministic failure + retry — submit build that fails reliably via **non-INFRA cause** (malformed `otto.yaml` with a missing required key, pre-staged intent referencing nonexistent local file, deterministic-failing acceptance test). Avoid bad-provider-key — that's classified `INFRA` by the harness, not `FAIL`. → UI clarity → Retry **after deterministic harness-side correction** (harness fixes the yaml / removes the impossible reference / rewrites intent) → succeeds. Same-intent retry without correction would loop forever. | weekly | ~$2 | 12m |
| W7 | iPhone live — W1 flow on Playwright `devices["iPhone 14"]` + webkit. Verify touch targets, mobile layout, viewport-specific affordances. | nightly | ~$1 | 8m |
| W8 | Power-user keyboard-only — full W2 session driven entirely via keyboard. | weekly | ~$3 | 15m |
| W9 | Backgrounded tab — start long build, switch tab away ~2min, return, verify polling caught up + state coherent. | weekly | ~$2 | 10m |
| W10 | Two-tab consistency — two real browser tabs same project, mutation in tab A visible in tab B within poll window. | weekly | ~$1 | 6m |
| **W12a** | **CLI atomic run → web** — start `otto build` (atomic, CLI-originated) → open `otto web` → CLI run appears in Live → cancel/retry from UI → verify CLI-side reflection. **Atomic runs do not expose merge — only cancel/resume/retry/cleanup are exercised.** | weekly | ~$2 | 12m |
| **W12b** | **CLI-queued task → web** — `otto queue build "..." --as <task>` from terminal (verified subcommand: `otto queue build|improve|certify` per `cli_queue.py:396,432,488` — there is no `otto queue submit`) → open `otto web` → queue row appears → start watcher → run completes → **merge from UI** (queue rows support merge) → verify branch landed, history archived. | weekly | ~$2 | 12m |
| **W13** | **Outage recovery** — submit a real build, restart `otto web` server mid-run, reopen browser, verify Live/History/Artifacts all recover; subsequently re-attach to the still-running pipeline; verify no actions lost. | weekly | ~$2 | 12m |

### 5C. Cadence (revised — tiered)

The user's intent is comprehensive; cost discipline keeps it sustainable. Tiers:

| Cadence | What runs | Cost | Why |
|---|---|---|---|
| **Per PR** | Recorded-fixture Playwright (Phase 3+3.5) — t01–t40 + tv01–tv08 | $0 | fast feedback, catches UI/schema regressions |
| **Nightly** | W11 (operator day) + W1 (first-time user) + W7 (iPhone) | ~$7 | core operator signal + mobile floor every day |
| **Weekly** | All W1–W13 (W12 = W12a + W12b separately) on primary provider (Claude) | ~$28 | full coverage cadence |
| **Pre-release** | All W1–W13 × {Claude, Codex parity subset} | ~$42 | release gate |

Codex parity subset (not the full set): **W1, W2, W6, W11, W12a, W12b** — covers submit, queue, deterministic-failure, operator day, and both interop paths (atomic-cancel + queue-merge) where prior provider-specific bugs hit hardest.

### 5D. Verdict semantics + product verification

| Verdict | Definition |
|---|---|
| `PASS` | Playwright assertions green; no unexpected console errors / failed network; recording captured; product verification passes if the scenario produces real artifacts |
| `FAIL` | Assertion miss, expected outcome not reached, or product verification fails |
| `INFRA` | Provider rate-limit / auth / network; auto-retry once |

**Mission Control says succeeded ≠ comprehensive.** Every scenario that produces real artifacts must verify those artifacts. Coverage matrix:

| Scenario | Produces real artifacts | Product verification required |
|---|---|---|
| W1 first-time user | Yes (built app) | Yes — load built app + run its tests + check ≥1 acceptance criterion |
| W2 multi-job operator | Yes (3 builds) | Yes — at least the non-cancelled jobs verify their built artifacts |
| W3 improve | Yes (improved app) | Yes — built app's tests pass after improvement |
| W4 merge happy | Yes (audit branch landed) | Yes — inspect target branch HEAD + verify file diff applied |
| W5 merge blocking | No (block, no land) | No — verify the block reason, not artifacts |
| W6 failure+retry | Yes (eventual success after correction) | Yes — successful retry's artifacts |
| W7 iPhone | Yes (built app from W1's flow on mobile) | Yes — same product check as W1 |
| W8 keyboard-only | Yes (W2's builds) | Yes — same as W2 |
| W9 backgrounded tab | Yes (the long build) | Yes — its artifacts |
| W10 two-tab | Maybe (mutation may be a job submit) | Yes if a build completes |
| W11 operator day | Yes (the merged run) | **Yes (mandatory) — full check on the merged audit branch** |
| W12a CLI atomic→web | Yes (the cancelled or retried run) | Yes if retry-success |
| W12b CLI-queued→web | Yes (merged queue task) | Yes — verify branch landed + diff applied |
| W13 outage recovery | Yes (the run that survived restart) | Yes — its artifacts post-recovery |

### 5E. Real-cost guardrails (mirror `scripts/otto_as_user.py`)

- Require `OTTO_ALLOW_REAL_COST=1` env var to invoke live mode
- `--dry-run` flag (no LLM calls; verifies harness wiring only)
- `--list` flag (enumerate scenarios)
- `--scenario W1,W11` (selective)
- `--provider {claude,codex}`
- `--bail-fast`
- Artifact root: `bench-results/web-as-user/<run-id>/<scenario>/`
- INFRA classification for provider auth/rate-limit
- Process-group cleanup on exit (no orphan otto/queue/playwright processes)

### 5F. CI placement (revised — concrete enforcement mechanics)

| Job | Trigger | Enforcement | Secrets |
|---|---|---|---|
| **PR-CI: recorded Playwright** | every PR (incl. fork) | **GitHub branch protection** on `main`: required check before merge | none |
| **PR-CI: drift gate + freshness gate** | every PR touching MC schemas | **Required branch-protection check**; blocks merge unless recordings refreshed or `recording-waiver` label applied by maintainer | none |
| Nightly: W11 + W1 + W7 | scheduled cron | **Failure escalation**: posts an issue + Slack ping; second consecutive failure auto-opens a P1 issue. Does not auto-block existing `main` (already-merged commits aren't undone), but blocks the next release-tag workflow (see below) | provider keys (no fork access) |
| Weekly: full W-suite | scheduled cron | Same as nightly: failure → issue + escalation; not a merge block, but feeds into release gating | provider keys |
| **Pre-release: full × providers** | `release/*` branch push **OR** workflow_dispatch on a release branch | **Required check before tag publication**: a tagged release workflow refuses to publish unless this job is green within 24h of the tag commit | provider keys |
| Live-from-PR | manual workflow_dispatch with `live-tests` label | Maintainer-only label gate; never auto-runs on fork PRs | provider keys |

Mechanics:
- Branch protection covers PR-CI + drift gate. Tagged-release workflow includes pre-release check as a required input, refuses publication otherwise.
- Failure artifacts (Playwright traces, videos, screenshots, recording.cast equivalents) retained 14 days; redaction step on upload (no `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN` leakage).
- Scheduled jobs cannot retroactively block merged commits; instead they escalate via issues + gate the next release.

### 5G. Layer-A vs Layer-B distinction (operational rule)

| Layer | Where | Speed | Cost | Catches |
|---|---|---|---|---|
| Phase 3+3.5 (recorded fixtures) | pytest, per-PR | <30s/test | $0 | UI rendering bugs, regressions, schema drift (with refresh policy) |
| Phase 5 (live harness) | nightly/weekly/pre-release | 5–25min/scenario | $1–5/scenario | Live-pipeline bugs, real subprocess lifecycle, polling timing, real artifact production, real merge, CLI/web interop, outage recovery, product correctness |

Both are required. Recorded fixtures keep PR loops fast; live harness catches what frozen fixtures can't.

### 5H. Soft-assert + auto-mine pattern (mandatory for long live scenarios)

Adopted directly from `docs/tui-mission-control-lessons.md` ("Real-LLM E2E + soft-asserts + auto-mine post-run audit"). Long live scenarios (W2, W11, W12a, W12b, W13) MUST:

- Replace every fail-fast `assert` / `raise` in the test body with a `soft_assert(cond, msg)` that appends to a `RunFailures` list
- Capture all subprocess stderr to file (never `/dev/null`)
- After the scripted scenario phases finish (regardless of outcome), run an **artifact-mine pass** that scans known invariants on the on-disk state: queue files coherent, run registry consistent with sessions, no orphan worktrees, no leaked runtime files in project root, gitignore properly excludes Otto runtime files
- Fail once with the **full collected failure list**; never short-circuit on first miss

Otherwise: a $5/25min run finds one bug per take, costing 9× the wall time and dollars to find 9 bugs (per the cited TUI lessons doc, this exact thing happened during N9 hardening).

### 5I. Verification

- `scripts/web_as_user.py --list` shows the explicit set: W1, W2, W3, W4, W5, W6, W7, W8, W9, W10, W11 (nightly core), W12a, W12b, W13 — 14 scenarios
- Nightly tier (W11 + W1 + W7) lands green at least once on `worktree-i2p` before merge to `main`
- One weekly full sweep lands green pre-merge to `main`
- Recording artifacts (Playwright traces, videos, network logs, captured stderr) viewable post-run
- State-contract artifact emitted per scenario, drift-gated against recorded fixtures
- Cost matches estimate within ±20%
- Real-cost guardrails block accidental invocation without `OTTO_ALLOW_REAL_COST=1`
- Long scenarios use soft-assert + post-run artifact-mine pattern; failure reports include all collected misses, not just the first

---

## Verification criteria (per phase, expanded)

### Phase 0
- Every entry in `scripts/e2e_web_mission_control.py:COVERAGE_MODEL` appears in `coverage-migration.md` with a target Playwright test id.

### Phase 1
- 1A: every `@app.<verb>` route in `otto/web/app.py` appears in `server-endpoints.md`
- 1B: every named region in `App.tsx` appears in `client-views.md`; spot-check by walking the live app
- 1C: every flow has a one-sentence expected-outcome line written before Phase 2 begins
- 1D: matrix exhaustive — every flow × every test layer cell filled (including "n/a")

### Phase 2
- Every CRITICAL + IMPORTANT finding has file:line + concrete fix
- Every accessibility finding tagged separately (regardless of severity tier)
- User reviews CRITICAL+IMPORTANT before Phase 4 starts
- Deferred items have explicit acceptance criteria

### Phase 3
- Backend fixture spins up in <2s
- One example Playwright test passes end-to-end against built bundle
- Coverage parity: every `COVERAGE_MODEL` state/action mapped to either server unit test or browser test
- No unexpected console errors / network failures in any test
- Stable seed data; tests run reliably 10x in a row
- Failure artifacts (screenshot + trace + API state dump) attached on every failure
- Visual baselines per browser × OS committed; mask-region budget noted

### Phase 4
- Every code change ships with a paired browser/server test that fails before the change and passes after
- No skipped/xfail tests added without an issue link
- Pre-merge sequence: `npm run web:typecheck` → `npm run web:build` → focused pytest → focused browser → full browser smoke
- Test count grows monotonically; existing test count never decreases

---

## Adversarial review

Per `CLAUDE.md` policy:

1. **Plan Gate (mandatory):** invoke `/codex-gate` Plan Gate on this file before any Phase 0 doc is written. Up to 4 rounds.
2. **Implementation Gate (mandatory):** invoke `/codex-gate` Implementation Gate before each Phase 4 commit ships to `worktree-i2p`.

Reviews appended below.

---

## Decisions (locked in by user 2026-04-25)

- **Tooling:** Playwright (Python, `pytest-playwright`). agent-browser stays the certifier's exploratory tool, gets retired from the regression harness role once Playwright reaches parity.
- **Visual regression:** in scope, **curated**. Frozen seeds, masked regions, disabled animations, fixed fonts, baselines per browser+OS.
- **Viewports:** 3 device profiles — Mac mini (~1920×1080), MBA (~1440×900), iPhone (`devices["iPhone 14"]` + webkit). Functional flows pick one profile each unless layout-relevant; visual regression captures all three.
- **Cost ceiling:** uncapped.
- **Audit docs location:** `docs/mc-audit/` (in-repo, durable).

---

## Plan Review

### Round 1 — Codex (2026-04-25)

REVISE — 12 issues, all addressed:

1. **[FIXED]** Existing `scripts/e2e_web_mission_control.py` browser harness ignored. → Added Phase 0 (migration inventory) + retirement policy.
2. **[FIXED]** `/api/events` is JSON polling, not SSE. → Removed t10 SSE-reconnect; added polling-resilience tests t30–t33; corrected description in surface-area inventory.
3. **[FIXED]** UI surface undercounted. → Expanded preliminary surface list to 18+ named regions; rewrote 1B to catalog product-states-and-controls walked from the App.tsx render tree, not just named components.
4. **[FIXED]** Queue/action conflation. → Split into job-submission tests (t06–t10) and run-action tests (t11–t17).
5. **[FIXED]** Static bundle risk. → Phase 3B mandates `npm web:typecheck && npm web:build` per run; CI sequence specified; stale-asset assertion added.
6. **[FIXED]** Playwright setup underspecified. → Phase 3A names the deps, pins versions, names browsers, names CI cache path, names install command.
7. **[FIXED]** Watcher tests can spawn real subprocesses. → Phase 3B forbids real subprocess; mandates monkeypatch + teardown orphan-process assert.
8. **[FIXED]** Parallelism global-state risk. → One backend per test; atomic socket bind; explicit two-tab test only.
9. **[FIXED]** Visual regression too broad. → Curated `tv01–tv08` list (8 shots, not 100s); frozen seeds; masked regions; disabled animations; fixed fonts; per-browser+OS baselines; tolerance budget.
10. **[FIXED]** Test inventory gaps + redundancy. → Inventory rewritten: 40 functional tests organized by surface; project launcher edge cases (t02–t04); job dialog matrix (t06–t10); filters (t19); diff edge cases (t21); advanced run actions (t11–t17); stale/unverified watcher (t28–t29); deep-link edges (t36–t37). Redundant t10/t14 + t11/t20 collapsed.
11. **[FIXED]** Phase 2A blind spots. → Added 5 new hunter angles: destructive-action safety, evidence trustworthiness, packaging integrity, large-data ergonomics, keyboard-only operation.
12. **[FIXED]** Severity gate too bug-shaped. → Replaced with severity+effort+theme matrix; accessibility blockers always fix; deferred items require explicit acceptance.

### Round 2 — Codex (2026-04-25)

REVISE — 14 new issues, all addressed:

1. **[FIXED]** W1–W10 are isolated feature checks, not real operator journeys. → Added W11 (operator day, modeled on N9) as **nightly core signal**.
2. **[FIXED]** Recording boundary too narrow (only `otto_logs/sessions/`). → Phase 3.5A rewritten: capture **full project runtime tree** (sessions + cross-sessions + registry + queue + merge state + events + watcher + project files + `state-contract.json` snapshot).
3. **[FIXED]** R1–R8 under-cover queue/watcher/operator surfaces. → Added R9–R14: mixed-row queue, active watcher, stale watcher, blocked merge, merge-with-conflicts, large history.
4. **[FIXED]** W3 assumed Improve UI action that doesn't exist. → Verified: "Improve" exists in JobDialog, not as a row-level CTA. W3 reframed accordingly; row-level Improve tracked as P2 product gap to surface in `findings.md`.
5. **[FIXED]** Live harness must verify built software, not just UI. → W1/W3/W4/W6/W11/W12 require post-run product verification (load app, run tests, inspect audit branch). Verdict semantics updated.
6. **[FIXED]** CLI/web interop missing. → Added W12: CLI build → web inspects → web actions → CLI reflects.
7. **[FIXED]** Outage recovery missing. → Added W13: server restart mid-run, browser reload, verify recovery.
8. **[FIXED]** Real-cost guardrails missing. → Phase 5E mirrors `scripts/otto_as_user.py`: `OTTO_ALLOW_REAL_COST=1` required, `--dry-run`, `--list`, `--scenario`, `--provider`, `--bail-fast`, INFRA classification, process-group cleanup.
9. **[FIXED]** CI placement underspecified. → Phase 5F: PR-CI=recorded only; nightly=W11+W1+W7; weekly=full W; pre-release=full × providers; manual workflow with maintainer-only label gate, never fork PRs; redaction on upload, 14-day retention.
10. **[FIXED]** Cost plan too blunt ($17/night). → Re-tiered: $0/PR, ~$7/nightly (core+mobile), ~$26/weekly (full), ~$40/pre-release (full × parity).
11. **[FIXED]** Provider parity blanket. → Codex parity subset: W1, W2, W6, W11, W12 only (submit/queue/deterministic-failure/operator-day/CLI-interop). Full Claude only.
12. **[FIXED]** Failure scenario flaky. → W6 uses **deterministic known-bad fixture intent**, not random LLM "hopefully fails."
13. **[FIXED]** Recorded-vs-live sync. → Phase 3.5C adds drift gate: every live run emits `state-contract.json`; CI compares to recorded fixture; mismatch fails or opens refresh issue. Freshness window 30 days; schema-snapshot diff in CI.
14. **[FIXED]** Wrong npm command path. → Confirmed `package.json` is at repo root; all references corrected to `npm run web:typecheck && npm run web:build`.

### Honest gap (acknowledged, not fixed)

This plan does not cover:
- Multi-week real human projects (only short scenarios; unknown how the system behaves under sustained real use)
- Real iOS Safari (Playwright webkit emulates, but isn't actual iPhone Safari)
- Windows / non-macOS browser font and sub-pixel rendering differences
- Multi-user collaboration (single-operator assumption)
- Hostile network conditions (slow links, packet loss, DNS flakes) beyond what tab-background simulates
- Very large monorepos (recordings cap at ~100-file diff)
- Humans editing files mid-build (could be added as W14 later)

These are explicitly out of scope for this pass. If they matter, they're a separate plan.

### Round 3 — Codex (2026-04-25)

REVISE — 10 new issues, all addressed:

1. **[FIXED]** Wrong path strings in capture boundary. → Verified against `otto/paths.py`: live runs at `cross-sessions/runs/live/`, queue state at project-root `.otto-queue-state.json` + `.otto-queue-commands*.jsonl`, events at `mission-control/events.jsonl`, watcher at `web/watcher-supervisor.json`. **Capture script now generates manifest from `paths.py` helpers, never strings.** If a path lacks a helper, the missing helper is the bug.
2. **[FIXED]** Phase 4 said R1–R8. → Updated to R1–R14; closeout gate fails if any recording lacks a smoke-render Playwright test.
3. **[FIXED]** W6 retry semantics inconsistent (same intent that just failed shouldn't pass). → W6 retry now requires **harness-side fixture mutation** before retry (fix env var, valid yaml, swap intent). Same-intent retry would loop forever.
4. **[FIXED]** W12 action legality (atomic runs don't expose merge). → Split into W12a (atomic CLI run → web cancel/retry only) and W12b (CLI-queued task → web merge). Atomic vs queue action sets verified against `adapters/atomic.py` and `adapters/queue.py`.
5. **[FIXED]** Product verification inconsistent. → Coverage matrix added: explicit yes/no per W-scenario; W2/W7/W8/W9/W11/W12a/W12b/W13 now require artifact verification when they produce real artifacts. W5 marked "verify the block, not artifacts."
6. **[FIXED]** `.git/` raw capture risky. → Replaced with **minimal git bundle** + reconstruction script approach. No hooks, no remotes, no reflog beyond what's needed. Stored under `tests/browser/_fixtures/recorded-runs/<scenario>/git/`.
7. **[FIXED]** Sanitization referential-integrity. → Added 3.5A-bis: invariant pass after scrubbing — every artifact path resolves, every run_id has session, every queue task ID matches manifest, every audit branch in merge state exists in bundle, FastAPI app boots clean against fixture, all linked endpoints return 200. Recording rejected on failure.
8. **[FIXED]** Freshness was advisory. → 3.5C revised: PR-CI blocks merge if MC-schema-touching PR has stale recordings (override only via `recording-waiver` label); scheduled `main` job fails (not warns) on stale; pre-release blocks tag publication if drift gate red.
9. **[FIXED]** CI gating mechanics overstated. → 5F revised with concrete mechanics: branch protection covers PR + drift; tagged-release workflow refuses tag publication unless pre-release sweep green within 24h of tag commit; scheduled jobs escalate via issues, can't retroactively undo merges.
10. **[FIXED]** Long scenarios should soft-assert + auto-mine. → Added 5H: explicit pattern adopted from `docs/tui-mission-control-lessons.md` — `RunFailures.soft_assert`, subprocess stderr capture, post-run artifact-mine pass with on-disk invariant scan, fail once with full collected list.

### Round 4 — Codex (2026-04-25)

REVISE — 8 narrow implementation issues, all addressed:

1. **[FIXED]** Stale helper/endpoint names. → Pseudo-code corrected: `paths.sessions_root` (not `sessions_dir`); `state-contract.json` lists only GET endpoints (`/api/queue/{command}` is POST-only and excluded).
2. **[FIXED]** Repo-relative path scrubbing breaks artifact reads (`service.py:678` resolves vs project_dir). → Sanitization uses `$PROJECT_ROOT/...` placeholders; restore step hydrates to absolute tmp-project paths.
3. **[FIXED]** No W→R drift-gate mapping. → New section 3.5B-bis: explicit table mapping each W scenario to its target R fixture(s); composite scenarios (W11, W12b) declare multiple targets.
4. **[FIXED]** W6 used bad-provider-key (which the harness classifies INFRA, not FAIL). → W6 now uses non-INFRA causes: malformed yaml / missing-key, pre-staged intent referencing nonexistent local file, or deterministic-failing acceptance test.
5. **[FIXED]** W12b used nonexistent `otto queue submit`. → Corrected to `otto queue build "..." --as <task>` per verified `cli_queue.py:396` subcommands (`build|improve|certify`).
6. **[FIXED]** W12 split not propagated. → Cadence, Codex parity (now `W1, W2, W6, W11, W12a, W12b`), `--list` (explicit 14-scenario list), cost (~$28 weekly, ~$42 pre-release) all updated.
7. **[FIXED]** Invariant pass needs status contracts per route. → Endpoint-status contract table added to 3.5A-bis: per-recording expected status per endpoint; R8 minimal-edge declares 404 for `/proof-report`.
8. **[FIXED]** Git restore order. → Spelled out in 3.5A-ter: `git init` empty, fetch from bundle, checkout branch, then overlay `otto_logs/` and hydrate placeholders. Avoids the "clone into non-empty dir" trap.

### Round 5 — Codex (2026-04-25)

REVISE — 2 narrow blockers, both addressed:

1. **[FIXED]** Mid-run scenarios (W9/W10/W13) mapped to a terminal recording (R1). → Multi-phase contracts: `R1.pre`, `R1.mid`, `R1.post` snapshots in a single recording session. W→R mapping uses explicit phase suffix. Drift gate compares against the named phase.
2. **[FIXED]** `/api/projects` leaks recorder's local `~/otto-projects`. → Recording always sets `projects_root` to a fixture-owned isolated dir; `/api/projects` excluded from the contract for typical recordings (only included for future R-launcher fixture).

### Round 6 — Codex (2026-04-25)

**APPROVED.** Both round-5 fixes verified present:
- R1.pre / R1.mid / R1.post phase contracts defined; W9/W10/W13 map to explicit phases
- `projects_root` isolated per recording; `/api/projects` excluded from typical state contracts

No new blockers. Plan cleared for Phase 0 implementation.
