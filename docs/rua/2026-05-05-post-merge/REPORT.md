# RUA — visual UI/UX audit (post round-3 + codex merge), 2nd pass

Date: 2026-05-05
Worktree HEAD: `1564a2d33` (after fast-forward merge of codex-i2p-v2)
Server: `scripts/rua/serve_fixture.py /tmp/rua-cc-i2p-2-101927 8881`
Fixture: 3 sessions (passed / partial / blocked) seeded by
`scripts/rua/seed_fixture_sessions.py`. Seed script had stale
`title=` / `g.title` field accesses left over from the A2 rename
(fixed inline before audit could run).

**Methodology**: 14 screenshots across 5 unique screens × multiple
states + interactions + viewport sizes. Each screenshot read
through 4–5 vision passes with different focus questions
(layout/spacing → typography → color/tone → information
completeness → edge cases). a11y snapshots cross-checked.

## TL;DR — same conclusion, more concrete

The frontend works as a debugging surface for engineers. **It is
NOT visually close to the wireframes.** The core failure is
mechanical: every status icon, severity tag, and KPI label is
rendered as `StaticText` directly adjacent to the next
`StaticText` with no CSS gap or margin — so they visually
concatenate. This is the single root cause behind ~70% of the
visible problems. Fixing it is one CSS pass over `RunDrawer.tsx` /
`FeatureList.tsx` / `Guardrails.tsx` / `GroupList.tsx` /
`StageTimeline.tsx` / `FeatureDrilldown.tsx` adding
`display: flex; gap: 8px;` (or equivalent) on the row containers.

The other 30% is missing wireframe scaffolding: no two-column
drawer, no header bar with action buttons, no horizontal stage
timeline, raw markdown HTML comments leaking, ISO timestamps
unformatted, no card visual on the landing page, brittle 404
error UX.

Aesthetics scorecard: **layout 1/5, density 2/5, whitespace 1/5,
typography 2/5, color/tone 3/5, wireframe fidelity 2/5**.

## Captured screens (14 total)

| # | File | View |
|---|------|------|
| 01 | `01-landing.png` | RunListLanding (3 sessions) |
| 02 | `02-rundrawer-passed.png` | RunDrawer (passed session, desktop) |
| 03 | `03-rundrawer-blocked.png` | RunDrawer (blocked session) |
| 04 | `04-spec-review.png` | SpecReviewPage (read mode) |
| 05 | `05-spec-diff.png` | SpecDiffPage (empty state) |
| 06 | `06-rundrawer-partial.png` | RunDrawer (partial session) |
| 07 | `07-rundrawer-partial-groups-expanded.png` | Same + Groups disclosure expanded |
| 08 | `08-feature-drilldown.png` | Per-Feature drilldown (partial / DM) |
| 09 | `09-spec-edit-mode.png` | SpecReviewPage edit mode (textarea) |
| 10 | `10-add-feature-modal.png` | AddFeatureModal (overlaid on edit) |
| 11 | `11-spec-edit-mobile.png` | Same as #10 at 375px viewport |
| 12 | `12-rundrawer-passed-mobile.png` | RunDrawer at 375px viewport |
| 13 | `13-feature-drilldown-blocked.png` | Per-Feature drilldown (blocked / Stripe) |
| 14 | `14-bad-session-error.png` | Error state for non-existent session id |

## Bugs catalog (numbered for codex)

### Critical visual bugs (every screen affected)

**B1. Text concatenation due to missing CSS gap.**
Every status icon, severity tag, and KPI label is rendered as
adjacent `StaticText` nodes with no flex-gap or margin. The a11y
snapshot of run-view shows the KPI line is built from 14 separate
nodes:

```
"1" "/" "3" " features" "quality:" " " "1" " critical" "wall " "7:01" "cost " "$2.91"
```

Spaces are sometimes baked into leading text (`" features"`,
`" critical"`, `"wall "`, `"cost "`) — fragile pattern. Visual
result: `1/3 featuresquality: 1 criticalwall 7:01cost $2.91`.

Examples (file:visual-string):
- `02-rundrawer-passed.png`: `passedA small markdown note-taking webapp...`
- `02-rundrawer-passed.png`: `3/3 featuresquality: 0 criticalwall 5:13cost $1.84`
- `02-rundrawer-passed.png`: `✓Email + password loginUsers sign in with email...`
- `02-rundrawer-passed.png`: `compiledone29s` × 7 stages
- `06-rundrawer-partial.png`: `△Send a direct messageproxy_onlymulti-actorUser opens a DM thread...`
- `07-rundrawer-partial-groups-expanded.png`: `Roomspassing1 feature` × 3 groups
- `08-feature-drilldown.png`: `polishDM notifications could include sender avatar.`
- `13-feature-drilldown-blocked.png`: `criticalStripe checkout fails — env var STRIPE_SECRET_KEY missing in test harness.`

**Fix**: each row container needs `display: flex; gap: 8px;
align-items: baseline;`. Don't bake spaces into label strings —
that's locale-fragile.

**B2. Status pills are not pills.**
`partial`, `blocked`, `passed`, `passing`, `pending`, `proxy_only`,
`multi-actor` all render as plain inline text in default body color.
Wireframe specifies colored pill chips: green for passed, amber for
partial, red for blocked, blue for in-progress, grey for pending.
Reality: monochrome text concatenated with name.

**B3. Severity badges (`critical` / `polish`) have no color.**
Findings render as `bullet · severity-text body-text`. The severity
should be a colored badge: red for critical, amber for important,
grey for polish. Currently all severities are body-text-grey.

### Structural bugs (architecture vs wireframe)

**B4. Landing page = `<ul>` of session-IDs.** No status pills, no
intent text, no count rollups, no wall+cost, no group summary, no
`[Review spec]` button on awaiting-review rows. Wireframe Screen 2
specifies cards. Current implementation has < 5% of the wireframe
intent.

**B5. RunDrawer is a single-column dump, not a drawer.** Wireframe
Screen 3 specifies: left column = runs sidebar, right column =
drawer with header bar + KPI row + action button row + body. Reality:
no sidebar, no header bar, no action buttons (`[Open proof packet]`,
`[View spec]`, `[Logs]`, `[Files]`).

**B6. Stage timeline is a numbered `<ol>`.** Wireframe shows a
horizontal Compile → Build → Audit → Render → Land stepper with
durations under each. Reality:

```
1. compiledone29s
2. spec_reviewdone34s
3. builddone3:24
4. seedpending—
5. auditdone59s
6. renderdone29s
7. landdone29s
```

**B7. Group rows lack wall/cost/actions.** Expanded Groups
disclosure shows `Roomspassing1 feature` × 3 lines. Wireframe
specifies per-Group: title + status + feature count + wall + cost +
`[diff] [logs]` action buttons. Wall, cost, and actions are absent.

**B8. Feature drilldown lacks breadcrumb + actions.** No `Run ›
Direct messages › Send a direct message` breadcrumb. No action
buttons (`[Open evidence dir]`, `[Re-audit just this Feature]`,
`[Logs]`). `Back to run` button is centered top instead of
left-aligned per wireframe.

### Spec review surface

**B9. Raw HTML comments leak into the markdown body.** The
SpecReviewPage renders the spec markdown including
`<!-- group: auth -->` and `<!-- feature: login | evidence:
BrowserJourney -->` as visible text. These are metadata for the
form view; should be parsed out of the markdown view (or the
component should toggle to wireframe 4b's structured form view —
currently no toggle exists).

**B10. Header date label runs together.** Renders
`draftupdated 2026-05-05T17:19:27Z` — three separate `StaticText`
nodes (`"draft"`, `"updated "`, `ISO`) with no separator. ISO
timestamp is also unfriendly; should be "Updated 4m ago" with the
ISO as a `title` attribute.

**B11. Edit/Approve buttons at bottom-left.** Wireframe Screen 4
puts approval actions in the top-right header zone. Currently they
sit in the page footer.

**B12. Edit-mode shows raw markdown textarea, not the structured
form view.** Wireframe 4b specifies a "Form view" toggle alongside
the markdown view. Reality: only the markdown textarea exists
(though `AddFeatureModal` is wired in as a partial form-style
affordance).

### AddFeatureModal

**B13. Field labels are clipped/cropped above their inputs**
(visible in `10-add-feature-modal.png` and especially
`11-spec-edit-mobile.png`). Looks like a floating-label CSS
positioning bug — labels are layered inside input borders.

**B14. Add to spec button mis-aligned.** "Cancel" left, "Add to
spec" far right — looks like `justify-content: space-between` with
no max-width. Should be right-aligned action group.

### SpecDiffPage

**B15. No version selectors visible in empty state.** Wireframe 4d
shows from/to dropdowns at top regardless of populated state.
Reality: empty state shows only the empty-state text. (Not
necessarily wrong — but inconsistent with wireframe header layout.)

### Mobile / responsive

**B16. RunDrawer at 375px viewport has same problems.** Mobile is
not a separate render path; same text-concat root cause.

**B17. SpecReviewPage stacks correctly at 375px** — spec history
sidebar moves below body. **However** the AddFeatureModal field
labels become visibly clipped (`11-spec-edit-mobile.png`).

**B18. Action buttons "Add Feature / Cancel / Save" sit flush to
left edge on mobile** with no padding. Hard to tap accurately.

### Error / empty states

**B19. Bad session id error is brutal.** "Failed to load run: HTTP
404 Not Found" + a `[Retry]` button. Retry on a bad id will fail
again. Should say "Run not found" + `[Back to runs]` link.

**B20. No console logging on success or fail.** Console messages
panel was empty for every navigation, including the 404. Suggests
no diagnostic logging from the frontend; debugging in production
will be harder.

### Accessibility

**B21. KPI line is split into 14 `StaticText` nodes** with no
parent semantic role. Screen readers will read each fragment
individually, which makes the line:
"1 / 3 features quality: 1 critical wall 7 colon 01 cost dollar 2 dot 91"
a confusing audio dump.

**B22. Status text not announced as state.** No `role=status` or
`aria-live`, so polling updates won't be announced.

**B23. Disclosure caret has no label.** "▶ 3 groups" works
visually but the disclosure widget needs `aria-label="Show 3 groups"`
or similar — currently "DisclosureTriangle" with no semantic name
beyond the visible text (which is good but minimal).

### Bonus bug from a11y inspection

**B24. AddFeatureModal Group dropdown shows
"`— Ungrouped —`" option but the spec dataclass currently doesn't
support null `group_id` cleanly.** Either drop the option, or wire
"Ungrouped" Features to land outside any Group (ambiguous; verify).

## Aesthetics scorecard

| Dimension | Score | Notes |
|---|---|---|
| Information density | 2/5 | Concatenated text, no KPI separators |
| Layout architecture | 1/5 | No two-column drawer; landing is `<ul>`; spec actions bottom not top |
| Typography hierarchy | 2/5 | Headings exist but no scale rhythm; weights flat |
| Color / tone | 3/5 | Guardrail green/red works; status pills/severity badges absent |
| Whitespace + spacing | 1/5 | Text concatenation is the dominant problem |
| Wireframe fidelity | 2/5 | ~30% of wireframe elements present |
| Empty / error states | 2/5 | Spec history empty-state OK; 404 is HTTP-raw |
| Mobile responsive | 3/5 | Stacking works; modal field labels clip; padding missing |
| Accessibility | 1/5 | StaticText fragments; no roles; no aria-live |

## Honest call

The redesign **shipped its data plumbing** (RunView serializer,
useRunView polling, FeatureDrilldown component, SpecDiffPage
routing, AddFeatureModal). The components correctly render their
data. But **they did not ship the visual design**: spacing,
typography, color, layout architecture, accessibility, error UX
all need a designer-led pass.

A single CSS gap pass on the row containers will lift ~70% of
the visible problems. The remaining 30% is real layout work
(landing cards, drawer header bar, horizontal stage timeline,
markdown comment-stripping, friendly error state, button
positioning).

## Recommendation for codex

Promote a **"Mission Control visual polish"** track to the same
priority as the bug-hunt items. Three sub-tracks:

**P1 — single CSS pass (~½ day, fixes ~70% of bugs):**
- Add `display: flex; gap: 8px; align-items: baseline;` to every row
  container in: `RunDrawer.tsx`, `FeatureList.tsx`, `Guardrails.tsx`,
  `GroupList.tsx`, `StageTimeline.tsx` (rename from `<ol>`),
  `FeatureDrilldown.tsx`, `SpecReviewPage.tsx` header.
- Strip leading/trailing spaces baked into label strings (fragile).
- Add color tokens for status pills (passed/partial/blocked/
  pending/in-progress) and severity badges (critical/important/
  polish) and apply them via small `<Pill>` and `<Badge>` components.

**P2 — layout architecture (~1.5 days):**
- Build the proper RunListLanding cards per wireframe Screen 2.
- Add the RunDrawer header bar (status pill + verdict + ✕ close).
- Add the action button row (`[Open proof packet] [View spec]
  [Logs] [Files]`).
- Replace numbered Stages list with horizontal timeline.
- Move SpecReviewPage Edit/Approve to top-right header zone.
- Add AddFeatureModal field-label CSS fix (likely a transform
  positioning bug).
- Friendly 404 page with `[Back to runs]` link.
- Format ISO timestamps as relative ("Updated 4m ago").
- Strip `<!-- comment -->` from markdown view in SpecReviewPage
  (or wire the wireframe 4b form view).

**P3 — accessibility + telemetry (~½ day):**
- Wrap KPI rows in `<dl>` with `<dt>`/`<dd>` semantics.
- Add `role=status aria-live="polite"` to the live-polling regions.
- Add `aria-label` to the Groups disclosure.
- Add console logging for fetch failures + status transitions
  (currently silent).

## Side bug found before audit could even run

`scripts/rua/seed_fixture_sessions.py` used pre-A2 field names
(`Group(id=..., title=...)`, `g.title`) and crashed before producing
fixtures. **Fixed inline.** Codex should re-grep `scripts/` for
more pre-A2 leftovers (`Group(.*title=`, `\.tasks\b`, `\.deps\b`
referring to Spec groups).
