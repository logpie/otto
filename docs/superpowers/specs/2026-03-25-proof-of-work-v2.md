# Proof-of-Work v2: Per-Item Proof Descriptions

## Problem

Today proof data is scraped from raw QA tool traffic. This means:
- Exploration noise leaks in (React fiber walking, document.title checks)
- Evidence isn't linked to specific must items
- Regression script runs generic `npx jest`, not specific tests per criterion
- A reader can't tell which proof supports which claim

## Design

The proof report is an **audit trail**, not a trustless verification. A human reads it.
The regression script is the only trustless part (independently runnable ground truth).

QA writes proof descriptions per must item. Otto renders them in the report.
Regression script stays ground-truth from captured qa_actions — flat, no grouping.

### Verdict schema change

```json
{
  "must_items": [
    {
      "spec_id": 1,
      "criterion": "Banner appears when wind > 60 km/h",
      "status": "pass",
      "evidence": "Human-readable summary",
      "proof": [
        "jest weatherAlerts: 'shows banner for high wind' passes",
        "browser: banner visible after injecting extreme data",
        "screenshot: qa-proofs/screenshot-banner.png"
      ]
    }
  ]
}
```

`proof` is a list of strings. QA describes what it did. No commands to match, no validation overhead.

### QA prompt addition

```
For each [must] item, include a "proof" array — a list of strings describing
what you did to verify this specific criterion. Examples:
  "ran jest weatherAlerts: 'shows banner for high wind' test passes"
  "browser: confirmed banner visible with extreme wind data injected"
  "screenshot: qa-proofs/screenshot-banner.png shows red banner at top"
Only include proof that directly verifies THIS criterion.
```

### spec_id

Assigned from original spec order (before QA sort). QA prompt shows:
`1. [must] criterion (spec_id=3)`. Report uses original spec_id for stable cross-run comparison.

### Report format

```markdown
# Proof Report
**Task:** Add weather severity banner
**Result:** abc123 | ✓ passed | 6/6 must | 4/6 proved | QA $0.69

## ✓ [1] Banner appears when wind > 60 km/h
Evidence: Summary from QA
Proof:
- jest weatherAlerts: 'shows banner for high wind' passes
- browser: banner visible after injecting extreme data
- [screenshot-banner.png](screenshot-banner.png) — Red banner at top

## ✓ [2] Banner not shown when no conditions met
Evidence: Summary from QA
Proof: (none recorded)

---
Proof descriptions are QA's account. Regression script is independently runnable.
```

### Proof coverage

- QA summary event includes `proof_coverage: "4/6"`
- Display: `✓ qa  90s  $0.67  6 specs passed  4/6 proved`
- Items with empty proof show "none recorded" — visible gap

### Screenshot links

Only render as `[file](file)` if file exists in current qa-proofs/. Otherwise: `file (missing)`.

### Regression script

**Stays flat. No per-criterion grouping.** Ground truth from captured qa_actions.
Existing _is_verification_command() filtering unchanged.
Disclaimer in proof-report.md: "Regression script is independently runnable ground truth."

### Implementation

1. Add `spec_id` to spec items before QA sort, include in QA prompt numbering
2. Add proof recording instructions to QA system prompt (~5 lines)
3. Update verdict parsing to extract `proof` arrays (list[str])
4. Update `_write_proof_artifacts()` to render proof per item from verdict
5. Add proof_coverage to QA summary event + display
6. Screenshot link existence check
7. Keep all existing qa_actions / regression script logic unchanged

### Tests

1. Proof strings rendered per item in report
2. Empty proof shows "none recorded"
3. proof_coverage in summary event
4. Screenshot proof string → link if file exists, plain text if missing
5. spec_id stable across QA sort
6. Regression script unchanged (still flat, ground truth)

## Plan Review

### Round 1 — Codex
- [ISSUE] Freeform proof commands could be hallucinated — fixed: changed to descriptions, no command matching
- [ISSUE] Fail-fast conflicts with proof-per-item — fixed: added not_reached status
- [ISSUE] Regression script weakened to test-only — fixed: kept all replayable types
- [ISSUE] testNamePattern is Jest-specific — fixed: no test_names, just descriptions
- [ISSUE] Lose observed output — fixed: output stays in captured qa_actions
- [ISSUE] Fallback per-item underspecified — fixed: no fallback, just "unproven"
- [ISSUE] Browser proof needs page_url — deferred (complexity vs value)
- [ISSUE] Freeform criterion matching — fixed: added spec_id
- [ISSUE] Preserve artifact contract — fixed: same filenames

### Round 2 — Codex
- [ISSUE] proof_refs by index is fragile — fixed: changed to description strings
- [ISSUE] Per-item fallback not implementable — fixed: removed, just "unproven"
- [ISSUE] Shared proof duplicates regression commands — fixed: regression stays flat
- [ISSUE] Pass + 0 valid proof = fail-open — addressed: visible in report + coverage metric
- [ISSUE] page_url tracking incomplete — deferred

### Round 3 — Codex
- [ISSUE] Command matching ambiguous — fixed: no matching, just descriptions
- [ISSUE] Matched command doesn't prove success — fixed: no matching
- [ISSUE] browser_check ungrounded — accepted: audit trail, not trustless
- [ISSUE] Contradictory status — addressed: proof_coverage makes gaps visible

### Round 4 — Codex
- [ISSUE] spec_id from sort order unstable — fixed: assign from original order
- [ISSUE] Empty proof silently decays — fixed: proof_coverage in display
- [ISSUE] Screenshot links can be stale — fixed: existence check
- [ISSUE] Regression script grouping misleading — fixed: stays flat
- APPROVED
