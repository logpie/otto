# Mission Control — Accessibility (a11y) Hunter Findings

Scope: WCAG 2.2 AA + practical screen-reader, keyboard-only, reduced-motion, contrast, touch-target, color-scheme. Per the plan's accessibility-blocker rule, every finding below is fix-required regardless of severity tier.

Source files audited:
- `otto/web/client/src/App.tsx` (3198 lines)
- `otto/web/client/src/styles.css` (2581 lines)
- `otto/web/client/index.html`

Reference catalog: `docs/mc-audit/client-views.md`.

---

## Summary counts

- CRITICAL: 6
- IMPORTANT: 12
- NOTE: 7
- **Total: 25**

By theme: focus 4 · aria 6 · landmark 3 · contrast 2 · keyboard 4 · motion 1 · live-region 2 · label 3.

---

## Findings

### A11Y-01 — Inspector overlay does not hide background content; underlying app remains focusable through Tab

- severity: CRITICAL
- wcag-criterion: 2.4.3 Focus Order, 4.1.2 Name/Role/Value, 1.3.1 Info and Relationships
- effort: M
- theme: focus
- location: `otto/web/client/src/App.tsx:621` (`<main aria-hidden={modalOpen ...}>`), `App.tsx:1531` (`<section role="dialog" aria-modal="true">` for `RunInspector`)
- problem: `modalOpen` is computed as `jobOpen || Boolean(confirm)` only (App.tsx:503). The **RunInspector** is rendered as a child of `<main>` (App.tsx:660-665, 718-722) and declares `role="dialog" aria-modal="true"`, but the surrounding TaskBoard / RecentActivity / RunDetailPanel etc. are NOT `aria-hidden` and NOT `inert` while the inspector is open. `useDialogFocus` traps Tab inside the inspector once focus is in it, but a screen-reader user can still arrow/swipe into all the underlying landmarks — and a sighted keyboard user can click out and Tab into background controls. `aria-modal=true` lies to AT.
- concrete fix: When `inspectorOpen`, apply `inert` (or `aria-hidden=true` + `inert` polyfill) to `<aside class="sidebar">` and the Toolbar/MissionLayout/DiagnosticsLayout siblings, just like is done for `modalOpen`. Better: extend `modalOpen = jobOpen || Boolean(confirm) || inspectorOpen`.

---

### A11Y-02 — Modal aria-hidden on `<main>` hides the open RunInspector when a confirm/job dialog stacks on top of it

- severity: CRITICAL
- wcag-criterion: 4.1.2 Name/Role/Value, 1.3.1
- effort: S
- theme: aria
- location: `App.tsx:621` (`<main aria-hidden={modalOpen ? true : undefined}>`); `App.tsx:660` (RunInspector mount inside `<main>`)
- problem: When the RunInspector is open and the user triggers a confirm dialog (e.g. cancel / cleanup / land from the inspector or its action bar), `modalOpen` becomes true and `<main aria-hidden=true>` hides the entire main subtree — **including the inspector**. The inspector's `role="dialog"` still matters visually, but AT now reports it as hidden. The confirm dialog is correctly a sibling of `<main>` (App.tsx:752-761), but the inspector being a child of `<main>` is an architectural mismatch with the aria-hidden strategy.
- concrete fix: Render `RunInspector` as a sibling of `<main>` (portal it into the same layer as JobDialog/ConfirmDialog), or move the aria-hidden gate to a more granular subtree, or move to `inert` on specific siblings instead of `aria-hidden` on the whole `<main>`.

---

### A11Y-03 — Inspector tab pattern: all 4 tabs in tab order (no roving tabindex), no `aria-controls`, panel lacks `role="tabpanel"`

- severity: IMPORTANT
- wcag-criterion: 4.1.2, 2.1.1 Keyboard
- effort: M
- theme: keyboard
- location: `App.tsx:1545-1549` (tablist), `App.tsx:1553-1568` (panel container)
- problem: `<div className="detail-tabs" role="tablist">` contains four `<button role="tab" aria-selected=…>` but every button is in the natural tab order. WAI-ARIA APG tab pattern requires roving tabindex (only the selected tab is `tabIndex=0`, others `tabIndex=-1`) and arrow-key navigation between tabs. There is also no `aria-controls` on each tab pointing to a `tabpanel`, and `<div className="run-inspector-body">` is not `role="tabpanel"` with `aria-labelledby`. AT cannot programmatically map tab → panel.
- concrete fix: Add `tabIndex={mode === thisMode ? 0 : -1}` to each tab; handle ArrowLeft/ArrowRight/Home/End to move focus + activate tab; add `aria-controls="run-inspector-panel"` on each tab and wrap the body in `<div id="run-inspector-panel" role="tabpanel" aria-labelledby="…activeTabId">`. Alternative: drop the `tablist`/`tab` roles and use plain `aria-pressed` toggle buttons (matches the view-tabs Toolbar pattern at App.tsx:918-937, which is correct for that case).

---

### A11Y-04 — Live runs / history rows are `role="button"` on `<tr>` — invalid role nesting; not announced as table rows

- severity: IMPORTANT
- wcag-criterion: 4.1.2, 1.3.1
- effort: M
- theme: aria
- location: `App.tsx:1350-1359` (LiveRuns), `App.tsx:1397-1406` (History)
- problem: `<tr role="button" tabIndex=0 aria-selected aria-label onClick onKeyDown>` overrides the implicit `row` role with `button`, then adds `aria-selected` (which is meaningful on `option`/`row`/`tab`/`gridcell`, NOT on `button`). Screen readers will announce "button" and lose the table-row context (column headers won't be paired). Also `<table>` is used without `role="grid"`, so it isn't a true interactive grid. Result: AT users don't get column header pairing, don't hear that this is a row, and `aria-selected` is ignored on a button.
- concrete fix: Remove `role="button"`; instead make the **first cell** (or a wrapping link inside the row) the focusable activator: `<tr aria-selected=…><td><button onClick onKeyDown aria-label=…>{display_id}</button></td>…</tr>`. Then keyboard activation lives on the button, the row keeps its semantic role, and `aria-selected` works. If a clickable row is essential, switch to `role="grid"` on the table and `role="row"` + roving tabindex per cell.

---

### A11Y-05 — `aria-controls` on TaskCard "More/Less" toggle points to a DOM node that is unmounted when collapsed

- severity: NOTE
- wcag-criterion: 4.1.2
- effort: S
- theme: aria
- location: `App.tsx:1258-1268`
- problem: `<button aria-expanded={expanded} aria-controls="${id}-drawer">` references `<div id="${id}-drawer">`, but the drawer is conditionally rendered (`expanded ? … : null`). When `expanded=false`, the referenced ID does not exist in the DOM. JAWS/NVDA tolerate this but it is technically a broken `IDREF`.
- concrete fix: Render the drawer always; toggle its visibility with `hidden` or `aria-hidden` (and CSS `display:none`) instead of unmounting. Bonus: removes layout flicker.

---

### A11Y-06 — 25 `<section aria-label>` regions create extreme landmark noise

- severity: IMPORTANT
- wcag-criterion: 1.3.1, 2.4.1 Bypass Blocks
- effort: M
- theme: landmark
- location: `App.tsx` 25 instances (e.g. `:631, :687, :839, :995, :1063, :1072, :1086, :1100, :1135, :1197, :1206 (×4 task columns), :1289, :1331, :1379, :1427, :1606, :1618, :1630, :1635, :1653, :1672, :1683, :1696, :1711, :1798`)
- problem: Every labeled `<section>` becomes a `region` landmark in AT. A screen-reader user pulls up the landmarks list and sees 25+ regions named "Mission focus", "Task Board", "Needs Action", "Queued / Running", "Ready To Land", "Landed", "Live Runs", "Run History", "Operator Timeline", "Mission overview", "Diagnostics Summary", "Command backlog", "Runtime issues", "Landing states", "Review packet", "Proof of work", "Next action", "What failed", "Certification checks", "Stories tested", "Changed files", "Code diff", "Evidence artifacts", "Evidence content"… most of them nested. Best practice is one `region` per top-level grouping the user might want to jump to (3-7 typically).
- concrete fix: Replace inner `<section aria-label>` with `<div aria-labelledby>` (no role) and let the heading hierarchy do the work. Keep `region` only on the very top groupings: Mission Control task workflow, Mission Control diagnostics, Project Launcher, Run Inspector body. Drop the column-level `aria-label`s on `.task-column` (the `<h*>` heading is enough).

---

### A11Y-07 — Filter bar and view-tabs `<div aria-label>` without `role` — labels are dropped

- severity: NOTE
- wcag-criterion: 4.1.2
- effort: S
- theme: aria
- location: `App.tsx:918` (`<div className="view-tabs" aria-label="Mission Control views">`), `App.tsx:938` (`<div className="filters" aria-label="Run filters">`), `App.tsx:1497` (`<div className="detail-inspector-actions" aria-label="Evidence shortcuts">`), `App.tsx:1947` (`<div className="action-bar" aria-label="Advanced run actions">`), `App.tsx:2089` (`<div className="target-guard" aria-label="Target project">`), `App.tsx:1545` already has `role="tablist"` so it's fine.
- problem: `aria-label` on a generic `<div>` with no role is ignored by browsers/AT (the element has no accessible name slot). The grouping is invisible to AT.
- concrete fix: Add `role="group"` (or use `<fieldset><legend>`) to each, OR convert to `role="toolbar"` for the actual toolbars. Cheapest: `role="group"` + keep `aria-label`.

---

### A11Y-08 — No skip-link to bypass sidebar / toolbar; no `<nav>` for sidebar navigation either

- severity: IMPORTANT
- wcag-criterion: 2.4.1 Bypass Blocks
- effort: S
- theme: keyboard
- location: `App.tsx:601-619` (sidebar) — no `<nav>`, no skip link in shell
- problem: Every page load forces a keyboard user to Tab through the sidebar (Switch project, New job, Start watcher, Stop watcher) and the entire toolbar (Tasks, Diagnostics, four filter controls, Clear filters, Refresh) before they can reach the task board content. There is no "Skip to main content" affordance and the `<aside>` is not a `<nav>` (so it doesn't show in the landmark list as navigation either).
- concrete fix: Add a visually hidden skip link as the first focusable element: `<a href="#main-workspace" className="skip-link">Skip to main content</a>` (with a `:focus { position: static; … }` reveal style); add `id="main-workspace"` and `tabIndex={-1}` to `<main>`. Optional: wrap the sidebar action buttons in `<nav aria-label="Sidebar actions">`.

---

### A11Y-09 — `document.title` never updates per view; URL changes silently

- severity: NOTE
- wcag-criterion: 2.4.2 Page Titled (single-page-app interpretation)
- effort: S
- theme: aria
- location: `index.html` sets `<title>Otto Mission Control</title>`; no `useEffect` in `App.tsx` updates `document.title` based on `viewMode` or selected run.
- problem: Screen-reader users navigating via `pushState` (Tasks ↔ Diagnostics, run selection) get no auditory confirmation that the "page" changed. The URL changes but the title stays static.
- concrete fix: `useEffect` on `[viewMode, selectedRunId]` that sets `document.title = "Otto MC — Tasks"` / `"Otto MC — Diagnostics"` / `"Otto MC — Run <id>"`.

---

### A11Y-10 — No `aria-live` region announces inspector tab / view / run-selection changes

- severity: IMPORTANT
- wcag-criterion: 4.1.3 Status Messages
- effort: M
- theme: live-region
- location: throughout — only `#toast` (line 596, 762), `<p className="launcher-status" aria-live="polite">` (line 868), and `<span id="jobDialogStatus" aria-live="polite">` (line 2173) exist as live regions.
- problem: Switching from Tasks → Diagnostics, opening RunInspector, switching tabs Proof→Diff→Logs→Artifacts, polling `/api/state` with new event arrivals — none of these announce. New events appear in the timeline / RecentActivity silently. Loading→Loaded transitions in DiffPane / LogPane / ProofPane have no live announcement; the user discovers content only by re-Tabbing in.
- concrete fix: Add a single visually-hidden `<div role="status" aria-live="polite" aria-atomic="true">` that is updated when: view changes, inspector opens, inspector tab changes, run selection changes, refresh completes ("Loaded N runs"). For the event timeline specifically, consider a separate `aria-live="polite"` region announcing "+N new event(s)" with a debounce.

---

### A11Y-11 — Polling log/diff load states have no SR feedback; "Loading…" is visible-only

- severity: NOTE
- wcag-criterion: 4.1.3
- effort: S
- theme: live-region
- location: `LogPane` App.tsx:1574-1586 ("waiting for output"), `DiffPane` 1727-1769 ("Loading diff..."), `ProofPane` 1719-1722 ("Loading evidence content..."), 1.2 s poll loop App.tsx:332-336.
- problem: These strings live in `<pre>` and `<span>` without `aria-live`. SR users don't get notified when content arrives.
- concrete fix: Wrap toolbar status spans in `<span role="status">` (or `aria-live="polite"`). Don't make the `<pre>` itself live — that would announce every log byte.

---

### A11Y-12 — Status text relies on color-coded class but the `--amber` token (#a16207) on white is borderline for 4.5:1

- severity: NOTE
- wcag-criterion: 1.4.3 Contrast (Minimum)
- effort: S
- theme: contrast
- location: `styles.css:13` `--amber: #a16207`; used at lines 1176, 1214 for `.status-queued`/`.status-paused`/`.status-interrupted`/`.status-stale` text, also 965 in event-warn.
- problem: `#a16207` on white surface yields a contrast ratio of ~4.50–4.55:1 — passes AA at exactly the threshold but fails the moment the surface tint drifts (e.g. on `--surface-alt #f4f7fb` the ratio drops to ~4.4:1, sub-AA). Any anti-aliasing or display gamma variance pushes it under.
- concrete fix: Darken `--amber` to `#854d0e` (≈5.9:1 on white) or `#92400e` (≈7.0:1). Mirror change anywhere amber text appears on `--surface-alt`.

---

### A11Y-13 — `--muted: #647180` on `--surface-alt #f4f7fb` is below 4.5:1 in some panels

- severity: IMPORTANT
- wcag-criterion: 1.4.3
- effort: S
- theme: contrast
- location: `styles.css:8` token; many panel subtitles, hint copy (e.g. lines 168, 243, 376, 456, 483, 536, 668, 858, 1052, 1069, 1074, 1158, 2167) use `color: var(--muted)` and several panels render on `--surface-alt`.
- problem: `#647180` on `#ffffff` ≈ 4.84:1 (AA pass). On `#f4f7fb` ≈ 4.62:1. On `#f8fafc` (used for review-evidence backgrounds, line 324, 528, 2285) ≈ 4.59:1. On `#eef1f5` disabled-button bg ≈ 4.34:1 — sub-AA.
- concrete fix: Darken `--muted` to `#4b5563` (≈7.5:1 on white, ≈7.0:1 on surface-alt). Visual change is minimal; AA margin grows substantially.

---

### A11Y-14 — Disabled buttons: `color: #9aa4b2` on `#eef1f5` ≈ 2.5:1 — non-disabled-button text in many cases

- severity: NOTE (informational; WCAG exempts disabled controls from 1.4.11)
- wcag-criterion: 1.4.11 Non-text Contrast (exempted) / usability
- effort: S
- theme: contrast
- location: `styles.css:74-78`
- problem: Disabled controls are exempt from contrast minimums by WCAG, but Otto uses the disabled state heavily for important actions (Start/Stop watcher, Land all ready, primary action when Project is dirty, Diff button, etc.) and the user must read the label to know whether the action is available. The current disabled style is hard to read for low-vision users.
- concrete fix: Bump disabled text to `#6b7280` (≈4.0:1 on `#eef1f5`) or apply an additional `text-decoration` / `cursor: not-allowed` cue. Pair with `aria-describedby` (already present on watcher buttons via `watcher-action-hint` — extend to others).

---

### A11Y-15 — Activity-list mixes events (`<div>`) and history (`<button>`) without a `role="list"`; events are non-interactive but history rows are buttons in a flat flow

- severity: IMPORTANT
- wcag-criterion: 1.3.1, 4.1.2
- effort: S
- theme: aria
- location: `App.tsx:1297-1320`
- problem: The activity-list parent has no `role="list"`, and the items mix non-interactive `<div className="activity-item">` (events) with interactive `<button className="activity-item history-activity">` (history). A screen-reader user has no count and no way to know the items are siblings; they get "div" then "button" then "div"… Compare to the EventTimeline which correctly uses `<div role="list">` + `<div role="listitem">` (App.tsx:1435-1440).
- concrete fix: Wrap the children in `<div role="list">` and add `role="listitem"` to each event div / history button. Or convert to `<ul>` + `<li>` with the button inside the `<li>`.

---

### A11Y-16 — `<dl>` used for sidebar "ProjectMeta" but each `MetaItem` wraps a `<div>` around `dt`+`dd` — not a valid HTML5 description list grouping in old AT

- severity: NOTE
- wcag-criterion: 1.3.1
- effort: S
- theme: aria
- location: `App.tsx:776-787` (the `<dl>`), `App.tsx:788-794` (`MetaItem` body)
- problem: `<dl>` direct children should be `dt` / `dd` / optional `<div>` (HTML5 spec allows wrapping a `dt`+`dd` group in a single `<div>` since 2017; iOS VoiceOver historically misannounced this — partially resolved but still inconsistent).
- concrete fix: Verify with VoiceOver/NVDA. If announcement is broken, flatten — emit `dt` + `dd` directly without wrapper `<div>`. Or add `role="group"` to the wrapping `<div>` to make grouping explicit.

---

### A11Y-17 — JobDialog `<select>` "Provider", "Reasoning effort", "Certification" rely on inherited-default option labels that include parenthetical source — long select-option text wraps poorly and isn't a `<label>` association

- severity: NOTE
- wcag-criterion: 3.3.2 Labels or Instructions
- effort: S
- theme: label
- location: `App.tsx:2132-2147`, `2153-2170`; help string in `field-hint` span 2163
- problem: The `<select>` is properly labelled by the wrapping `<label>`, but the inherit-default semantics (e.g. `"Inherit: Codex (otto.yaml | built-in default)"`) is encoded inside the option label. SR announces the value, not the help. The `<span className="field-hint">` carrying `certificationHelp` is not associated to the `<select>` — no `aria-describedby`.
- concrete fix: Give the `<span>` an `id` and reference it from the select via `aria-describedby` so SR users hear the help text after the field name.

---

### A11Y-18 — JobDialog target-guard "I understand…" checkbox: required-when-dirty, but no `aria-required` and no error association if the user submits without it

- severity: IMPORTANT
- wcag-criterion: 3.3.1 Error Identification, 3.3.3 Error Suggestion
- effort: S
- theme: label
- location: `App.tsx:2098-2106`; submit-disabled gating App.tsx:2030 (in submit fn) + footer status text at 2173
- problem: Submit is disabled when the checkbox is required-but-unchecked, and the live region 2173 only updates after a submit attempt. The checkbox itself doesn't carry `aria-required="true"` or an `aria-describedby` linking to the explanation paragraph "This job can create branches/worktrees…". A keyboard-only user encountering a disabled submit has no programmatic explanation of WHY.
- concrete fix: Add `aria-required="true"` to the checkbox; add `id="target-confirm-help"` to the explanatory `<p>` (line 2096) and `aria-describedby="target-confirm-help"` to the checkbox. Also update the live-region status proactively when the user changes `command` or `subcommand` to mention the dirty-confirmation requirement.

---

### A11Y-19 — Modal backdrop is `role="presentation"` and has no click-to-dismiss; OK semantics, but `<form>` is the dialog container — submit-on-Enter is a hidden interaction

- severity: NOTE
- wcag-criterion: 3.2.2 On Input
- effort: S
- theme: keyboard
- location: `App.tsx:2068-2076`
- problem: `<form role="dialog" aria-modal="true" onSubmit=submit>`. Pressing Enter in any text field will submit the dialog. The Intent textarea (5 rows) needs Enter for line breaks — that works in a `<textarea>` since Enter inserts a newline rather than submitting. But the single-line `<input>` Task id, After, Model fields will submit-on-Enter, which the user may not expect (the visual primary button reads "Queue job"). With required validation it's mostly safe, but with empty intent + dirty target unconfirmed, Enter fires `submit()` which sets `setStatus("Build intent is required.")` — silent if the user isn't watching the live region.
- concrete fix: Either (a) make Enter-submit explicit by giving the form `<button type="submit">` only at the footer (already the case) and noting the behavior in instructions, or (b) trap Enter in the inputs and require explicit click. Most users expect form submission on Enter; the real fix is just to make sure A11Y-18's `aria-describedby` exists so the validation state is reachable.

---

### A11Y-20 — Inspector "Close inspector" button: a clear icon-only "✕" pattern is avoided (good), but the close-button is positioned after the tablist in DOM, so Tab order goes Tabs → Close, not Close → Tabs

- severity: NOTE
- wcag-criterion: 2.4.3 Focus Order
- effort: S
- theme: focus
- location: `App.tsx:1540-1551`
- problem: Visual order in `.run-inspector-heading` (CSS grid) may differ from DOM order. The dialog focuses its first focusable element on mount (`focusableDialogElements`); if visual placement puts Close on the right (typical), screen-reader sequential focus order matches DOM, which is fine — but verify with the actual CSS grid `order` rules. ConfirmDialog and JobDialog both place Close in the header (App.tsx:2080, 2283) — fine. JobDialog places primary submit at the footer end (App.tsx:2174) — Tab cycles correctly.
- concrete fix: Verify visual vs DOM match by tabbing in a real browser. Likely no change needed; flag for QA.

---

### A11Y-21 — `prefers-reduced-motion` not honored; one `transition` exists on toast (16ms ease) — borderline

- severity: NOTE
- wcag-criterion: 2.3.3 Animation from Interactions (AAA, but related 2.3.1 AA)
- effort: S
- theme: motion
- location: `styles.css:2181` (`#toast { transition: opacity 0.16s ease, transform 0.16s ease; }`)
- problem: client-views.md correctly notes "no transitions or animations authored aside from CSS pseudo `:hover`/`:focus`" — but the toast does have a 160 ms slide+fade. Tiny, but not respected by `prefers-reduced-motion: reduce`. Otherwise the UI is static (state changes are instantaneous), which is actually friendly to vestibular users — but there is no explicit `@media (prefers-reduced-motion: reduce)` block to confirm intent. If someone adds animation later, there's no opt-out.
- concrete fix: Add a `@media (prefers-reduced-motion: reduce) { #toast { transition: none; } * { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; } }` block at the bottom of styles.css.

---

### A11Y-22 — `prefers-color-scheme: dark` is unsupported; `:root { color-scheme: light; }` locks to light

- severity: IMPORTANT
- wcag-criterion: 1.4.3 (indirect — many users with low-vision rely on dark mode), 1.4.6 (AAA contrast)
- effort: L
- theme: contrast
- location: `styles.css:2`
- problem: `color-scheme: light` explicitly opts out of OS-level dark mode — meaning users who set their OS to dark see a forced bright UI in MC. There is no `@media (prefers-color-scheme: dark)` override. The sidebar IS dark (`#111827`) regardless, which means dark-mode users get jarring mixed contrast. No `light-dark()` CSS function usage.
- concrete fix: Either declare `color-scheme: light dark` and add a full dark-mode token set under `@media (prefers-color-scheme: dark)`, or commit to "always light" deliberately and document it. The current state is a bug — it actively rejects user preference.

---

### A11Y-23 — Sidebar action buttons are icon-free text but use `title` for the disabled-reason hint; `title` is mouse-only

- severity: IMPORTANT
- wcag-criterion: 1.3.1 / 3.3.2 / 4.1.2
- effort: S
- theme: label
- location: `App.tsx:616-617` Start/Stop watcher buttons set `title={start_blocked_reason}` and use `aria-describedby="watcher-action-hint"` (good), but the `title` attribute is a duplicate channel that keyboard / SR users won't see.
- problem: Many other places use `title=…` to convey UI text that is otherwise inaccessible: `App.tsx:1069` (pill `title=`), `1253` (task-title `title=`), `1271-1272` (Branch `title=`), `1301, 1313` (event/activity `title=`), `1360-1365` (live-runs cells `title=`), `1408-1411` (history cells `title=`). When the cell text is truncated by ellipsis, `title` is mouse-hover only — screen reader users get the truncated string only. Several `<code title={path}>` are also mouse-only.
- concrete fix: For truncated content, prefer `aria-label` (for AT) plus `title` (for mouse), and ensure the truncation does not happen via `text-overflow: ellipsis` on the same node that displays the label. For event/activity rows in a list, use `aria-describedby` on the row and a visually-hidden full-text node.

---

### A11Y-24 — `tabIndex={0}` on `<pre>` log/diff/artifact panes is correct (scrollable region focusable) but `aria-label` is generic — screen-reader user gets no line count or "showing latest" suffix

- severity: NOTE
- wcag-criterion: 1.3.1
- effort: S
- theme: aria
- location: `App.tsx:1583` (run log), `1719-1721` (proof content), `1764` (diff), `1915` (failure log), `1980` (artifact content)
- problem: `aria-label="Run log output"` is static. The toolbar above shows "N lines · showing latest output" but is NOT linked to the `<pre>` via `aria-describedby`. SR users focus the pre, hear "Run log output" only.
- concrete fix: Give the toolbar `<span>` an id; reference it via `aria-describedby` on the `<pre>`. Or include the line count directly in `aria-label`.

---

### A11Y-25 — Touch targets on view-tabs (`min-height: 30px`) and base buttons (`min-height: 34px`) are below 44×44 AAA and likely below 24×24 AA on extreme zoom; mobile breakpoint does not enlarge them

- severity: IMPORTANT
- wcag-criterion: 2.5.8 Target Size (Minimum) — AA in WCAG 2.2 requires 24×24 CSS px
- effort: S
- theme: keyboard
- location: `styles.css:65` (button 34px), `styles.css:354` (view-tabs 30px), mobile media query `styles.css:2447-2546` does not enlarge.
- problem: 30 / 34 px height passes the 24 px AA floor but only marginally — when paired with narrow buttons (auto-width), the actual hit-target may dip under 24×24. On iOS webkit, single-tap precision is lower; the activity-item history-buttons (likely tighter on phones) and project-row meta sub-spans inside buttons may overlap. Also, the JobDialog "Close" button in header (`App.tsx:2080, 2283`) is text "Close" with default padding — small target.
- concrete fix: Audit min target with a real device. Bump `button { min-height: 36px; padding: 0 14px; }` and `.view-tabs button { min-height: 36px; padding: 0 12px; }`. Increase mobile breakpoint button sizes to 44px. Ensure inline icon-style buttons (Dismiss banner, Close header, Less/More toggle) hit 44×44 on mobile.

---

## Other observations (not findings)

- `tabIndex={-1}` on dialog containers (`App.tsx:1538, 2075, 2279`) is correct — allows programmatic focus for the dialog root if no inner focusable exists (used by `useDialogFocus` fallback at App.tsx:2714, 2728).
- `useDialogFocus` correctly stores `previousFocus` and restores on unmount (App.tsx:2712, 2746). Tab-trap implementation is conventional and correct for the simple case (single dialog at a time). It does NOT defend against a dialog opening on top of the inspector — see A11Y-02.
- ConfirmDialog correctly disables Cancel/Close while `pending`, and `useDialogFocus` passes `disabled=pending` so Escape is also disabled — prevents accidental dismiss mid-action. Good.
- Tables in `LiveRuns` and `History` use proper `<thead>`/`<th>` headers — good (apart from the row-as-button issue in A11Y-04).
- `<details>` / `<summary>` pattern is used for ReviewDrawer, run metadata, advanced job options — natively keyboard accessible. Good.
- Toast is correctly `role="status" aria-live="polite"` and rendered as a sibling of the modal layer so it remains visible when sidebar+main are aria-hidden — good per client-views.md note.
- `selectOnKeyboard` (App.tsx:2690-2694) handles Enter and Space activation on table rows — correct (preventDefault for Space scroll).
- No `autoComplete` / `autoCapitalize` hints on JobDialog inputs (Task id, After, Model) — minor; not a WCAG issue.
- Heading hierarchy is broadly h1 (sidebar) → h2 (panel/dialog) → h3 (subsection) → no h4. Looks coherent overall, but every dialog pairs a fresh `<h2>` against the same shell `<h1>`, which is normal for SPA dialogs.
