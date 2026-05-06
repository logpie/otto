---
name: otto-frontend-rua
description: Visual + interaction + performance + accessibility RUA (Real-User-Audit) of Otto Mission Control. Multi-round, multi-pass, multi-step audits using chrome-devtools MCP. Each round catches what the previous round physically couldn't. Output a numbered bug catalog with file:line citations, screenshots, lighthouse scores, and a 4-track fix action plan.
---

# Otto Frontend RUA — visual + interaction + performance + a11y audit

## Overview

Otto's frontend ships data plumbing correctly (RunView serializer,
polling hooks, route components) but visual polish, interaction-flow
bugs, and performance/a11y issues hide in places unit tests can't see.
This skill drives a **multi-round, multi-pass, multi-step** audit that
progressively surfaces bugs each prior pass physically could not catch.

**Why the rounds matter** — empirical evidence from the 2026-05-05 audit:

| Round | Method | Bugs found |
|-------|--------|-----------:|
| 1 | 5 static screenshots, single-pass each | 7 macro layout problems |
| 2 | 14 screenshots × 4–5 vision passes + a11y + mobile + modal + drilldown | 24 bugs (root cause identified) |
| 3 | Multi-step interaction simulation: edit → save → save → diff → approve → cancel | 8 brand-new bugs invisible to any screenshot |
| 4 (new) | Network waterfall + console + performance trace + bundle | TBD |
| 5 (new) | Lighthouse + axe-core + keyboard + colorblind + i18n stress | TBD |

Round 3 alone caught: silent destructive actions (Cancel discards
unsaved edits), state bugs (`versions.length < 2` hides v1 forever),
and React controlled-input pitfalls (`fill()` doesn't dirty the form).
None of these surface from screenshots OR from typecheck/build/unit tests.

When in doubt: do another round. They keep paying off.

## When to use

- After any frontend change claimed "done" but verified only by
  `npm run web:typecheck && web:build`. **Type-checking does not
  verify what users see.**
- After a wireframe-driven redesign — to honestly score visual fidelity.
- Before a release / merge to main.
- When a user says "is the UI good?" — never answer without running this.
- Periodically as the codebase evolves (regressions creep in via
  data-class renames, removed CSS utilities, etc.).

## Non-negotiable product-level gate

RUA is not a component existence test. It must answer whether a real user can
understand and act on Mission Control from the actual entry point. Before
claiming the frontend is usable, run this product-level gate against the live
server and the same URL a user would open.

For each core surface, first write the user's immediate job in one sentence,
then inspect the screen against that job. Examples: "choose a project", "queue
a new job", "understand whether this run passed", "review and approve the
spec", "open proof evidence". If the screen has all the right DOM nodes but the
job is not obvious in five seconds, it fails RUA.

Minimum product-level checks:

- Start at the real launcher URL, not a deep-linked fixture route. Verify the
  no-project state first, then select a project and verify the selected-project
  state.
- Test desktop-first viewports that match real use: at least 1280x800 and
  1440x900; use 1920x1080 as a wide sanity check. Mobile can be deferred for
  pre-release Otto, but laptop desktop cannot.
- The first screen must make these visible without scrolling: where the user is,
  whether a project is selected, the primary next action, and the primary
  worklist or review surface. A giant hero, marketing copy, or decorative blank
  space cannot dominate the first viewport.
- The launcher must behave like an operational workspace: project list/search
  is the primary region on desktop; create/help can be a rail; useful project
  rows are above the fold; refresh is in a predictable header/action position.
- Navigation must match the product model: brand/project-home click returns to
  the project launcher when launcher mode is enabled; run cards open the
  expected drawer or detail surface without surprising route jumps; deep links
  remain available when intentionally opened.
- Project context must be explicit after selection. A list of unknown historical
  runs without the selected project identity is a failure even if `/api/run-view`
  succeeds.
- Inspect the browser console and network panel during this gate. A normal
  launcher load must not issue `/api/run-view` before a project is selected,
  and must not show first-load 4xx/5xx for expected UI requests.

Use measurable visual oracles when fixing layout bugs:

- Add browser assertions for bounding boxes, not just text presence. Examples:
  primary workflow region starts high in the viewport, is wider than secondary
  rails, and has actionable rows/buttons visible above the fold.
- Pair the numeric assertions with a saved screenshot and a short verdict:
  "Would a real user know what to do in five seconds? yes/no, because ...".
- If a screenshot is technically centered but still looks like a marketing
  splash or low-density debugging page, call it out as a product-level failure.

Any RUA report that only says "elements render", "routes click", or
"typecheck/build passed" is incomplete.

## Tiered rigor — calibrate effort to product stage

Otto is a developer tool used by a small known audience pre-release.
Don't waste compute on production-grade public-release polish until
the audience expands. **Default to the pre-release tier unless the
user explicitly asks for broader rigor.**

### Pre-release tier (Otto today — use this)

The 5-second test is "can the user read what's on screen and act on
it correctly?" Everything below is in service of that.

**MUST verify:**
- **Product-level first-screen usefulness** — from the actual Mission Control
  entry URL, a real user can identify the current project/no-project state and
  next action in five seconds. The launcher and selected-project landing must
  look like a usable app, not a left-pinned debug dump, marketing hero, or
  low-density blank page.
- **Truthfulness of UI vs backend state** — UI must not lie. Examples
  from real Otto bugs: `versions.length < 2` hid v1 forever (B27);
  history sidebar didn't refetch after save (B26); approve silently
  flipped state (B30); 404 page showed `HTTP 404 Not Found + Retry`
  with no recovery (B19).
- **Don't lose user work** — destructive actions (Cancel with unsaved
  edits B31, navigate-away with dirty form, double-click submit) must
  confirm or auto-save.
- **Wireframe fidelity on core screens** — landing, run drawer, spec
  review, spec diff, feature drilldown. The load-bearing screens.
- **Layout coherence** — looks like a designed product, not a
  debugging dump. Two-column drawer (sidebar + body) where wireframed.
  Content hierarchy obvious (verdict before count before timestamp).
  No "wall of text" sections. Cards/sections have breathing room
  (whitespace tokens, not flush). Lists use sane row heights via
  `min-height`, not whatever-the-content-is.
- **UI / text clarity** — copy is plain English, not jargon-laden
  (`feature_id`, `proxy_only`, `multi-actor` are token strings —
  surface as "Feature ID", "Proxy-only", "Requires multi-actor").
  Heading hierarchy obvious. Body text size ≥14px, not microcopy.
  Numbers have units (`5m 13s` not `313`). Timestamps relativized.
  Empty states say what would be there + how to make it appear,
  not just "Nothing here".
- **De-duplication** — same action doesn't appear in two places
  (header + footer + sidebar all expose `Approve`?). Same info
  doesn't render twice (status pill in header + status word in body
  + status icon next to verdict + status in URL crumb). Pick one
  primary site per surface; remove the rest.
- **Cross-screen consistency** — Run drawer + Spec review + Spec
  diff feel like the same app. Same button style (filled primary,
  outlined secondary, link tertiary — pick & honor). Same card
  chrome (border, radius, padding, shadow). Same typography (one
  body size, one mono size, one heading scale). Same color tokens
  for status (green=passed identically across all screens). Same
  spacing rhythm.
- **Button / text style discipline** — no two button styles on the
  same screen ("Save" looks different from "Approve" without a
  reason). Disabled state visibly distinct (lower contrast, no
  hover). Primary actions filled, secondary outlined, never both
  filled in the same row. Links underlined or distinctly colored
  with hover state. No `<div>` styled as a button.
- **Readability at 5 seconds** — text NOT concatenated (B1); status
  pills VISUALLY distinguishable (color + icon + label, NOT raw text);
  KPI rows have visible separators; severity badges have contrast.
- **Live polling reflects state** — running runs visibly update; the
  3s `useRunView` poll cadence works.
- **Every documented flow completes end-to-end** — edit→save→approve,
  pause→resume, abort, view diff. Use Round 3 interaction simulation.
- **Console + network sanity** — no React hydration warnings, no
  unmounted-setState, no duplicate API calls per drawer open. These
  destroy debugging when they pile up.
- **No-blank loading state** — never show a blank screen during
  fetch. Minimum: a spinner, "Loading…" text, or a single-line
  skeleton. (Full skeleton matching final layout = polish; defer.)
- **No content shift (CLS) after initial fetch** — when data loads,
  content shouldn't jump. Reserve container heights with
  `min-height` or skeletons. (LCP/INP perf metrics = defer; CLS
  affects daily usability — keep.)
- **Microinteraction minimum** — hover/focus state transitions
  exist (100-250ms ease — not 0ms instant snaps; that feels
  broken). Modal open/close has SOME animation (even just a 150ms
  fade). Disabled→enabled doesn't pop. (Full motion design system =
  polish; defer. The minimum = "doesn't feel like a 1995 webpage".)
- **Browser zoom 125% / 150% works** — devs often work zoomed in.
  Use `rem` for typography and spacing where possible; don't trap
  content in fixed-px containers that cut off when scaled. Test by
  Cmd+Plus a few times.
- **Initial-load smell test** — does the page feel snappy on
  localhost? If the bundle is so large it takes >2s to render on
  localhost, something is wrong (memory leak, infinite re-render,
  bundle bloat). NOT a Lighthouse Performance score target — just
  "doesn't feel sluggish".
- **Basic design-token consistency** — type sizes from a small set
  (not "every heading a random px"). Border radii from a small set
  (not "buttons 4, cards 6, modals 7"). Shadows used sparingly +
  consistently. The exact ratio (1.25 vs 1.333) is polish; the
  EXISTENCE of any rhythm is pre-release.

**Skip (defer until public release):**

Audience-scale concerns:
- Colorblind simulation (Otto's user knows their pills work; ~5%
  protan/deutan/tritan audience matters at scale, not now).
- Full WCAG AA screen-reader audit.
- Keyboard navigation polish (Tab/Enter/Escape consistency, focus
  trap in modals, focus return on close, `:focus-visible` instead
  of `outline: none`). Otto's audience is small + uses mouse; can
  defer until broader release.
- i18n / RTL / Unicode stress (Otto's UI is en-US for now).
- Browser compat beyond Chrome (Otto runs locally; user picks one).
- Mobile / tablet viewports (Otto is a desktop dev tool; 1440×900 is
  the target).

Performance budget:
- Lighthouse Performance score / bundle-size budget < 250kB.
- LCP/CLS/INP performance metrics.
- `prefers-reduced-motion` / `prefers-reduced-data`.
- Print stylesheet.

Aesthetic micro-tuning:
- Brand voice through typography (functional > aspirational).
- Iconography family unification (✓/✗/⊘ emoji is fine for now —
  but DO ensure they render at consistent size). Note: emoji
  render differently across OSes; if your dev fleet mixes
  macOS/Linux/Windows, normalize to SVG. Otherwise defer.
- Type scale ratio precision (1.25 vs 1.333 vs 1.5 — the EXACT
  ratio is polish; "any consistent rhythm exists" is pre-release).
- Radius/shadow token PRECISION (the EXACT values are polish;
  basic consistency is pre-release).
- Full motion design system (ease curves, spring physics, staggered
  reveals) — the MINIMUM (transitions exist, modals fade) is
  pre-release; the system is polish.
- Empty-state illustrations + onboarding CTAs (clear copy is
  pre-release; pretty illustrations are polish).
- Skeleton loaders matching final layout (any non-blank loading
  state is pre-release; the matching-shape skeleton is polish).
- `prefers-reduced-motion` respect (most users default to motion
  on; polish for the minority. But: if you're shipping ANY
  animation > 300ms, gate it behind reduced-motion).

**Important — what stays IN the pre-release tier**: layout coherence,
UI/text clarity, dedup, cross-screen consistency, button/text style
discipline. These are NOT "polish" — they're whether the user can
understand and use the tool confidently every day. Confusing layout
or duplicated controls cost daily friction even on an audience of one.

**Concrete budget:** 1.5–2 hours of audit, 60% on Rounds 2–3 (visual +
interaction), 40% on truthfulness probes against the API + console.

### Public-release tier (when audience expands — defer)

Add Rounds 4 + 5 (perf + a11y + colorblind + i18n + browser compat),
plus full design-system aesthetics deep-dive (Pass 6 with all 17
axes). Budget: 2.5–4 hours.

This tier is documented below for reference but **don't run it on
pre-release Otto** unless explicitly asked.

## Tools required

- Browser automation that can inspect pixels, DOM/a11y tree, console, and
  network. Use the native tool for the current agent:
  - Claude: `chrome-devtools` MCP.
  - Codex: Playwright skill, agent-browser, or browser-use for local targets.
- Required browser capabilities:
  - Pixels: `navigate_page`, `take_screenshot`, `take_snapshot`,
    `resize_page`, `new_page`, `list_pages`.
  - Interaction: `click`, `fill`, `type_text`, `press_key`, `hover`,
    `drag`.
  - Diagnostics: `list_console_messages`, `list_network_requests`,
    `get_network_request`, `evaluate_script`,
    `performance_start_trace`, `performance_stop_trace`,
    `performance_analyze_insight`, `lighthouse_audit`,
    `take_memory_snapshot`, `emulate` (network/CPU throttling).
- `scripts/rua/seed_fixture_sessions.py` — seeds 3 fixture sessions.
- `scripts/rua/serve_fixture.py` — starts MC web server.
- (Optional) `axe-core` injection script for automated a11y rule checks.

## Pre-flight: seed fixture, then sanity-check

The seed script periodically rots when otto data-class field names
change (`title=`, `tasks=`, `deps=` were renamed in A2). **Always run
the seed first:**

```bash
PROJ=/tmp/rua-$(date +%H%M%S)
uv run python scripts/rua/seed_fixture_sessions.py "$PROJ"
# If it crashes with a TypeError on Group(...), patch:
#   sed -i '' 's/title=/name=/g' scripts/rua/seed_fixture_sessions.py
#   sed -i '' 's/g\.title/g.name/g' scripts/rua/seed_fixture_sessions.py
# Then re-run; flag the leftover for follow-up.
```

Start the server on a known-free port:

```bash
uv run python scripts/rua/serve_fixture.py "$PROJ" 8881 > /tmp/rua-server.log 2>&1 &
```

Kill at end (`kill $(pgrep -f serve_fixture)`).

## Audit protocol — 5 rounds

### Round 1 — static screenshots (~10 min) [PRE-RELEASE TIER]

Goal: product-level first-screen usefulness plus macro layout vs wireframe.

1. Start at the real Mission Control root URL in launcher mode.
2. Resize to 1280×800 and 1440×900 desktop. Capture the no-project launcher,
   then select a project and capture the selected-project landing. Do not skip
   this just because route-level screenshots exist.
3. For each screenshot, state the user's immediate job and answer the
   five-second question: can they tell where they are and what to do next?
4. Measure primary/secondary layout boxes when the answer depends on geometry:
   first actionable region top position, primary region width, secondary rail
   position, visible rows above the fold.
5. Resize to 1440×900 desktop for the remaining route/wireframe screens.
6. Walk every wireframe screen via the canonical URL pattern (see
   `otto/web/client/src/main.tsx` for the `?view=...` routing) and
   take a full-page screenshot of each.
7. Read each screenshot once, compare to wireframe, list visible
   problems.

### Round 2 — multi-pass per screenshot (~30–45 min) [PRE-RELEASE TIER, passes 1/3/4/5 only; pass 2 partial; pass 6 SKIP]

For pre-release Otto: do passes 1 (layout/spacing), 3 (color
distinguishability — NOT contrast), 4 (info completeness vs
wireframe), 5 (edge cases). Skip the typography-rhythm and
design-system axes — those are public-release polish.

For passes 2 + 6 (typography rhythm + full design-system aesthetics)
— these are the public-release tier. They're documented below for
reference; only run them when audience expands.

Goal: exhaustive bug-mining per surface.

1. Re-take the same screenshots **plus** these additional states:
   - Mobile viewport (375×812) for ≥3 screens.
   - Tablet (768×1024) — often catches breakpoint bugs.
   - Wide (1920×1080) — does layout cap reasonably?
   - Modal overlays (click `Edit` → `Add Feature`).
   - Disclosure expanded states.
   - Drilldown navigation.
   - Bad inputs (`?session=DOES-NOT-EXIST`).
   - **Long-content stress** — open a real session with many features
     OR temporarily edit a fixture spec to include a 50-feature list,
     a 1000-char intent, a feature with a 500-char acceptance.
   - **Hover** on every interactive element (links, buttons, rows) —
     screenshot the hover state.
2. **For each screenshot, run the 6-pass inner loop.** **MANDATORY,
   NOT OPTIONAL.** Empirical: in 2026-05-05 round-2 a single-pass
   review of 8 screenshots caught 3 bugs. A re-read of the SAME 8
   screenshots with the 6-pass framing surfaced **30+ additional
   bugs** the single pass had completely missed (page heading
   hierarchy inversion, KPI tabular-nums, audit-context pill
   styling, app shell consistency, diff theme inconsistency,
   multiple h1 elements on a page, etc.).

   **The inner loop has a strict protocol:**

   ```
   for screenshot in [each captured screenshot]:
     findings_so_far = []
     for pass in [1..6]:
       state your focus question explicitly ("Pass N — typography:
         is the heading hierarchy obvious + scaled? does the body
         use a single sans? do KPI numbers use tabular-nums?")
       list NEW findings produced by this pass (anything not yet
         in findings_so_far)
       append to findings_so_far
     if total findings_so_far is < 3 — RE-EXAMINE. Most surfaces
       have at least 3 things wrong with them; getting 0-2 means
       you skimmed. Run the 6 passes again with sharper questions.
   ```

   **Rules:**
   - Don't merge passes. Run pass 1, write findings, then pass 2,
     etc. If you find yourself reading the screenshot once and
     listing all problems at once, you're back to single-pass.
   - State the focus question at the start of each pass so future
     readers (and you re-running in a week) know which lens
     produced which finding.
   - If the screenshot looks "clean" on pass 1, that's the
     strongest signal you need passes 2-6. Pass 1 is layout/
     spacing — the most obvious dimension. Bugs in typography,
     color tone, info-completeness, edge-cases, design-system
     coherence are all INVISIBLE on a layout-focused first read.
   - After the screenshot, count findings. < 3 ⇒ re-run with
     sharper questions before moving on.

   | Pass | Focus | Specific things to look for |
   |------|-------|------------------------------|
   | 1 | Layout / spacing | Adjacent `StaticText` with no CSS gap → text concatenates ("passedA real-time chat..."); flush-to-edge; clipped labels; modal field-label overlaps; container-max-width; CLS during load |
   | 2 | Typography | Heading hierarchy h1→h2→h3 (no skipping); weight rhythm; mono font on IDs/timestamps; `font-variant-numeric: tabular-nums` on KPIs (else `1:01:23` shifts under `1:02:01`); line-height vs leading; orphan/widow lines on wrap |
   | 3 | Color / tone / contrast | Status pills colored (green/amber/red/blue/grey)? Severity badges (`critical`/`important`/`polish`) colored? Color used as ONLY semantic cue (colorblind fail)? WCAG AA contrast ≥4.5:1 body, ≥3:1 large? Dark mode exist + work? |
   | 4 | Information completeness | Wireframe promises X — is X present? Missing action buttons, breadcrumbs, KPI rollups, evidence-drilldown affordances. Empty states have a CTA? Error states have recovery? |
   | 5 | Edge cases / invisible bugs | Raw ISO timestamps not relativized; raw HTML comments leaking into markdown view; default browser bullets/`<ol>` numbers; raw `display: inline-block` thumbs (vs CSS grid); pointer-events traps; z-index stacking; overflow-x scrollbars; fixed elements covering content |
   | 6 | **Design system aesthetics** | See dedicated table below — typeface coherence, type scale ratio, palette discipline, spacing rhythm, radius/shadow scale, iconography consistency, density coherence, hierarchy balance, microinteraction quality, brand voice. |

#### Pass 6 — design-system aesthetics deep-dive

The other 5 passes catch bugs. Pass 6 catches the difference between
"works" and "feels designed". A senior frontend / design-aware
engineer scans for these:

| Aesthetic dimension | Healthy | Smell |
|---|---|---|
| **Typeface coherence** | One sans for body, one mono for IDs/timestamps. Loaded with proper fallback chain (`Inter, system-ui, -apple-system, ...`). Single font family on every screen. | Mixing Helvetica + Inter + Arial on different screens. Display-only fonts (Comic Sans, Pacifico) for body text. FOUT/FOIT flash on load (no `font-display: swap`). |
| **Type scale ratio** | Heading sizes follow a consistent ratio (1.25 minor third / 1.333 perfect fourth / 1.5 perfect fifth / 1.618 golden). e.g. 12 → 14 → 16 → 20 → 24 → 32. | Arbitrary `px` values (`<h1>` is 28px, `<h2>` is 19px, `<h3>` is 17.5px). Body is 16px on one screen, 14px on another. |
| **Type weight rhythm** | 400 body, 500/600 emphasis, 700 headers. Don't mix 350 / 450 mid-weights. Avoid `font-style: italic` for emphasis — use weight or color. | Bold-everything (500/600/700 mixed indiscriminately). Underlined non-link body text. ALL CAPS for non-acronym text. |
| **Color palette discipline** | 3–5 base hues (primary, neutral, success, warning, danger) + a 50/100/.../900 grayscale ramp. Every color is a token, not a hex. Pills are filled-bg + sr-only text. | 12 different greys across screens. Hex literals scattered in CSS. Status conveyed by hue alone (no icon + sr-only). Pill bg low-contrast (`#e6f3ff` on `#f9fafb`). |
| **Spacing rhythm** | Multiples of a base (4 or 8). e.g. `4 / 8 / 12 / 16 / 24 / 32 / 48 / 64`. Same gap between rows in a list. Same padding inside a card. | Random px values (`padding: 13px 17px`). Two cards with subtly different paddings (`16` vs `18`). Margin-collapse confusion (margins doubling unexpectedly). |
| **Radius scale** | Tokens (`--radius-sm: 4` / `md: 8` / `lg: 12` / `full: 999`). Buttons consistent. Cards consistent. Pills are full-radius. | Each button has a different radius. Sharp corners on cards next to rounded corners on buttons. |
| **Shadow / elevation scale** | 3–4 shadow tokens (`sm` / `md` / `lg` / `xl`). Modals/popovers higher than drawers higher than cards higher than rows. | Inline `box-shadow: 0 2px 4px rgba(...)` everywhere. Modal under tooltip. Or no shadows at all where elevation matters. |
| **Iconography consistency** | One icon family (Lucide, Phosphor, Material — pick one). Same stroke width. Same fill style. ARIA-hidden on decorative; sr-only label on functional. | Mixing emoji `✓ ✗ ⊘` with `lucide-check` SVG with `material-icon-error_outline`. Emoji for status pills (renders as colorful junk on different OSes). |
| **Density coherence** | Touch targets ≥ 44px tall on mobile, ≥ 32px desktop. Same row height in lists. Same padding in form fields. | Some buttons 24px tall, others 40px. List rows visually different heights based on content (no `min-height`). Squished forms with cramped inputs. |
| **Information hierarchy** | F-pattern reading. Most important info top-left. Verdict → count → timestamp. Visual weight (size, weight, color) matches semantic priority. | Important info buried under noise. Tiny verdict, huge KPIs. Equal visual weight on everything = nothing stands out. |
| **Visual weight balance** | Heavy elements (cards, headers) balanced with whitespace. Page doesn't feel either claustrophobic or sparse. | Wall of text. Or 80% empty space with one tiny widget. Cards stacked edge-to-edge with no breathing room. |
| **Affordances** | Buttons look clickable (filled bg, raised, hover state). Links underlined or distinctly colored + hover state. Disabled state visually distinct (lower contrast, no hover). Drag handles visible. | A `<div>` styled as a button (no cursor, no role). Underline-only links on a body of underlined text. Disabled buttons that look identical to enabled. |
| **Microinteraction quality** | Transitions 150–250ms ease-out on hover/focus. Active state on click. Subtle animations (color, transform — never width/height). Skeleton loaders smoother than spinners. | No transitions (instant snaps feel cheap). Long animations (>500ms) on every hover (laggy feel). Animated `width` on a 1000-item list (jank). `prefers-reduced-motion` ignored. |
| **Brand voice through typography** | Otto is a serious dev tool. Geometric sans (Inter, IBM Plex Sans) + monospace (JetBrains Mono, IBM Plex Mono) feels right. Subtle, not playful. | Comic Sans, Pacifico, anything cursive. Display fonts on body. Emoji-heavy UI. |
| **Cross-screen consistency** | Run drawer + Spec review feel like the same app. Same header bar, same button style, same card chrome, same color tokens. | Each screen looks like a different developer wrote it solo. Drawer uses sentence case headers; Spec review uses Title Case. Different button styles per screen. |
| **Empty state design** | Illustration (or icon) + headline + body + primary CTA. Tells the user what would be here and how to make it appear. | Just text ("No prior versions yet" — done). No CTA. No visual placeholder. User stares blankly. |
| **Loading state design** | Skeleton placeholders matching final layout. Or shimmer. Or progress with messaging ("Compiling spec…"). | Generic spinner in middle of page. Or no indicator at all (page just sits blank during fetch). |

3. **Cross-check with `take_snapshot` (a11y tree)**. Pixels lie about
   semantics — the "KPI line is 14 separate `StaticText` nodes"
   finding came from the a11y tree, not the screenshot. Look for:
   - Missing landmarks (`banner`/`main`/`navigation`/`complementary`/`contentinfo`).
   - Heading order skips (h1 → h3 with no h2).
   - Form fields without labels.
   - Buttons that should be links and vice versa.
   - `DisclosureTriangle` without an `aria-label`.
   - Lists rendered as raw `StaticText`.

### Round 3 — multi-step interaction simulation (~30–45 min) [PRE-RELEASE TIER — highest leverage; do this]

Goal: catch state-machine bugs that screenshots can't see.

For each interactive surface, simulate a real user journey end-to-end
with an a11y snapshot between every step:

0. **Launcher and landing doorway flow**:
   - Load Mission Control root with no project selected. Assert the project
     launcher is visible and no `/api/run-view` call is made before selection.
   - Select a real or fixture project. Assert the selected project identity is
     visible in the app shell/landing.
   - Click the brand/project-home control. Assert it returns to the launcher in
     launcher mode.
   - Click a run card. Assert the intended drawer/detail behavior and record
     whether URL navigation changed. Surprising page jumps are bugs unless they
     are the explicit design.
   - Save a screenshot and bounding-box evidence for the first screen.

1. **Spec review edit cycle**:
   - Click `Edit` → assert textarea appears.
   - `fill()` — programmatic fill DOESN'T trigger React's onChange on
     controlled inputs. Always follow with `type_text(" ")` to dirty
     the form. (B25)
   - Click `Save` → snapshot. **Verify SPEC HISTORY sidebar shows the
     new archived version.** Common bug: backend archives but frontend
     hook doesn't refetch (B26).

2. **History threshold probe**:
   - First save → if sidebar still says "No prior versions yet", check
     `curl /api/specs/<id>/versions`. If API returns `{"versions":[1]}`
     but UI lies, the empty-state condition is wrong (B27 — found at
     `SpecReviewPage.tsx:286 versions.length < 2`).

3. **SpecDiff completeness probe**:
   - Navigate to spec-diff. Are From/To dropdowns visible at all
     archive-counts? (B15).
   - With 1 archived version, is `current` selectable as comparison
     target? (B28).
   - With From === To, does body explain "pick different versions" or
     just render a blank diff? (B29).

4. **Approve / Cancel flow**:
   - Click `Approve` → confirm dialog? success toast? (B30).
   - Click `Edit` → fill nonsense → click `Cancel`. Does it confirm
     "Discard unsaved changes?" or silently destroy work? (B31).
   - Once approved: are Edit/Approve hidden + is there an "approved
     (read-only)" banner with re-edit affordance? (B32).

5. **Polling probe** (when a running fixture exists):
   - Watch `useRunView` cadence (3s default). Are state transitions
     announced via `aria-live`? (B22). Console-logged? (B20). Network
     waterfall — see Round 4.

6. **Bad-input probes**:
   - `?session=DOES-NOT-EXIST` — friendly "Run not found" + `[Back to
     runs]`, or HTTP-raw `Failed to load run: HTTP 404 Not Found` +
     useless `Retry`? (B19).
   - Whitespace-only edit → save accepted as a no-op?
   - Submit form with all required fields empty → inline errors?
   - Click rapidly multiple times on Save (double-submit) → debounced?

7. **Multi-tab / state-sync probes**:
   - Open the same session in 2 tabs. Edit in tab A and save. Does
     tab B notice or stay stale? Last-write-wins on conflicting edits?
   - Navigate Back/Forward through the SPA — does state restore?
   - Refresh mid-edit — does the textarea preserve dirty content?
     (Localstorage backup recommended.)

8. **Form micro-flows**:
   - Tab through every field — focus visible (`:focus-visible`)?
   - Enter on a single-line input submits the form?
   - Escape in a modal closes it?
   - Is focus trapped inside a modal while open?
   - Does focus return to the trigger button on close?
   - Browser autofill — does it work on email/password fields?
   - Password manager — visible affordance?

### Round 4 — performance + network + console (~20–30 min) [PUBLIC-RELEASE TIER — defer for Otto today]

For pre-release Otto, do ONLY a quick smoke of the console + network
panel: skim for **React errors / warnings** (hydration mismatch, key
missing, unmounted-setState) and **duplicate API calls per drawer
open**. Skip Lighthouse perf, bundle budget, LCP/CLS/INP, memory
snapshots, throttling — those are public-release-tier.

Goal: find runtime cost, error budget, network anti-patterns.

1. **Network waterfall** (`list_network_requests` after navigation):
   - Are static assets cached (304 Not Modified on revisit)?
   - JS bundle size — single chunk or code-split per route?
   - API request count on a single drawer open — should be 1–2, NOT
     5+ duplicate fetches.
   - Polling overhead — 3s × N tabs × hours = how many MB?
   - Slow APIs — anything > 200ms p50?
   - Failed requests — any 4xx/5xx mixed in with successes?
   - Preflight CORS overhead.

2. **Console errors + warnings** (`list_console_messages`):
   - Filter `types: ["error", "warn", "issue"]`.
   - Any React hydration warnings? (Server/client HTML mismatch.)
   - `Warning: Each child in a list should have a unique "key"`?
   - `Warning: Can't perform a React state update on an unmounted component`?
   - Deprecated DOM APIs (`document.write`, etc.)?
   - 404s for missing assets / fonts?
   - CORS errors hidden in production?

3. **Performance trace** (`performance_start_trace` + interaction +
   `performance_stop_trace` + `performance_analyze_insight`):
   - Largest Contentful Paint (LCP) ≤ 2.5s?
   - Cumulative Layout Shift (CLS) ≤ 0.1? (CLS shows up when content
     loads after layout settles — common with web fonts.)
   - First Input Delay / INP ≤ 200ms?
   - Long tasks > 50ms blocking main thread?
   - Memory leak — open and close the drawer 50× via script; does
     heap grow unboundedly? (`take_memory_snapshot`.)

4. **Network throttling + offline** (`emulate`):
   - Slow 3G — does the drawer still render usefully? Are spinners
     visible during fetch? Does polling back off?
   - Offline mid-poll — is the failure surfaced or swallowed?

5. **Bundle inspection** (run `npm run web:build` and inspect):
   - Total JS size ≤ 250 kB gzip is healthy for an MC tool.
   - Any third-party that's not pulling weight (huge moment.js when
     `Intl.DateTimeFormat` would do)?
   - Source maps present in dev, stripped in prod.

### Round 5 — accessibility + colorblind + i18n + browser compat (~30 min) [PUBLIC-RELEASE TIER — defer for Otto today]

For pre-release Otto, **skip this entire round.** Document as a
follow-up gate to run before broader release. Reasoning: Otto's
audience is ~1 user who picks the browser, runs locally, and reads
en-US. WCAG / colorblind / RTL / Safari quirks matter when audience
expands.

Goal: the parts of "it works" that production-quality tools need.

1. **Lighthouse audit** (`lighthouse_audit`):
   - Run for Performance / Accessibility / Best Practices / SEO.
   - Target: Accessibility ≥ 95, Performance ≥ 80, BP ≥ 90.
   - Read every flagged item; note even "passes" that are 1 bug from
     becoming fails.

2. **axe-core injection** via `evaluate_script`:
   ```js
   const s = document.createElement('script');
   s.src = 'https://cdn.jsdelivr.net/npm/axe-core@4/axe.min.js';
   document.head.appendChild(s);
   await new Promise(r => s.onload = r);
   const r = await axe.run();
   return r.violations.map(v => ({ rule: v.id, impact: v.impact, nodes: v.nodes.length }));
   ```
   Look for: `color-contrast`, `label`, `aria-required-attr`,
   `landmark-one-main`, `region`, `link-name`, `button-name`,
   `image-alt`.

3. **Keyboard-only navigation**:
   - Disconnect/ignore the mouse. Tab through the entire app.
   - Can you reach every action? Open every modal? Submit every form?
     Close them?
   - Skip-to-content link present? (Should be the first focusable
     element.)
   - Focus indicator visible? (`:focus-visible` styling — not just
     `outline: none`.)

4. **Screen reader** (manual / pretend):
   - Read the page top-to-bottom via the a11y tree. Does it form a
     coherent narrative?
   - Are state changes announced (`role="status"`, `aria-live`)?
   - Are loading states announced?
   - Are errors associated with their fields (`aria-describedby` +
     `aria-invalid`)?

5. **Colorblind simulation** (use Chrome DevTools "Rendering" panel
   via `evaluate_script` to apply CSS filters):
   - `filter: grayscale(1)` — is everything still distinguishable?
   - Protanopia / Deuteranopia / Tritanopia — Otto's status pills
     (green=passed, red=blocked, amber=partial) collapse into mush
     for ~5% of male users without a non-color cue.

6. **i18n / unicode stress**:
   - Edit a fixture spec to insert German compound nouns
     ("Donaudampfschifffahrtsgesellschaftskapitän") in a Group title
     — does it overflow gracefully?
   - Insert Arabic/Hebrew (RTL) — does layout flip correctly with
     `dir="rtl"`?
   - Insert emoji (👨‍👩‍👧‍👦, ZWJ sequences) — render correctly?
   - Insert zero-width chars (U+200B) — visible bug or quietly
     accepted?

7. **Browser compat spot-check**:
   - Test in Safari (date inputs, scroll behavior, `:has()` support).
   - Test in Firefox (CSS subgrid, `dialog` element).
   - Print stylesheet — does the proof-packet print as intended?

8. **Reduced motion + reduced data**:
   - `prefers-reduced-motion: reduce` — animations respected?
   - `prefers-reduced-data` — heavy assets gated?

## Bug categories an expert hunts (cheat sheet)

When reading screenshots/snapshots, scan for these patterns:

| Category | Symptoms | Root cause / fix |
|----------|----------|-------------------|
| **Text concat** | "passedA real-time chat", "✓Email + password loginUsers..." | Adjacent `StaticText` siblings without flex/grid gap. `display: flex; gap: 8px;` on row container. |
| **Status as text** | `partial`, `blocked`, `passed` rendered in body color | Wrap in `<Pill tone="warn">` / `<Pill tone="error">` / `<Pill tone="ok">`. |
| **Severity flat** | `critical Stripe checkout fails...` indistinguishable from `polish DM avatars...` | `<Badge severity="critical">` with red bg. Color + icon + sr-only text. |
| **Empty-state lies** | "No prior versions yet" while API returns `{"versions":[1]}` | Wrong threshold (`<2` instead of `<1`); split list rendering from compare-controls. |
| **Stale fetch** | UI doesn't update after a write | Refetch on mutation OR optimistic update OR SWR mutate. |
| **Silent destruction** | Cancel/Approve flips state without dialog or toast | `<ConfirmDialog>` for any destructive verb; toast for any state change. |
| **Disabled-without-reason** | Save button greyed; user doesn't know why | `aria-disabled` + tooltip explaining ("Edit content to enable Save"). |
| **HTTP-raw errors** | `Failed to load run: HTTP 404 Not Found` | Map status codes to friendly copy + recovery link. |
| **Raw timestamps** | `2026-05-05T17:43:45Z` in UI | `Intl.RelativeTimeFormat` + `<time dateTime="..." title="...">`. |
| **Comment leakage** | `<!-- group: auth -->` rendered as visible text in markdown | Strip via remark plugin OR provide a structured form view. |
| **No focus indicator** | Tab key moves focus invisibly | Use `:focus-visible` (don't `outline: none`). |
| **Modal not trapping** | Tab escapes modal back to body | Use `<dialog>` element OR react-aria FocusScope. |
| **Polling without aria-live** | Status updates land silently for screen-reader users | Wrap polled region in `<div role="status" aria-live="polite">`. |
| **Number jitter** | KPIs `1:23` then `2:01` — column width shifts | `font-variant-numeric: tabular-nums`. |
| **Layout shift on load** | Content jumps after ~500ms | Reserve space (skeleton, aspect-ratio, fixed dimensions). |
| **Pointer-events trap** | Click bubbles to wrong element | Audit `pointer-events: none` on overlays; use `<dialog>` proper. |
| **No back-restore** | Forward then back loses form state | `history.state` + restore on `popstate`. |
| **z-index whack-a-mole** | Modals under tooltips, tooltips under headers | Establish z-index scale tokens; document stacking contexts. |
| **Mixed scroll containers** | Drawer scrolls inside main scrolls inside modal | One scroll container per region; `overscroll-behavior: contain`. |
| **Hex literals in CSS** | `#3b82f6` / `#e2e8f0` scattered everywhere | Centralize via CSS custom properties / design tokens. Audit with `grep -rE '#[0-9a-fA-F]{3,6}' otto/web/client/src/`. |
| **Mid-weight type chaos** | Bold (700) headlines next to semibold (600) callouts next to medium (500) emphasis with no rhythm | Pick 3 weights max (regular/medium/bold). Document as tokens. |
| **Inconsistent radii** | Buttons rounded 4px, cards rounded 6px, modal rounded 8px, pills rounded 12px — no scale | Token: `--r-sm: 4 / --r-md: 8 / --r-lg: 12 / --r-pill: 999`. |
| **Inconsistent shadows** | Inline `box-shadow: 0 2px 4px rgba(0,0,0,0.1)` on each card | Tokens: `--shadow-sm/md/lg/xl`. Use them. |
| **Mixed icon families** | `✓` emoji next to `<lucide-check>` SVG next to Material icons | One family. ARIA-hidden on decorative; sr-only label on functional. |
| **Density jitter** | Same widget renders 32px tall on one screen, 40px on another | Token: `--row-height: 32` (desktop) / 44 (mobile-touch). Apply via `min-height`. |
| **No empty-state design** | "No items yet" text, nothing else | Add: icon + headline + body + primary CTA. |
| **No loading-state design** | Page blank during fetch | Skeleton placeholders matching final shape. Match layout to avoid CLS. |
| **Cross-screen drift** | Each screen has its own header style/spacing | Shared layout components (`<PageHeader>`, `<Card>`, `<ActionBar>`). |

## Anti-patterns

- **One round and done.** You will miss B25–B32-class bugs every time.
  Always do at least Round 2 + Round 3.
- **Single-pass per screenshot.** Round-2 of 2026-05-05 caught 3 bugs
  on single-pass; the same 8 screenshots re-read with proper 6-pass
  framing surfaced 30+ more. EVERY screenshot needs all 6 passes —
  layout, typography, color, info-completeness, edge-cases, design-
  system aesthetics — even when the screenshot looks "fine" at first
  glance.
- **Skipping a11y snapshots.** Pixels lie about semantics.
- **Single viewport.** Mobile + tablet + wide reveal different bugs.
- **Screenshots without interactions.** B30/B31/B32-class silent
  destructive flows are invisible without clicking.
- **`fill()` without follow-up keystroke.** React controlled inputs
  ignore programmatic value mutation.
- **Trusting "typecheck + build green" as UI verification.** They only
  verify TypeScript shape, not what users see.
- **Component-level RUA only.** Clicking each card and checking that a page
  loads is not a real-user audit. You must inspect whether the desktop first
  screen is useful, dense enough, and navigationally coherent from the actual
  entry URL.
- **Centered but unusable.** A layout can pass "centered" geometry while still
  failing product UX because a hero, blank space, or secondary card dominates
  the first viewport.
- **Ignoring console / network panels.** A clean-looking UI can hide
  3 React warnings + 5 duplicate fetches per drawer open.
- **Skipping Lighthouse / axe.** Manual a11y review misses ~50% of
  WCAG violations that automated tools catch trivially.
- **Audit only happy-path data.** Long content, RTL text, and dense
  fixtures expose bugs that a 3-feature seed never will.

## Output format

Every audit produces `docs/rua/<DATE>-<topic>/REPORT.md` with:

- **TL;DR** (3 sentences max).
- **Methodology** (rounds run, tools used).
- **Product-level verdict** for the launcher and selected-project landing:
  five-second answer, screenshots, viewport sizes, and bounding-box evidence.
- **Captured screens** table (file → state).
- **Numbered bug catalog** (B1, B2, ...) — each bug names file or
  visible string, wireframe expectation, fix hint.
- **Aesthetics scorecard** across ~12 dimensions (1–5 each).
- **Lighthouse scores** + axe-core violations summary.
- **Network/console highlights**.
- **4-track action plan**:
  - **P1 — single CSS pass** (`display: flex; gap: 8px;` — fixes
    ~70% of visual bugs).
  - **P2 — layout architecture** (cards, header bars, action rows).
  - **P3 — interaction flow** (confirm dialogs, refetch on save,
    feedback toasts, focus management).
  - **P4 — performance + a11y + telemetry** (Lighthouse fixes,
    `aria-live`, console logging, bundle trimming).

## Reference

- `docs/rua/2026-05-04-172101/` — initial RUA, 16 screenshots.
- `docs/rua/2026-05-05-post-merge/` — full 3-round audit, 32 numbered
  bugs. Use as the template; round-4/5 are still to be added on a
  future pass.

## Cost

Zero LLM cost. Human time ~70 min for a 3-round pass; ~2.5 hours for
a full 5-round expert audit. Worth it before any release.
