# Plan review — Otto redesign

Reviewer: Claude Sonnet 4.6. Date: 2026-05-04. Branch: `cc-i2p-2`.

This review is adversarial and concrete. Its job is to make the plan
executable. Every finding below has a specific location and a
recommended fix. Abstract findings ("the plan could be clearer") have
been discarded in favor of findings with a line-level suggestion.

---

## 1. Plan vs reality gaps

### What exists but the plan doesn't mention

The plan reads as if `otto/spec.py`, `otto/build.py`, `otto/audit.py`,
`otto/render.py`, and `otto/checks.py` are new files to be created in
Phase A1–A3. They all already exist — and they are non-trivial:

- `otto/build.py` — 1000+ lines, fully implemented `build_groups`
  orchestration using `Slice` (not `Group`) as the dispatch unit, with
  `BuildBudget`, `SliceStatus`, progress-based retry logic.
- `otto/audit.py` — 1400+ lines, full audit loop with
  `CapabilityVerdict`, `SliceVerdict`, `AuditVerdict`, and a fix loop.
- `otto/render.py` — 500+ lines, working `ProofPacket` renderer emitting
  `proof-packet.html` and `proof-packet.json`.
- `otto/checks.py` — full `Check` hierarchy with `run_checks`.
- `otto/spec_compile.py` — `Spec` + `Slice` dataclasses, `compile_spec`,
  JSON schema validation, amendment chain. The plan says "new: `otto/spec.py`"
  but that file is the legacy markdown spec gate. The real structured
  spec lives in `otto/spec_compile.py`.
- `otto/merge_queue.py` — eligibility-gated FIFO already exists.
- `otto/merge/` — full module: `orchestrator.py`, `conflict_agent.py`,
  `git_ops.py`, `state.py`, `edit_scope.py`, `stories.py`.
- `otto/spec_state.py` — `state.jsonl` event emitter already exists
  (named `spec-state.jsonl`), with `run.finished` event support.
- `otto/spec_amend.py`, `otto/spec_warnings.py`, `otto/spec_schemas/` —
  exist and are wired into compile.
- `otto/cli_run.py` — `otto run` command already exists and drives the
  full pipeline.
- `otto/queue/` — queue runner module (`runner.py`, `runtime.py`, etc.)
  — the plan says this is replaced in Phase C but doesn't document its
  current role.

**The plan treats Phase A1 as greenfield when it is actually a large
refactor of working code.** This is the single most dangerous gap:
implementers will either (a) rewrite working code from scratch and
introduce regressions, or (b) realize mid-sprint that the "new modules"
already exist and lose the phase's budget to archaeology.

**Recommended fix:** Add a "Current state" section to Phase A1 listing
which files already exist, what they implement, what dataclasses they
use (`Slice`, `CapabilityVerdict`), and explicitly stating that Phase A1
is a rename+reshape of existing code, not net-new code.

### What the plan mentions that genuinely doesn't exist yet

- `otto/defaults.py` — confirmed absent. Magic numbers live in `BuildBudget`
  dataclass fields (e.g. `per_slice_retries_hard_cap: int = 8`,
  `per_slice_wall_s: int = 30 * 60`), `otto/config.py`, and scattered
  callsites. This is real work.
- `otto/audit_loop.py` as a *separate* module — audit loop logic currently
  lives inside `otto/audit.py`. The plan calls for splitting it out; that
  doesn't exist yet.
- `otto/mission_control/run_view.py` — confirmed absent.
- `otto/web/client/src/components/run/` directory — confirmed absent.
- `otto/web/client/src/types/run.ts` — confirmed absent.
- Per-Feature proof at `proof/features/<feature-id>/` — not in the
  existing render output. `render.py` produces `SlicePacket` (per-slice),
  not per-Feature pages.
- `otto/prompts/audit.md` replacing legacy certifier prompts — the
  legacy prompts (`certifier-thorough.md`, `certifier-fast.md`, etc.)
  still exist and are the active audit prompts.
- `otto/web/templates/proof-packet.html.j2` — render currently uses
  inline string generation, not a Jinja template.
- `otto/mission_control/spec_review_routes.py` — absent.

---

## 2. Phase A0 vocabulary refactor feasibility

Grep results across all in-scope targets:

- `otto/**/*.py`: **1,288 hits** across 31 files.
- `tests/**/*.py`: **700 hits** across ~40 files.
- `otto/**/*.ts` / `*.tsx`: **172 hits** across ~20 files.
- `otto/prompts/*.md`: **173 hits** across 15 files.

Total: **~2,353 hits** spanning ~110 files.

The dominant terms are `slice` (180+ occurrences in `build.py` alone;
`Slice` dataclass is imported by 10 modules), `certifier` (used heavily
in `config.py`, `cli_improve.py`, `memory.py`, prompt filenames), and
`story`/`stories` (present in `merge/stories.py`, `types.ts`,
`history.py`).

**The 1-2 day estimate is unrealistic.** Rename tooling can handle
simple variable renames, but:

1. `Slice` is a frozen dataclass with 10 import sites. Renaming to
   `Group` requires updating all JSON serialization (`spec_to_dict`),
   all JSON deserialization (`load_spec`), the existing `spec-state.jsonl`
   schema, and 10+ test fixtures that hardcode `"slices"` as JSON keys.
   Old session dirs under `otto_logs/` have `spec.json` files with
   `"slices"` keys — the plan says "not touched," but the
   deserialization code will need backward-compat shims.
2. `certifier` is baked into `otto/config.py`'s `AGENT_TYPES` tuple
   (`"build", "certifier", "spec", "fix"`), the config schema at the
   bottom of `config.py`, `otto.yaml` user files, and MC frontend
   polling that reads `certifier_mode` from API responses. A rename
   here ripples into user-visible config keys and API response shapes.
3. `capability_verdict` is a dataclass field name in `AuditResult` and
   `AuditAgentOutput`, which are serialized to `proof-packet.json`.
   Old proof packets that existing bench results read will no longer
   deserialize.
4. The `merge/stories.py` module has `stories` in both its name and its
   core logic (it parses `"stories"` JSON keys from legacy proof-of-work
   files). Renaming it requires changing the JSON keys it reads from
   manifests, which may be produced by legacy `otto build` runs still in
   flight.

**Realistic estimate: 5-7 days** for a careful, test-verified refactor
with backward-compat shims for old session dirs. This is not a 2-day
find-replace. The hidden cost is JSON schema compatibility for existing
`proof-packet.json` files read by MC and bench scripts.

**Recommended fix:** Add to Phase A0: (a) explicit schema migration
plan for `spec.json` key renames (`"slices"` → `"groups"`), with a
reader shim for legacy session dirs; (b) API response backward-compat
layer for MC (old keys still readable during Phase A coexistence); (c)
increase time estimate to 5-7 days.

---

## 3. Phase A1 sequencing risk

The plan groups five distinct work items into one 4-6 day phase:
- New `Spec`/`Group`/`Feature`/`Guardrail` dataclasses
- `compile_spec` producing the new shape
- `build_groups` dispatching per-Group
- `otto/merge.py` (eligibility-gated FIFO)
- `otto/checks.py` gaining `feature_id` on Evidence

Each has its own test surface and its own risk of breakage.
The 4-6 day estimate assumes linear progress; in reality each item
gates the next, and any one item failing its Codex gate blocks all of
them.

**The sequencing risk:** if `compile_spec` producing the new
`Group`+`Feature` shape doesn't pass Codex gate, `build_groups`
can't be tested. If `merge.py` has a serialization bug, the
integration test can't run. The phase has no internal checkpoints.

**Recommended split:**

- **A1a (2 days):** New dataclasses only. `Spec`, `Feature`, `Group`,
  `Guardrail` in `otto/spec_compile.py` (rename from `Slice`). Unit
  tests for serialization round-trip, backward-compat reader for old
  `"slices"` JSON, `compile_spec` LLM call producing new shape.
  Gate: `pytest tests/test_spec.py` green.

- **A1b (2 days):** Build and Check loop. `build_groups` dispatching
  per `Group` (renaming slice dispatch, threading `feature_id` into
  `Evidence`). `otto/defaults.py` extracting magic numbers. Gate:
  `pytest tests/test_build.py tests/test_checks.py` green.

- **A1c (2 days):** Merge. Eligibility-gated FIFO with `Group.dependencies`
  and `owned_paths` serialized in new format. Gate: `pytest tests/test_merge.py`
  green, integration test showing two Groups landing in dep order.

Each sub-phase gets its own Codex review. Codex gate takes time; don't
stack three gates onto one review.

---

## 4. Phase A2 audit feature-tagging realism

This is the highest-risk technical bet in the plan. The LLM must emit
`feature_ids[]` on every walkthrough action, without missing any, while
simultaneously navigating the product. The plan's mitigation is:

> "Audit prompt explicitly instructs the agent to tag each action with
> the Feature(s) it evidences. Prompt enforces 'no untagged actions.'"

This is a prompt instruction, not an enforcement mechanism. LLMs drop
structured fields under high cognitive load (complex navigation,
multi-step workflows) or when a step doesn't obviously map to a single
feature (infrastructure checks, cross-feature navigation). The existing
`audit.py` already has `CapabilityVerdict` but no per-action tagging in
`walkthrough.jsonl`.

**What the plan doesn't address:**

1. **Partial tagging scenario.** If 20% of walkthrough actions have
   `feature_ids: []` (not `"exploration"`), what happens? The plan says
   "parser rejects untagged-non-exploration." That means the audit fails
   and the audit loop triggers. But one audit pass costs $0.50–$1.50;
   two passes is $1–3 just for a tagging failure, not a product
   deficiency. The plan has no cost ceiling for repeated tagging failures.

2. **Feature id stability assumption.** The audit agent must emit
   `feature_ids` that match the ids compiled by `compile_spec`. Any id
   mismatch (typo, old id, unknown feature) produces an orphan evidence
   record. The plan has no validation step that checks emitted ids
   against the spec's known feature ids.

3. **Backup plan is absent.** The plan's risk register says "audit
   prompt enforces tagging; parser rejects untagged-non-exploration."
   That's the mechanism, not the backup. The backup plan if tagging
   coverage is < 95% after 3 real-cost test runs needs to be explicit.
   Options: (a) fall back to per-slice verdicts with no per-Feature
   evidence; (b) post-process coverage by mapping walkthrough narrative
   text to feature ids using a cheap LLM pass; (c) use acceptance
   criteria from the spec as feature-to-action matching anchors.

**Recommended fix:** Add to Phase A2: (a) a validation step after
parsing `walkthrough.jsonl` that cross-checks all emitted `feature_ids`
against the spec's known ids and logs unrecognized ids as a warning;
(b) a coverage threshold (e.g. ≥ 90% of non-exploration actions tagged)
with a fallback to per-Group verdicts when the threshold is missed;
(c) an explicit backup plan if three real-cost runs don't reach the
coverage threshold.

---

## 5. Phase A3 render determinism trap

The plan claims `render_proof` is a pure deterministic function. The
existing `render.py` already has these non-determinism sources:

1. **`time.time()` calls.** The existing renderer calls `time.time()`
   for timestamps embedded in HTML (e.g. "rendered at..."). If the
   plan's re-run byte-stability test uses wall clock time, it will fail
   on the second run.

2. **Asset path absoluteness.** The existing `ProofPacket` stores
   `walkthrough_artifacts: list[str]` as absolute paths. If Render is
   re-run from a different working directory, absolute paths break.
   Relative-to-session-dir paths are required for portability.

3. **Dict iteration order.** Python 3.7+ preserves insertion order for
   `dict`, but any `json.loads` + `json.dumps` round-trip where the
   source JSON has keys in non-deterministic order (e.g. produced by
   Go/Java tooling) will produce different output order.

4. **Screenshot timestamps.** Screenshots captured by the audit agent
   have timestamps in their filenames. If Render sorts assets by
   filename, the order is deterministic; if it uses filesystem mtime,
   it is not.

5. **Cost display rounding.** `cost_usd` from agent telemetry may have
   floating-point representation differences across runs if computed
   differently. If Render formats `$1.234567` vs `$1.23`, the HTML
   changes on re-render.

6. **HTML template whitespace.** If Render uses Jinja templates, Jinja's
   `trim_blocks`/`lstrip_blocks` behavior is version-sensitive. A Jinja
   upgrade can change output whitespace without breaking rendering.

**Recommended fix:** The plan's "re-run test: same session, run Render
twice, output is byte-stable" verification should be:
(a) rendered with a fixed `rendered_at` sentinel (e.g. `1970-01-01T00:00:00Z`
when running under test); (b) all file paths normalized to
session-relative; (c) floats round-tripped through a consistent format
function (`f"{cost:.4f}"`); (d) golden snapshot test checks semantic
content (feature count, verdict, evidence links) not raw bytes.

---

## 6. Phase A4 MC redesign — frontend cost and ambiguity

The plan specifies new components but leaves the type/state model
critically underspecified:

1. **`RunView` shape is not defined.** The plan says `build_run_view`
   returns a `RunView` "ready for frontend" but gives no field
   definition. The frontend components (`FeatureList`, `GroupList`,
   `StageTimeline`) will be coded against an undefined type. This
   produces a painful cycle: frontend dev finds a missing field,
   requests it, backend adds it, API changes, frontend updates. Define
   `RunView` and `FeatureView` fully before any component is written.

2. **Live run state model is unspecified.** The plan says "live runs
   render via the same drawer with fields appropriately partial." What
   does `RunView` look like mid-build? Which fields are `null`? Which
   fields are filled from `spec-state.jsonl` events in real time?
   The existing `i2p_routes.py` returns a flat `run_data` dict; the new
   `run_view.py` needs to define the delta.

3. **Routing switch is ambiguous.** The plan says route to `<RunDrawer />`
   if `run.domain === "i2p"`. But the goal is to eventually remove this
   domain split. If the routing condition is `domain === "i2p"`, Phase B
   cutover requires changing the condition to `domain in ("i2p", "build",
   ...)`, adding surface area. Better condition: route to `<RunDrawer />`
   if the session dir has `spec.json` with the new schema version.

4. **`RunsView.tsx` replacing "Tasks panel"** — the wireframe shows this
   as the run list. But `App.tsx` currently has 3,094 lines with
   `<RunInspector />` integrated directly. Moving to a new component
   requires understanding what state `App.tsx` currently manages for the
   inspector (live polling, event subscriptions, drawer open/close). The
   plan doesn't address this state migration.

**Recommended fix:** Add a sub-step to Phase A4: define `RunView`
TypeScript interface with all fields (including `null` states for
in-flight runs) in `run.ts` before writing any component. That type
definition becomes the API contract between frontend and `run_view.py`.

---

## 7. Phase B/C cutover — parity criteria not precise enough

The plan says Phase B is "gated on Microfeed parity bench (research
§12.7)." Research §12.7 says "Microfeed parity bench" but defers to
"the original plan step 11." The original plan step 11 (in
`~/.claude/plans/plan-based-on-this-soft-cloud.md`) has concrete
criteria:

- Hidden evaluator passes.
- Browser private evaluator passes.
- 0 slices blocked.
- Wall time ≤ 1.5× mono baseline (≈ 36 min).
- Cost ≤ 1.2× mono baseline.
- Audit verdict = `passed`.
- App shell visually comparable (human-checked proof packet screenshot).

These criteria need to be copied verbatim into `plan.md` with the
current baseline numbers. "Inheriting from step 11" is a pointer into
a file that may not be readable during Phase B implementation. Also:

1. The criteria use "0 slices blocked" — this needs updating to "0
   Features blocked" after the rename.
2. "Human-checked" is a pass/fail criterion with no definition of who
   checks and what "comparable" means. Add: "reviewer checks: landing
   page loads, at least 2 features visible in the UI, no console errors."
3. Variance: these are single-run criteria. A single run can pass by
   luck. Require 2 of 3 runs passing all criteria for gate clearance.

**Recommended fix:** Copy the step 11 criteria into `plan.md §Phase B`
verbatim, update `"slices"` to `"features"`, add the human-check
definition, and add the "2 of 3 runs" requirement.

---

## 8. Verification plan honest assessment

The plan's verification steps have three categories of gaps:

**Structural gaps:**

- Verification 1 (vocabulary grep) is sound but missing the
  `docs/` directory grep for UI strings and the full list of JSON key
  names that also need renaming (`"slices"`, `"capability_verdicts"`,
  `"certifier_mode"`).
- Verification 4 ("each line of walkthrough.jsonl has feature_ids[]
  populated") is a real-cost integration test that requires a live LLM
  run. The plan gates this behind `OTTO_ALLOW_REAL_COST` but doesn't
  say who runs it, when, or how many runs are required to validate the
  claim.
- Verification 6 (MC renders new shape) depends on having actual session
  data to render. The plan needs to specify what fixture session is used
  for this test.

**Missing verifications:**

- No verification that `parse_spec_md(render_spec_md(spec)) == spec`
  (the round-trip byte-stability stated as a requirement in research.md).
- No verification that `Feature.id` is stable across compile→build→audit
  (i.e. the audit agent's emitted `feature_ids` match what compile
  produced). This is a cross-stage contract with no test.
- No verification that `otto render <old-session-id>` (re-render on a
  legacy session dir) doesn't crash — forward-compatibility is
  mentioned but untested.
- No verification that `otto run --resume` works after a Phase A0 rename
  (checkpoint files may have old field names).

**Verification steps that don't actually verify what they claim:**

- "pytest -q green" after A0 only verifies that test assertions match
  new names. If a test was asserting `result.stories_passed == 3` and
  was updated to `result.features_passed == 3` without re-running a
  real build, the test is correct but the code under test may still use
  `stories_passed` internally.
- "snapshot test: golden HTML for a fixture spec — review changes
  manually before merging" is a manual check, not a verification step.
  Add a machine-readable check: parse the HTML, count `<div
  class="feature">` elements, assert count equals spec feature count.

---

## 9. Sequence revision

The plan's sequence (A0 → A1 → A2 → A3 → A4 → A5 → A6 → B → C) has
one structural problem: it completes the entire backend stack before
touching the frontend (A4). This means 3-4 weeks of backend work with
no visual feedback loop on whether the new data shape is actually
renderable in the UI. Problems found in A4 ("RunView needs a field we
didn't emit") require backtracking into A1/A2.

**Recommended sequence:**

1. **A0** — vocabulary refactor, but scope it explicitly: rename Python
   dataclasses and fields first; rename JSON keys with shims; rename
   prompts last. 5-7 days.

2. **A1a** — new `Feature`/`Group`/`Guardrail` dataclasses + `compile_spec`
   producing the new shape. Unit tests only, no integration. 2 days.

3. **A4 (type contract only)** — define `RunView`/`FeatureView` TypeScript
   interface and the `build_run_view` function signature (stub
   implementation). 1 day. This ensures A1b-A3 are built against the
   correct output shape.

4. **A1b** — build dispatch per-Group. 2 days.

5. **A1c** — merge queue. 2 days.

6. **A2** — audit Feature-tagging + audit loop module. 3 days. Include
   the real-cost tagging coverage test.

7. **A3** — render per-Feature proof. 2 days.

8. **A4 (implementation)** — MC frontend using the now-concrete
   `RunView` shape. 3-4 days.

9. **A5, A6** — spec review screen and brownfield mode. 4-5 days.

10. **B** — cutover behind parity bench. 2 days.

11. **C** — deletion. 1 day.

Total: ~23-25 days vs the plan's implicit ~18-20 days (more realistic
given the existing code realities).

---

## 10. Top 10 specific edits to plan.md

**1. Add "Current state" to Phase A1 scope.**

Before the bulleted scope list, add:
> "Existing state: `otto/spec_compile.py` already has `Spec` + `Slice`
> dataclasses; `otto/build.py` has `build_groups` and `BuildBudget`;
> `otto/audit.py` has `CapabilityVerdict`. This phase renames and
> reshapes those, not creates them. Identify every import site before
> writing a line of code."

**2. Change Phase A0 time estimate.**

Change "1-2 days" (implied by position in a 4-6 week project) to
"5-7 days" and add a sub-bullet:
> "JSON key backward-compat: add reader shim in `spec_compile.load_spec`
> accepting both `"slices"` and `"groups"` keys, returning `Group`
> objects. Shim stays until Phase C."

**3. Split Phase A1 into A1a / A1b / A1c.**

See section 3 above. Each sub-phase gets its own Codex review cue.

**4. Add coverage threshold to Phase A2 verification.**

After the real-cost integration test bullet, add:
> "Acceptance threshold: ≥ 90% of non-exploration walkthrough actions
> have non-empty `feature_ids[]`. Below 90%: rewrite audit prompt before
> proceeding. Backup plan if 3 real-cost runs are below threshold:
> post-process via a cheap LLM pass mapping action text to spec feature
> ids."

**5. Add `RunView` type definition as a Phase A4 pre-step.**

Before the frontend component list, add:
> "Sub-step 0 (1 day): write `otto/web/client/src/types/run.ts` with
> full `RunView`, `FeatureView`, `GroupView`, `GuardrailView`,
> `StageView` TypeScript interfaces, including `null` values for
> in-flight fields. Write `build_run_view` stub returning the correct
> shape. No component work until this is reviewed and approved."

**6. Define parity criteria inline in Phase B.**

Replace:
> "Phase B is gated on Microfeed parity bench (research §12.7)."

With:
> "Phase B parity gate (2 of 3 runs must pass all criteria):
> - 0 Features blocked.
> - Wall time ≤ 36 min (1.5× mono baseline of ≈ 24 min).
> - Cost ≤ 1.2× mono baseline.
> - Audit verdict = `passed`.
> - Human-check: landing page loads, ≥ 2 features visible in UI,
>   no console errors in browser DevTools."

**7. Add spec round-trip test to Phase A1 verification.**

After the integration test bullet, add:
> "Round-trip test: `assert parse_spec_md(render_spec_md(s), base=s) == s`
> for a fixture spec. Run this as part of `pytest tests/test_spec.py`.
> This is the byte-stability contract stated in research §2.1."

**8. Remove "New file: `otto/checks.py`" from Phase A1 files list.**

`otto/checks.py` already exists. Change to:
> "Modified: `otto/checks.py` — add `feature_id` field to `Evidence`,
> update `run_checks` signature to accept and thread `feature_id`."

**9. Add `otto/spec_compile.py` to the Phase A0 rename scope.**

The current Phase A0 files list says "All `otto/**` Python files." Add
an explicit bullet:
> "`otto/spec_compile.py`: rename `Slice` → `Group`, `Slice.tasks` →
> `Group.features` (list of Feature ids), add `Feature` and `Guardrail`
> dataclasses, update `spec_to_dict`/`load_spec` with backward-compat
> shim."

**10. Add non-determinism guard to Phase A3 verification.**

Replace:
> "Re-run test: same session, run Render twice, output is byte-stable."

With:
> "Re-run test: same session, run Render twice. Output is byte-stable
> only when `rendered_at` is pinned to a sentinel value in test mode
> (set via `OTTO_RENDER_TIMESTAMP` env var). Test asserts: same feature
> count, same verdict, same evidence link hrefs, same feature ids.
> Raw HTML byte equality is NOT required and should not be asserted —
> it will false-negative on any template whitespace change."
