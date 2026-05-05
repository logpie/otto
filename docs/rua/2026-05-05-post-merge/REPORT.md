# RUA — visual UI/UX audit (post round-3 + codex merge)

Date: 2026-05-05
Worktree HEAD: `1564a2d33` (after fast-forward merge of codex-i2p-v2)
Server: `scripts/rua/serve_fixture.py /tmp/rua-cc-i2p-2-101927 8881`
Fixture: 3 sessions (passed / partial / blocked) seeded by
`scripts/rua/seed_fixture_sessions.py`. **The seed script had stale
`title=` / `g.title` field accesses left over from the A2 rename;
fixed inline before audit could proceed.**

## TL;DR

**The frontend works as a debugging surface for engineers. It is NOT
visually close to the wireframes.** Functional rendering is correct
(data flows, status colors land, edit/approve actions wire up); but
typography, spacing, layout architecture, and information density are
all far below the wireframe target. Call it ~30–40% of the wireframe
intent shipped.

This is the gap codex should pick up.

## Screen-by-screen findings

### Screen 1/2 — Landing page (`01-landing.png`)

Wireframe Screen 2 specifies status-pill cards per Run with intent
text, count rollups (features/capabilities/quality), wall+cost,
group summary, and inline `[Review spec]` buttons for awaiting-review
runs.

**Reality:** an `<h1>` "Otto Mission Control", "3 sessions", and a
`<ul>` of three plain underlined-blue session-id strings
(`2026-05-04-passed-abc123`, etc.). No status pill. No intent text.
No counts. No wall/cost. No styling.

This is a placeholder, not a UI.

### Screen 3 — Run drawer · passed (`02-rundrawer-passed.png`)

Wireframe specifies a two-column layout (runs sidebar + drawer),
header bar with status pill + verdict count + wall/cost row, intent
preview with "▸ View full intent", action buttons
`[Open proof packet] [View spec] [Logs] [Files]`, then per-Feature
verdict rows with ✓/⚠/✗ icons + name + acceptance + "▸ evidence
(N items)" drilldown affordance, then Guardrails section with
✓/⊘/✗ tones, then collapsible Groups summary, then Stage timeline.

**Reality observed problems:**

- **Text concatenation everywhere** — CSS spacing missing:
  - `passedA small markdown note-taking webapp...` (status pill text
    glued to intent)
  - `3/3 featuresquality: 0 criticalwall 5:13cost $1.84` (KPIs
    jammed into one unstyled string with no separators)
  - `✓Email + password loginUsers sign in with email...` (status
    icon glued to feature name, name glued to acceptance text)
  - `compiledone29s` (stage name + status + duration concatenated)
- No two-column layout. No runs sidebar.
- No header bar with status pill + ✕ close.
- No `[Open proof packet]` / `[View spec]` / `[Logs]` / `[Files]`
  action buttons anywhere.
- No "▸ evidence (N items)" drilldown affordance per Feature.
- "Stages" rendered as a numbered `<ol>` (1. compiledone29s, 2.
  spec_reviewdone34s, …). Wireframe shows a horizontal timeline.
- "▶ 2 groups" is a bare disclosure caret with no visible group
  cards even when expanded (would need to click to confirm).

### Screen 3 — Run drawer · blocked (`03-rundrawer-blocked.png`)

Same problems as passed. **One bright spot:** the Guardrail failure
card renders as a clean red-bordered row with ✗ icon and the
guardrail text — this matches wireframe tone for "verified=failed".
Round-3's Guardrail tone work landed correctly.

### Screen 4 — Spec review (`04-spec-review.png`)

This is **the best-looking screen** but still off-target:

**What works:**
- Card layout for the spec body with proper border + spacing.
- "SPEC HISTORY" sidebar to the right (matches wireframe 4 / 4a's
  intent of version listing) — empty-state text "No prior versions
  yet" is correct for a fresh fixture.
- Edit + Approve buttons present.

**What's wrong:**
- Header is `Spec review` + the literal text `draftupdated 2026-05-05T17:19:27Z`
  — the lifecycle ("draft") and "updated" label are concatenated, and
  the ISO timestamp is presented raw, not as "Updated 4m ago".
- The markdown body shows raw HTML metadata comments to the user:
  `<!-- group: auth -->`, `<!-- feature: login | evidence: BrowserJourney -->`.
  These belong in the structured editor (wireframe 4b) or be parsed
  out, not rendered as visible code in the user-facing markdown view.
- Edit + Approve buttons sit at the BOTTOM-LEFT. Wireframe Screen 4
  puts approval actions in the top-right header zone.
- No "Versions" / "Diffs" widgets within the history sidebar besides
  the empty state — once versions exist there's no test fixture
  here to validate the populated case (we'd need to perform an edit
  cycle).

### Screen 4d — Spec diff (`05-spec-diff.png`)

Wireframe specifies side-by-side or unified diff with from/to version
dropdowns and added/removed line styling.

**Reality:** "Spec diff" h1 + plain text "No archived spec versions
for session 2026-05-04-passed-abc123. A version is created each time
the spec is edited through the spec-review flow."

Empty state is honest, but there are no version dropdowns visible at
all (they only appear once N≥1 archived versions exist, per the
component code). Need a populated test fixture to truly evaluate
the diff layout.

## Aesthetics scorecard

| Dimension | Score | Notes |
|---|---|---|
| Information density | 2/5 | Concatenated text, no KPI separators, no Feature drilldown affordances |
| Layout architecture | 1/5 | No two-column drawer; landing is a `<ul>`; spec-review actions at bottom not top |
| Typography hierarchy | 2/5 | Headings exist but no scale rhythm; weights flat |
| Color / status tones | 3/5 | Guardrail green/red borders work; but status pills (landed/partial/blocked) at the top of cards aren't visible |
| Whitespace + spacing | 1/5 | Text concatenation is the dominant visual problem |
| Wireframe fidelity | 2/5 | ~30% of wireframe elements present; key surfaces (action buttons, sidebar, KPI row, evidence drilldown) missing entirely |
| Accessibility | not assessed | Focus states, ARIA, contrast all uninspected |
| Responsive | not assessed | Only 1440×900 captured |

## Honest call

The redesign **shipped its data plumbing** (RunView serializer,
useRunView polling, FeatureDrilldown component, SpecDiffPage routing)
but **did not ship the visual design**. The components render their
data correctly; they just don't apply the spacing/grid/typography
the wireframes demanded.

The earlier RUA pass at `docs/rua/2026-05-04-172101/` showed the
same problems pre-round-3 — none of the round-2/round-3 work
addressed the visual gap. It was bug-hunt + feature-add work, not
UI polish.

## Recommendation for codex

Promote a "Mission Control visual polish" task to the same priority
as the bug-hunt items. The work is mostly CSS:

1. Fix text concatenation (CSS gap / margin between status pills,
   names, descriptions, KPI rows). Likely a missing
   `display: flex; gap: 8px` on each row in `RunDrawer.tsx` /
   `FeatureList.tsx` / `Guardrails.tsx`.
2. Build the Screen 1/2 RunListLanding cards properly: status pill,
   intent, count rollup, wall+cost row, group summary,
   `[Review spec]` button for `awaiting_spec_review` rows.
3. Restructure RunDrawer to a 2-column layout with runs sidebar +
   header bar (status pill + ✕ close) + action button row + body.
4. Strip the raw `<!-- group: ... -->` HTML comments from the
   user-facing markdown render in `SpecReviewPage`. They're metadata,
   not user-visible content.
5. Format the spec-review timestamp as "Updated 4m ago" instead of
   raw ISO.
6. Move spec-review Edit/Approve buttons to the top-right header.
7. Replace the numbered-list Stages rendering with the horizontal
   timeline from wireframe Screen 3 footer.
8. Verify against wireframes 4a (markdown editor) and 4b (form
   editor) — they may not be implemented at all (wireframe says
   "Toggle: Markdown view ↔ Form view"; current SpecReviewPage shows
   only one view with no toggle).

The functional foundation is there. What's missing is the day a
designer sat next to a frontend dev with the wireframes open.

## Side bug found

`scripts/rua/seed_fixture_sessions.py` used pre-A2 field names
(`Group(id=..., title=...)`, `g.title`) and crashed before producing
fixtures. **Fixed inline during this audit** — `title=` →
`name=`, `g.title` → `g.name`. This is the same kind of leftover
the round-3 i2p_routes.py fix caught on the JS side. There may be
more in scripts/ — codex should re-grep `scripts/` for any
`Group(.*title=`, `\.tasks\b`, `\.deps\b` patterns referring to Spec
groups.
