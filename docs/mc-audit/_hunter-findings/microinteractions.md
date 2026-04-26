# Mission Control — Microinteractions Audit

Files audited:
- `/Users/yuxuan/work/cc-autonomous/.claude/worktrees/i2p/otto/web/client/src/App.tsx` (3198 lines)
- `/Users/yuxuan/work/cc-autonomous/.claude/worktrees/i2p/otto/web/client/src/styles.css` (2581 lines)

Hunting: hover/focus/click affordances, disabled-state clarity, loading-state granularity, button-after-click feedback.

---

## CRITICAL

### C1. No `:active` / depressed-state styling anywhere
- severity: CRITICAL
- effort: S
- theme: click-feedback
- location: `styles.css` (entire file — zero `:active` rules)
- problem: There is **no `:active` selector in the entire stylesheet**. Buttons, table rows, task cards, tabs, view tabs, primary CTAs — none of them visually depress, dim, or shift on mousedown. The only feedback a user gets between clicking and the network response is the `:hover` border colour they already had. On a slow network this looks like the click was lost; on a fast network it looks like the UI ignored the user. This is the single largest perceived-responsiveness gap in MC.
- concrete fix: Add a global `button:active:not(:disabled){transform:translateY(1px);filter:brightness(0.95);}` and a corresponding `.task-card-main:active`, `tbody tr:active`, `.tab:active`, `.view-tabs button:active` so every clickable surface acknowledges the press within ~16ms.

### C2. Async action buttons stay enabled during the POST — duplicate-submit risk
- severity: CRITICAL
- effort: M
- theme: click-feedback / loading
- location: `App.tsx:338-362` (`runActionForRun`), `App.tsx:391-415` (`runWatcherAction`), `App.tsx:1141-1155` (MissionFocus "Land all ready" / "Start watcher"), `App.tsx:1495` (ActionBar buttons), `App.tsx:1805-1816` (review-next-action-button), `App.tsx:613-617` (sidebar Start/Stop watcher)
- problem:
  - `runWatcherAction("start")` (App.tsx:391) executes immediately with **no `confirm` dialog** and **no pending state**. The "Start watcher" button does not disable, does not change label, does not show a spinner. A user who clicks twice during the API round-trip will fire two POSTs.
  - For actions routed through `requestConfirm` (cancel, merge, retry, cleanup), the `confirm.onConfirm` POST runs while `confirmPending` is true — which only disables the **inside-the-modal** Confirm button (App.tsx:2287). The original triggering button (e.g. review-next-action-button, ActionBar button, sidebar Stop watcher) remains visually enabled; the modal backdrop is the only thing preventing a second click, and once `setConfirm(null)` runs at the end of `executeConfirmedAction` there is still a window during `await refresh(true)` (App.tsx:355-356) where the trigger button is clickable again before fresh state arrives — so a fast user can double-fire.
  - "Land all ready" (App.tsx:1143) and the proof-pane evidence buttons (App.tsx:1701) have no per-click disabled latch.
- concrete fix: Add a per-button `inFlight` set keyed by action+runId in App-level state. Wrap each action invocation: set the key before POST, clear in `finally`. Pass an `inFlight` prop into `RunDetailPanel`, `ActionBar`, `MissionFocus`, sidebar so each button can render `disabled={inFlight}` and swap label to a working spinner. For watcher start/stop, wrap them the same way — no exemption for "fast" actions, since the failure mode is duplicate POSTs not perceived speed.

### C3. No spinner / progress indicator anywhere in the UI
- severity: CRITICAL
- effort: M
- theme: loading
- location: entire codebase — grep finds zero `spinner`, `loader`, `progress`, `@keyframes` declarations
- problem: Every loading state is communicated via plain text only:
  - JobDialog submit: button label `"Queueing"` (App.tsx:2174) — no animated indicator.
  - ConfirmDialog confirm: label `"Working"` (App.tsx:2289).
  - ProjectLauncher create: label `"Working"` (App.tsx:867).
  - LogPane: text `"waiting for output"` (App.tsx:1581).
  - DiffPane initial load: pre body literal `"Loading diff..."` (App.tsx:1735).
  - ProofPane evidence: pre body literal `"Loading evidence content..."` (App.tsx:1720).
  - Refresh status: `<span className="muted">refreshing</span>` (App.tsx:847,979) — static text.
  - Toolbar refresh button (App.tsx:980): not disabled while refreshing, no visual change.
  Without a moving indicator, users cannot distinguish "loading" from "stuck". Particularly bad for the log poll (1200ms cadence at App.tsx:334) where the pane just sits silently between polls.
- concrete fix: Add a single global `@keyframes mc-spin{to{transform:rotate(360deg)}}` and a `.spinner` utility (16×16 SVG ring, animation 0.8s linear infinite). Render a `.spinner` next to every "loading" text used above. For buttons in submitting state, replace text-only `"Queueing"` with `<><Spinner/> Queueing…</>`.

### C4. No tooltips on disabled "Diff" buttons — user can't tell why
- severity: CRITICAL
- effort: S
- theme: disabled-clarity / tooltip
- location: `App.tsx:1499` (`open-diff-button`), `App.tsx:1547` (Diff tab), `App.tsx:1690` (`proof-open-diff-button`)
- problem: The Diff button is disabled by `!canShowDiff(detail)` (App.tsx:2882), which is true when there's no branch, when there's a `diff_error`, or while the run is in progress. None of these three buttons set `title` to surface the reason. Compare this with the watcher start/stop buttons (App.tsx:616-617) and the review-next-action-button (App.tsx:1811) which **do** provide `title=...` reasons. This is an inconsistency that hides the recovery path: a user looking at a fail-branch run sees a greyed-out "Diff" button with no explanation.
- concrete fix: Compute a reason string `diffDisabledReason(detail)` returning e.g. "Branch is missing — wait for the task to create it", "Diff failed: <diff_error>", "Run still in progress". Pass as `title` on all three buttons. Same applies to ActionBar buttons that don't already have a reason (App.tsx:1953 already does this for the merge case, generalise it).

---

## IMPORTANT

### I1. JobDialog submit button has no inline validation — user discovers requirement on submit
- severity: IMPORTANT
- effort: S
- theme: click-feedback / loading
- location: `App.tsx:2019` (submitDisabled), `App.tsx:2118-2120` (intent textarea), `App.tsx:2174`
- problem: `submitDisabled` is true when `command === "build" && !intent.trim()`. The submit button is correctly disabled — but there is no inline message telling the user *why* the button is disabled or what they need to type. The textarea has placeholder "Describe the requested outcome" but no error/hint state when the user has tabbed past it without typing. The status line only fires after a submit attempt (App.tsx:2034). For dirty-target confirmation (App.tsx:2098-2107), same issue: the checkbox is offered but the submit button greys out with no inline hint about which gate is unmet.
- concrete fix: Render an `aria-live` hint immediately under the disabled submit button: "Type the build intent to enable submit." / "Confirm the dirty target above." Drive it from the same `submitDisabled` predicates. Bonus: add `aria-invalid` to the empty intent textarea after first blur, and an `aria-errormessage` linkage.

### I2. Refresh button stays clickable during refresh — duplicate fetch
- severity: IMPORTANT
- effort: S
- theme: click-feedback / loading
- location: `App.tsx:847-849` (launcher refresh), `App.tsx:979-981` (toolbar refresh)
- problem: Both Refresh buttons issue `void refresh(true)` without disabling themselves. While `refreshStatus === "refreshing"`, only the muted text label changes — the button itself is fully clickable, allowing the user to stack multiple `loadProjects()` + `state` fetches in flight. There's no AbortController plumbing in `api.ts` (assumed from usage), so racing responses can flicker the UI.
- concrete fix: Disable both buttons while `refreshStatus === "refreshing"` and replace the button label with `Refreshing…` plus a spinner (after C3 lands). Also add `aria-busy={refreshStatus === "refreshing"}` to the workspace.

### I3. Search input fires on every keystroke — no debounce, no cursor feedback
- severity: IMPORTANT
- effort: S
- theme: loading
- location: `App.tsx:962-966` (Toolbar search), `App.tsx:115` (filters state)
- problem: Each character triggers `onChange({...filters, query: ...})`. The `filters` state is in `refresh`'s dependency list (App.tsx:286), but the live keystroke does not show a refreshing indicator near the input itself. On a slow connection the filter result list flickers as queries race. There's also no clear-button affordance to cancel search beyond the global "Clear filters".
- concrete fix: Debounce filters.query with a 200ms timer before invoking `onChange`. Add a small "filtering…" microcopy beside the search input while the new state is being applied. The browser-native `type="search"` clear (✕) is OK; verify it appears in the supported browsers and don't strip it via CSS.

### I4. "Optimistic" UI is absent — every action requires full refresh round-trip before the user sees anything change
- severity: IMPORTANT
- effort: L
- theme: optimistic / click-feedback
- location: every action handler (e.g. `App.tsx:349-360`, `App.tsx:379-387`, `App.tsx:391-414`)
- problem: After a successful POST, the action handler awaits `refresh(true)` before the UI mutates. For example: clicking "Cancel" on a running task — between confirm and refresh, the task card still shows "running"; only after the polled `/api/state` returns does the status flip to "cancelled". On a 1.5s `refresh_interval_s` (App.tsx:2585) this is up to 5s of stale UI. There is no "cancelling…" intermediate state on the card.
- concrete fix: For each action, write an optimistic delta into local `data` state (e.g. set `display_status = "cancelling"`, set `landing_state = "merging"`) immediately on confirm, then let the refresh either confirm or roll back. On rollback (server error), surface a warning toast and revert the local mutation. Start small with cancel and merge — the two highest-latency user-visible actions.

### I5. `.task-card-toggle` More/Less drawer has no animation
- severity: IMPORTANT
- effort: S
- theme: animation
- location: `App.tsx:1258-1275`, `styles.css:679-717`
- problem: The drawer appears/disappears instantly with no height transition. With multiple cards open/closed in sequence, the layout jumps abruptly, making it hard for the eye to follow which card was just toggled. The toggle button has no chevron — only text "More"/"Less" — so there is no rotating-icon affordance.
- concrete fix: Use CSS grid `grid-template-rows: 0fr` → `1fr` transition on `.task-card-drawer` (modern browsers support animatable `1fr`). 200ms ease-out. Add a chevron SVG rotated 0deg/180deg via a transition.

### I6. Cancel button in ConfirmDialog uses generic styling regardless of `tone`
- severity: IMPORTANT
- effort: S
- theme: disabled-clarity
- location: `App.tsx:2287-2290`, `styles.css:99-107`
- problem: When `confirm.tone === "danger"` the *confirm* button gets `.danger-button` styling (red), but the *cancel* button is identical to a non-danger cancel. Worse, in a danger flow (e.g. "Stop watcher? Running tasks will be interrupted.") the visually red button is the destructive action — but a panicked user pressing Esc or clicking the close-X header button has no visual reinforcement that "Cancel" is the safe choice. The header has both a "Close" button (App.tsx:2283) and footer "Cancel" (App.tsx:2287) — two ways to abort, neither emphasised.
- concrete fix: For danger-tone dialogs, explicitly style the Cancel button with subtle outline emphasis (border-2, slightly bolder) and add a hint above the footer: "Press Esc to cancel." Remove the redundant header Close button when the same dialog has a footer Cancel — two close affordances in a confirm dialog dilute focus.

### I7. Diff tab in inspector is disabled but uses identical styling to enabled tabs
- severity: IMPORTANT
- effort: S
- theme: disabled-clarity
- location: `App.tsx:1547`, `styles.css:1619-1634`
- problem: `.tab:disabled` falls back to the global `button:disabled` (`color: #9aa4b2; background: #eef1f5;`). But `.tab` overrides `background: var(--surface-alt);` and there's no explicit disabled override for `.tab`, so disabled tabs look almost identical to inactive tabs — operators may not realise the Diff tab is disabled vs simply not the active one.
- concrete fix: Add `.tab:disabled { color: #c1c8d4; background: repeating-linear-gradient(...); cursor: not-allowed; }` plus a `title` attribute giving the reason (see C4).

### I8. Toast auto-dismisses after 3.2s — no pause-on-hover, not dismissible
- severity: IMPORTANT
- effort: S
- theme: tooltip / loading
- location: `App.tsx:172-176`, `styles.css:2170-2195`
- problem: `showToast` schedules `setTimeout(... 3200)` unconditionally. There is no hover-to-pause, no manual dismiss button, no queue management — a second toast within 3.2s replaces the first silently. Errors that are also written to `lastError` survive (visible in the status banner) but warnings (e.g. "No land-ready tasks", App.tsx:373) disappear with no recovery. Operators glancing away miss them.
- concrete fix: Track toast timer in a ref, clear on `mouseenter`, restart on `mouseleave`. Add a small ✕ inside the toast for manual dismiss. Bump duration to 5s for warning, 8s for error (already-redirected to banner, but still — the toast should give people time to read).

---

## NOTE

### N1. Hover affordance on table rows is correct, but `cursor: pointer` is set on empty-cell rows too
- severity: NOTE
- effort: S
- theme: cursor / hover
- location: `styles.css:1109-1116` (`tbody tr { cursor: pointer; }`)
- problem: The cursor:pointer rule applies unconditionally to any `tbody tr` — including the empty-state row (`<tr><td colSpan=6 className="empty-cell">No live runs.</td></tr>`, App.tsx:1368). Hovering the empty state shows a hand cursor and the same `#e8f3f1` highlight, suggesting interactivity that doesn't exist.
- concrete fix: Scope to clickable rows: `tbody tr[role="button"] { cursor: pointer; }` and remove the cursor / hover background on empty-cell rows.

### N2. `Clear filters` button stays clickable when filters are already default
- severity: NOTE
- effort: S
- theme: disabled-clarity
- location: `App.tsx:976`
- problem: Clicking "Clear filters" when the state is already the default does nothing user-visible but still triggers a re-render and refresh debounce. Minor but it makes the button feel inert.
- concrete fix: `disabled={JSON.stringify(filters) === JSON.stringify(defaultFilters)}` with a `title="Filters are already at default."` while disabled.

### N3. View tabs and sidebar buttons lack tooltips for keyboard users
- severity: NOTE
- effort: S
- theme: tooltip
- location: `App.tsx:919-936` (Tasks/Diagnostics tabs), `App.tsx:613-615` (sidebar Switch project / New job)
- problem: These buttons rely on visible label text, which is fine for sighted/mouse users — but the watcher controls *do* set `title=` and these don't, creating an asymmetry. Operators who use hover-tooltips as a shortcut to learn behaviour get nothing here. Particularly the "Switch project" button has no hint about whether unsaved work in the current project will be lost.
- concrete fix: Add `title="Switch to a different managed project. Queued runs continue in the background."` to the switch-project button. View tabs can stay as-is, but consider `title="Mission Control task workflow"` / `title="Operational diagnostics, runtime issues, run history"` for at-a-glance hints.

### N4. `.task-card-toggle` button is *inside* the `.task-card`, but its hover isn't visually distinguished from the main card hover
- severity: NOTE
- effort: S
- theme: hover
- location: `App.tsx:1258-1266`, `styles.css:679-685`
- problem: The "More/Less" toggle button is a small chip below the main click target. Hovering it triggers the global `button:hover { border-color: var(--accent); }` — but because `.task-card-toggle` has `min-height: 26px` and small padding, the accented border looks busy. There's no visual cue that hovering the toggle won't select the run; users may worry that clicking the toggle area changes selection.
- concrete fix: Give `.task-card-toggle:hover` a subtle `background: rgba(15, 118, 110, 0.08); border-color: transparent;` so the toggle reads as a tertiary affordance, not a primary CTA.

### N5. "Land all ready" CTA appears in MissionFocus even when watcher is stopped — no contextual disabled reason
- severity: NOTE
- effort: S
- theme: disabled-clarity
- location: `App.tsx:1143`, `App.tsx:2803-2805` (`canMerge`)
- problem: `disabled={!canMerge(...)}` — but no `title=` attribute. When the user sees "Land all ready" greyed out, the only place the reason surfaces is via `mergeBlockedText` *inside* `runActionForRun` — which only triggers if they could click. A disabled button gives no surface for the reason.
- concrete fix: `title={canMerge(...) ? "" : (mergeBlockedText(landing) || "No land-ready tasks.")}`.

### N6. Inspector modal Close button has no danger styling, but can lose unsaved scroll state in log/diff viewers
- severity: NOTE
- effort: S
- theme: focus-restore / animation
- location: `App.tsx:1551`, `App.tsx:2696-2751` (`useDialogFocus`)
- problem: `useDialogFocus` correctly restores focus to the previously-focused element. However, the inspector body (LogPane, DiffPane, ArtifactPane) holds significant scroll state. Closing the inspector and re-opening for the same run resets the scroll to the top — frustrating during long log review. There's also no transition on inspector open/close (it appears/disappears).
- concrete fix: (1) Persist `scrollTop` per (runId, mode) in a `useRef<Map>`; restore on remount. (2) Add a 160ms fade+slide-from-right transition: `.run-inspector { opacity:0; transform:translateX(8px); transition:opacity .16s, transform .16s; } .run-inspector[data-open] { opacity:1; transform:none; }` driven by an `inspectorOpen` data attribute.

### N7. Long-press / hold-to-confirm not used for any destructive action
- severity: NOTE
- effort: M
- theme: click-feedback
- location: any cancel / cleanup / stop-watcher action
- problem: Every destructive action goes through a modal confirm — which is fine, but operators repeating the same action (e.g. cancelling several stale runs) tap through "Cancel run? → Cancel" four times in a row, defeating the safety. There's no faster-but-still-safe path. (This is a feature note, not a bug.)
- concrete fix: Optional — for repeated-same-action workflows, allow a `Shift+click` shortcut that bypasses confirm but shows a 3-second undo toast (Gmail-style "Cancelled. Undo"). Lower priority than the rest of the list.

### N8. No focus ring shift after action completes — focus drops to body if the trigger button unmounts
- severity: NOTE
- effort: S
- theme: focus-restore
- location: `App.tsx:204-215` (`executeConfirmedAction`)
- problem: After a successful action, `setConfirm(null)` runs and `useDialogFocus` restores focus to the previously-focused element — *unless that element has unmounted*, which often happens (e.g. a "Land task" button in the review packet disappears once readiness flips to "merged"). In that case `previousFocus.isConnected` is false, the restore branch is skipped, and focus falls back to `<body>` — keyboard users lose context.
- concrete fix: When the previously-focused element is no longer connected, fall back to focusing the next semantic landmark (the panel heading the action came from). Could be done by passing an explicit `focusFallback` ref through `requestConfirm`.

### N9. Empty-state copy is bare text, no illustrative hint
- severity: NOTE
- effort: S
- theme: loading
- location: `App.tsx:1505` (`Select a run.`), `App.tsx:1220` (column empty), `App.tsx:1505` (detail panel empty)
- problem: When no run is selected, the detail panel shows just "Select a run." in `.empty` muted style. New operators have no signal *what* to do — there's no arrow-style hint like "Pick a card from the Task Board to load its review packet." Same for "No queued or running tasks." — operators don't know whether to start the watcher or queue a job.
- concrete fix: Two-line empty states: bold action ("Pick a task"), muted help ("Select a card from the board to load its review packet, evidence, and diff."). For the queue empty state, link to the New Job CTA explicitly.

### N10. ProofPane evidence buttons hover state borrows global button hover (accent border) — looks identical to selected state
- severity: NOTE
- effort: S
- theme: hover / disabled-clarity
- location: `App.tsx:1701`, `styles.css:1860-1872`
- problem: `.proof-artifacts button.selected` uses `border-color: var(--accent); background: #ecfdf5;`. The global `button:hover:not(:disabled)` only sets `border-color: var(--accent);` — so hovering an unselected artifact gives the same green border as the selected one (background still differs, but it's subtle). On dense lists, users second-guess which one is selected.
- concrete fix: `.proof-artifacts button:hover:not(.selected) { border-color: var(--accent); background: var(--surface-alt); }` with a 1-line transition.

### N11. Watcher Start button doesn't latch — repeated rapid clicks during the start window queue multiple POSTs
- severity: NOTE (subset of C2 but different code path; called out separately because watcher start has no confirm gate)
- effort: S
- theme: click-feedback
- location: `App.tsx:391-414`
- problem: `runWatcherAction("start")` skips the confirm dialog. The button has `disabled={!canStartWatcher(data)}` which only re-evaluates after the *next* refresh — so during the in-flight POST the button is still considered "can start" and remains clickable.
- concrete fix: Same fix as C2 — local in-flight latch keyed by action.

### N12. No "scroll-into-view" when selecting a run from RecentActivity / DiagnosticsSummary
- severity: NOTE
- effort: S
- theme: animation
- location: `selectRun` at `App.tsx:187-202`; selection sources at App.tsx:1108, 1310
- problem: Clicking a run in RecentActivity (bottom-of-screen) sets `selectedRunId` — but the RunDetailPanel on the right doesn't scroll into view if the user is on a small viewport. The detail panel becomes the source of truth but the user's eye is still on the activity row.
- concrete fix: After `setSelectedRunId`, call `document.querySelector('[data-testid="run-detail-panel"]')?.scrollIntoView({behavior:"smooth", block:"nearest"})`. Cheap, effective on narrow layouts.

### N13. ProjectLauncher project rows lack hover affordance beyond global button hover
- severity: NOTE
- effort: S
- theme: hover
- location: `App.tsx:888-898`, `styles.css:294-325`
- problem: `.project-row` is a button. Hover gets the generic `border-color: var(--accent);` from the global rule — but nothing else changes (no background, no shadow). Combined with the row already having a code path / pill cluster, the hover signal is faint.
- concrete fix: Add `.project-row:hover:not(:disabled) { background: #f8fafc; box-shadow: inset 3px 0 0 var(--accent); }` for a clearer "this is the click target" cue.

### N14. ConfirmDialog "Working" button label is the only thing that changes during pending — no spinner, no progress
- severity: NOTE (covered by C3 broadly, called out for the highest-frequency dialog)
- effort: S
- theme: loading
- location: `App.tsx:2289`
- problem: While `pending` is true, the dialog's confirm button reads "Working" and is disabled. No spinner. For long-running merges (POST /api/actions/merge-all could take several seconds), the user has no indication the request is alive vs hung. The cancel/close buttons are also disabled, leaving the user staring at a frozen dialog.
- concrete fix: Render `<><Spinner/> Working…</>` once C3's spinner exists. Also: enable the Close button after, say, 8 seconds and surface a "Still working — close to dismiss the dialog (action will continue in background)" hint.

---

## Summary counts

| severity | count |
|---|---|
| CRITICAL | 4 |
| IMPORTANT | 8 |
| NOTE | 14 |
| **TOTAL** | **26** |

| theme | count |
|---|---|
| click-feedback | 6 |
| loading | 7 |
| disabled-clarity | 6 |
| hover | 4 |
| tooltip | 2 |
| cursor | 1 |
| focus-restore | 2 |
| animation | 3 |
| optimistic | 1 |

(Some findings are tagged with multiple themes; counts above reflect primary theme.)

## Top three concrete recommendations (in priority order)

1. **Add `:active` styling globally + per-button in-flight latch** (C1 + C2). Together these are ~40 lines of CSS and ~30 lines of TS state. Eliminates the entire class of "did my click register?" and "did I just double-fire merge-all?" bugs.
2. **Ship a single `<Spinner/>` component and replace text-only loading states** (C3). Touches roughly 12 sites; uniformly improves perceived progress.
3. **Add `title=` reasons to every disabled async control** (C4 + I7 + N5). One-line changes. Without this, operators silently work around blocked CTAs instead of fixing the upstream cause.
