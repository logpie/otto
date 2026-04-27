# Otto Web Redesign — Handoff to Codex

**Audience**: Codex, picking up the Mission Control web redesign after a multi-session collaboration with Claude.
**Owner**: yuxuan (logpie)
**Branch**: `worktree-i2p` (worktree under `.claude/worktrees/i2p/`)
**Server**: live on `http://100.104.175.44:8889/` for visual verification.

---

## 1. Mission

Otto is a CLI tool that runs autonomous coding agents under a queue. The web app — "Mission Control" — is the visual surface users use to:

1. Pick or create a project (launcher / onboarding).
2. Queue jobs (build / improve / certify) with intent text.
3. Watch the watcher dispatch them, see results stream in.
4. Review and merge the result.

**End goal**: distribute Otto to external customers. The web app should feel calm, simple, self-serve, and aesthetically pleasing — not like an internal admin dashboard.

This redesign session moved Otto's web app from a **dense, internal-tool dashboard** (dark sidebar + 4-column kanban + always-visible right rail + 6 dashboard regions per screen) toward a **modern Linear/Vercel-style SaaS** (top bar + single-table queue + on-demand right drawer + 1 primary object per screen).

---

## 2. What is done

### 2.1 Visual + IA redesign

| Area | State |
|---|---|
| **Layout primitive** | Sidebar → top bar (56px) + workspace full width |
| **Tasks view** | 4-column kanban → single-table queue with Status / Task / Stories / Files / Time |
| **Run detail** | Always-visible right rail → on-demand slide-in drawer (560px, animated) |
| **Inspector** | Fullscreen overlay → wider right drawer (960px), sits below topbar, queue stays visible |
| **New Job** | Vertical form modal → centered command-palette-style overlay (intent textarea front and center) |
| **Launcher** | Sidebar + multi-card grid → centered hero with brand mark + "otto" wordmark + project list with avatar squares + slim create-form |
| **IDLE banner** | Big hero card → 1-line state strip (or removed when clean) |
| **Project Overview / Recent Activity** | Always-visible panels → collapsed under "Project info & activity" expander |
| **Health** | Per-task list duplicated from Tasks → removed (only system status remains) |

### 2.2 Identity + typography

- **Inter** loaded via Google Fonts for body text.
- **JetBrains Mono** for code/logs.
- **Custom SVG BrandMark** (`components/BrandMark.tsx`): teal rounded square with arc + dot — semantic = "iteration loop / agent build cycle". Replaces the flat "O" letter.
- Lowercase **"otto"** wordmark, weight 600, -0.04em letter-spacing.
- Heading scale tokens: `--font-display`, `--font-section`, `--font-subsection`, `--font-meta`.
- 5-state semantic palette: `--color-{success,warning,danger,info}-{fg,bg,border}`.
- Soft chrome: 12px radius, 2-layer shadow, generous (16-24px) padding.

### 2.3 Dark mode

- Full `prefers-color-scheme: dark` palette in `styles.css` (~line 110).
- Same accent teal preserved for brand identity.
- All semantic tokens swap automatically.
- No user toggle UI — purely OS-driven.

### 2.4 Responsive

- **1440px desktop**: full layout with drawer pushing content via `padding-right`.
- **1280px MBA 13"**: same as desktop, drawer overlay shifts content (no clipping).
- **≤900px tablet**: sidebar collapses (legacy code path).
- **≤640px mobile / iPhone 14 (390px)**:
  - Topbar simplifies (wordmark + status pills hidden, only mark + project + New job).
  - Queue table drops Files/Time columns (Status/Task/Stories only).
  - Drawer + inspector go full-width.
  - Launcher form stacks vertically.
  - Modal goes near full-width.
- **Important**: drawer/inspector below topbar (`top: 57px`) so topbar always usable.

### 2.5 Keyboard

| Key | Action |
|---|---|
| `n` | New job (gated by typing-target check) |
| `Cmd+K` / `Ctrl+K` | Command palette / project picker |
| `j` / `k` | Next / previous task row (auto-scrolls into view) |
| `1`–`5` | Switch inspector tabs (Try / Result / Diff / Logs / Artifacts) |
| `Esc` | Close help overlay → inspector → drawer (in that priority) |
| `?` | Open help overlay (cheat sheet) |

Help overlay is the discoverable surface for shortcuts. Lives in `components/HelpOverlay.tsx`.

### 2.6 Component extraction (partial)

App.tsx went from **8859 → 8246 LOC** (-613 LOC, -7%). The pattern is established for further extraction.

**New component tree:**
```
otto/web/client/src/
├── components/
│   ├── BrandMark.tsx
│   ├── HelpOverlay.tsx
│   ├── MicroComponents.tsx     ← 8 leaf primitives
│   ├── Pill.tsx                ← typed pill primitive (PillTone)
│   ├── Spinner.tsx
│   ├── launcher/
│   │   ├── LauncherExplainer.tsx
│   │   └── ProjectLauncher.tsx ← + ProjectMeta + launcherErrorMessage
│   ├── tasks/
│   │   └── TaskQueueList.tsx   ← + TaskRow
│   ├── topbar/
│   │   └── TopBar.tsx
│   └── (empty: health, inspector, new-job, overview, review, toolbar)
└── utils/
    └── format.ts                ← 17 pure formatters
```

**Helpers exposed**: 199 functions in `App.tsx` are now `export function …` (was 9). Types exported: `BoardTask`, `Filters`, `ViewMode`, `BoardStage`, `InspectorMode`. This means future extractions just need to import what they use; no further App.tsx changes required to expose helpers.

---

## 3. What's not done — the refactor backlog

The **biggest deferred chunk** is component extraction. Pattern is established but ~1500-2000 LOC of components still live in App.tsx, mixed with state hooks. App.tsx currently has 38 useState calls, 199 helper functions, and the main `App()` component (~1900 lines of composition + state).

### 3.1 Components to extract

In suggested order (smallest/safest first → largest/riskiest last):

| # | Component cluster | Approx LOC | Target file | Difficulty |
|---|---|---|---|---|
| 1 | `MissionFocus` + `OperationalOverview` + `RuntimeWarnings` | ~140 | `components/overview/MissionFocus.tsx` | Easy |
| 2 | `ProjectOverview` + `RecentActivity` + `RecentRunsPanel` + `LiveRuns` | ~200 | `components/overview/index.tsx` | Easy |
| 3 | `SystemHealth` + `DiagnosticsSummary` | ~150 | `components/health/index.tsx` | Easy |
| 4 | `EventTimeline` | ~40 | `components/EventTimeline.tsx` | Trivial |
| 5 | `Toolbar` | ~150 | `components/toolbar/Toolbar.tsx` | Easy |
| 6 | `History` (table) | ~250 | `components/history/History.tsx` | Easy |
| 7 | `ToastDisplay` | ~30 | `components/ToastDisplay.tsx` | Trivial |
| 8 | `ConfirmDialog` | ~100 | `components/ConfirmDialog.tsx` | Easy |
| 9 | `CommandPalette` | ~100 | `components/CommandPalette.tsx` | Easy |
| 10 | `InertEffect` + `LiveRegion` | ~50 | `components/a11y.tsx` | Trivial |
| 11 | **Inspector cluster**: `RunDetailPanel`, `PhaseTimeline`, `RunInspector`, `ProductHandoffPane`, `LogPane`, `ProofPane`, `DiffPane`, `ReviewPacket`, `FailureSummary`, `DetailLine`, `RecoveryActionBar`, `ActionBar`, `ArtifactPane`, `ProofProvenance`, `CertificationRoundTabs`, `ProofEvidenceContent` | ~1400 | `components/inspector/` (one file per component, or `index.tsx` aggregate) | **HARD** — see §3.2 |
| 12 | `JobDialog` + `PhaseRoutingFields` + helpers | ~700 | `components/new-job/JobDialog.tsx` | Hard — many dependencies on cert/planning helpers |

After all extractions, target App.tsx shape: ~500-700 LOC consisting of:
- Hooks (useState, useEffect, useCallback, useRef)
- Top-level `App()` composition (JSX)
- API call helpers (`refresh`, `loadLogs`, `loadDiff`, `loadProofArtifact`, etc.)
- Confirm/dispatch wrappers (`runActionForRun`, `mergeReadyTasks`, etc.)
- Route state effects

### 3.2 Why the inspector cluster failed in my attempt

I tried to extract this in a single pass and reverted because the cluster has **non-obvious dependencies that span outside its line range**:

| Dependency | Location | Need |
|---|---|---|
| `useDialogFocus` | App.tsx ~line 7212 | Hook used by RunInspector — must export |
| `LogState` | App.tsx interface, not in types.ts | Move to types.ts or re-export from App |
| `LogStatus` | App.tsx local type | Move to types.ts |
| `ArtifactPane` | App.tsx line 5148 (after the cluster) | Either include in cluster or import |
| `productKindHint`, `shortPath` | App.tsx helpers | Export and import |
| `renderDiffText` | App.tsx helper | Export and import |
| `ReactKeyboardEvent` | React type import | Add to imports |
| `formatTechnicalIssue` | Already in `utils/format.ts` | Import from there, NOT App |
| `renderLogTextWithHighlight` | App.tsx, but also self-defined inside cluster | Use `as` alias or rename |
| `describeLogHeader` | Same | Same |
| `isTypingTarget` | Same | Same |

**Recommendation for Codex**: extract the inspector cluster in **smaller sub-batches** rather than one big move:
- First: `LogPane`, `DiffPane` (logs/diff terminal panes — self-contained)
- Then: `ProofPane` + `ProofProvenance` + `CertificationRoundTabs` + `ProofEvidenceContent` (proof-related, all coupled)
- Then: `ProductHandoffPane` + `productHandoffFor` (try-product pane)
- Then: `RunInspector` (the wrapper with tabs, depends on the panes above)
- Last: `RunDetailPanel` + `ReviewPacket` + `PhaseTimeline` + `FailureSummary` + `DetailLine` + `RecoveryActionBar` + `ActionBar` (drawer-level)

Each sub-batch should typecheck clean before proceeding. Pre-flight: search the cluster for **every identifier that isn't React-builtin or a parameter name** and verify it's either (a) inside the cluster, (b) exported from App.tsx, (c) in `utils/format.ts`, or (d) in `types.ts`. If not, export/move it first.

### 3.3 Helpers to migrate from App.tsx → utils/

These are pure functions with no React state, candidates for `utils/state.ts` or `utils/run.ts`:

```
canMerge, mergeButtonTitle, canStartWatcher, canStopWatcher,
startWatcherTooltip, watcherSummary, commandBacklogLine,
detailStatusLabel, providerLine, certificationLine, timeoutLine,
limitLine, flagsLine, agentsLine, projectConfigLine,
taskBoardColumns, boardTaskFromLanding, boardTaskFromLive,
boardTaskMatchesFilters, compareBoardTasks, taskBoardSubtitle,
testIdForTask, statusTone, toneIcon, taskColumnTooltip,
filtersAreActive, computeBoardEmptyReason, columnEmptyCopy,
isAttentionStatus, isProjectFirstRun, domainLabel,
computeTaskChips, taskChangeLine, taskConfigSummary,
liveEventLabel, progressLabel, activeRunSummary,
workflowHealth, missionFocus,
isReviewEvidenceArtifact, isReadableArtifact, preferredProofArtifact,
isLogArtifact, formatArtifactContent, splitDiffIntoFiles,
renderLogText, renderLogTextWithHighlight, isTypingTarget,
describeLogHeader, productHandoffFor, phaseProviderLine, phaseUsageLine,
isRepositoryBlockedPacket, landingStateText, diagnosticLandingAction,
reviewActionLabel, formatReviewText, userVisibleDetailLine,
checkStatusIcon, storyStatusIcon, checkStatusLabel, storyStatusLabel,
storyStatusClass, compactLongText, errorMessage, detailWasRemoved,
actionName, actionConfirmationBody, sortHistoryItems,
safeCompareString, safeCompareNumber, recoveryActionLabel,
pickRecoveryActions, providerDefaultLabel, effortDefaultLabel,
modelDefaultPlaceholder, certificationDefaultLabel, certificationOptions,
certificationPolicyAllowed, jobRunSummary, describeVerificationPolicy,
executionModeHelp, planningHelp, certificationHelp, staticCertificationLabel,
configSourceLabel, titleCase, evidenceLine, canShowDiff,
diffDisabledReason, productKindHint, shortPath
```

Suggested grouping:
- `utils/state.ts` — board/landing/watcher/runtime helpers
- `utils/run.ts` — run-detail field formatters (providerLine, etc.)
- `utils/proof.ts` — artifact + proof helpers
- `utils/job.ts` — job dialog helpers (certification, planning, providerDefault, etc.)

### 3.4 State / hooks reorganization

App.tsx currently has 38 useState calls. Group into 5-6 reducers:

| Reducer | Owns |
|---|---|
| `useRunState` | selectedRunId, selectedQueuedTask, detail, logState, diffContent, artifactContent, proofContent, proofArtifactIndex, selectedArtifactIndex |
| `useInspectorState` | inspectorOpen, inspectorMode |
| `useUiState` | toast, confirm, paletteOpen, helpOpen, jobOpen, lastError, resultBanner, viewMode |
| `useDataState` | data, projectsState, projectsLoaded, bootError |
| `useFiltersState` | filters, historyPage, historyPageSize, historySort, historySortDir |
| `usePendingState` | refreshStatus, optimisticRunStates, stateFailureStreak, confirmPending, confirmError, confirmCheckboxAck |

### 3.5 Polling abstraction

Hardcoded constants `LOG_POLL_BASE_MS`, `STATE_POLL_HIDDEN_MS`, `LOG_POLL_BACKOFF_MS`, etc. Extract into `usePolling(spec, fetcher, options)` hook in `hooks/usePolling.ts`. This unifies visibility-aware polling across state, logs, and projects.

### 3.6 Other deferred polish

- Inspector tab keyboard `o` to open from a selected row (currently row click does it — `o` would also do it).
- New Job: provider/effort/model defaults in advanced section render with full descriptive labels — could be tighter.
- `?` overlay positioning on phone (currently fine but could be sheet-style).
- Performance: virtual scroll for History tab (only matters at 500+ rows).
- Light/Dark toggle in user settings (currently OS-only).
- Branded marketing screenshots for onboarding cards.

---

## 4. Constraints / non-negotiables

### 4.1 Don't break

- **All existing `data-testid`** values. Browser tests in `tests/browser/` rely on them. Changing one breaks tests.
- **Keyboard shortcuts** as documented in §2.5.
- **Public API of state hooks** — `setSelectedRunId`, `setInspectorOpen`, `setInspectorMode` must keep their signatures so the App component can call them in handlers.
- **All existing routes** — `?view=tasks`, `?view=diagnostics`, `?run=<id>`, `?historyPage=N`, etc.
- **Cmd+Enter in New Job intent** to submit (power-user shortcut).

### 4.2 Visual contract

- Top bar always visible (z-index above drawer).
- Drawer/inspector slides in from the right, sits below topbar (top: 57px).
- Drawer width 560px; inspector width 960px (capped at calc(100vw - 80px)).
- Drawer/inspector goes full-width on phones (≤640px).
- When drawer/inspector is open at ≥1100px, workspace gets right padding so content isn't clipped (no backdrop dim at desktop widths).
- Brand mark = SVG (BrandMark.tsx), never a flat letter.

### 4.3 Build pipeline

- `npm run web:typecheck` — must pass before commit.
- `npm run web:build` — produces hashed bundles in `otto/web/static/assets/`.
- `otto/web/bundle.py` checks bundle freshness against source hash; bundle must be rebuilt and committed when sources change.
- Use `pre-commit` (or just `npm run web:build`) before commits that touch `otto/web/client/src/`.

### 4.4 Backend tests

`tests/test_v3_pipeline.py`, `tests/test_cli_smoke.py`, `tests/test_logstream.py` — Python unit tests, must continue passing.

`tests/browser/` — pytest-playwright tests. Not run in this session (pytest-playwright not installed locally) but they're part of CI and important.

---

## 5. Live verification

Server is running for visual checks: **http://100.104.175.44:8889/** (Tailscale IP for `yuxuans-mac-mini`, port 8889). Background pid stored in `/tmp/otto-web-8889.log`.

Project: `acme-expense-portal` with 5 landed tasks for testing.

### Smoke flow

1. Land at the launcher (if no project selected), see hero + project list.
2. Click `acme-expense-portal` → project view loads.
3. Topbar shows brand mark, "otto" wordmark, project picker, Clean pill, Watcher: Stopped pill, New job button.
4. Tasks view shows single-table queue with 5 landed rows.
5. Press `?` → help overlay opens.
6. Press `Esc` → help closes.
7. Click a task row → drawer slides in from right, queue stays visible.
8. Inside drawer, click "Logs" → wider inspector slides over drawer.
9. Press `1` / `2` / `3` / `4` / `5` → tabs cycle.
10. Press `Esc` → inspector closes (drawer remains).
11. Press `Esc` → drawer closes.
12. Press `n` → New job command palette opens (centered, intent textarea autofocused).
13. Press `Esc` → palette closes.
14. Resize to 390px → all panels go full-width, queue drops Files/Time columns.

---

## 6. Files Codex needs to know

| File | Purpose |
|---|---|
| `otto/web/client/src/App.tsx` | God file. 8246 LOC. 199 exported helpers + main App component + 38 useState. To be reduced to ~500 LOC. |
| `otto/web/client/src/types.ts` | API types. `RunDetail`, `StateResponse`, `LandingItem`, etc. |
| `otto/web/client/src/api.ts` | API client. `api()` fetcher, `ApiError`, `friendlyApiMessage`. |
| `otto/web/client/src/styles.css` | All styles. ~3000 LOC. Uses CSS variables — the redesign tokens are at the top (`:root`). Dark mode at `~line 110`. |
| `otto/web/client/src/components/` | Extracted components. Add new ones here. |
| `otto/web/client/src/utils/format.ts` | Pure formatters. Numeric, time, byte, etc. |
| `otto/web/static/` | Built bundle (regenerated by `npm run web:build`). |
| `otto/web/bundle.py` | Bundle freshness check. Updates `otto/web/static/build-stamp.json`. |

---

## 7. Working with Claude's prior work

### 7.1 Audit / plan documents (in repo root)

- `plan-web-ui-redesign.md` — original 4-phase audit plan with sections §1–§10. Refer to this for original UX rationale.
- `plan-web-ui-impl.md` — implementation plan tracking all waves (1–9 + Phase B–F).
- `architecture.md`, `product-spec.md`, `research-*.md` — broader Otto context.
- `handoff-codex-redesign.md` — **THIS FILE**.

### 7.2 Patterns established

- **Component extraction**: helper functions exported from App.tsx; extracted components import them via `import { foo } from "../../App"`. Circular imports work because functions are only called at render time, not at module load.
- **Type extraction**: types defined locally in App.tsx are exported. Components import via `import type { BoardTask } from "../../App"`.
- **Tone-based styling**: `tone-${success|warning|danger|info|neutral}` CSS classes apply the 5-state palette uniformly. Used by `Pill`, `MetricTile`, status dots, etc.
- **CSS naming**: feature classes like `.queue-list-row`, `.run-drawer`, `.topbar-watcher`. Avoid generic names. mc-audit comments document why each rule exists.

### 7.3 Anti-patterns avoided (don't reintroduce)

- ❌ Always-visible right-rail Review Packet (was making the queue feel cluttered)
- ❌ Dark sidebar against light workspace (was the strongest "internal tool" cue)
- ❌ Kanban with 4 columns (Status / Task / Stories / Files / Time table reads cleaner)
- ❌ Multiple metric tiles for the same data (Project Overview "Current work" was duplicating Task Board counts)
- ❌ Backdrop dim covering the queue when drawer opens at desktop widths
- ❌ Helper text over 10 words (audit identified ~30% reducible copy)
- ❌ Naked sentences ending with periods on pills/chips (single words only)

---

## 8. Suggested sequence for Codex

If executing the remaining refactor end-to-end:

```
Day 1 — Easy extractions (low risk, build velocity)
1. Extract MissionFocus + helpers → components/overview/MissionFocus.tsx
2. Extract ProjectOverview + RecentActivity + RecentRunsPanel + LiveRuns → components/overview/
3. Extract SystemHealth + DiagnosticsSummary + RuntimeWarnings → components/health/
4. Extract Toolbar → components/toolbar/Toolbar.tsx
5. Extract EventTimeline + ToastDisplay + ConfirmDialog + CommandPalette → components/
6. Verify: typecheck + build + screenshot of every screen at 1440 / 1280 / 390.

Day 2 — Inspector cluster (the big one)
7. Pre-flight: search for all identifiers used by inspector cluster, verify exports.
8. Move LogState / LogStatus types to types.ts.
9. Export useDialogFocus from App.tsx.
10. Extract LogPane + DiffPane → components/inspector/LogPane.tsx + DiffPane.tsx (terminal panes, simpler).
11. Extract ProofPane + ProofProvenance + CertificationRoundTabs + ProofEvidenceContent → components/inspector/proof/.
12. Extract ProductHandoffPane + productHandoffFor → components/inspector/ProductHandoffPane.tsx.
13. Extract ArtifactPane → components/inspector/ArtifactPane.tsx.
14. Extract RunInspector (the tab wrapper) → components/inspector/RunInspector.tsx.
15. Extract RunDetailPanel + ReviewPacket + FailureSummary + DetailLine + RecoveryActionBar + ActionBar + PhaseTimeline → components/review/.
16. Verify after each step: typecheck must pass, screenshot of inspector must look identical.

Day 3 — Job + History + final cleanup
17. Extract JobDialog + PhaseRoutingFields + cert/planning helpers → components/new-job/.
18. Extract History (table) → components/history/History.tsx.
19. Extract InertEffect + LiveRegion → components/a11y.tsx.
20. Migrate state helpers from App.tsx → utils/state.ts, utils/run.ts, utils/proof.ts, utils/job.ts.
21. Group state hooks into 5-6 reducers under state/.
22. Extract polling logic → hooks/usePolling.ts.
23. Final verify: App.tsx ≤ 500 LOC, typecheck clean, build clean, all browser tests pass, visual diff matches reference screenshots.
```

Estimated effort: 2-3 days of focused work for a careful agent. Each step is mechanical but the inspector cluster has non-obvious dependencies — recommend smaller sub-batches with verification between each.

---

## 9. Quick reference: what to grep for

| Goal | Command |
|---|---|
| Find all helper exports | `grep -nE "^export function" otto/web/client/src/App.tsx` |
| Find type definitions | `grep -nE "^export (interface|type)" otto/web/client/src/App.tsx` |
| Find component definitions | `grep -nE "^export function [A-Z]" otto/web/client/src/App.tsx` |
| Find hooks at App level | `grep -nE "^  const \[" otto/web/client/src/App.tsx` |
| Find raw color hex (cleanup) | `grep -nE "(background\|color):\s*#[0-9a-fA-F]+" otto/web/client/src/styles.css` |
| Find data-testid | `grep -nE "data-testid=" otto/web/client/src/App.tsx \| head` |

---

## 10. Git state — IMPORTANT, NOTHING IS COMMITTED YET

**As of handoff, all redesign work is uncommitted in the working tree on `worktree-i2p`.**

```
Last commit on this branch: 976afd0d8  docs(mc-audit): merge guide for codex integration session
Branch: worktree-i2p (worktree at .claude/worktrees/i2p/)
Main: a240fc8b3 (already merged into worktree via fast-forward earlier in session)
```

### 10.1 Working-tree diff (uncommitted)

```
 otto/web/client/index.html                |   ~10 +   (added Inter + JBM Mono <link>)
 otto/web/client/src/App.tsx               | ~600 -   (extractions + 199 export keywords)
 otto/web/client/src/styles.css            | ~3000 +  (new tokens, layouts, dark mode, responsive)
 otto/web/static/assets/index-*.css        | regenerated bundle (CSS)
 otto/web/static/assets/index-*.js         | regenerated bundle (JS)
 otto/web/static/build-stamp.json          | regenerated
 otto/web/static/index.html                | regenerated (asset hashes)

NEW (untracked):
 otto/web/client/src/components/BrandMark.tsx               (31 LOC)
 otto/web/client/src/components/HelpOverlay.tsx             (47 LOC)
 otto/web/client/src/components/MicroComponents.tsx         (106 LOC — 8 leaf primitives)
 otto/web/client/src/components/Pill.tsx                    (20 LOC)
 otto/web/client/src/components/launcher/LauncherExplainer.tsx  (30 LOC)
 otto/web/client/src/components/launcher/ProjectLauncher.tsx    (213 LOC)
 otto/web/client/src/components/tasks/TaskQueueList.tsx         (208 LOC)
 otto/web/client/src/components/topbar/TopBar.tsx               (87 LOC)
 otto/web/client/src/utils/format.ts                            (170 LOC — 17 formatters)

 plan-web-ui-redesign.md      (audit + 4-phase plan, ~360 lines)
 plan-web-ui-impl.md          (impl plan tracking waves 1-9)
 handoff-codex-redesign.md    (this file)

Total: 7 modified + 9 new component files + 3 plan docs + regenerated bundles.
~2457 insertions / ~1158 deletions (net +1300 LOC across all sources).
```

### 10.2 Suggested commit strategy for Codex

Don't commit everything in one giant commit — break by phase so reviewers (and `git log`) can follow the redesign timeline:

```bash
# Commit 1 — Visual tokens + dark mode + Inter/JetBrains Mono fonts
git add otto/web/client/index.html otto/web/client/src/styles.css
git commit -m "feat(web): visual redesign — tokens, fonts, dark mode

- Inter for body, JetBrains Mono for code/logs.
- Lighter palette (was navy sidebar against off-white workspace).
- 5-state semantic tokens (success/warning/danger/info/neutral).
- Typographic scale tokens (display/section/subsection/meta).
- Soft chrome: 12px radius, 2-layer shadows, generous whitespace.
- Dark mode via prefers-color-scheme — same accent teal preserved.
- Responsive cascades at 1100/900/640/640px.

mc-audit redesign Phase B + D + F.
"

# Commit 2 — Layout: TopBar replacing sidebar, drawer-mode RunDetailPanel
git add otto/web/client/src/components/BrandMark.tsx \
        otto/web/client/src/components/topbar/ \
        otto/web/client/src/components/Pill.tsx
git commit -m "feat(web): replace sidebar with TopBar; RunDetailPanel becomes drawer

- BrandMark SVG (teal rounded square with arc + dot — 'iteration loop').
- Lowercase 'otto' wordmark, weight 600, -0.04em letter-spacing.
- Slim 56px top bar with brand / project picker / status pills / New job.
- Right-side drawer replaces always-visible Review Packet rail.
- Drawer slides in only when a task is selected; auto-pads workspace
  on ≥1100px so content isn't clipped.

mc-audit redesign Phase C.
"

# Commit 3 — Tasks: kanban → single-table queue
git add otto/web/client/src/components/tasks/
git commit -m "feat(web): replace kanban with single-table TaskQueueList

Linear-style ranked list (Status / Task / Stories / Files / Time).
Click a row → drawer opens. Replaces 4-column kanban TaskBoard.

mc-audit redesign Phase C.
"

# Commit 4 — Onboarding: launcher hero + LauncherExplainer
git add otto/web/client/src/components/launcher/ \
        otto/web/client/src/components/HelpOverlay.tsx
git commit -m "feat(web): launcher hero + HelpOverlay + onboarding

- Centered hero on launcher: brand mark + 'otto' + tagline.
- Project list with avatar squares, Linear-style row treatment.
- LauncherExplainer card (cost/time expectations, dismissible).
- HelpOverlay bound to '?' showing keyboard shortcuts.
- Slim 'Create new' form, hero version when no projects exist.

mc-audit redesign Phase E.
"

# Commit 5 — New Job command-palette overlay
git add otto/web/client/src/App.tsx  # JobDialog rewrite is in App still
# (defer this until JobDialog is extracted to its own file in Codex's pass)

# Commit 6 — Keyboard shortcuts (n / j / k / 1-5 / ?)
# (already in App.tsx — squashed into the App.tsx commit)

# Commit 7 — utils/format.ts + MicroComponents extraction (refactor scaffolding)
git add otto/web/client/src/utils/ \
        otto/web/client/src/components/MicroComponents.tsx
git commit -m "refactor(web): extract pure formatters to utils/, leaf primitives

- 17 pure formatters (formatDuration, humanBytes, tokenBreakdownLine…)
  → otto/web/client/src/utils/format.ts.
- 8 leaf components (MetaItem, ProjectStatCard, HealthCard, …)
  → otto/web/client/src/components/MicroComponents.tsx.
- App.tsx: 199 helper functions exposed via 'export' for future
  component extraction (no behavior change).

mc-audit redesign Wave 8 partial. Pattern established for the larger
component extraction backlog (see handoff-codex-redesign.md §3).
"

# Commit 8 — Bundle + planning docs
git add otto/web/client/src/App.tsx otto/web/static/ \
        plan-web-ui-redesign.md plan-web-ui-impl.md \
        handoff-codex-redesign.md
git commit -m "build(web): regenerate bundle; add redesign docs + Codex handoff

- Rebuild SPA bundle with all redesign changes (Inter+JBM fonts,
  topbar, drawer, queue table, dark mode, responsive).
- plan-web-ui-redesign.md — original UX audit (4 phases).
- plan-web-ui-impl.md — implementation plan tracking waves 1-9.
- handoff-codex-redesign.md — Codex handoff with refactor backlog.
"
```

If Codex prefers a **single squash commit**: that's also fine, but the message must summarize all six chunks above.

---

## 11. Design system specifics

Concrete reference values for everything Codex needs to maintain or extend.

### 11.1 Color tokens (light mode default)

```css
/* Surfaces */
--bg:           #fafbfb;   /* page background, slightly warm off-white */
--surface:      #ffffff;   /* cards, panels, drawer */
--surface-alt:  #f4f6f9;   /* alt rows, subtle backgrounds */
--line:         #e3e7ed;   /* borders, dividers */

/* Type */
--text:         #1a2231;   /* body */
--muted:        #5a6473;   /* secondary text */

/* Brand */
--accent:        #0d8b81;  /* teal, all primary CTAs */
--accent-strong: #0a6e66;  /* hover state */
--accent-soft:   #e6f4f2;  /* selected row tint, hover backgrounds */

/* States */
--blue:          #2557d4;
--green:         #15803d;
--amber:         #854d0e;
--red:           #b91c1c;

/* Semantic state palettes — every chip/banner/tag uses these */
--color-success-fg/bg/border:  #15803d / #dcfce7 / #bbf7d0
--color-warning-fg/bg/border:  #854d0e / #fef3c7 / #fde68a
--color-danger-fg/bg/border:   #b91c1c / #fee2e2 / #fecaca
--color-info-fg/bg/border:     #2557d4 / #dbeafe / #bfdbfe

/* Shadows — soft 2-layer, not flat box-shadow */
--shadow:         0 1px 2px rgba(20,28,41,.04), 0 4px 14px rgba(20,28,41,.06);
--shadow-strong:  0 8px 28px rgba(20,28,41,.10);

--focus-ring:    #1d4ed8;
--hover-bg:      #e8f3f1;
--disabled-opacity: 0.55;
```

**Dark mode** swaps neutrals to `#0d1117 / #151b23 / #1c232c / #2a3340 / #e8edf2 / #9aa5b1`, brightens semantic FGs (`#4ade80`, `#fbbf24`, `#f87171`, `#60a5fa`), and keeps the accent the same teal `#1aa89c` (slightly brightened for AA contrast on dark surfaces).

### 11.2 Typography

```css
font-family: "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
code/pre:    "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;

base size:   14px / 1.55  (was 14px/1.45 with system-ui)
letter-spacing: -0.005em on body
font-feature-settings: "cv11" on, "ss03" on  (Inter: alternate digits)
antialiased

Headings: -0.015em letter-spacing
Wordmark "otto": 18px, weight 600, -0.04em, lowercase

Type scale tokens:
  --text-xs:  12px
  --text-sm:  14px
  --text-base:16px
  --text-md:  18px
  --text-lg:  22px
  --text-xl:  28px

Semantic scale:
  --font-display:    700 28px/1.2 (page primary object — one per screen)
  --font-section:    700 22px/1.25
  --font-subsection: 600 18px/1.3
  --font-meta:       700 12px/1.2 (small-caps eyebrow labels)

  --eyebrow-letter-spacing: 0.06em
```

### 11.3 Spacing + radii

```css
Spacing (8px grid):
  --space-1: 4px   (half-grid for tight pill paddings)
  --space-2: 8px
  --space-3: 12px
  --space-4: 16px
  --space-6: 24px
  --space-8: 32px

Radii:
  --radius-sm: 4px   (inputs, chips)
  --radius-md: 8px   (buttons)
  --radius-lg: 12px  (cards, panels, drawer)
  pill: border-radius: 999px

Workspace padding: 24px 32px (capped at max-width 1280px, centered)
Card padding:      14-22px depending on density
Topbar:            12px 24px, min-height 56px, z-index 60
Drawer:            560px wide, top: 57px, z-index 50
Inspector:         min(960px, calc(100vw - 80px)), top: 57px, z-index 55
```

### 11.4 Component anatomies

#### TopBar (`components/topbar/TopBar.tsx`)
```
[ BrandMark · "otto" ] [ project-picker ▾ ] [ Clean | Watcher: Running 3s ago | New job ]
        ↑ left              ↑ middle                  ↑ right
    grid-template-columns: auto minmax(0, 1fr) auto
```
- Brand mark = 28px SVG (`BrandMark size={28}`)
- Project picker is a button styled as a dropdown trigger; clicking calls `onSwitchProject` which clears the project (returns to launcher).
- Watcher pill: tone derived from `watcher.health.state` (running=success, stale=warning, stopped=neutral). Click toggles start/stop based on state.
- `Clean` / `Dirty` repo pill always visible when project loaded.
- `New job` is the only `.primary` button.
- On ≤640px: brand wordmark hidden, status + watcher pills hidden, only mark + project picker + New job remain.

#### TaskQueueList (`components/tasks/TaskQueueList.tsx`)
```
┌─────────────────────────────────────────────────────────────────────┐
│ Tasks                                              [Land 2 ready]   │
│ 5 tasks on main                                                     │
├─────────────────────────────────────────────────────────────────────┤
│ STATUS    │ TASK                            │ STORIES│ FILES│ TIME  │
├─────────────────────────────────────────────────────────────────────┤
│ ● Landed  │ add-csv-export-for-the-…        │ 5/5    │ 4    │ 12m   │
│           │ Add CSV export for the filter…   │        │      │       │
├─────────────────────────────────────────────────────────────────────┤
│ ● Running │ add-dark-mode-then-polish-…     │ —      │ —    │ 4m    │
│           │ Add dark mode then polish ui…   │        │      │       │
└─────────────────────────────────────────────────────────────────────┘
```
- Grid columns: `140px minmax(220px, 1fr) 80px 80px 80px 100px`
- Status dot uses `tone-{success|running|warning|danger|neutral}`.
- Selected row: `--accent-soft` background + 3px teal left bar via `::before`.
- Empty hero (no tasks): centered "No tasks yet — Describe what you want…" + "Queue your first job" primary CTA.
- On ≤640px: collapses to 3 columns (Status/Task/Stories), Files/Time hidden.

#### RunDetailPanel drawer (`RunDetailPanel`, currently in App.tsx)
```
┌────────────────────────┐
│ Run detail · merged  × │  ← 12px 18px header, sticky
├────────────────────────┤
│ Already merged into…   │  ← Review Packet headline
│ build-an-expense-…     │
│                        │
│ Next: No merge…        │
│                        │
│ ┌──────┬──────┬──────┐ │  ← 4 stat tiles
│ │5/5   │10 fs │10/10│ │
│ │Stor. │Chgs  │Evid.│ │
│ └──────┴──────┴──────┘ │
│ ▾ Checks       5 rec   │  ← ReviewDrawer disclosures
│ ▾ Changed files 10 fs  │
│ ▸ View all evidence    │
│                        │
│ ▸ Run metadata · ID    │
│ ▸ Advanced run actions │
└────────────────────────┘
```
- Slides in from right with `cubic-bezier(0.32, 0.72, 0, 1)` 0.18s.
- Backdrop dim only at <1100px (overlay mode); at ≥1100px workspace shifts via `padding-right` to make room — no dim, queue stays interactive.
- Close = `×` button or click outside (or `Esc`).

#### Run Inspector (full evidence view, opens over drawer)
```
┌────────────────────────────────────────────────────┐
│ queue:add-csv-…                                    │
│ merged review                                       │
│ [Try product] [Result] [Code changes] [Logs] [Art] │
│                                          [×]      │
├────────────────────────────────────────────────────┤
│  (tab content — scrollable)                        │
└────────────────────────────────────────────────────┘
```
- Width: 960px (capped at viewport - 80px, full-width on phone).
- Tabs: 1=Try product, 2=Result (proof), 3=Code changes (diff), 4=Logs, 5=Artifacts.
- Logs pane: dark terminal styling, "Hide heartbeats" toggle (default on for terminal runs), "Jump to end" button, search box.
- Diff pane: Linear/GitHub-style file list + diff body, with hunk count + size header.
- Proof pane: rendered Markdown of proof report + provenance chip + certification rounds.
- Try product pane: launch commands in clickable `<code>` blocks + URLs + "Try this story" buttons per story.
- Artifacts pane: grouped by purpose (inputs / output / proof / debug — though grouping is partial).

#### Launcher (`components/launcher/ProjectLauncher.tsx`)
```
                    [↻]
              [BrandMark 48px]
                  otto
       Describe a feature. Otto builds,
            verifies, and lands it.

   ┌───────────────────────────────────────┐
   │ How Otto works.                    ×  │  ← LauncherExplainer (dismissible)
   │ Queue a job → Otto runs it → review… │
   └───────────────────────────────────────┘

   ┌────────────────────────────┬──────────┐
   │ Open a project   1 project │          │
   ├────────────────────────────┴──────────┤
   │ [A]  acme-expense-portal    main   →  │  ← project-row: avatar + name + path + branch + arrow
   │      /Users/yuxuan/otto-projects/…    │
   └───────────────────────────────────────┘

   ┌───────────────────────────────────────┐
   │ Create new                            │
   ├───────────────────────────────────────┤
   │ [e.g. Expense approval portal] [Create]│
   │ Stored under /Users/yuxuan/otto-proj. │
   └───────────────────────────────────────┘
```
- Centered single column, max-width 560px.
- Refresh ↻ button is a discreet 32px circle in the top-right, not a primary action.
- When `projects.length === 0`: "Open a project" section hidden, "Create new" gets `launcher-create-hero` class (taller).

#### New Job command palette (`JobDialog` in App.tsx)
```
┌────────────────────────────────────────┐
│ NEW JOB                          ×     │
│                                        │
│  Describe what you want Otto to build. │  ← intent textarea (autofocus, 18px)
│  [                                  ]  │
│                                        │
│  [Build] [Improve] [Certify]           │  ← command pills
│                                        │
│  Target acme-expense-portal on main    │  ← compact target line
│       claude · model default · …       │
│                                        │
│  ▸ Advanced options                    │  ← collapsed
│                                        │
│  ──────────────────────────────────    │
│                       [Queue job]      │  ← sticky footer
└────────────────────────────────────────┘
```
- Modal backdrop: `place-items: start center; padding-top: 16vh` (Raycast-style, doesn't cover whole screen).
- Width 640px, max-height calc(100vh - 32vh).
- Intent textarea is the dominant element: 18px, no border, transparent bg.
- Command = pills not select. Improve adds sub-mode pills (Bugs / Feature / Target).
- Cmd+Enter from textarea submits.
- 3-second grace window banner appears between submit and POST.

### 11.5 State color matrix

Every status badge/pill/chip across the app uses one of these:

| Tone | Used for | FG | BG | Border | Dot color |
|---|---|---|---|---|---|
| **success** | landed, ready, merged, done, watcher running | `#15803d` | `#dcfce7` | `#bbf7d0` | `#15803d` (with rgba ring) |
| **running** | running, starting, initializing, in_flight | `#2557d4` | `#dbeafe` | `#bfdbfe` | `#2557d4` (with rgba ring) |
| **warning** | blocked, queued, paused, interrupted, stale, dirty | `#854d0e` | `#fef3c7` | `#fde68a` | `#854d0e` |
| **danger** | failed, cancelled, error, removed | `#b91c1c` | `#fee2e2` | `#fecaca` | `#b91c1c` |
| **neutral** | idle, default, unknown | `#5a6473` | `#f4f6f9` | `#e3e7ed` | `#5a6473` |

Use `statusTone(status, stage)` from App.tsx to derive tone from API status strings.

### 11.6 Layout breakpoints

```css
@media (max-width: 1100px) — drawer overlays content (no padding-right shift); backdrop dims
@media (max-width: 900px)  — sidebar collapses to top stack (legacy code path, vestigial)
@media (max-width: 640px)  — phone layout:
  - topbar simplified (mark + project + New job only)
  - queue table 3 columns (Status/Task/Stories)
  - drawer + inspector full-width
  - launcher form vertical
  - modal nearly full-width
```

### 11.7 Animation

```css
Transitions on hover/state changes: 0.12s ease (subtle)
Drawer slide-in:    0.18s cubic-bezier(0.32, 0.72, 0, 1)  (Linear-style)
Modal fade-in:      0.16s ease-out
prefers-reduced-motion: reduce — all the above become `none`
```

### 11.8 Identity

- Name: "otto" (lowercase wordmark)
- Mark: SVG, teal rounded square (border-radius 10/40 = 25%), white "C-shape" arc (270° opening top-left) + small dot pivot at center. Suggests iteration / build loop / agent cycle.
- File: `otto/web/client/src/components/BrandMark.tsx` — accepts `size` prop in pixels.
- Used in: TopBar (28px), Launcher hero (48px). Free to use elsewhere.

---

## 12. One-line summary

**The redesign is shipped and working but uncommitted. Major delivered: top bar layout, single-table queue, on-demand right drawer, command-palette New Job, calmer palette, Inter + JetBrains Mono, custom brand mark, dark mode, full responsive. App.tsx 8859 → 8246; remaining ~7700 LOC of components to extract per §3 + §8. Pattern established. Visual contract documented in §11.**

— Claude (handoff date: 2026-04-26)
