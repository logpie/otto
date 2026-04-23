# Otto MVP: Intent to PR-Ready Product Slice

## Purpose

Otto's long-term vision is `intent -> product` with enough reliability to run
autonomously. The usable MVP should be narrower:

> Given a clear product intent in an existing or new repo, Otto produces a
> PR-ready vertical slice with tests, certification evidence, and an honest
> report of what is done and what remains.

This doc focuses on product-building quality from the user's perspective. It
intentionally defers heavier infrastructure work such as a 24x7 daemon, VM
isolation, cloud scheduling, and full deployment automation.

## Current State

Otto already has the core reliability loop:

- `otto build`: build, certify, fix, and commit.
- `otto build --spec`: generate and approve a short spec before building.
- `otto certify`: independently verify any project.
- `otto improve`: iterate on bugs, features, or measurable targets.
- Checkpoint/resume, run budgets, proof-of-work reports, and provider
  abstraction for Claude/Codex.

What works today is best described as:

> Bounded feature work and small greenfield apps can succeed, especially when
> the intent is clear and the repo is conventional.

The MVP target is stronger but still scoped:

> Bounded PR-ready vertical slices in small-to-medium existing repos, with
> explicit product brief, implementation plan, tests, certification, and
> requirement-to-evidence traceability.

## Non-Goals for This MVP

- Fully autonomous multi-day product ownership.
- Arbitrary complex enterprise migrations.
- Production deployment, rollback, or secret management.
- VM/container isolation as a hard requirement.
- A dashboard-first product surface.
- Guaranteeing success on large unfamiliar repos.
- Replacing human product judgment on ambiguous requirements.

## User Promise

The MVP user experience should feel like this:

```bash
otto build "add saved searches to this analytics dashboard" --spec
```

Otto should:

1. Inspect the repo and understand the product shape.
2. Turn the intent into a short product brief.
3. Produce an implementation plan for one shippable vertical slice.
4. Ask for approval when scope or architecture choices matter.
5. Implement the slice in the existing repo style.
6. Run existing tests and add focused tests.
7. Certify the user-facing behavior.
8. Fix certification failures.
9. Produce a final report that a reviewer can use to decide whether to merge.

The final report should answer:

- What changed?
- How do I try it?
- Which requirements were satisfied?
- Which tests and certifier stories passed?
- What evidence exists?
- What remains out of scope?
- What risks should a human reviewer inspect?

## Core Product Capabilities

### 1. Product Brief Intake

The current spec gate should become a lightweight product brief, not only a
generic spec. It should capture:

- Target user.
- Core workflow.
- User-visible acceptance criteria.
- Non-goals and deferred scope.
- Product/design expectations.
- Data model assumptions.
- Existing repo constraints.
- Open questions.

For simple tasks, Otto can auto-approve with `--yes`. For ambiguous tasks, it
should pause with precise questions or a reviewable brief.

New artifact:

```text
otto_logs/sessions/<id>/spec/product-brief.md
```

### 2. Repo Discovery

Before implementation in an existing repo, Otto should generate a concise repo
map. This is not a full architecture document. It is the minimum context needed
to avoid random local patches.

Repo map should include:

- App type and stack.
- Entry points.
- Main UI routes/screens.
- Data/API boundaries.
- Existing test commands.
- Existing style/design conventions.
- Likely files to edit.
- Risky areas.

New artifact:

```text
otto_logs/sessions/<id>/build/repo-map.md
```

### 3. Vertical Slice Plan

Complex intents should be broken into one shippable slice. Otto should avoid
big-bang builds unless the repo and scope are tiny.

The plan should include:

- Chosen slice.
- Requirements included in this slice.
- Requirements explicitly deferred.
- Files/modules likely touched.
- Tests to add or update.
- Certifier stories to expect.
- Risk notes.

New artifact:

```text
otto_logs/sessions/<id>/build/implementation-plan.md
```

Approval behavior:

- Default: pause for plan approval when `--spec` is used and repo is existing.
- `--yes`: auto-approve brief and plan.
- Future option: `--plan-only` to stop before implementation.

### 4. Requirement Traceability

Every product requirement should be traceable to implementation and evidence.
This is the product-quality backbone.

Example:

```json
{
  "requirement_id": "saved-search-create",
  "description": "User can save the current dashboard filters as a named search.",
  "status": "satisfied",
  "implementation": ["src/features/searches/SavedSearchButton.tsx"],
  "tests": ["src/features/searches/SavedSearchButton.test.tsx"],
  "certifier_stories": ["story-save-current-filters"],
  "evidence": ["certify/evidence/story-save-current-filters.png"]
}
```

New artifact:

```text
otto_logs/sessions/<id>/traceability.json
```

The final report should clearly flag:

- Requirements with no tests.
- Requirements with no certifier evidence.
- Deferred requirements.
- Requirements marked done by the builder but rejected by the certifier.

### 5. Product Quality Rubric

Certification should check more than "does the happy path work." The certifier
should score product quality for the slice:

- Completeness: required workflow works end to end.
- Integration fit: follows repo patterns and does not bypass existing systems.
- UX clarity: labels, empty states, loading/error states, responsive layout.
- Data correctness: persistence, validation, edge cases.
- Regression safety: existing behavior still works.
- Evidence integrity: screenshots and walkthroughs match real user actions.

The rubric should produce both a verdict and structured warnings:

```text
VERDICT: PASS
QUALITY_SCORE: 4/5
WARN: no empty state for saved searches list
WARN: mobile filter drawer not covered by certifier story
```

### 6. Reviewer-Ready Final Report

The current proof-of-work report is valuable, but the MVP needs a reviewer
summary for PR decisions.

New artifact:

```text
otto_logs/sessions/<id>/review-summary.md
```

It should include:

- Summary of product slice.
- Changed files grouped by purpose.
- How to run locally.
- Test commands and results.
- Certification result and evidence link.
- Requirement traceability summary.
- Deferred scope.
- Review risks.
- Suggested PR title/body.

## UX Shape

### Default Build

```bash
otto build "add saved searches to this analytics dashboard" --spec
```

Interactive checkpoints:

1. Product brief approval.
2. Implementation plan approval.
3. Final merge/readiness decision in the report.

### Fast Path

```bash
otto build "add saved searches to this analytics dashboard" --spec --yes
```

Auto-approves brief and plan. Still writes artifacts.

### Planning Only

```bash
otto build "add saved searches to this analytics dashboard" --spec --plan-only
```

Generates product brief, repo map, and implementation plan, then stops. This is
useful when the user wants product judgment without code changes yet.

### Certification Only

```bash
otto certify "saved searches can be created, reused, renamed, and deleted"
```

Should produce story evidence and product-quality warnings, even for projects
not built by Otto.

## Implementation Plan

### Phase 0: Baseline Fixtures

Build a small benchmark suite before changing behavior. Use fixtures that
represent real product-building quality, not only parser correctness.

Suggested fixtures:

1. Greenfield static todo app.
2. Existing React dashboard: add saved searches.
3. Existing API app: add tags/search to notes.
4. Existing CLI: add a new transformation option.
5. Existing app with auth-like boundaries: add a user-specific preference.

Each fixture should define:

- Intent.
- Expected files or capabilities.
- Existing test command.
- Acceptance criteria.
- Known non-goals.
- Certification stories.

Output:

```text
tests/fixtures/product-mvp/
```

### Phase 1: Product Brief Prompt and Artifact

Change the spec flow from "short spec" to "product brief."

Touch points:

- `otto/prompts/spec-light.md`
- `otto/spec.py`
- `otto/cli.py`
- `otto/paths.py`

Add sections:

- Intent.
- Target User.
- Core Workflow.
- Must Have.
- Must Not Have Yet.
- Success Criteria.
- Product Quality Bar.
- Open Questions.

Verification:

- Unit tests for parsing/persisting brief versions.
- CLI test that `--spec` writes `product-brief.md`.
- Golden fixture for a vague intent that produces open questions.

### Phase 2: Repo Discovery Artifact

Add a repo-discovery step before the build prompt for existing repos.

Implementation options:

- Initially prompt-driven inside the build agent.
- Prefer a separate lightweight pre-build agent later, so the plan is stable
  and auditable before implementation.

Touch points:

- `otto/pipeline.py`
- `otto/prompts/build.md`
- `otto/paths.py`

Verification:

- Unit test that repo map path is created.
- E2E fixture checks that repo map mentions stack, test command, and likely
  edit files.

### Phase 3: Implementation Plan Gate

Add plan generation after product brief and repo map, before edits.

Touch points:

- New prompt: `otto/prompts/plan.md`
- `otto/spec.py` or new `otto/planning.py`
- `otto/cli.py`
- `otto/checkpoint.py`

Checkpoint phases:

```text
spec -> spec_review -> spec_approved -> repo_discovery -> plan_review ->
plan_approved -> build -> certify -> round_complete
```

Verification:

- Resume from `plan_review` does not regenerate prior artifacts unless asked.
- `--yes` auto-approves plan.
- `--plan-only` exits before code edits.

### Phase 4: Requirement IDs and Traceability

Introduce stable requirement IDs from the product brief.

Touch points:

- `otto/spec.py`
- `otto/markers.py`
- `otto/certifier/report.py`
- `otto/prompts/build.md`
- `otto/prompts/certifier*.md`

Builder should report:

```text
REQUIREMENT_IMPLEMENTED: saved-search-create | files=...
REQUIREMENT_TESTED: saved-search-create | command=...
```

Certifier should report:

```text
REQUIREMENT_CERTIFIED: saved-search-create | story=story-save-current-filters
```

Otto should merge these into `traceability.json`.

Verification:

- Parser tests for requirement markers.
- Report test for missing-test and missing-evidence warnings.
- E2E fixture where one requirement is intentionally deferred.

### Phase 5: Product Quality Certifier Rubric

Upgrade certifier prompts and report schema with quality dimensions.

Touch points:

- `otto/prompts/certifier-fast.md`
- `otto/prompts/certifier.md`
- `otto/prompts/certifier-thorough.md`
- `otto/certifier/report.py`
- `otto/markers.py`

New structured outputs:

```text
QUALITY_SCORE: 1-5
QUALITY_DIMENSION: ux_clarity | PASS|WARN|FAIL | reason
QUALITY_DIMENSION: integration_fit | PASS|WARN|FAIL | reason
QUALITY_DIMENSION: regression_safety | PASS|WARN|FAIL | reason
```

Verification:

- Parser tests for quality markers.
- Certifier report renders warnings separately from failing stories.
- E2E fixture with a working but poor UX should pass stories with WARN, not
  silently look perfect.

### Phase 6: Reviewer Summary

Generate `review-summary.md` at the end of every build/certify/fix run.

Touch points:

- `otto/pipeline.py`
- `otto/certifier/report.py`
- `otto/history.py`
- `otto/paths.py`

Verification:

- Snapshot tests for success, certification failure, and paused budget cases.
- CLI prints path to review summary.

### Phase 7: MVP Eval Harness

Add a command or test utility that runs the product MVP fixtures across
providers and records comparable metrics.

Possible command:

```bash
otto eval product-mvp --provider codex
otto eval product-mvp --provider claude
```

If a full CLI is too much for MVP, use a documented script under `tests/` or
`scripts/`.

Metrics:

- Final pass rate.
- Requirement coverage.
- Requirement-to-evidence coverage.
- Existing test pass rate.
- Certification pass rate.
- Quality warnings.
- Human approval count.
- Runtime.
- Token/cost usage.
- Provider/tool error count.

## Verification and Eval Plan

### Unit and Contract Tests

Required tests:

- Product brief parsing and persistence.
- Plan checkpoint/resume.
- Requirement marker parsing.
- Traceability merge behavior.
- Quality marker parsing.
- Review summary rendering.
- Backward compatibility with existing spec/checkpoint files.

### E2E Functional Tests

For each MVP fixture, run:

```bash
otto build "<intent>" --spec --yes --standard --provider <provider>
```

Assertions:

- Build exits successfully or fails honestly.
- Existing tests run and pass unless failure is explained.
- Certification produces story results.
- `traceability.json` exists.
- Every Must Have requirement is satisfied, deferred, or failed with reason.
- `review-summary.md` exists and names how to try the result.
- No evidence integrity violations.

### Human Review Eval

At least two humans should review final outputs without watching the run.

Questions:

- Can they understand what changed in under 5 minutes?
- Can they run the product locally from the report?
- Do they agree the requirement coverage is honest?
- Would they merge the branch or know exactly what blocks it?

### Provider Comparison

Run the same fixtures with Claude and Codex.

Compare:

- Pass rate.
- Duration.
- Token/cost reporting.
- Tool error rate.
- Browser interaction quality.
- Number of fix rounds.
- Quality warnings.
- False PASS or false FAIL incidents.

This should become a regular regression suite for provider changes.

### MVP Exit Criteria

The MVP is ready to claim "intent to PR-ready product slice" when:

- At least 4 representative fixtures pass with one or more providers.
- No fixture has an unreported missing Must Have requirement.
- No certification PASS lacks required evidence.
- Review summary is sufficient for a human reviewer to run and inspect the
  result.
- Resume works from brief review, plan review, build, and certify phases.
- Provider-specific failures are classified honestly, not shown as product
  failures.

## Risks

- The plan gate could add friction for simple tasks. Mitigation: auto-skip or
  auto-approve for tiny repos and `--yes`.
- Requirement traceability could become noisy. Mitigation: keep IDs limited to
  user-visible Must Have requirements.
- Certifier quality warnings could block too much work. Mitigation: warnings
  are non-blocking unless they violate explicit success criteria.
- Repo discovery can be shallow. Mitigation: make it auditable and improve from
  fixture failures.
- Provider behavior differs. Mitigation: compare providers on the same fixtures
  and keep provider quirks out of product verdicts.

## Recommended First PRs

1. Add MVP fixture definitions and an eval README.
2. Upgrade `spec-light.md` into product brief format.
3. Add `implementation-plan.md` artifact with `--plan-only`.
4. Add requirement IDs and `traceability.json`.
5. Add reviewer summary generation.
6. Expand certifier prompts/report schema with product-quality dimensions.

The highest-value first slice is product brief + implementation plan + reviewer
summary. That improves user trust immediately, even before deeper traceability
and eval automation are complete.
