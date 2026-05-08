## Your task

Inspect the integrated worktree (you may read files), review the evidence, and
output:

  1. A short narrative of what works and what doesn't.
  2. A per-Group verdict: for each group_id, pass or fail with reason.
  3. **Per-Feature audits (REQUIRED)**: one verdict per Feature listed in the
     spec above. Use the exact `feature_id`; `name` is display text only. Each
     entry MUST cite specific evidence — what file/page/log you inspected to
     reach the verdict. Format:
       {feature_id: "<exact Feature.id>", name: "<Feature.name>", status: "passed"|"partial"|"blocked", detail: "1-2 sentence rationale", evidence_refs: ["path/to/file:line" or URL or screenshot path], surface: "DOM|HTTP|CLI|source-level|screenshot|video|...", methodology: "live-ui-events|http-request|cli-execution|source-review|visual-only|...", evidence_completeness: "full|proxy_only|partial", coverage_confidence: "high|medium|low"}

     Emit one entry per Feature. If a Feature is implemented but with caveats,
     mark partial and explain. Empty list is NOT acceptable when done_means has
     items.

     Evidence-gating rule: screenshots/video are proof artifacts, not product
     repair triggers by themselves. For browser UI features, `passed` and
     repairable `partial`/`blocked` verdicts need live UI events, deterministic
     DOM/browser assertions, contract tests, or a concrete measurable failure
     such as hidden/clipped/unreachable controls. `visual-only` or
     `http-request` evidence for a UI feature should use
     `evidence_completeness: "partial"` or `coverage_confidence: "low"` unless
     it is paired with a reproducible user-action failure.

     Source-level architecture findings are not user-facing failures by
     themselves. Use source review to explain a verdict only when it maps to a
     measurable product failure, failing deterministic contract, broken public
     API, data-loss/persistence risk, security risk, or explicit user-requested
     architecture requirement. If live user behavior, deterministic browser
     checks, and contract tests pass but the implementation uses a different
     internal extension pattern than the spec suggested, record that as a
     quality/advisory finding instead of marking a user-facing Feature partial.

     Non-functional duplicate controls are a real user-facing UX failure. If a
     placeholder/disabled/search/import/export/control copy remains visible
     alongside the real control with the same or confusing accessible label,
     mark the affected Feature partial and route it to repair.

     For repaired or newly implemented behavior, a `passed` Feature needs
     direct executable evidence for the exact acceptance examples and
     edge/error cases in the intent or audit detail. Do NOT infer that an
     error-preservation requirement works from a different invalid value; test
     an invalid input that exercises the same changed parser/normalizer/validation path.
     If the repo has a test suite and no focused regression test was added for
     the new behavior, mark the Feature `partial` unless
     your walkthrough directly executes every named success and failure case.

     The user's intent and acceptance text are the product contract.
     Tests, docstrings, or comments added by the repair agent are evidence only; they
     are NOT allowed to redefine that contract. If a newly added test expects
     behavior that contradicts the user's intent, mark the Feature `partial` or
     `blocked` and call out the bad test. In particular, if the contract says
     an invalid string is `unchanged`, verify exact string equality with the original input,
     including punctuation/separators.

     A docstring example counts as regression coverage only when the repo's
     native test/lint command actually runs doctests. If a normal editable test
     file exists and no repo-native focused test was added for the changed
     behavior, do not treat the Feature as fully tested merely because a
     docstring example or manual command passed.

  4. A final verdict: `passed`, `partial`, or `blocked`.
  5. A quality assessment of the user-facing experience (REQUIRED, independent
     of the functional verdict):

     **Calibration — what each score MEANS** (be honest, don't grade-inflate):

     - **1/5 = unusable**: errors visible, broken layout, can't complete
       primary action.
     - **2/5 = broken UX**: things work but UX is wrong — missing labels, no
       error states, controls hidden where users won't find them, horizontal
       page overflow on normal mobile viewports, or clipped primary controls.
     - **3/5 = MVP**: this is the DEFAULT for a project that passed acceptance
       tests with no extra design effort. Browser-default form styling,
       vertical-stacked sections, minimal CSS, plain typography, basic nav.
       Functional, but looks like a code sample, not a product. **Most projects
       that shipped for the first time will land here.**
     - **4/5 = thoughtful**: clear design language — consistent spacing,
       typography, color, hover/focus states, responsive at narrow widths,
       error states styled, visual hierarchy beyond <h1>/<h2>. Goes beyond MVP.
     - **5/5 = polished**: production-ready feel — accessibility (aria-labels,
       keyboard nav), loading states, animations, branded look-and-feel,
       consistent design system across all surfaces.

     **Anti-grade-inflation rule**: if you find yourself wanting to give 4/5 to
     a product whose home page is just stacked forms with browser-default
     styling, that's a 3/5. Reserve 4 for products that show evidence of design
     thinking, not just label/nav presence.

     - quality_findings: list of CONCRETE observations about the user-facing
       experience. **Required: list at least 2 specific findings, even if the
       product is good** — name the WEAKEST thing you see and the
       next-most-actionable improvement. Findings can be issues ("home page has
       no responsive styling — overflows on mobile") OR opportunities ("could
       group account-related forms into a single section instead of three
       sections at top of home"). Empty list is NOT acceptable for a real
       product — if you can't find ANY improvement, you're not looking hard
       enough.

     **Severity consistency rule**: if any standard desktop or mobile viewport
     has horizontal overflow, clipped primary controls, overlapping text, or a
     hidden/unreachable primary action, the quality_score MUST be 2 or lower
     and the affected Feature MUST be marked partial or blocked. Do not bury a
     severe product-quality failure only in `quality_findings` or an untagged
     exploration row.

Quality criteria by project_kind (use as a checklist):

  - **webapp**: nav present and consistent; primary actions discoverable from
    /; forms labelled; error states visible; responsive at narrow widths;
    visual hierarchy (not raw browser default styling); each page has the same
    design language.
  - **static-site / blog**: navigation between pages works; post list ordered
    properly; dates formatted; tag links clickable; **RSS feed has both a
    discovery <link> in head AND a visible footer/header link** (artifact
    existing isn't enough); readable typography (not raw browser default).
  - **cli / library**: --help text complete; error messages actionable; exit
    codes meaningful; usage-friendly defaults.

**Be specific in findings.** "Could be better" is not useful. "Home page has 6
forms stacked vertically with no styling, no labels, no nav bar — feels like
1998" is useful.

Output as a single fenced JSON block with keys:
{
  verdict: passed|partial|blocked,
  narrative: str,
  group_verdicts: [{group_id, passed: bool, detail: str}, ...],
  feature_audits: [{feature_id: str, name: str,
                    status: passed|partial|blocked,
                    detail: str, evidence_refs: [str, ...],
                    surface: str, methodology: str,
                    evidence_completeness: full|proxy_only|partial,
                    coverage_confidence: high|medium|low}, ...],
  quality_score: int (1-5),
  quality_findings: [str, ...]
}
