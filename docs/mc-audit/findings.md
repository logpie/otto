# Mission Control UI/UX Audit — Findings

**Audit date:** 2026-04-25
**Hunters:** 8 Codex (gpt-5.5 xhigh) + 5 Claude (Opus 4.7) parallel adversarial review
**Triage rule:** CRITICAL + IMPORTANT + accessibility blockers (any severity) → fix list. NOTE/MINOR → `deferred.md`. Source per-hunter findings under `_hunter-findings/`.

## Tally

| Severity | Count | Action |
|---|---|---|
| CRITICAL | 20 | Fix in Phase 4 (TDD per pair) |
| IMPORTANT | 132 | Fix in Phase 4 |
| Accessibility (any) | 25 | Fix regardless of severity |
| NOTE/MINOR | 76 | `deferred.md` (record, defer) |

(Some CRITICAL/IMPORTANT items overlap with accessibility; counted once in the rollup.)

## Action list (CRITICAL + IMPORTANT, themed)

### Theme: Async action discipline (multiple hunters converged here)

The single biggest systemic UX gap. Fix once via shared in-flight latch + spinner system; many CRITICAL/IMPORTANT findings dissolve.

- **CRITICAL** — `App.tsx:391-414,616,1146`: `Start watcher` no pending guard; repeated clicks fire multiple POSTs (Codex first-time-user #14)
- **CRITICAL** — Async action buttons stay enabled during their POST; only modal Confirm button disables; original triggers don't (Claude microinteractions #2)
- **CRITICAL** — No `:active` rule anywhere in styles.css; zero depressed-click feedback (Claude microinteractions #1)
- **CRITICAL** — No spinners anywhere; every loading state is plain text (Claude microinteractions #3)
- **IMPORTANT** — `App.tsx:204`: duplicate destructive POSTs possible on rapid double-click — `confirmPending` is React state not synchronous lock (Codex state-management #10)
- **IMPORTANT** — Refresh button doesn't disable while refreshing (Claude microinteractions)
- **IMPORTANT** — Search input has no debounce — every keystroke fires filter+refresh (Claude microinteractions)

### Theme: Connection / polling resilience

- **IMPORTANT** — `App.tsx:282`: `/api/state` polling failures leave old data rendered; only "refresh failed" + transient toasts (Codex error-empty-states #1)
- **IMPORTANT** — `api.ts:31`: HTTP layer has no retry/backoff/abort; outage hammers the server every interval (Codex state-management #7)
- **IMPORTANT** — `App.tsx:251`: backend restart in launcher mode drops to launcher and clears local state without preserving route to recover (Codex state-management #8)
- **IMPORTANT** — `App.tsx:247`: overlapping refreshes; older poll can overwrite fresher action state. Need request-id or AbortController (Codex state-management #9)

### Theme: Stale data & 404 recovery

- **IMPORTANT** — `App.tsx:269`: selected run disappears, polling ignores 404, leaves stale detail (Codex error-empty-states #4)
- **IMPORTANT** — `App.tsx:274,314`: tab A removes selected run, tab B silently auto-selects different run; deep link to deleted run handled by silently clearing without 404 UI (Codex state-management #1, #2)
- **IMPORTANT** — `App.tsx:1456`: selected-run loading indistinguishable from no-selection — both render "Select a run." (Codex error-empty-states #5)

### Theme: Pagination, history, large data

- **CRITICAL** — `App.tsx:217,225,1575,1583,3169`: logs append forever into `logText`; 10MB log can lock the browser (Codex long-string #1)
- **IMPORTANT** — `App.tsx:1377, api.ts:22, model.py:316`: `/api/state` returns `total_pages` but client never sends `history_page` and renders no pagination — power user with 200+ runs stuck on page 1 (Codex long-string #3, Codex state-management #6, Claude heavy-user)
- **IMPORTANT** — `App.tsx:1074`: command backlog renders every item; large backlog → huge `<details>` DOM (Codex long-string #4)
- **IMPORTANT** — Diff parsing concatenates section text line-by-line, span per line, can go quadratic (Codex long-string #2)
- **IMPORTANT** — Task board sorts/builds columns per render, renders every card (Codex long-string #5)

### Theme: Long-string overflow

- **IMPORTANT** — Toasts have max width but no `overflow-wrap`; 500-char URL/error overflows offscreen (Codex long-string #6)
- **IMPORTANT** — Sticky last-error/result banners can grow unbounded on long messages, push main workflow out of view (Codex long-string #7)
- **IMPORTANT** — Job dialog status/help text in flex rows without wrap; long errors push submit button or overflow modal (Codex long-string #8)
- **IMPORTANT** — Confirm dialog body lacks long-token wrapping for branch names (Codex long-string #9)
- **IMPORTANT** — Review packet headline/summary/check/failure text inconsistent clipping (Codex long-string #10)
- **IMPORTANT** — Diff toolbar branch/target text without `min-width:0`; long branch names crowd toolbar (Codex long-string #14)

### Theme: First-run clarity (Codex first-time-user)

- **CRITICAL** — `App.tsx:574,601`: before `/api/projects` returns, projectsState is null → main shell renders with enabled `New job`; can submit with project undefined
- **CRITICAL** — `App.tsx:2121-2164`: provider/model/effort/policy hidden under `Advanced options` but affect cost/runtime/QA — show pre-submit summary outside Advanced
- **IMPORTANT** — Launcher doesn't explain what Mission Control does; "Managed root" looks like user's repo disappeared
- **IMPORTANT** — `Build` / `Improve` / `Certify` / `Intent / focus` jargon unexplained for first-run
- **IMPORTANT** — `improve` and `certify` can queue blank intent — require for all first-run
- **IMPORTANT** — Disabled `Queue job` button gives no visible reason
- **IMPORTANT** — Dirty-project confirmation doesn't show dirty files or how to fix
- **IMPORTANT** — After queueing, "Start watcher" CTA doesn't say what watcher does
- **IMPORTANT** — Sidebar status exposes Watcher/Heartbeat/In flight/queued/ready/landed immediately to first-run user
- **IMPORTANT** — Filter no-match still shows "No work queued" not "No matching tasks" (overlaps with empty-state hunter)
- **IMPORTANT** — First empty board shows 4 empty columns instead of guided first-job state
- **IMPORTANT** — Queued cards without runId disabled, can't open Details
- **IMPORTANT** — App auto-selects first visible run on refresh — random old task on first project load
- **IMPORTANT** — Empty detail says only "Select a run."; needs "Select a task card to review logs, code changes, verification, and next action."
- **IMPORTANT** — Detail vocabulary `Open proof`, `evidence packet`, `Artifacts`, `Certification checks` jargon
- **IMPORTANT** — Recovery actions hidden under "Advanced run actions"; surface retry/remove/cleanup as contextual buttons
- **IMPORTANT** — Project create/select failures surface raw `HTTP 409` etc; map to recovery copy

### Theme: Information density / task-card scanability (Codex info-density)

- **IMPORTANT** — Task cards lead with id/title; summary, branch, reason hidden behind More
- **IMPORTANT** — Card meta mixes files/stories/queue-status/cost into identical unlabeled pills
- **IMPORTANT** — All task status badges are same gray pill; ready/blocked/running/failed don't scan differently
- **IMPORTANT** — `queued`/`paused`/`interrupted`/`stale` all amber despite different urgency
- **IMPORTANT** — `resultBanner.severity === "information"` renders with warning style
- **IMPORTANT** — Mission Focus working state says only `N tasks in flight`; no active task name, elapsed, cost, last event
- **IMPORTANT** — Task cards sort only by title — failures/stale/ready buried alphabetically
- **IMPORTANT** — History rows omit `completed_at_display` and `intent` even though both exist in types
- **IMPORTANT** — Recent Activity renders all events before history; latest outcomes pushed below
- **IMPORTANT** — Runtime warnings compressed into one pipe-separated string with details only in tooltip
- **IMPORTANT** — Run selection always resets inspector to `proof`; primary shortcut always `Open proof`. Default tab should depend on run state
- **IMPORTANT** — Proof tab shows `Next action` before `What failed`; failure cause not first

### Theme: Error / empty / partial-data states (Codex error-empty-states)

- **IMPORTANT** — Action POST failures caught inside confirm callback, not rethrown; modal closes anyway, no inline retry
- **IMPORTANT** — Proof evidence-content pane stuck on "Loading selected evidence artifact" even when no readable artifacts
- **IMPORTANT** — Artifact detail shows generic `artifact` / `No content.` while loading or after failed read; permission/missing errors only toast
- **IMPORTANT** — Diff transport failures leave `diffContent` null; pane stuck on "Loading diff..." forever
- **IMPORTANT** — Logs ignore `exists` and `path`; missing log file looks like "waiting for output"
- **IMPORTANT** — Diagnostics receive `[]`/`0` when `data` is null — loading or outage renders "No live runs"
- **IMPORTANT** — Task Board empty states filter-blind — with filters active still says "No work queued"
- **IMPORTANT** — Truncation labels vague: `truncated`, `(truncated)`, `more files not shown` — no total or download path

### Theme: Destructive-action safety (Codex)

- **CRITICAL** — `Land all ready` confirm only lists first 5 task IDs + `+N more`; doesn't enumerate every task. Need typed-phrase confirm for N>1
- **IMPORTANT** — Single merge confirm omits run/task ID, branch, target, file count, files
- **IMPORTANT** — Cleanup says only "Clean up this run?"; needs "Remove run record and cleanup worktree? This cannot be undone"
- **IMPORTANT** — Cancel says only "Cancel this run?"; needs run ID, SIGTERM behavior, "Work in progress may be lost"
- **IMPORTANT** — Watcher stop confirm doesn't show queued count, command backlog, running count, pid
- **IMPORTANT** — Stale UI enables action; POST returns 409/404; handler swallows error and closes dialog as success
- **IMPORTANT** — `Queue job` writes immediately; cost-incurring build dispatches with no cancel-before-dispatch window

### Theme: Evidence trustworthiness (Codex)

- **CRITICAL** — `App.tsx:478, service.py:166`: diff fetched once and held client-side without target/branch SHAs or merge-base; **the code merged can differ from the diff reviewed**
- **IMPORTANT** — Diff truncation is bare `truncated` suffix; no total bytes/hunks shown, no full-diff download
- **IMPORTANT** — Proof drawer cached by artifact index, never reloaded for same run; server accepts first matching proof file without validating run identity
- **IMPORTANT** — Mission Control flattens certification to final stories/counts; per-round evidence not visible
- **IMPORTANT** — Logs poll every 1.2s but UI doesn't say "Live, polling" vs "Final"
- **IMPORTANT** — Artifact content always decoded as UTF-8; binary appears as garbage
- **IMPORTANT** — Artifact lists expose only label/path/kind/exists; no size/mtime/hash/source
- **IMPORTANT** — Visual evidence (screenshots/recordings) discovered by globbing; no capture-time/story/round/run identity manifest
- **IMPORTANT** — `proof-of-work.json/html` has schema metadata but no digest/signature; edited files render happily

### Theme: Packaging / static-bundle integrity (Codex)

- **CRITICAL** — `app.py:35`: `otto web` serves checked-in `otto/web/static` bundle; nothing verifies match with source. Developer skips `npm run web:build` → silent stale UI
- **IMPORTANT** — `pyproject.toml:49`: Python build never builds frontend; `pip install -e .` ships whatever static is in tree
- **IMPORTANT** — `web:build` runs only Vite; `web:typecheck` separate and advisory; no CI gates
- **IMPORTANT** — Default cache headers on `/static/*` underuse hashed assets
- **IMPORTANT** — Server doesn't validate `index.html` referenced assets exist; lost asset → silent 404/blank UI

### Theme: Heavy-user / power-use gaps (Claude heavy-user)

- **CRITICAL** — Pagination unrendered (overlaps with theme above)
- **IMPORTANT** — Filters live only in React state, reset on refresh/project-switch/popstate
- **IMPORTANT** — Table columns non-interactive `<th>` cells; no sorting at App.tsx:1386-1393
- **IMPORTANT** — Inspector tab forcibly resets to Proof on every run-switch (App.tsx:197,305) — kills "open Logs across 10 failed runs" workflow
- **IMPORTANT** — No global keyboard shortcuts beyond Tab/Enter
- **IMPORTANT** — No bulk ops besides merge-all (no bulk cleanup, retry)
- **IMPORTANT** — No log search (Cmd-F)
- **IMPORTANT** — No `Notification` API usage; long run finishing in backgrounded tab is invisible
- **IMPORTANT** — No re-run-with-modified-provider, no templates, no cost dashboard, no export, no compare-two-runs, no pinning
- **IMPORTANT** — Dirty-target consent checkbox forgets between consecutive submits to same dirty worktree
- **IMPORTANT** — No quick project switcher

### Theme: Accessibility (Claude — fix regardless of severity tier)

All 25 a11y findings are fix-required. Top items:

- **CRITICAL A11Y-01** — `<RunInspector role="dialog" aria-modal=true>` is a lie; sidebar/main remain focusable behind it because `modalOpen` excludes `inspectorOpen`
- **CRITICAL A11Y-02** — When confirm/job dialog stacks on inspector, `<main aria-hidden=true>` hides the inspector itself (inspector mounts inside main)
- **CRITICAL** — Tab-pattern violations: tablist has no Left/Right arrow nav (WCAG keyboard nav)
- **CRITICAL** — Table-row-as-button anti-pattern (overrides `<tr>` with role=button)
- **CRITICAL** — Generic-div `aria-label`s ignored
- **CRITICAL** — Status-text contrast borderline (sub-4.5:1)
- **IMPORTANT** — 25 region landmarks → screen-reader noise
- **IMPORTANT** — No skip link
- **IMPORTANT** — No per-view document.title updates
- **IMPORTANT** — No aria-live for view/run/tab changes
- **IMPORTANT** — `--muted` token sub-AA on tinted surfaces
- **IMPORTANT** — `prefers-color-scheme: dark` actively rejected (`color-scheme: light` hardcoded)
- **IMPORTANT** — `title=` attributes used as primary text channel for truncated content (invisible to keyboard)
- **IMPORTANT** — Sub-44px touch targets on mobile

### Theme: Visual coherence (Claude)

- **CRITICAL** — No design-token discipline (51 raw hex codes); 11 CSS vars exist but bypassed; `#fff8e1` appears 7× hardcoded
- **CRITICAL** — No dark mode (despite half-dark sidebar/log panes); macOS dark-mode users see worst of both
- **CRITICAL** — No `:active`/pressed state anywhere (overlap with async-action theme)
- **IMPORTANT** — Disabled-state inconsistency (3 idioms)
- **IMPORTANT** — Hover-treatment drift (5 idioms)
- **IMPORTANT** — 10-step ad-hoc font scale (10/11/12/13/14/15/16/18/20/22)
- **IMPORTANT** — Off-grid 7/9px paddings; 7px+10px outlier radii in 8px card system
- **IMPORTANT** — Status-color drift (`#166534` vs `#15803d` for "pass"; "warn" amber jumps `#92400e` ↔ `#a16207`)
- **IMPORTANT** — Color-blind unsafe (color-only PASS/FAIL/WARN signaling)
- **IMPORTANT** — Input-vs-button baseline jaggy in toolbar
- **IMPORTANT** — Magenta status without legend, borderline AA
- **IMPORTANT** — Invisible Otto brand identity

### Theme: Microinteractions (Claude)

- **CRITICAL** — `:active` missing (themed above)
- **CRITICAL** — Async buttons stay enabled (themed above)
- **CRITICAL** — No spinners (themed above)
- **CRITICAL** — Disabled Diff buttons (3 sites) have no `title=` reason; canShowDiff returns false silently
- **IMPORTANT** — JobDialog has no inline validation hint
- **IMPORTANT** — Refresh doesn't disable during refresh
- **IMPORTANT** — Search input no debounce
- **IMPORTANT** — No optimistic UI; every action waits full refresh cycle (up to 5s)
- **IMPORTANT** — Drawer toggle is instant, no animation/chevron
- **IMPORTANT** — ConfirmDialog has TWO close affordances (header X + footer Cancel)
- **IMPORTANT** — Disabled tabs look almost identical to inactive tabs
- **IMPORTANT** — Toast doesn't pause-on-hover, isn't manually dismissible

### Theme: Keyboard-only operation (Claude)

(Major items folded into Accessibility above.)

- **IMPORTANT K-03** — JobDialog initial focus lands on "Close" not the intent textarea
- **IMPORTANT K-04** — Inspector tablist no arrow nav (WAI-ARIA tablist)
- **IMPORTANT K-05** — Zero global hotkeys
- **IMPORTANT K-07** — No `/` to focus search
- **IMPORTANT K-09** — No skip-link
- **IMPORTANT** — `<main>` aria-hidden=true when inspector open hides the inspector itself

## Implementation order (Phase 4 TDD pairing)

Per the plan's Phase 4 ordering: every fix lands with a paired browser test that fails before the change and passes after.

1. **Async action discipline** (themed cluster) — kills 4 CRITICAL + many IMPORTANT in one go. Build shared `useInFlight` hook + `Spinner` component + `:active` style + per-button latch.
2. **Build/bundle integrity** (CRITICAL packaging) — CI gate, source-hash check, prevent silent stale-UI regressions.
3. **Diff freshness contract** (CRITICAL evidence) — round-trip merge SHAs.
4. **Long-log buffering** (CRITICAL long-string) — bounded tail buffer prevents browser lockup.
5. **History pagination** (CRITICAL+IMPORTANT) — restores power-user flow.
6. **Boot-loading gate + first-run clarity** (CRITICAL first-run) — prevent submit-before-state-ready bug.
7. **Pre-submit advanced-options summary** (CRITICAL first-run).
8. **Accessibility blockers** A11Y-01/02/04 — modal-isolation, tablist arrows, table-row-as-button.
9. **Bulk-merge confirmation** (CRITICAL destructive) — typed-phrase for N>1.
10. **Status semantics** (IMPORTANT info-density cluster) — typed chips, badge tone classes.
11. **Connection / polling resilience** (IMPORTANT cluster) — backoff, retry, stale-banner, request-ids.
12. **Stale data / 404 recovery** (IMPORTANT cluster).
13. **Empty/error/partial-data states** (IMPORTANT cluster) — actionable copy + recovery.
14. **Long-string overflow** (IMPORTANT cluster) — wrap-anywhere helpers, line clamps.
15. **Visual coherence** (IMPORTANT cluster) — design tokens, status color/icon pairs.
16. **Microinteractions polish** (IMPORTANT cluster).
17. **Keyboard-only flows** (IMPORTANT cluster).
18. **Heavy-user gaps** (IMPORTANT cluster) — sort, bulk, filters-in-URL, log search, notifications.
19. **Per-flow remaining individual fixes**.
