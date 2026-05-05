# RUA — visual UI/UX audit (post round-3 + codex merge), v3

Date: 2026-05-05
Worktree HEAD: `1564a2d33` (after fast-forward merge of codex-i2p-v2)
Server: `scripts/rua/serve_fixture.py /tmp/rua-cc-i2p-2-101927 8881`

**Methodology**: 18 screenshots across 8 distinct screens × multiple
states + interactions + viewport sizes. Three audit rounds:
1. Round 1: 5 static screenshots, layout/spacing pass.
2. Round 2: +9 screenshots covering more states + 2 viewports +
   modal + drilldown + 4–5 vision passes per image.
3. Round 3: full multi-step interaction simulation — edit cycle
   (Edit → fill → save), verify history populated, Compare diff
   navigation, Approve flow, Cancel-with-unsaved-changes,
   approved-readonly state, programmatic-fill regression.

a11y snapshots cross-checked at every interaction to catch invisible
semantics.

## TL;DR

The frontend works as a debugging surface. **It is NOT visually
close to the wireframes.** Multiple interaction-flow bugs surfaced
on top of the static visual issues from the first two rounds:

- **B25 — fill() doesn't dirty the form** (programmatic input
  doesn't trigger React onChange; one keystroke after fill works).
- **B26 — first save doesn't refresh history sidebar.**
- **B27 — empty-state condition `versions.length < 2` hides v1
  always**; user can't see their freshly-archived v1 until a v2 is
  created.
- **B28 — SpecDiff doesn't expose the current working spec as a
  comparison target.**
- **B29 — `v1 → v1` default in dropdowns gives a meaningless
  "diff" with no indicator that this is a no-op.**
- **B30 — Approve has no confirmation dialog and no success
  toast.** Page silently updates.
- **B31 — Cancel silently discards unsaved edits with zero
  confirmation.** Real users will lose work.
- **B32 — Approved-state UI has no read-only indicator** beyond
  the tiny pill; no hint why Edit/Approve disappeared.

These join the 24 layout/spacing/aesthetic bugs from round 2 for a
**32-bug catalog**.

Aesthetics scorecard: layout 1/5, density 2/5, whitespace 1/5,
typography 2/5, color 3/5, wireframe fidelity 2/5, accessibility
1/5, mobile 3/5, empty/error states 2/5, **interaction flow 2/5**,
**state-change feedback 1/5**.

## Captured screens (18 total)

| # | File | View / state |
|---|------|------|
| 01 | `01-landing.png` | RunListLanding (3 sessions) |
| 02 | `02-rundrawer-passed.png` | RunDrawer (passed, desktop) |
| 03 | `03-rundrawer-blocked.png` | RunDrawer (blocked) |
| 04 | `04-spec-review.png` | SpecReviewPage (read mode, draft) |
| 05 | `05-spec-diff.png` | SpecDiffPage (empty state) |
| 06 | `06-rundrawer-partial.png` | RunDrawer (partial) |
| 07 | `07-rundrawer-partial-groups-expanded.png` | Groups expanded |
| 08 | `08-feature-drilldown.png` | Per-Feature drilldown (DM) |
| 09 | `09-spec-edit-mode.png` | SpecReviewPage edit mode (textarea) |
| 10 | `10-add-feature-modal.png` | AddFeatureModal (overlaid on edit) |
| 11 | `11-spec-edit-mobile.png` | Add modal at 375px viewport |
| 12 | `12-rundrawer-passed-mobile.png` | RunDrawer at 375px |
| 13 | `13-feature-drilldown-blocked.png` | Drilldown for Stripe/blocked |
| 14 | `14-bad-session-error.png` | 404 on bad session id |
| 15 | `15-spec-diff-only-v1.png` | SpecDiff with only v1 archived |
| 16 | `16-spec-review-history-populated.png` | History sidebar after 2 saves |
| 17 | `17-spec-diff-v1-to-v2.png` | Real v1→v2 diff |
| 18 | `18-spec-approved-readonly.png` | Approved spec, no actions |

## Bugs catalog (numbered for codex)

### A. Visual/CSS bugs (rounds 1–2)

**B1. Text concatenation due to missing CSS gap.**
Every status icon, severity tag, and KPI label is rendered as
adjacent `StaticText` nodes with no flex-gap or margin. Examples:
- `passedA small markdown note-taking webapp...`
- `3/3 featuresquality: 0 criticalwall 5:13cost $1.84`
- `✓Email + password loginUsers sign in with email...`
- `compiledone29s` × 7 stages
- `△Send a direct messageproxy_onlymulti-actorUser opens...`
- `Roomspassing1 feature` × 3 groups
- `polishDM notifications could include sender avatar.`
- `criticalStripe checkout fails — env var STRIPE_SECRET_KEY...`

Fix: each row container needs `display: flex; gap: 8px;
align-items: baseline;`. Don't bake spaces into label strings.

**B2. Status pills are not pills.** `partial`, `blocked`, `passed`,
etc. render as plain inline text in body color. No green/amber/red
pill chips. Wireframe specifies colored backgrounds.

**B3. Severity badges (`critical` / `polish`) have no color.**
Findings render as `bullet · severity-text body-text`. All severities
look identical.

### B. Structural / wireframe gaps

**B4. Landing page = `<ul>` of session-IDs.** Wireframe Screen 2
specifies status-pill cards with intent, count rollups, wall+cost,
group summary, action buttons. Current is < 5% of intent.

**B5. RunDrawer is a single-column dump, not a drawer.** No
two-column sidebar+drawer, no header bar, no action button row.

**B6. Stage timeline is a numbered `<ol>`** instead of horizontal
stepper.

**B7. Group rows lack wall/cost/`[diff]`/`[logs]` actions.**

**B8. Feature drilldown lacks breadcrumb + per-Feature actions**
(`[Open evidence dir]`, `[Re-audit just this Feature]`, `[Logs]`).

### C. Spec review surface

**B9. Raw HTML comments leak into the markdown body.**
`<!-- group: auth -->` and `<!-- feature: login | evidence: ... -->`
render as visible text. Should be parsed out (or wireframe 4b form
view should toggle).

**B10. Header date label runs together** —
`draftupdated 2026-05-05T17:19:27Z`. ISO unfriendly; should be
"Updated 4m ago" with raw ISO as `title` attribute.

**B11. Edit/Approve buttons at bottom-left.** Wireframe Screen 4
puts them top-right.

**B12. No structured "Form view" toggle** alongside markdown view.

### D. AddFeatureModal

**B13. Field labels clipped/cropped above inputs** (visible
desktop + worse on mobile).

**B14. Footer buttons mis-aligned** — `Cancel` left, `Add to spec`
far right via `space-between`. Should be a right-aligned action
group.

### E. SpecDiff

**B15. No version selectors visible in empty state.**

### F. Mobile / responsive

**B16. RunDrawer at 375px has same text-concat root cause.**
**B17. AddFeatureModal field labels especially clip on mobile.**
**B18. Action buttons flush-to-edge (no padding) on mobile.**

### G. Error / empty states

**B19. Bad session id error is brutal** —
`Failed to load run: HTTP 404 Not Found` + a useless `Retry`
button. Should be friendly "Run not found" + `Back to runs` link.

**B20. No console logging anywhere.** Production debugging will
suffer; navigation, fetch errors, and state transitions are all
silent.

### H. Accessibility

**B21. KPI line is split into 14 `StaticText` nodes** with no
parent semantic role. Screen readers read each fragment
individually:
"1 / 3 features quality: 1 critical wall 7 colon 01 cost dollar 2 dot 91"

**B22. Status text not announced as state.** No `role=status` or
`aria-live`, so polling updates won't announce.

**B23. Disclosure caret has no `aria-label`** beyond the visible
text.

**B24. AddFeatureModal "Ungrouped" option** may not match Spec
dataclass semantics (`group_id` likely required). Verify before
shipping.

### I. Interaction-flow bugs (round 3, NEW)

**B25. `fill()` programmatic edit doesn't dirty the form.** Save
stays disabled until at least one real keystroke. Likely React's
onChange listener; controlled-input pattern has a known interaction
with imperative DOM value mutation. Wraps Playwright/Cypress test
authoring badly. Workaround for tests: use `type` not `fill`. Code
fix: dispatch a synthetic `input` event after `fill`.

**B26. First save doesn't refresh the spec-history sidebar.**
Even though the backend correctly archives `spec-v1.json` and the
API returns `{"versions":[1]}`, the UI keeps showing
"No prior versions yet" until you reload OR until B27's threshold
trips on a second save.

**B27. Empty-state condition `versions.length < 2` at
SpecReviewPage.tsx:286 hides v1 always.** Backend archives v1 on
first save → `versions.length === 1` → UI shows "No prior versions
yet" (lying to the user). The "you need ≥2 to diff" mental model
broke the "you need ≥1 to see history" intent.

Fix: split history-list rendering from compare-controls. Show v1
as soon as it exists; only the diff dropdown needs ≥2.

**B28. SpecDiff doesn't expose the current working spec as a
comparison target.** With only v1 archived, you can't compare v1
→ current. Even after v2 archived, v3-current isn't selectable.
For a "what's about to change" preview, this is a blocker.

**B29. `v1 → v1` default with no no-op indicator.** When SpecDiff
loads with only one archived version, both From/To dropdowns
default to v1; the diff renders empty (no `+`/`-` lines, just
context) but no message says "compare two different versions to
see a diff." Users will think the feature is broken.

**B30. Approve has no confirmation + no success toast.** Page
silently flips lifecycle pill from `draft` → `approved` and
removes the action buttons. Destructive (locks edits) with no
friction or feedback.

**B31. Cancel silently discards unsaved edits.** No "Discard
unsaved changes?" dialog. A user mid-edit clicking Cancel by
accident loses everything.

**B32. Approved-state UI has no read-only indicator.** Edit and
Approve buttons just disappear. No banner saying "Spec approved.
This view is read-only." No affordance to unlock (frontend doesn't
know about the CLI `--force` resume path, but should at least say
"Re-run with `otto build --resume --force` to edit again.").

## Aesthetics scorecard

| Dimension | Score | Notes |
|---|---|---|
| Information density | 2/5 | Concatenated text, no KPI separators |
| Layout architecture | 1/5 | No two-column drawer; landing is `<ul>`; spec actions bottom not top |
| Typography hierarchy | 2/5 | Headings exist, no scale rhythm |
| Color / tone | 3/5 | Guardrail green/red works; status pills/severity badges absent |
| Whitespace + spacing | 1/5 | Text concatenation dominates |
| Wireframe fidelity | 2/5 | ~30% of wireframe elements present |
| Empty / error states | 2/5 | 404 is HTTP-raw |
| Mobile responsive | 3/5 | Stacking works; modal labels clip |
| Accessibility | 1/5 | StaticText fragments; no roles; no aria-live |
| Interaction flow | 2/5 | History+save broken (B26/B27); diff broken (B28/B29) |
| State-change feedback | 1/5 | Approve/Cancel silent; B25 hidden disabled state |

## Honest call

The data plumbing shipped (RunView serializer, useRunView polling,
FeatureDrilldown, SpecDiffPage, AddFeatureModal, edit/save/approve
endpoints all wire correctly). What didn't ship was:

- A designer-led visual pass (~70% of round 1–2 bugs).
- Interaction-flow polish (round 3 bugs B25–B32).
- Accessibility semantics (B21–B24).

Both are tractable, mostly mechanical, and very high-leverage for a
human-facing tool.

## Recommendation for codex (4 sub-tracks)

**P1 — single CSS pass (~½ day, fixes ~70%):**
Add `display: flex; gap: 8px; align-items: baseline;` to row
containers in `RunDrawer.tsx`, `FeatureList.tsx`, `Guardrails.tsx`,
`GroupList.tsx`, `StageTimeline.tsx`, `FeatureDrilldown.tsx`,
`SpecReviewPage.tsx` header. Rip leading/trailing spaces from
label strings. Add `<Pill>` and `<Badge>` components with
status/severity color tokens.

**P2 — layout architecture (~1.5 days):**
- Landing cards per wireframe Screen 2.
- RunDrawer header bar + action button row.
- Horizontal Stage timeline.
- Move SpecReviewPage Edit/Approve to top-right.
- AddFeatureModal field-label CSS fix + footer alignment.
- Friendly 404 page with `[Back to runs]`.
- Format ISO timestamps relative.
- Strip `<!-- comment -->` from markdown view.

**P3 — interaction flow (~½ day, addresses B25–B32):**
- B26/B27: split history-list (show ≥1) from diff-controls (≥2).
  Refetch versions after save.
- B28: include current working spec.json as a comparison target
  in SpecDiff.
- B29: when From === To, replace diff body with "Pick two
  different versions to compare."
- B30: confirm dialog + success toast on Approve.
- B31: confirm dialog when Cancel hits unsaved edits.
- B32: approved-state banner with re-edit affordance text.

**P4 — accessibility + telemetry (~½ day):**
- Wrap KPI rows in `<dl>` with `<dt>`/`<dd>`.
- Add `role=status aria-live="polite"` to live-polling regions.
- Add `aria-label` to disclosure caret.
- Add console logging for fetch failures + state transitions.

## Side bug found before audit could even run

`scripts/rua/seed_fixture_sessions.py` used pre-A2 field names
(`Group(id=..., title=...)`, `g.title`) and crashed before producing
fixtures. Fixed inline. Codex should re-grep `scripts/` for more
pre-A2 leftovers.
