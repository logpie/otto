# Hunter findings — keyboard-only operation

Audit target: `otto/web/client/src/App.tsx` (3198 lines), `otto/web/client/src/styles.css` (2581 lines).
Scope: keyboard-only reachability of every flow in `docs/mc-audit/user-flows.md`, missing power-user hotkeys, focus-management bugs (focus trap, focus cycle, tab skip, modal dismiss).

---

## Per-flow keyboard reachability matrix

Each row: can a keyboard-only user accomplish the flow with no mouse?

| Flow | Can keyboard-only user complete? | Path | Caveats |
|------|----------------------------------|------|---------|
| **F1 cold start (launcher)** | YES | `<input>` is auto-focused (App.tsx:861), Tab → "Create project" button → project rows (`<button class="project-row">`, App.tsx:888) → Enter | Refresh button reachable via Tab. Auto-focus on `name` input means screen-reader users land in the right place. |
| **F6 submit build (happy)** | YES | Tab to `new-job-button` (App.tsx:615) → Enter → JobDialog opens with `useDialogFocus` putting focus on first focusable (the Close button — see Bug K-08) → Tab through fields → Enter on textarea is NOT submit (good) → Tab to `Queue job` → Enter | The first focusable in JobDialog is "Close" (App.tsx:2080), not the Command select or Intent textarea. Wastes 4–5 Tab presses every dialog open. |
| **F12 cancel run** | PARTIAL | Tab to task card → Enter → Tab to `<details>Advanced run actions</details>` summary → Enter to expand → Tab to cancel button → Enter → ConfirmDialog focuses Close button first → Tab to Cancel/Confirm | Works but indirect: there's no dedicated keyboard shortcut for the most common destructive action. The "Working" state of the confirm button (App.tsx:2289) re-renders the label, which on some screen readers re-announces the entire dialog. |
| **F14 merge** | YES | Tab to `review-next-action-button` (App.tsx:1809) → Enter → ConfirmDialog → Tab to confirm → Enter | OR `Land all ready` in MissionFocus → Enter → Confirm. Works but no `m` shortcut. |
| **F17 open run detail (inspector tabs)** | YES | Tab to `open-proof-button` (App.tsx:1498) → Enter → inspector opens, focus moves inside → Tab cycles through Proof/Diff/Logs/Artifacts tabs → Enter activates tab → Escape closes | Tab order across tabs is sequential (Tab through every tab button before reaching content). There is NO arrow-key navigation inside the `role="tablist"` — this VIOLATES WAI-ARIA tablist pattern (Left/Right should switch tabs, see Bug K-04). |
| **F22 watcher start** | YES | Tab to `start-watcher-button` (App.tsx:616) → Enter | Reachable. |
| **F26 server outage / reload — recovery** | PARTIAL | Refresh button is reachable via Tab in toolbar (App.tsx:980). User can press Enter. | But there is no keyboard shortcut to retry/refresh (e.g. `r` or browser-style F5 hint). The status banner dismiss buttons (App.tsx:1165, 1172) are reachable via Tab but require many presses past the entire toolbar/sidebar. |

**Verdict:** every flow is reachable keyboard-only. Several flows are *tediously* reachable (no skip-links, no power-user hotkeys, deep Tab counts before reaching the action). The app meets baseline accessibility but completely misses the "faster than mouse for repeated tasks" bar.

---

## Findings (ordered: CRITICAL → IMPORTANT → NOTE)

### K-01 — Inspector dialog does not aria-hide the background while open

- **severity:** IMPORTANT
- **effort:** S
- **theme:** focus-trap
- **location:** `otto/web/client/src/App.tsx:503` (definition of `modalOpen`); `:603, :621` (aria-hidden binding); `:662–684, :712–734` (RunInspector render sites); `:1529` (RunInspector uses `useDialogFocus`).
- **problem:** `modalOpen = jobOpen || Boolean(confirm)` excludes `inspectorOpen`. RunInspector declares `role="dialog" aria-modal="true"` and installs a Tab/Shift+Tab trap via `useDialogFocus`, but the surrounding `<aside class="sidebar">` and `<main class="workspace">` are NOT given `aria-hidden=true` while it is open. Two concrete consequences:
  1. The focus trap is implemented as a `keydown` handler on the dialog element only — if the user clicks (or programmatic focus from a polling re-render lands) on a background element, focus escapes the "modal" without closing it. Tab then walks through the background.
  2. Screen readers, AT browsers, and non-Tab keyboard navigation tools (e.g. Vimium-style) still see the workspace + sidebar as live, contradicting `aria-modal="true"`.
- **concrete fix:** change `App.tsx:503` to `const modalOpen = jobOpen || Boolean(confirm) || inspectorOpen;`. The existing aria-hidden binding will then cover inspector too. Optionally also add a top-level `keydown` capture inside `useDialogFocus` (instead of the current dialog-scoped listener) so focus that escapes the dialog is force-returned on next Tab.

### K-02 — Focus trap installs only one keydown listener (capture-phase) on the dialog; clicks outside escape silently

- **severity:** IMPORTANT
- **effort:** S
- **theme:** focus-trap
- **location:** `otto/web/client/src/App.tsx:2696–2748` (`useDialogFocus`).
- **problem:** The keydown listener is attached to the dialog root (`dialog.addEventListener("keydown", onKeyDown)`). It only fires for keypresses while focus is *inside* the dialog. If the user mouse-clicks any visible background element (sidebar buttons remain clickable because workspace/sidebar lack `pointer-events:none` and lack `aria-hidden` per K-01), focus moves out. Subsequent Tabs traverse the background without ever being recaptured. The trap is non-monotonic.
- **concrete fix:** install the listener on `document` (capture phase) and check `if (!dialog.contains(event.target))` — if focus has escaped, `event.preventDefault()` and refocus the dialog's first element. Alternative: render dialogs into a portal at `<body>` root and add `pointer-events:none` + `aria-hidden=true` to the rest. Either works; portal is the React-idiomatic answer.

### K-03 — Modal first-focused element is "Close", not the primary input

- **severity:** IMPORTANT
- **effort:** S
- **theme:** focus-cycle
- **location:** `otto/web/client/src/App.tsx:2080` (JobDialog header Close button rendered before any field); `:2283` (ConfirmDialog header Close); `:2714` (`useDialogFocus` picks first match of `focusableDialogElements`).
- **problem:** `focusableDialogElements` selects in DOM order, so the header `Close` button is index 0 in both JobDialog and ConfirmDialog. Every modal open immediately puts focus on a dismiss control — keyboard users must Shift+Tab back (going past the textarea, past selects) or forward through the entire form to reach the action. For a power user who opens `New job` ten times a day, this is ~50 wasted keystrokes.
- **concrete fix:** in `useDialogFocus`, accept an optional `initialFocusSelector` arg. JobDialog passes `'textarea'` (the intent box), ConfirmDialog passes `'.danger-button, .primary'` (the action). Fall back to the existing first-focusable behavior. Alternatively, mark the header Close with `data-skip-initial-focus` and filter it out of the search.

### K-04 — Inspector tablist does not support arrow-key navigation (WAI-ARIA tablist violation)

- **severity:** IMPORTANT
- **effort:** S
- **theme:** missing-hotkey
- **location:** `otto/web/client/src/App.tsx:1545–1550` (`<div role="tablist">` containing four `<button role="tab">` Proof/Diff/Logs/Artifacts).
- **problem:** Per the WAI-ARIA Authoring Practices for the Tabs pattern, a `role="tablist"` MUST handle Left/Right (and ideally Home/End) arrow keys to move between tabs. Mission Control's inspector tablist requires Tab+Tab+Tab+Tab to move between four tabs; there is no roving tabindex either. This is both a hotkey miss and a screen-reader compliance bug.
- **concrete fix:** add an `onKeyDown` to the tablist container that handles `ArrowLeft`/`ArrowRight`/`Home`/`End`, finds enabled tab buttons, and calls the corresponding `onShow*` handler + `.focus()`. Set `tabIndex` on tab buttons via roving-tabindex (active tab `tabIndex=0`, others `tabIndex=-1`).

### K-05 — No global keyboard shortcuts at all (`?`, `/`, `j/k`, `g g`, `b`, `m`, `c`, Cmd-K)

- **severity:** IMPORTANT
- **effort:** M
- **theme:** missing-hotkey
- **location:** entire `otto/web/client/src/App.tsx` — `grep -n metaKey|ctrlKey|altKey App.tsx` returns zero matches. Confirmed in `docs/mc-audit/client-views.md:49–55` ("There are no global keyboard shortcuts").
- **problem:** The app is built for repeated daily use (queue jobs, watch runs, merge, cancel). It has zero power-user shortcuts. A daily user trying to drive the queue keyboard-only must Tab past 10–20 elements every time. There is no command palette, no row navigation (`j/k`), no quick-action key (`b` for build, `m` for merge, `c` for cancel), no help overlay (`?`).
- **concrete fix:** add a single top-level `useEffect` in `App` that installs a `document.keydown` listener with this minimum set:
  - `?` → open help/cheatsheet modal listing all shortcuts.
  - `/` → focus the toolbar Search input (App.tsx:961). Already a `<input type=search>`, just needs a ref + `.focus()`.
  - `g t` / `g d` → navigate Tasks / Diagnostics view (chord for view switch).
  - `j` / `k` → next / previous task card (within currently visible TaskBoard column or all rows).
  - `Enter` on a focused row → select run (already works via TaskCard button; document it).
  - `b` → open `new-job-button` (App.tsx:615). Equivalent of `JobDialog` open.
  - `m` → trigger `review-next-action-button` (App.tsx:1809) when label is "Land/Merge", OR `Land all ready` (App.tsx:1143). Pick the more selective one based on `selectedRunId`.
  - `c` → trigger Cancel action on the selected run if `legal_actions` includes cancel; otherwise no-op + brief toast.
  - `Escape` (already handled inside dialogs) — extend to blur any focused input on the page.
  Skip handler when `event.target` is `<input>`/`<textarea>`/`<select>` to avoid hijacking typing. This is a self-contained change of ~80–120 lines, no library dependency required.

### K-06 — No command palette (Cmd-K / Ctrl-K)

- **severity:** NOTE
- **effort:** M
- **theme:** missing-hotkey
- **location:** N/A (not implemented).
- **problem:** Mission Control has ~20 distinct actions (queue build, queue improve, queue certify, switch project, start/stop watcher, refresh, clear filters, open diagnostics, land all ready, retry run, cancel run, cleanup run, resume run, open proof/diff/logs/artifacts, dismiss banners). A Cmd-K palette listing them by name with fuzzy filter would let power users issue any action in 3–4 keystrokes, regardless of where focus currently is. Stripe, Linear, GitHub, VS Code all do this; users expect it.
- **concrete fix:** add a `CommandPalette` component (Cmd/Ctrl-K opens it, Esc closes, Up/Down navigates, Enter executes). Each entry maps to one of the existing callbacks already wired in `App` (`openJobDialog`, `mergeReadyTasks`, `runWatcherAction`, `runActionForRun`, `navigateView`, etc.). Estimate ~150 LOC. Defer if K-05 ships first — palette needs the same shortcut infra.

### K-07 — Search input is not reachable via a hotkey; Tab path through is long

- **severity:** IMPORTANT
- **effort:** S
- **theme:** missing-hotkey
- **location:** `otto/web/client/src/App.tsx:961` (Search input in Toolbar).
- **problem:** To filter runs by query, a keyboard user must Tab through: the entire sidebar (Switch project, New job, Start watcher, Stop watcher) → the Toolbar's Tasks/Diagnostics tabs → Type select → Outcome select → THEN Search. Roughly 8 Tab presses. There is no `/` shortcut.
- **concrete fix:** part of K-05. Bind `/` (when not in input) to focus the search input. Also expose a top-of-page skip-link "Skip to search" (see K-09).

### K-08 — Native `<details>` advanced-actions hides Cancel/Cleanup behind an extra Tab + Enter

- **severity:** NOTE
- **effort:** S
- **theme:** missing-hotkey
- **location:** `otto/web/client/src/App.tsx:1945` (ActionBar uses `<details>`); `:2121` (JobDialog uses `<details>` for advanced options).
- **problem:** Cancel and Cleanup are critical actions (Flows 12, 14) but they live behind an extra Tab to a `<details>` summary, then Enter to expand, then Tab to the action button. For a destructive operation that's defensible. But there's no hotkey alternative, and the user has no idea the actions exist until they expand.
- **concrete fix:** part of K-05 (`c` for cancel, etc.). Additionally consider `defaultOpen` on `<details>` when `legal_actions` includes a destructive action AND the run is selected — pre-expand so it's visible on Tab order.

### K-09 — No skip-link to bypass the sidebar / toolbar

- **severity:** IMPORTANT
- **effort:** S
- **theme:** tab-skip
- **location:** `otto/web/client/src/App.tsx:601–619` (sidebar) — first focusable on the page is `switch-project-button`, then `new-job-button`, then watcher buttons, then toolbar tabs, then filters. Roughly 8–10 Tab presses to reach a task card. `client-views.md` line 49 explicitly notes "No keyboard shortcuts beyond Tab/Escape/Enter-Space-on-row".
- **problem:** WCAG 2.4.1 (Bypass Blocks) recommends a skip-link as the very first focusable element on a page when there's repeated nav. Otto's sidebar repeats on every render and is the entire left rail; the user always has to Tab past it.
- **concrete fix:** at the top of `<div className="app-shell">`, add `<a href="#task-board" className="skip-link">Skip to task board</a>` styled to be visually hidden until `:focus`. Add a matching `id="task-board"` on the TaskBoard `<section>` (App.tsx:1197 — already has `data-testid="task-board"`, just add `id`).

### K-10 — Activity panel mixes non-focusable event divs and focusable history buttons

- **severity:** NOTE
- **effort:** S
- **theme:** tab-skip
- **location:** `otto/web/client/src/App.tsx:1297–1318` (RecentActivity).
- **problem:** Recent Activity renders both event entries (plain `<div>`, not focusable) and history entries (`<button>`, focusable) intermixed. A keyboard user Tabs through the focusable subset only and may miss event context (severity, message) that is visually adjacent. Also: the events themselves are not actionable, but they DO contain run-target IDs in the timeline (App.tsx:1444 EventTimeline) — keyboard users cannot jump from event to its run.
- **concrete fix:** add `tabIndex={0}` + `role="button"` + the same `selectOnKeyboard` pattern used elsewhere for any timeline/activity item that has a run target, so Enter/Space jumps to the run. For purely informational events, leave them non-focusable (correct).

### K-11 — Watcher Stop button has identical `disabled` + `title` semantics that cannot be read keyboard-only

- **severity:** NOTE
- **effort:** S
- **theme:** missing-hotkey
- **location:** `otto/web/client/src/App.tsx:616–618`.
- **problem:** When `start-watcher-button` is disabled, the reason lives in `title` attribute (`watcher?.health.next_action || ""`). `title` only surfaces on mouse hover. A keyboard-only user focuses the button via Tab and gets no tooltip. `aria-describedby="watcher-action-hint"` points at a sibling `<p>` (App.tsx:618) which contains a more generic hint, not the specific `start_blocked_reason`.
- **concrete fix:** when disabled, render the `start_blocked_reason` text either inline in `#watcher-action-hint` (so AT reads it) or via `aria-label` that includes the reason. `title` on a disabled button is invisible to keyboard users.

### K-12 — `merge_blocked` reason on review-next-action / merge buttons hidden in `title` only

- **severity:** NOTE
- **effort:** S
- **theme:** missing-hotkey
- **location:** `otto/web/client/src/App.tsx:1811` (review-next-action title), `:1951` (ActionBar title), `:2808` (`mergeButtonTitle`).
- **problem:** Same root cause as K-11. The "Commit, stash, or revert local project changes before merging." explanation (App.tsx:2808) is in `title` only. Keyboard users tabbing onto a disabled merge button get no AT readout.
- **concrete fix:** add `aria-describedby` to a sibling element that always renders the merge-blocked reason text, OR render the reason as visible secondary text under the button.

### K-13 — Diff file list lacks roving-tabindex / arrow nav

- **severity:** NOTE
- **effort:** S
- **theme:** missing-hotkey
- **location:** `otto/web/client/src/App.tsx:1745–1758` (`<nav class="diff-file-list">` with one button per changed file).
- **problem:** With 20+ changed files, walking through them keyboard-only is one Tab per file. Standard pattern: arrow keys (Up/Down or J/K) move within the file list, Tab leaves the list. Currently each file button is an independent Tab stop, and they're rendered before the diff `<pre>` so a user has to Tab through ALL files to reach the diff content.
- **concrete fix:** add `onKeyDown` handler on the `<nav>` for `ArrowDown`/`ArrowUp`/`Home`/`End`, set roving tabindex on the buttons (only the active one is `tabIndex=0`). Tab then escapes to the diff `<pre>` after one stop.

### K-14 — `Reduced motion` contract not enforced (no `@media (prefers-reduced-motion: reduce)`)

- **severity:** NOTE
- **effort:** S
- **theme:** focus-cycle (adjacent — affects keyboard users with vestibular triggers)
- **location:** `otto/web/client/src/styles.css` — `grep -n prefers-reduced-motion` returns zero matches. Flow 39 expects this honored.
- **problem:** Flow 39 in `user-flows.md` asserts that reduced-motion users see no transitions. Today, `styles.css` has no `prefers-reduced-motion` media block at all. Per `client-views.md:53`, transitions are limited to hover/focus color shifts, so practical impact is small — but the contract is unmet, and any future animation regresses it silently.
- **concrete fix:** at end of `styles.css`, add:
  ```css
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }
  ```

### K-15 — Focus-visible outline color is the same as `--accent`; insufficient contrast on disabled buttons

- **severity:** NOTE
- **effort:** S
- **theme:** focus-cycle
- **location:** `otto/web/client/src/styles.css:80–87` (`outline: 3px solid #2563eb`).
- **problem:** The focus ring uses `#2563eb`, the same color as `.primary` background (App.tsx:91 `background: var(--accent)` = `#2563eb`). When a primary button (e.g. `Queue job`, `Land all ready`) is focused, the outline is barely distinguishable from the button background. Keyboard-only users lose track of focus on the primary CTAs — exactly the elements they're trying to find.
- **concrete fix:** change focus-visible outline to a high-contrast color that sits on top of any background — e.g. `outline: 3px solid #fff; box-shadow: 0 0 0 5px #2563eb;` (white inner ring, blue outer halo). Or pick a yellow/orange focus accent that is distinct from both `--accent` and `--red`.

### K-16 — Toolbar filter "Active" checkbox label has no Tab focus on the input itself

- **severity:** NOTE
- **effort:** S
- **theme:** focus-cycle
- **location:** `otto/web/client/src/App.tsx:968–975` (`<label class="check-label">` wraps the checkbox).
- **problem:** The checkbox is wrapped by `<label>` with no `htmlFor`, so Tab does land on the checkbox input itself (browser default), but the focus ring is rendered around the input only — visually narrow, easy to miss given the surrounding white space. Adjacent control are full-width selects with much more prominent focus rings.
- **concrete fix:** make the check-label itself the focusable target via `:focus-within` styling, OR add a `:focus-visible` selector that applies a ring to the parent label. CSS-only fix.

### K-17 — Toast region not announced when error is surfaced via the `lastError` banner

- **severity:** NOTE
- **effort:** S
- **theme:** modal-dismiss
- **location:** `otto/web/client/src/App.tsx:762` (toast `aria-live="polite"`); `:1161–1167` (`lastError` banner — no `aria-live`, no `role`).
- **problem:** When an action fails, `setLastError(message)` populates the `MissionFocus` "Last error" banner. The banner has no `role="alert"` / `aria-live`. Screen readers do not announce it. The toast IS announced (App.tsx:762 `role="status" aria-live="polite"`) but disappears in 3.2s (App.tsx:175). Keyboard users without sight will miss persistent errors.
- **concrete fix:** add `role="alert"` (or `aria-live="assertive"`) to the `<div class="status-banner error">` at App.tsx:1162 and the result-banner at :1169.

### K-18 — Confirm dialog `Working` label change re-announces the dialog

- **severity:** NOTE
- **effort:** S
- **theme:** modal-dismiss
- **location:** `otto/web/client/src/App.tsx:2289` (`{pending ? "Working" : confirm.confirmLabel}`).
- **problem:** When user presses Enter on the confirm button, the button's text content changes (`confirmLabel` → "Working"). Some screen readers re-announce the entire dialog because of the DOM mutation inside an `aria-modal` region. Repeated rapid clicks cause flicker.
- **concrete fix:** keep the button label stable; use `aria-busy="true"` + an adjacent `<span class="visually-hidden" aria-live="polite">Working</span>` for the announcement instead of replacing the label. Or keep the label and add a small spinner glyph.

### K-19 — Escape inside JobDialog does NOT reset partially-typed intent

- **severity:** NOTE
- **effort:** S
- **theme:** modal-dismiss
- **location:** `otto/web/client/src/App.tsx:2718–2722` (`useDialogFocus` calls `onCancel` on Escape).
- **problem:** `onCancel` for JobDialog is `onClose` (App.tsx:743), which only sets `setJobOpen(false)`. The local state inside JobDialog (`intent`, `taskId`, `provider`, etc.) persists because the component unmounts. Reopening the dialog with `setJobOpen(true)` mounts a fresh JobDialog with empty defaults — that's fine. But if user accidentally hits Escape after typing 200 chars, work is lost with no undo, no confirm-discard prompt. Power users hit Escape reflexively.
- **concrete fix:** when JobDialog has dirty intent (≥10 chars typed), make Escape ask `confirm("Discard draft?")` once before closing. OR persist the draft in `localStorage` keyed by project so reopening restores it. Latter is preferable for power users.

### K-20 — Tab order through the sidebar always exposes Switch-project before New-job

- **severity:** NOTE
- **effort:** S
- **theme:** focus-cycle
- **location:** `otto/web/client/src/App.tsx:612–615`.
- **problem:** `Switch project` is rendered before `New job` (App.tsx:613 vs 615). The most common daily action is "New job"; the rarest is "Switch project". Tab order should reflect frequency for power users.
- **concrete fix:** swap render order so `New job` is the first sidebar action button, with `Switch project` after watcher controls (or in a less-prominent footer slot).

---

## Counts

- CRITICAL: 0
- IMPORTANT: 6 — K-01, K-02, K-03, K-04, K-05, K-07, K-09 *(K-09 reclassified — see below)*
- NOTE: 14 — K-06, K-08, K-10, K-11, K-12, K-13, K-14, K-15, K-16, K-17, K-18, K-19, K-20

Recount with K-09 as IMPORTANT (skip-link is a WCAG-recommended item, not a nicety):
- IMPORTANT: 7 — K-01, K-02, K-03, K-04, K-05, K-07, K-09
- NOTE: 13 — K-06, K-08, K-10, K-11, K-12, K-13, K-14, K-15, K-16, K-17, K-18, K-19, K-20
- CRITICAL: 0
- **Total: 20 findings**

---

## Themes

- **focus-trap (2):** K-01, K-02 — inspector/dialog don't fully isolate background.
- **focus-cycle (5):** K-03, K-15, K-16, K-19 (escape discard), K-20 — order, ring visibility, escape semantics.
- **missing-hotkey (8):** K-04, K-05, K-06, K-07, K-08, K-11, K-12, K-13 — every primary or critical action lacks a power-user shortcut, and disabled-button reasons live only in `title`.
- **tab-skip (2):** K-09, K-10 — no skip-link, mixed-focusable lists.
- **modal-dismiss (3):** K-17, K-18, K-19 — alert-region, busy-announce, draft-loss.
- **flow-blocked (0):** every flow is reachable; the cost is keystroke count, not blockage.

---

## Highest leverage (by impact / effort)

1. **K-05** (global hotkeys: `?`, `/`, `j/k`, `b`, `m`, `c`, `g t/g d`) — single ~100-LOC effect, transforms the keyboard UX.
2. **K-01 + K-02** (fix inspector aria-hidden + portal/document-level focus trap) — small change, fixes a real accessibility bug + makes K-05 safer to ship.
3. **K-09** (skip-link) — 5 lines of CSS + one anchor, removes the 8-Tab penalty for every keyboard interaction.
4. **K-03** (initial focus in modals) — one-line API addition, saves 50 keystrokes/day for power users.
5. **K-04** (tablist arrow nav) — small, fixes WAI-ARIA compliance.
6. **K-15** (focus ring contrast on primary buttons) — pure CSS, makes everything else more usable.
