---
name: otto-frontend-rua
description: Visual + interaction RUA (Real-User-Audit) of Otto Mission Control. Run multi-round, multi-pass, multi-step audits using chrome-devtools MCP — find bugs no static screenshot or unit test can. Each round catches what the previous round physically couldn't. Output a numbered bug catalog with file:line citations + screenshots.
---

# Otto Frontend RUA — visual + interaction audit

## Overview

Otto's frontend ships data plumbing correctly (RunView serializer, polling
hooks, route components) but visual polish + interaction-flow bugs hide
in places unit tests can't see. This skill drives a **multi-round,
multi-pass, multi-step** audit that progressively surfaces bugs that
each prior pass physically could not catch.

**Why this works** — empirical evidence from the 2026-05-05 audit:

| Round | Method | Bugs found |
|-------|--------|------------|
| 1 | 5 static screenshots, single pass each | 7 macro layout problems |
| 2 | 14 screenshots × 4–5 vision passes + a11y + mobile + modal + drilldown | 24 bugs (root cause identified) |
| 3 | Multi-step interaction simulation: edit → save → save → diff → approve → cancel | **8 brand-new bugs** invisible to any screenshot |

Round 3 alone caught: silent destructive actions (Cancel discards
unsaved edits), a state bug where `versions.length < 2` hides v1
forever, and a "fill doesn't dirty form" controlled-input pitfall.
None of these would surface from screenshots OR from unit tests.

When in doubt: do another round. They keep paying off.

## When to use

- After any frontend change that's been claimed "done" but only verified
  by `npm run web:typecheck && web:build`.
- After a wireframe-driven redesign — to honestly score visual fidelity.
- Before a release / merge to main.
- When a user says "is the UI good?" — never answer without running this.

If unit tests pass but a screenshot has never been compared to the
wireframe, **the UI is not verified**.

## Tools required

- `chrome-devtools` MCP (navigate_page, take_screenshot, take_snapshot,
  click, fill, type_text, list_console_messages, list_pages,
  resize_page).
- `scripts/rua/seed_fixture_sessions.py` — seeds 3 fixture sessions
  (passed / partial / blocked).
- `scripts/rua/serve_fixture.py` — starts the MC web server pointed
  at a fixture project.

## Prerequisites — fixture seeding

The seed script periodically rots when otto data-class field names
change (`title=`, `tasks=`, `deps=` were renamed in A2). **Always run
the seed first and fix any TypeError before screenshotting:**

```bash
PROJ=/tmp/rua-$(date +%H%M%S)
uv run python scripts/rua/seed_fixture_sessions.py "$PROJ"
# If it crashes with `TypeError: Group.__init__() got an unexpected keyword argument 'title'`,
# patch scripts/rua/seed_fixture_sessions.py — sed 's/title=/name=/g' and 's/g\.title/g.name/g'
# Then re-run. Codex should re-grep scripts/ for similar leftovers when promoting a rename.
```

Then start the server on a free port:

```bash
uv run python scripts/rua/serve_fixture.py "$PROJ" 8881 > /tmp/rua-server.log 2>&1 &
```

Kill it after the audit (`kill $!`).

## Audit protocol — 3 rounds, each a real shift in lens

### Round 1 — static screenshots (~10 min)

Goal: macro layout vs wireframe.

1. Resize page to 1440×900 desktop.
2. For each of the 8 wireframe screens (per `docs/otto-wireframes.md`),
   navigate via the canonical URL and screenshot full-page:
   - `/` — landing (Screen 2)
   - `?view=run-view&session=<passed>` — drawer landed (Screen 3)
   - `?view=run-view&session=<blocked>` — drawer blocked
   - `?view=run-view&session=<partial>` — drawer partial
   - `?view=spec-review&spec=<passed>` — read mode (Screen 4)
   - `?view=spec-diff&session=<passed>` — diff (Screen 4d)
3. Read each screenshot once, compare to wireframe, list visible
   problems.

### Round 2 — multi-pass per screenshot (~30 min)

Goal: exhaustive bug-mining per surface.

1. Re-take the same screenshots **plus**:
   - Mobile viewport (375×812) for at least 2 screens.
   - Modal overlays (click `Edit` then `Add Feature` to surface
     AddFeatureModal).
   - Disclosure expanded states (click `▶ N groups`).
   - Drilldown navigation (click into a Feature).
   - Bad inputs (navigate to `?view=run-view&session=DOES-NOT-EXIST`).
2. **For each screenshot, do 4–5 vision passes** with different focus
   questions:
   - **Pass 1 — layout/spacing**: are elements where the wireframe
     puts them? Any overlap, clipping, flush-to-edge? (Fixes ~30%
     of CSS bugs.)
   - **Pass 2 — typography**: heading hierarchy? font-weight rhythm?
     monospace where appropriate?
   - **Pass 3 — color/tone**: status pills colored? severity badges
     colored? guardrail tones (✓/✗/⊘) consistent?
   - **Pass 4 — information completeness**: what's the wireframe
     promising that's missing? (Action buttons, breadcrumbs, KPI
     rollups, evidence-drilldown affordances.)
   - **Pass 5 — edge cases / invisible bugs**: text concatenation
     ("passedA small markdown..." = adjacent StaticText with no
     CSS gap), default browser `<ul>` bullets, raw ISO timestamps.
3. **Cross-check with `take_snapshot`** (a11y tree). The accessibility
   semantics often reveal bugs invisible in pixels — e.g. a KPI line
   built from 14 separate `StaticText` nodes is a red flag for
   `<dl>`/`<dt>`/`<dd>` semantic missing.

### Round 3 — multi-step interaction simulation (~30 min)

Goal: catch state-machine bugs that screenshots can't see.

For each interactive surface, simulate a real user journey end-to-end
with a11y-snapshot inspection between every step:

1. **Spec review edit cycle**:
   - Click `Edit` → assert textarea appears.
   - `fill()` — note: programmatic fill DOESN'T trigger React's
     onChange. Always follow with one `type_text(" ")` to dirty
     the form. (B25)
   - Click `Save` → snapshot afterwards. **Verify the
     SPEC HISTORY sidebar shows the new archived version.** Common
     bug: backend archives but frontend hook doesn't refetch (B26).
   - Open browser console, then ask: did any state transition produce
     console output? Common bug: zero diagnostic logging anywhere
     (B20).

2. **History threshold probe**:
   - Save once → if sidebar still says "No prior versions yet",
     check the API: `curl /api/specs/<id>/versions`. If API returns
     `{"versions":[1]}` but UI lies, the empty-state condition is
     wrong (B27 — `versions.length < 2`).

3. **SpecDiff completeness probe**:
   - Navigate to spec-diff. Are From/To dropdowns visible even with
     0 archived versions? (B15).
   - With 1 version archived, does the dropdown include
     "current working spec" as a comparison target? (B28).
   - With From === To, is the body just blank or does it explain
     "pick different versions"? (B29).

4. **Approve / Cancel flow**:
   - Click `Approve` → does it confirm? toast? (B30).
   - Click `Edit` → fill nonsense → click `Cancel`. Does it confirm
     "Discard unsaved changes?" or silently destroy your work? (B31).
   - Once approved: are Edit/Approve hidden, AND is there a banner
     explaining why and how to unlock? (B32).

5. **Polling probe** (when a running fixture exists):
   - Watch `useRunView` poll cadence (3s default). Are state
     transitions announced via `aria-live`? (B22). Do they trigger
     console logs? (B20).

6. **Bad-input probes**:
   - `?session=DOES-NOT-EXIST` → does the error UI say
     "Run not found" with `[Back to runs]` link, or just throw
     `Failed to load run: HTTP 404 Not Found` + a useless `Retry`
     button? (B19).

## Output: numbered bug catalog with file:line + screenshots

Every audit produces `docs/rua/<DATE>-<topic>/REPORT.md` with:

- Numbered bug list (B1, B2, ...) — each bug names the file or visible
  string, the wireframe expectation, and a fix hint.
- Screenshot for each bug (full-page PNG saved alongside the report).
- Aesthetics scorecard table across ~10 dimensions (1–5 each).
- Action plan grouped into 4 sub-tracks:
  - **P1 — single CSS pass** (`display: flex; gap: 8px;` — fixes
    ~70% of visual bugs).
  - **P2 — layout architecture** (cards, header bars, action rows).
  - **P3 — interaction flow** (confirmation dialogs, refetch on save,
    feedback toasts).
  - **P4 — accessibility + telemetry** (`<dl>`/`<dt>`/`<dd>`,
    `aria-live`, console logging).

## Anti-patterns

- **One round and done**. You will miss B25–B32-class bugs every
  time. Always do at least Round 2 and ideally Round 3.
- **Skipping a11y snapshots**. Pixels lie about semantics. The "KPI
  is 14 separate StaticText nodes" finding came from the a11y tree,
  not the screenshot.
- **Single viewport**. Mobile (375px) reveals modal field clipping
  + flush-to-edge button bugs that desktop never shows.
- **Screenshots without interactions**. B30/B31/B32 (silent
  destructive flows) are invisible without clicking. B26/B27 (state
  staleness) are invisible without saving.
- **`fill()` without follow-up keystroke**. React controlled inputs
  ignore programmatic value mutation. Always: `fill(...)` then
  `type_text(" ")` to dirty the form.
- **Trusting "typecheck + build green" as UI verification**. They
  only verify TypeScript shape, not what the user sees.

## Reference: prior audits

- `docs/rua/2026-05-04-172101/` — initial RUA pass, pre-round-2/3
  features. 16 screenshots covering RunDrawer, FeatureDrilldown,
  SpecReviewPage edit/cancel/save/approve.
- `docs/rua/2026-05-05-post-merge/` — full 3-round audit
  surfacing 32 numbered bugs. Use this as the template for future
  audits.

## Cost

Zero LLM cost. The audit drives the local web server only. The only
cost is human time — ~70 minutes for a thorough 3-round pass.
