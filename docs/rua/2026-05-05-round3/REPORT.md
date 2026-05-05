# RUA Round 3 — post-round-2-extended fix wave (multi-pass mandatory)

Date: 2026-05-05
Worktree HEAD: `9d78ace0c` (round-2-extended fix wave) + `75429a97a`
(skill update making multi-pass mandatory)
Audit type: Pre-release tier with strict 6-pass inner loop per
screenshot.

## TL;DR

Multi-pass surfaced **47 findings across 6 screenshots** that
single-pass would have missed. Most are minor polish; several real
bugs (Stage stepper truncation, duplicate "Back to runs" buttons,
underscore leakage in stage name, raw page-name duplication).

The earlier round-2 fixes all hold up — 23+ confirmed-good. Round
3's findings cluster into 4 themes:

1. **Page chrome duplication** — branding shows in both AppShell
   and page h1; "Back to runs" appears in both topbar and 404 card.
2. **Token leakage** — `Spec_review` (underscore), `proxy_only`,
   `multi-actor` displayed raw without humanization.
3. **Real overflow bugs** — Stage stepper cuts "Render" mid-word
   ("Render don[e]") on standard 1440 viewport.
4. **Wireframe gaps** — no "+ New run" button on landing, no
   "View full intent ▸" affordance, no version count visible on
   spec-review.

## Findings by surface

### Landing (`01-landing.png`) — 13 findings

| ID | Pass | Finding |
|----|------|---------|
| R3-B1 | 1 layout | "Otto Mission Control" duplicated — AppShell branding + page h1 both. Topbar should be product-name "Otto"; page h1 should be page-label "Runs". |
| R3-B2 | 1 layout | "3 sessions" badge tight against heading; needs more left margin. |
| R3-B3 | 1 layout | ~50% viewport empty below cards even with min-height. |
| R3-B4 | 2 type | Card intent + metric row use same color/size — hierarchy too flat. |
| R3-B5 | 2 type | "1/2 critical finding(s)" inline-grey — could use weight. |
| R3-B7 | 3 color | "Review spec" buttons still teal/outlined — should be neutral-blue per R2-B23 unification, OR rendered as secondary if "+ New run" is primary. |
| R3-B8 | 4 info | No `[+ New run]` action — wireframe Screen 2 has it. |
| R3-B9 | 4 info | No filter / refresh controls per wireframe. |
| R3-B10 | 4 info | No "Built in N groups" subline per card. |
| R3-B13 | 5 edge | No visual distinction for running vs terminal sessions. |
| R3-B14 | 6 design | No subtle separator between AppShell topbar and content. |
| R3-B15 | 6 design | Card internal sections have no row gap. |
| R3-B16 | 6 design | Cards-as-link affordance unclear. |

### Run drawer passed (`02-rundrawer-passed.png`) — 11 findings

| ID | Pass | Finding |
|----|------|---------|
| R3-B17 | 1 layout | Stage stepper truncated "Render don" — overflow bug. |
| R3-B18 | 1 layout | KPI grid inline with intent — wireframe shows separate block. |
| R3-B19 | 2 type | "Spec_review" stage label leaks underscore — should humanize to "Spec review". |
| R3-B20 | 2 type | Tabular-nums applied (R2-B13 verified). |
| R3-B22 | 4 info | No "View full intent ▸" — long intents overflow. |
| R3-B23 | 4 info | Feature rows lack visible "▸ evidence (N items)" drilldown affordance. |
| R3-B24 | 4 info | No "passing/blocking" verdict-grouping subhead. |
| R3-B25 | 5 edge | Topbar "...abc123" no hover-tooltip for full ID. |
| R3-B26 | 5 edge | No timestamp anywhere — when did run finish? |
| R3-B28 | 6 design | No section dividers between Features/Guardrails/Stages. |
| R3-B29 | 6 design | "▶ 2 groups" disclosure naked between Guardrails and Stages. |
| R3-B30 | 6 design | No separator between action button row and Features. |

### Run drawer blocked (`03-rundrawer-blocked.png`) — 4 new findings

| ID | Pass | Finding |
|----|------|---------|
| R3-B31 | 3 color | Verdict pill red + guardrail-fail card red unified — confirms R2-B17 fix. |
| R3-B32 | 4 info | Critical finding text not visible at row level — must drill in. |
| R3-B33 | 5 edge | 2-critical KPI count not reflected as per-feature red badge. |
| R3-B34 | 6 design | Stripe row has 3 semantic items (icon + verdict + audit pill) — visual noise; could fold icon into pill. |

### Spec review (`04-spec-review.png`) — 9 findings

| ID | Pass | Finding |
|----|------|---------|
| R3-B35 | 1 layout | Body card extends with empty space below "Guardrails" — auto-fit issue. |
| R3-B36 | 1 layout | Markdown body header levels visually flat after demotion. |
| R3-B37 | 2 type | h1/h2/h3/h4 in body have insufficient size+weight contrast. |
| R3-B38 | 3 color | `[draft]` pill same blue as Approve button — pill (state) and button (action) shouldn't share tone. |
| R3-B39 | 4 info | No version count visible — no "Spec v1" / "Spec at v3" anywhere. |
| R3-B40 | 5 edge | Body card extends below content (R3-B35 root). |
| R3-B41 | 5 edge | History sidebar empty state "No prior versions yet" still small grey muted. |
| R3-B42 | 6 design | Markdown/Form tabs use different style than section nav in run-drawer. |
| R3-B43 | 6 design | Body card and header have no visual separator/elevation. |

### Spec diff empty (`05-spec-diff.png`) — 5 findings

| ID | Pass | Finding |
|----|------|---------|
| R3-B44 | 4 info | Two empty-state messages stacked — redundant. |
| R3-B45 | 3 color | "Show only changes" button styling unclear (toggle vs primary). |
| R3-B46 | 4 info | Empty state has no "Edit spec to create a version" CTA. |
| R3-B47 | 5 edge | Disabled toggle could have tooltip "No diff to filter". |
| R3-B48 | 6 design | Header bar has lots of whitespace below before main content. |

### 404 page (`06-404.png`) — 5 findings

| ID | Pass | Finding |
|----|------|---------|
| R3-B49 | 1 layout | Topbar shows "DOES-NOT-EXIST" full; real sessions truncate to "...abc123" — inconsistent. |
| R3-B50 | 1 layout | TWO "Back to runs" buttons — topbar text-link + card filled button. Duplicate action. |
| R3-B51 | 3 color | Two different visual treatments for the same action. |
| R3-B52 | 4 info | No suggestion to check URL or list-of-known-sessions. |
| R3-B54 | 6 design | Card vertically near top — could center in available space. |

## Confirmed-good (R2 fixes verified visually)

- ✅ B1 text concat — no concatenation anywhere on round-3 screens.
- ✅ B2 status pills colored (passed=green, partial=amber, blocked=red).
- ✅ B6 horizontal stage stepper (modulo R3-B17 overflow bug).
- ✅ B9 HTML comments stripped from spec markdown.
- ✅ B10 "Updated 3 minutes ago" relative time.
- ✅ B11 Edit/Approve top-right.
- ✅ B12 Markdown/Form view toggle.
- ✅ B19 friendly 404 with [Back to runs].
- ✅ R2-B2 evidence-pill no longer dups with verdict.
- ✅ R2-B3 landing pills derive from verdict (not lifecycle).
- ✅ R2-B17 unified red across components.
- ✅ R2-B21 lifecycle pill + "Updated" bullet separator.
- ✅ R2-B22 markdown h1 demoted to h2.
- ✅ R2-B23 Approve / primary action neutral blue.
- ✅ R2-B24 "Created N ago · by compile-agent" subline.
- ✅ R2-B26/B27/B28/B29 spec-diff: light theme, swap button, "Show only changes" toggle, symmetric labels.
- ✅ R2-B30/B31 404 wrapped by AppShell.
- ✅ R2-B34 audit-context pills outlined-only.

## Recommendation

47 findings — but ~30 are minor polish (R3-B14/B15/B30/B43 etc.).
Real-bug priorities:
1. **R3-B17** — Stage stepper truncation (visible bug at default 1440 viewport).
2. **R3-B19** — Underscore leakage in "Spec_review" label.
3. **R3-B50** — Duplicate "Back to runs" buttons on 404 page.
4. **R3-B1** — Page heading duplicates AppShell branding.
5. **R3-B7** — "Review spec" not yet using unified primary color.
6. **R3-B22** — No "View full intent ▸" for long intents.

Cluster the 47 fixes into 3-4 disjoint sub-agents and run a fix
wave + round-4 verification.
