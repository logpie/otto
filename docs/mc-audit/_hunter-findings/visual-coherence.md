# Visual Coherence — Adversarial Audit

**Scope:** `otto/web/client/src/styles.css` (~2580 lines), `otto/web/client/src/App.tsx` (single-file component tree).
**Method:** Token frequency analysis, semantic mapping of color usage, hover/active/disabled state coverage, dark/light mode parity, scale-vs-ad-hoc detection.

## Inventory snapshot

| Token | Count | Notes |
|---|---|---|
| Distinct `font-size` values | **10** (10/11/12/13/14/15/16/18/20/22 px) | No declared scale; 12px dominates (42 uses) |
| Distinct `font-weight` values | **4** (500/600/700/800) | 800 used exactly once (proof story badge) — outlier |
| Distinct `border-radius` values | **6** (0, 6, 7, 8, 10, 999 px) | 7px appears 4× and 10px once — neither is on the 6/8 system |
| Distinct `padding` values | **25+** unique combos | 9px and 7px values are off-grid (3-pt drift from 8/10) |
| Distinct `gap` values | **12** (2/3/4/5/6/7/8/9/10/12/14/16) | 5/7/9px are off the 4/8 scale |
| Distinct hex colors | **51** | Plus 11 CSS vars. Many tints duplicated (e.g., `#fff8e1` appears 7× hardcoded instead of `--amber-bg` var) |
| `line-height` declarations | **2** explicit (1.4, 1.35) + base 1.45 | Most elements inherit body 1.45 |
| Box-shadow primitives | **2** (drop + inset accent stripe) | Coherent system (good) |
| `:hover` rules | 7 | Inconsistent — see §Hover |
| `:active`/pressed rules | **0** | No pressed state at all (CRITICAL feedback gap) |
| `:disabled` rules | 3 | Inconsistent treatment |
| `prefers-color-scheme: dark` rules | **0** | `color-scheme: light` is hardcoded; no dark mode |

---

## Findings

### F1 — CRITICAL · color · No design-token discipline; 51 hardcoded hex literals leak past the CSS-var palette

**Location:** `styles.css` throughout; vars defined at lines 1–17 but bypassed everywhere.
**Problem:** 11 CSS vars are declared (`--bg`, `--surface`, `--line`, `--text`, `--muted`, `--accent`, `--blue`, `--green`, `--amber`, `--red`, `--magenta`) yet the file contains **51 distinct raw hex codes**. Examples of bypass:
- Disabled button uses raw `#9aa4b2` / `#eef1f5` (lines 76–77) instead of var(--muted)/var(--surface-alt).
- Status banners hardcode `#fecaca`, `#fef2f2`, `#7f1d1d`, `#f4d58d`, `#fff8e1`, `#713f12`, `#bfdbfe`, `#eff6ff`, `#1e3a8a` (lines 982–992, 1188–1192, 1396–1403, 1556–1561, 2306–2316). Same tones appear in 5+ places, drifting silently.
- `#cbd5e1` (Tailwind slate-300) appears 6× in unrelated contexts (sidebar text, log toolbar, diff list) — would need 6 edits to recolor.
- Greens: `#15803d` (`--green`), `#86efac`, `#dcfce7`, `#166534`, `#ecfdf5` — five different greens, none scaled.
**Fix:** Extend vars to a tonal scale: `--red-50/100/600/700/800`, same for amber/green/blue. Replace every literal with a var. Net effect: instant theme-ability + dark mode unlock.

### F2 — CRITICAL · dark-light · No dark mode at all

**Location:** `styles.css:2` — `color-scheme: light;` (forced).
**Problem:** No `@media (prefers-color-scheme: dark)` block. Sidebar (`#111827`) and code/log/diff panes (`#0b1020`) are dark by default, but everything else is light-only — already a hybrid theme. macOS/Windows users in dark mode see jarring white panels next to a near-black sidebar. There is no dark surface/border/text token system.
**Fix:** Either (a) add a dark-mode token override `:root { ... } @media (prefers-color-scheme: dark) { :root { --bg: ...; --surface: ...; ... } }`, or (b) remove `color-scheme: light` and document that the app is intentionally light-only. Given the half-dark sidebar, option (a) is the right answer.

### F3 — CRITICAL · active · No pressed/active state on any interactive element

**Location:** `styles.css` — global. Zero `:active` selectors exist.
**Problem:** Buttons, tabs, table rows, task cards, and review-evidence chips have no pressed-state feedback. On click, the only visual change is the focus ring (if present) or a hover transition. On touch devices and trackpads, this means actions feel unconfirmed.
**Fix:** Add `button:active:not(:disabled) { transform: translateY(1px); border-color: var(--accent-strong); }` plus targeted `tbody tr:active`, `.task-card:active`. Pair with `transition: transform .08s ease, border-color .12s ease` (currently zero transitions per `client-views.md:54`).

### F4 — IMPORTANT · disabled · Three different disabled treatments

**Location:** `styles.css:74–78`, `586`, `616–620`, `2152–2155` (artifact buttons), `1869–1872` (proof artifacts `selected`).
**Problem:** Disabled state is inconsistent:
- Generic `button:disabled` → grey text `#9aa4b2` + grey bg `#eef1f5` + `cursor: not-allowed`.
- `.task-card-main:disabled` → keeps original `var(--text)` color, transparent bg, `cursor: default` (no "not-allowed"!).
- `.task-landed` → opacity 0.82 (not even semantic — applies to landed-stage cards regardless of interactivity).
- Review-evidence missing chips → amber border + amber bg (not greyed).
A user can't tell from look-and-feel which buttons are disabled vs which are styled accent.
**Fix:** Standardize: `[disabled] { opacity: .5; cursor: not-allowed; pointer-events: none; }` on all interactive primitives. Drop the bg/text override on `button:disabled`. Replace `.task-landed { opacity: 0.82 }` with `.task-landed { opacity: .65 }` and add a "Landed" badge.

### F5 — IMPORTANT · hover · Hover treatment varies by surface

**Location:** `styles.css:70–72`, `569`, `766`, `1113–1116`, `604–606`.
**Problem:** Five different hover idioms across similar elements:
- `button:hover` → border becomes `var(--accent)` (teal).
- `.task-card:hover` → border becomes `var(--accent)` (consistent).
- `.history-activity:hover` / `tbody tr:hover` → background `#e8f3f1` (raw hex, not a var; light-teal tint).
- `.task-card-main:hover` → border becomes **transparent** (i.e., explicitly suppressed because parent already shows hover).
- `.activity-item` is `<button>` but uses transparent bg with no hover override — falls through to `button:hover` → teal border, which then conflicts with `border-bottom: 1px solid var(--line)` and produces a partial border.
**Fix:** Define `--hover-bg: #e8f3f1` as a var. Standardize: rows use background tint, cards use border tint, buttons use border tint. Override `.activity-item:hover` explicitly.

### F6 — IMPORTANT · typography · Ad-hoc font-size scale (10 distinct values)

**Location:** declared sizes: `10/11/12/13/14/15/16/18/20/22 px`.
**Problem:** Sizes follow no scale. 13px (`timeline-item strong`, `static-field strong`, `target-confirm`) is 1px off from base 14px — likely a copy-paste mistake. 15px (`detail-body h3`) and 16px (`confirm-dialog h2`) are arbitrary one-offs. 22px (`launcher-head h2`) vs 20px (`focus-copy h2`) vs 18px (`brand h1`) — three "hero" sizes with no purpose.
**Fix:** Reduce to a 6-step scale: `--fs-xs: 11; --fs-sm: 12; --fs-base: 14; --fs-md: 16; --fs-lg: 20; --fs-xl: 24`. Drop 10/13/15/18/22.

### F7 — IMPORTANT · spacing · Off-grid padding values (9/7/3px)

**Location:** `styles.css` examples: `padding: 9px 10px` (line 476, 834, 1132, 1656, 1865, 2142), `padding: 7px 8px` (lines 1386, 1396, 1474, 1520), `padding: 7px 9px` (lines 384, 2030).
**Problem:** 9px and 7px are 1px off the 8/10 grid. Unclear if they were intentional optical tweaks or typos. Mixing 7/8/9/10 in adjacent rows produces a 3-pt jaggy edge that the eye reads as misalignment, especially at the input-vs-button junction (`select`/`input` height 34px with 7px V padding sits next to buttons with implicit 0 V padding).
**Fix:** Snap to 4/8/12/16 grid. Allow 6px only as a deliberate "tight" token. Audit each 7/9px value: keep only if it's a half-grid intent (pill/badge), else round to 8/10.

### F8 — IMPORTANT · borders · 7px and 10px radii are outliers

**Location:** `styles.css` — `border-radius: 7px` (lines 1754, 1788, 1807, 1865) and `10px` (line 1648).
**Problem:** Card system uses 8px (16 occurrences). Pills are 999px. Inputs/buttons are 6px. Then 7px appears for proof-metric tiles, proof-report-actions, proof-stories, proof-artifacts buttons — these sit *next to* 8px parent cards, producing visible 1px radius mismatch. 10px appears once on `.run-inspector` overlay — different from every other dialog (which use 8px on `.job-dialog`/`.confirm-dialog`).
**Fix:** Standardize on `--radius-sm: 6` (inputs/buttons), `--radius-md: 8` (cards/dialogs), `--radius-pill: 999`. Delete 7px and 10px.

### F9 — IMPORTANT · color-status · PASS/FAIL/WARN tones drift between contexts

**Location:** `proof-story.story-pass` (line 1823) `#dcfce7/#166534`, `check-pass` (line 1498) `#dcfce7/var(--green)` = `#15803d`, `event-success` only colors text `var(--green)`. Same-named "pass" uses two different green text shades (#166534 vs #15803d) and three rendering styles (filled badge / filled badge / plain text).
**Problem:** A user scanning task board → review packet → proof report sees three different greens for "pass". Same for amber: `proof-story.story-warn` (#fef3c7/#92400e) vs `check-warn` (#fff8e1/var(--amber)=#a16207) — those are visibly different ambers (warm yellow vs muted gold).
**Fix:** One token per tone × surface: `--state-pass-bg/-fg/-border`, `--state-fail-bg/-fg/-border`, `--state-warn-bg/-fg/-border`. Use everywhere.

### F10 — IMPORTANT · color-blind · Status colors not differentiated beyond hue

**Location:** Status text classes `styles.css:1194–1220`, focus-state classes `491–505`, task-state classes `573–587`.
**Problem:** Pass/fail/warn/info are signaled by *color alone* on event-timeline severity badges (`event-success .timeline-severity { color: var(--green) }`) and on task-board column accents (3px inset shadow). For deuteranopia/protanopia (~5% of men), green and red converge. No icon, no text-prefix, no shape differentiation.
**Fix:** Prefix every state label with a glyph: `✓ PASS`, `✗ FAIL`, `⚠ WARN`, `ℹ INFO`. Or add `text-decoration: underline wavy var(--red)` for fail variants. Don't rely on the 3px stripe alone.

### F11 — IMPORTANT · alignment · Form-control vertical rhythm breaks at button-vs-input row

**Location:** `styles.css:64–68` (button `min-height: 34px`, `padding: 0 12px`), `380–385` (select/input/textarea `min-height: 34px`, `padding: 7px 9px`).
**Problem:** Same min-height (34px) but the input has text inside 7px V-padding while the button centers content via flex. With a 14px line-height-1.45 string (~20px tall), input renders text starting ~7px from top; button text is vertically centered. In `Toolbar` (App.tsx ~938) the filters row puts a select next to a search input next to a checkbox-label next to a button — and `.filters` uses `align-items: end` (line 368). End-aligning a 34px input vs 34px button vs 34px checkbox-label is fine in heights, but the *internal text baseline* of the input sits ~3px lower than the button. Visible jaggy baseline.
**Fix:** Use `align-items: center` on `.filters` and `.toolbar-actions`. Set inputs to flex/grid place-items: center, or normalize internal padding to vertically center text at the same baseline as buttons.

### F12 — IMPORTANT · color-semantic · Magenta is reserved for cancelled/removed but never legend'd

**Location:** `styles.css:1217–1220` — `.status-cancelled, .status-removed { color: var(--magenta); }`. `--magenta: #a21caf` defined at line 15.
**Problem:** Magenta is a 6th status color outside the standard PASS/FAIL/WARN/INFO/NEUTRAL system, but it appears only in two places and never with a legend or explanation. A user seeing a magenta row will not know it means "cancelled" vs "removed". Also `#a21caf` on white surface fails WCAG AA at 14px regular (contrast ~4.4:1 — borderline; falls below for 14px below 18.66pt).
**Fix:** Replace magenta with a darker neutral grey + strikethrough for cancelled/removed (semantic: "no longer in flight"). Or add an icon prefix.

### F13 — IMPORTANT · brand · Otto identity is invisible

**Location:** `styles.css:126–154` (sidebar brand block), `App.tsx` brand mark.
**Problem:** Brand is a 38×38 teal square with what looks like a single letter (lines 132–140), label `<h1>` at 18px and tagline `<p>` at 12px. No logotype, no signature shape, no visual identity beyond a color square. Compared to operator tools like Linear, Vercel, Datadog — Otto looks generic. The teal accent (#0f766e) is the only brand cue.
**Fix:** Either commit to "minimalist generic ops UI" (then drop the brand block entirely and just put a wordmark) or introduce an actual mark + subtle wordmark in a custom typeface for the sidebar header. NOTE — labelled IMPORTANT not CRITICAL because it's a positioning choice, not a defect.

### F14 — NOTE · icons · No icon set used at all

**Location:** Search across App.tsx — no `<svg>` imports, no icon library, no Unicode glyphs in the literal copy I scanned.
**Problem:** Status changes, action buttons, severity badges, tabs (Proof/Diff/Logs/Artifacts), watcher start/stop — all are text-only. This isn't inherently wrong (CLI-adjacent tools sometimes do this on purpose) but it pushes more density onto the type ramp and makes scanning slower.
**Fix:** Add a single tiny icon set (Lucide SVG sprites or inline `<svg>` per-component) for the critical surfaces: tab strip, severity badge, watcher start/stop, action chips. 16px stroke 1.5, single color = currentColor.

### F15 — NOTE · spacing · Modal padding 16px vs panel padding 12–14px inconsistent

**Location:** `styles.css:2214` (`.job-dialog`/`.confirm-dialog` padding 16px), `261` (launcher-panel 16px), `1041` (panel-heading 10px 12px), `922` (overview 12px 14px).
**Problem:** Vertical density is highest in the main views (10/12px), drops to 16px in dialogs and the launcher. Visually correct (dialogs deserve more breathing room) but no token names this — easy to drift further.
**Fix:** Define `--pad-tight: 8`, `--pad-base: 12`, `--pad-loose: 16`. Use everywhere.

### F16 — NOTE · shadow · Two depths only — fine, but inconsistent application

**Location:** `--shadow: 0 10px 30px rgba(24,32,42,0.09)` at root, `0 24px 70px rgba(15,23,42,0.24)` inline at `.run-inspector` (line 1651), `0 1px 4px rgba(24,32,42,0.1)` inline on `.view-tabs button.active` (line 362).
**Problem:** Two well-thought shadows + one tiny tab-active shadow that is its own value. Also `.toolbar` (line 334) uses `border-bottom` instead of shadow, while `.panel-heading` (line 1036) uses border-bottom — consistent, good. But `.run-inspector` should reference a `--shadow-modal` var.
**Fix:** Define `--shadow-card`, `--shadow-modal`, `--shadow-tab-active`. Apply via var.

### F17 — NOTE · typography · Single weight does most of the heavy lifting

**Location:** Frequency: weight 700 = 17 uses, 600 = 5, 500 = 1, 800 = 1.
**Problem:** weight 700 is used both for "small uppercase label" (e.g., `.focus-copy span`, `.task-status`, `.task-column header`) and for "emphasis strong" (e.g., `.failure-summary strong`). Same weight, opposite intent. Weight 800 used **once** on `.proof-story > span` (line 1819) — outlier.
**Fix:** Use 600 for emphasis-strong, 700 for caps-eyebrow labels, 500 for body-strong, drop 800.

### F18 — NOTE · whitespace · Diagnostics view feels denser than tasks

**Location:** `styles.css:402–410` (mission-layout 14px gap, 14px padding) vs `770–778` (diagnostics-layout same) — but diagnostics-grid (line 788) packs 4 sections into the same vertical column with 14px gaps, while tasks-layout (`.main-stack`, line 412) has only 3.
**Problem:** Same outer rhythm, more inner density on diagnostics. No visual cue tells the user diagnostics is the "denser" view; it just feels cluttered when both panels populate.
**Fix:** Either reduce diagnostics-grid to 3 panels (collapse a less-critical one into a tab) or expand the gap to 18–20px on diagnostics.

### F19 — NOTE · color · Link/visited state never defined

**Location:** No `a:visited` rules. Sole `<a>` styling at `.proof-report-actions a` (line 1781) — looks like a button.
**Problem:** Open-HTML-proof links don't differentiate visited; users can't tell which proof reports they've already opened.
**Fix:** `a:visited { color: var(--magenta); }` on plain links, plus a `data-visited` indicator on the proof-report-action button (e.g., a small dot or "(viewed)" suffix).

### F20 — NOTE · alignment · Activity-item grid columns are jagged across rows

**Location:** `styles.css:735–748` — `.activity-item` uses `grid-template-columns: 76px minmax(0, 1fr) auto`. Mixed with `RecentActivity` rendering both events (severity span + message + time) and history (status pill + run id + time). The 76px first column holds severity for events but holds a status pill for history — different content widths inside same fixed track.
**Problem:** Severity strings ("INFO", "ERROR") are 4–5 chars; status pills are wider (`waiting for output`, `cancelled`). 76px clips the latter or leaves the former floating.
**Fix:** Auto-size first column with `auto` (let widest content set width) and right-align the eyebrow text inside the cell. Or split into two grid templates per row type.

---

## Counts

| Severity | Count |
|---|---|
| CRITICAL | 3 (F1 token discipline, F2 dark mode, F3 no active state) |
| IMPORTANT | 10 (F4–F13) |
| NOTE | 7 (F14–F20) |
| **Total** | **20** |

| Theme | Count |
|---|---|
| color | 5 (F1, F9, F10, F12, F19) |
| dark-light | 1 (F2) |
| active | 1 (F3) |
| disabled | 1 (F4) |
| hover | 1 (F5) |
| typography | 2 (F6, F17) |
| spacing | 2 (F7, F15) |
| borders | 1 (F8) |
| alignment | 2 (F11, F20) |
| brand | 1 (F13) |
| icons | 1 (F14) |
| shadow/depth | 1 (F16) |
| whitespace | 1 (F18) |

| Effort | Count |
|---|---|
| S | 8 (F3, F4 partial, F8, F11, F15, F16, F19, F20) |
| M | 9 (F1, F4 full, F5, F6, F7, F9, F10, F12, F17, F18) |
| L | 3 (F2 dark mode, F13 brand, F14 icon set) |
