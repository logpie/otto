# Otto redesign — research

## P0 verdict/merge integrity hardening (2026-05-15)

Scope: enforce the pass-1 policy that detector findings route to agent repair
or block; they must not downgrade to lenient merge, warn, skip, or empty
contract fallback.

Relevant current paths:

- `otto/v5_runner.py` merges direct child worktrees into the parent branch when
  the child verdict is `pass`, `partial`, or `unverified`; decomposed child
  integration propagation uses the same permissive verdict set.
- `otto/lead.py` canonicalizes non-canonical success/status blobs to `pass`
  even when they carry no journey or evidence. Runner verification-plan
  exceptions are only logged and preserve the agent's self-reported verdict.
- `otto/merge_queue.py` lets degraded build slices land when failed checks are
  classified as non-structural.
- `otto/spec_compile_flat.py` accepts an empty fallback spec after all
  compile attempts fail JSON/schema checks.
- `otto/v5_preflight.py` maps scaffold unknown/internal failures to warning
  severity; `smoke_clean_deploy` has the same unknown catch-all pattern.

Constraints and decisions:

- Represent explicitly reviewed partials with the minimal durable field
  `review_state: "reviewed_partial"` plus optional `reviewed_partial_*`
  metadata. Do not introduce a new terminal verdict string that would leak into
  aggregate product verdict semantics.
- A raw `partial` or `unverified` child is not mergeable. The runner may run one
  verify/repair dispatch in the same child worktree; after that, only `pass` or
  explicit reviewed-partial can merge. Otherwise mark the child
  `merge_blocked` and emit a blocking event.
- Keep compile hardening fail-loud: the existing compile retry loop is already
  the repair/redelivery path. If it still cannot produce a usable structured
  contract, raise instead of writing an empty `spec.json`.
- For preflight unknowns, use blocking severity so existing repair machinery or
  architect redispach sees the raw failure. Only named informational cases stay
  warnings.

Open question for later review:

- `review_state=reviewed_partial` is intentionally narrow. If the product wants
  a human UI workflow for approving partials, that should be a later pass.

## Leaf runtime invariant poisoning fix (2026-05-15)

Scope: fix the v5 modular-path failure where a leaf prompt could contain a
stale `SESSION_DIR` from the parent-authored child intent plus Otto's correct
runtime session dir. The leaf then wrote `verdict.json` to the wrong session.

Relevant current paths:

- `otto/lead.py` renders both normal Lead and integration prompts, saves the
  rendered prompt, and reads `verdict.json` after the agent run.
- `otto/prompts/lead.md` currently renders `TASK ID`, `INTENT`, and
  `SESSION_DIR` as sibling input bullets; multiline intent can therefore
  inject runtime-looking lines directly into the rendered prompt.
- `_read_agent_verdict()` already recovers canonical verdicts from the
  canonical path, worktree-misplaced `verdict.json`, legacy verify output, and
  final assistant prose, but not from Write tool inputs in `lead/messages.jsonl`.
- Existing regression style is focused private-helper tests under
  `tests/test_v5_*.py`, including `tests/test_v5_verdict_recovery.py` and
  prompt-rendering tests in `tests/test_v5_integration_worktree.py`.

Constraints and decisions:

- Keep the fix in `otto/lead.py`; avoid prompt-template churn.
- Sanitize runtime-looking line prefixes before interpolating agent-authored
  child intent, then render a clearly delimited runtime block after the intent.
- Reuse `_canonicalize_verdict_payload()` for rescued Write payloads, but only
  accept canonical-shaped verdict objects rather than bare status claims.

## Field-test forced tier research (2026-05-15)

Scope: update the v5 field-test rig so the next run exercises inline, flat
decomposition, and recursive decomposition instead of letting every scenario
default to auto/inline. No live Otto runs.

Relevant current paths:

- `scripts/run_field_tests.py` already parses `tier` from
  `success_criteria.md` metadata and passes `--tier` in `otto_command()`, but
  the scenario files all still set `tier: auto` and the result matrix does not
  show the tier.
- `bench/field-tests/*/expected_shape.md` describes the desired shape, but it
  does not declare the forced tier. The generated project includes this text in
  `FIELD_TEST.md`; the CLI `--tier` flag remains the actual enforcement path.
- `otto/lead.py` currently gives special prompt text for `solo` and `modular`,
  but not for `lead`. The `modular` text says to consider decomposition rather
  than requiring the architecture-first shape advertised by the CLI help.
- The root inline path in `otto/v5_runner.py` runs the root Lead and then
  aggregates the verdict without a runner-owned commit. Child build branches
  commit via `commit_worktree()`, and root integration commits via
  `commit_integration_worktree()`.

Decision:

- Keep tier metadata in `success_criteria.md`; adding a new config file would
  duplicate an existing parser path.
- Use the proposed mapping: `01=solo`, `02=auto`, `03=lead`,
  `04=modular`, `05=modular`. Tighten `04` for architect plus vertical leaves
  and `05` for recursive output-pipeline decomposition.
- Reuse `commit_worktree(..., message="v5 inline build")` for root inline
  finalization. Root inline owns the full product, including top-level CLI
  files such as `csv_to_json.py`, so the stricter integration allowlist would
  reject legitimate greenfield output.

Open constraints:

- The external Codex MCP gate tool is not available in this session. I will
  run the local `codex-gate` checklist and document plan/implementation gate
  status in `plan-field-tests.md` / `review.md`.

## Pre-v6c test coverage audit (2026-05-14)

Scope: raise confidence before another live v6 run by reading the focused
`tests/test_v5_*.py`, `tests/test_spec_compile*.py`, and branching coverage,
then adding deterministic high-signal regressions. No live runs.

Current focused coverage already includes:

- Spec compile structure/lint/legacy parsing, StructuredOutput tool extraction,
  result structured output extraction, cache reuse/miss by model, corrupt-cache
  ignore, compile metrics, and root spec artifact cleanup.
- V5 task graph, pending queue, dependency ordering, flat global dispatch lease,
  root and subtree integration preflight payloads, Step 0b summary/prompt
  helpers, skipped-report helper, clean-state verification, port cleanup,
  install-dir propagation helpers, IA contract direct validation, matrix-scope
  wiring, and branch merge/noise handling.
- The landed v6b regression suite for decomposed child subtree propagation to
  `main`, including shallow+deep mixed root children.

Highest-leverage gaps selected:

1. Provider divergence in Lead verdict recovery. Spec compile has explicit
   StructuredOutput/result fallbacks, but Lead verdict rescue only checks
   assistant text blocks. Codex-style inline final `result` records can
   silently become `unverified` even when a valid verdict JSON is present.
   Likelihood high, cost low, directly v6-relevant.
2. Spec cache invariant tests beyond model changes. Cache key payload includes
   prompt hash/schema version/provider/model/otto version, but focused tests
   only prove identical reuse and model miss. Add prompt/schema mismatch and
   legacy v2 cache-hit loading checks. Likelihood medium, cost low.
3. Nested global dispatch lease stress. Existing lease test covers concurrent
   flat schedulers; live failure involved nested decomposition and capacity
   across recursive scheduler loops. Likelihood high, cost medium.
4. Root integration sees real files from all root children. Existing tests show
   child files reach `main`; add full `run_v5_pipeline` coverage that root
   integration starts after four children and observes all product files before
   final pass. Likelihood high, cost medium.
5. Step 0b recovery in the full pipeline. Existing tests cover summary rendering
   and reconcile helper directly; add full root integration behavior where the
   integration agent merges a blocked child branch and final aggregate flips
   back to pass. Likelihood high, cost medium.
6. Skipped report through `run_lead`, not just helper. Existing test calls the
   writer directly; add a fake agent session with `intent_coverage.skipped` and
   verify `skipped_report.md` is append-written by the real finally path.
   Likelihood medium-high, cost low.
7. Architect-time IA coherence emission. Direct validator catches missing
   `product_overview.top_level_pages` routes, but runner-time architect
   preflight must emit that finding from the latest spec/CHARTER. Likelihood
   medium-high, cost low.

Not selected for this batch:

- `otto v5 run --resume` smoke. The current `otto v5` CLI has no `--resume`
  option; i2p resume coverage lives on the monolithic `build --resume` path,
  which the dispatch explicitly says not to touch. This remains a product gap
  to decide separately rather than a quick pre-v6c regression.
- Multi-attempt generic child retry. The implemented retry machinery is
  architect-preflight retry plus provider fallback, both covered at narrower
  levels. A generic child retry/session-reattach feature is not present enough
  to pin without changing product semantics.

## v6 bugfix batch research (2026-05-14)

Scope: fix P1/P2/P3 issues from the v6b audit in the `cc-i2p-2`
worktree. No live Otto runs; validation is focused unit/regression tests plus
the requested v5/spec_compile/runner/branching suite.

Relevant current paths:

- `otto/v5_runner.py` owns child dispatch, nested scheduling, subtree/root
  integration, toolchain preflight propagation, and integration summaries.
- `otto/queue/subtask.py` already skips graph-terminal tasks across recursive
  schedulers, but `_process_children()` still has only per-loop in-flight
  accounting and no shared lease.
- `otto/lead.py` passes `verification_plan.matrix_scope` into
  `validate_lead_verdict()`, so matrix scope is mostly present; this batch
  needs an explicit runner-level regression proving leaf vs integration call
  wiring.
- `otto/v5_clean_verify.py` already treats busy declared ports as a clean
  verification failure; `otto/v5_preflight.py` still maps
  `clean_deploy_port_busy` to a warning, making the integration path too soft.
- `otto/v5_capability_inventory.py` counts CHARTER prose excluding the IA JSON,
  but the target remains 500 lines and the warning lacks the requested split.
- `otto/prompts/lead.md` is the right place to tighten root decomposition,
  CHARTER prose cap language, and deprecation-warning expectations.

Open constraints:

- The Codex MCP peer tool required by the project-level `codex-gate` workflow
  is not available in this session, and the user explicitly requested one
  workspace-write dispatch with no extra Codex calls. I will document that in
  the plan and rely on local tests.

## Dispatch 3 v6 perf-quality research (2026-05-14)

Scope: implement Batches 5 and 6 from `plan-v6-perf-quality.md` on the
`cc-i2p-2` worktree only. Batches 1-4 are already present in this branch.

Relevant current paths:

- `otto/prompts/lead.md` contains the architect CHARTER instructions, the
  Information Architecture Contract JSON shape, the DAG critical-path rule,
  and the leaf read-first rules.
- `otto/v5_capability_inventory.py` parses and validates the CHARTER IA JSON
  block in `parse_information_architecture_contract()` and
  `validate_information_architecture_contract()`. The warning-only coherence
  gate is `check_coherence()`, which is the right place to add a CHARTER line
  cap warning without post-processing the architect output.
- `otto/v5_runner.py` copies parent `spec/spec.json` into each child session
  in `_run_child()`, then calls `_run_lead_with_fallback()`. This is the lowest
  impact place to generate opt-in per-child slice artifacts before prompt
  rendering.
- `otto/lead.py` renders `lead.md` and saves the rendered prompt. It currently
  has no per-child context placeholder, so slicing needs a small optional
  prompt note that defaults to full repo-root context.
- `otto/cli_v5.py` wires `otto v5 run`. Batch 5 needs an explicit
  `--full-context` escape hatch and an opt-in slicing switch or config value
  because slicing must remain off by default.
- `otto/spec_compile_flat.py` owns the compile-spec-flat prompt and structured
  spec validation. `validate_structured_spec(strict=False)` already returns
  warnings instead of raising, which matches the requested over-cap warning for
  legacy or externally loaded specs.

Constraints and decisions:

- No provider routing changes. Claude remains the configured default path.
- No live runs. Validation stays unit/focused test based.
- Slicing remains opt-in. Default `otto v5 run` still passes full context.
- Full IA JSON stays intact in CHARTER slices. Prose may be filtered, but
  `Agent operating notes` is treated as operational cross-cutting context and
  preserved conservatively when slicing is enabled.
- Scope ambiguity falls back to full context and is written to
  `<child_session>/context_slice.json`.
- The Codex MCP tool required by `codex-gate` is not available in this session;
  this matches the Dispatch 2 implementation note in the plan. I will record
  the unavailable gate in `review.md` and use local tests/ruff for validation.

Open questions resolved by conservative defaults:

- Existing subtask entries do not declare `owned_paths` or `action_ids`. The
  slicer can consume those fields if present, but for current children it must
  derive action scope from exact action-id mentions and entity/action words in
  the task intent. If no confident action/entity match exists, it falls back to
  full context.
- Child prompts currently tell agents to read repo-root `CHARTER.md`. The
  prompt will continue to say that for full-context runs. When slicing is
  enabled, a rendered note points the child at session-local slice artifacts
  first, with full artifact paths available as a fallback.

---

This document is the load-bearing source of truth for Otto's redesign
around Feature / Group / Guardrail. It supersedes any prior
"V21 detail panel" framing in this branch — the panel is one
consequence of the redesign, not the unit of work.

Conversation transcript that produced these decisions:
[`docs/otto-redesign-conversation.md`](docs/otto-redesign-conversation.md).

Implementation plan derived from this doc:
[`plan.md`](plan.md). Wireframes:
[`docs/otto-wireframes.md`](docs/otto-wireframes.md).

---

## 1. Mental model (user-facing)

A user opens Otto with a goal: "build me a doc editor." That's the
Intent. From the Intent, Otto produces a **Spec** the user can read,
edit, and approve.

A Spec contains three things:

- **Features** — what the product does. Each Feature is a unit of value
  the user asked for or accepted. Each gets a verdict in the Proof.
- **Groups** — logical product verticals that bundle related Features
  ("Editor surface," "Comments," "Auth"). Each Group is also Otto's
  execution unit at runtime.
- **Guardrails** — explicit "don'ts" pinned to the Spec. ("Don't
  support video upload yet.")

Example doc-editor Spec, in user-facing language:

```
Intent: a doc editor with markdown rendering and inline commenting

Groups & Features
  Editor surface
    ✓ Markdown rendering
    ✓ Save / load
    ✓ Image upload (drag-drop, max 5MB, embed inline)
  Comments
    ✓ Line-anchored comments
    ✓ Threaded replies (one level)
    ✓ Resolve thread

Guardrails
  ✗ No video upload
  ✗ No real-time presence
  ✗ No mobile-specific UI
```

The user reads this, may edit it (add Features, add Guardrails, split
Groups, drop Features), then approves. Otto runs. Proof comes back at
the end.

---

## 2. Vocabulary (unified, normative)

This vocabulary is used **everywhere** — code, prompts, MC labels,
file paths, debug output, docs. Aligning code and UI prevents the
"slice in code, group in UI" trap that produced today's confusion.

| Term | Meaning |
|---|---|
| **Intent** | Free-text user input. The starting point. |
| **Spec** | Otto's compiled plan. Fields: `intent`, `features[]`, `groups[]`, `guardrails[]`, `structure`, `project_kind`. |
| **Feature** | One unit of value with a verdict. Fields: `id`, `name`, `description`, `acceptance_detail`, `evidence_kinds[]`, `group_id`, `verdict?`. |
| **Group** | One logical product vertical. Fields: `id`, `name`, `description`, `feature_ids[]`, `dispatch_plan`, `owned_paths`, `dependencies[]`. |
| **Guardrail** | Pinned negative scope. Fields: `id`, `text`, `applies_to` (whole product / specific Group / specific Feature). |
| **Audit** | The verification stage. Verb. Produces Feature verdicts + walkthrough evidence. |
| **Proof** | The final artifact. Noun. HTML + JSON + assets. |
| **Stage** | One pipeline phase: Compile / Build / Audit / Render / Land. |
| **Run** | One end-to-end session. One session dir under `otto_logs/sessions/<id>/`. |
| **Check** | A deterministic contract test inside Build. Kinds: `RepoTestCheck`, `ApiProbe`, `StateInvariant`, `BrowserJourney` (focused). |
| **Check loop** | Layer 1 retry loop. Per-Group. Triggered by Check failure. |
| **Audit loop** | Layer 2 retry loop. Per-Feature. Triggered by Audit verdict. |

Retired words (do not use anywhere): `slice`, `capability`,
`capability_verdict`, `task` (in user surface), `certifier`, `story`,
`stories_passed/tested`, `acceptance check`, `AC`.

---

## 2.1 Spec artifact contract — two artifacts, one source of truth

The Spec exists in two synchronized artifacts:

| | `spec.md` (user-facing) | `spec.json` (runtime) |
|---|---|---|
| Format | Markdown with HTML-comment metadata | JSON, schema-validated |
| Audience | Humans — read, edit, approve, share | Otto's stages — compile, build, audit, render |
| Editability | Free-text prose edits welcome | No free edits; derived |
| Lifetime | Versioned per user-edit (`spec-v1.md`, ...) | Versioned in lockstep |
| Source of truth for | Intent prose, Feature names + descriptions, Guardrails, Project-kind summary | Feature ids, group_ids, owned_paths, dependencies, evidence_kinds, dispatch_plan, full structure detail |
| Sharable | Yes (paste in PR, render in GitHub) | Internal artifact in session dir |

**Three rules:**

1. **User owns prose; Otto owns mechanics.** Users edit feature names,
   descriptions, group placements, guardrails. Otto derives ids,
   owned_paths, dependencies, dispatch plans. User cannot edit ids —
   they're stable across renames.
2. **Round-trip is byte-stable.** `parse_spec_md(render_spec_md(s), base=s) == s`.
   Otto's HTML-comment markers (`<!-- feature: id | evidence: ... -->`)
   persist through user edits. User's prose persists.
3. **Runtime never reads markdown.** Compile / Build / Audit / Render /
   Merge / Land all read `spec.json`. If user breaks the markdown,
   runtime is unaffected — but the next save fails until fixed.

**API surface in `otto/spec.py`:**
- `parse_spec_md(md_text, base=None) -> Spec | ParseError`
- `render_spec_md(spec) -> str` — produces the `.md` rendering
- `compile_spec(intent, project_kind, base=None) -> Spec` — LLM call

`compile_spec` and `parse_spec_md` both produce the same `Spec`
dataclass; runtime doesn't care which produced it.

**Versioning:** every user-save creates `spec-v<N>.md` and
`spec-v<N>.json` side by side. Latest version is symlinked as
`spec.{md,json}`.

**Final artifact:** at end of Run, Render emits
`proof/spec-final.md` — what was actually built, with verdict
annotations per Feature. This is the human-readable "what shipped"
view distinct from "what was planned."

---

## 2.6 Components and shared paths (load-bearing additions)

The four mental-walkthrough reviews (browser, twitter, slack, non-webapp)
converged on two gaps in the original Group model that surface immediately
beyond doc-editor scale. Both must be fixed *during* Phase A1 dataclass
work, not retrofitted later.

### Component — non-Feature dispatch unit

Real products have infrastructure that isn't a Feature: a WebSocket hub,
a search indexer, a notification fan-out, a job queue. These have:

- Code that needs to be built (so they need an agent)
- Files they own (so they need owned_paths)
- Verifiable behavior (so they need checks)
- **No user-facing verdict** (the user didn't ask for "the WebSocket
  layer," they asked for "live updates")

Trying to fit these into the Feature model produces dishonest verdicts
("WebSocket layer: passed" — but passed *what*?). The right shape:

```
Component (in spec.json):
  id: "websocket-hub"
  name: "WebSocket hub"
  description: "Pub/sub layer for live updates."
  owned_paths: [...]
  dependencies: []
  checks: [StateInvariant, ApiProbe, ...]
  consumed_by: [feature_id, feature_id, ...]   # Features that need this Component
```

Components are dispatched like Groups (own agent, branch, worktree). They
have checks but no audit verdict — the audit pass verifies the Features
that consume them, transitively proving the Component works.

In the Proof:
- Whole-product packet has a "Components" section (collapsed by default)
  showing build status + check evidence + cost — but no pass/fail "verdict"
- Per-Feature pages list the Components they depend on, link to their
  build evidence

### Shared paths — files no Group owns

`models.py`, `app.py`, `routes/__init__.py`, `requirements.txt` get
touched by every Group. The current "overlap → merge into one Group"
rule collapses everything into one mega-Group at multi-tenant scale.

Fix: the Spec declares `shared_paths[]` — files everyone may edit but no
one owns:

```
{
  "shared_paths": [
    "models.py",
    "app.py",
    "requirements.txt",
    "routes/__init__.py"
  ]
}
```

Rules:
- Any Group may **add** to a shared file.
- Any Group may **modify** existing entries in a shared file (subject
  to merge-queue serialization on conflicts).
- Compile auto-detects shared paths from the project structure plus
  user override via the spec-review screen.
- Merge queue serializes lands across Groups that touched any shared
  path (already covered by existing eligibility logic).

Together, Components and shared_paths replace the old "merge Groups
that share files" rule. Groups remain logical product verticals;
file conflicts are handled by ownership tiers (owned by one Group /
owned by a Component / shared / unowned).

---

## 2.7 Per-kind structure schemas

The walkthrough on non-webapp projects (api, library, cli) confirmed
that today's design implicitly assumes webapp. The fix is per-kind
`structure` schemas in the Spec, plus per-kind evidence-kind defaults.

### `structure` field per `project_kind`

```
project_kind=webapp:
  structure: { framework, database, ui_style, auth, runtime_entry_url }
  default evidence kinds: BrowserJourney, ApiProbe, StateInvariant, RepoTestCheck

project_kind=api:
  structure: { framework, database, auth, openapi_spec_path, runtime_entry_url }
  default evidence kinds: ApiProbe, StateInvariant, RepoTestCheck

project_kind=library:
  structure: { language, package_name, api_surface_doc_path, package_manager }
  default evidence kinds: ImportCheck, TypeCheck, RepoTestCheck

project_kind=cli:
  structure: { language, binary_name, subcommands, runtime_entry_argv }
  default evidence kinds: CLIProbe, RepoTestCheck
```

`runtime_entry_*` fields tell the audit agent how to reach the product
under test. For webapp/api: a URL. For cli: an argv list. For library:
no runtime entry — audit verifies via import + tests.

### New evidence kinds (additions to `otto/checks.py`)

- **`CLIProbe`** — invoke a subprocess with given argv; assert exit
  code, stdout substring, stderr substring, file-system side effects.
- **`ImportCheck`** — `python -c "import <package>"` returns exit 0;
  optionally check `<package>.__version__` matches expected.
- **`TypeCheck`** — `mypy` / `pyright` / equivalent passes on declared
  package paths.

### Walkthrough schema extension

`audit/attempt-NN/walkthrough.jsonl` line schema gains kind-aware fields:

```
{
  "t": "00:42.13",
  "feature_ids": ["..."],
  "action_kind": "browser_navigation | api_request | cli_invoke | import_check",
  "narrative": "...",

  // browser_navigation
  "screenshot": "assets/...",
  "dom_snapshot": "assets/...",
  "url": "...",
  "method": "GET",

  // api_request
  "method": "POST",
  "path": "/api/users",
  "request_body": "...",
  "response_status": 201,
  "response_body": "...",

  // cli_invoke
  "command": ["git-flow-helper", "start", "feature/foo"],
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",

  // import_check
  "package": "retryable",
  "version": "0.1.0",
  "import_succeeded": true
}
```

Audit prompt must be parameterized by `project_kind` — webapp variant
gets browser-walkthrough instructions; cli variant gets shell-invocation
instructions; library variant gets import+test instructions.

### Proof template branches

`feature-proof.html.j2` gets per-`action_kind` rendering:
- Browser actions render as screenshot grid (current default)
- API actions render as request/response trace tables
- CLI actions render as terminal-style transcripts (monospace, ansi
  colors)
- Import actions render as a status table

UI checkbox filter: spec-review screen filters evidence-kind checkboxes
by `project_kind`. `BrowserJourney` doesn't appear when
`project_kind=library`.

---

## 3. Atomic units

The redesign distinguishes atomic units by *what they're atomic for*:

| Unit | Atomic for | Cardinality |
|---|---|---|
| **Feature** | Value / verdict | One Feature → one verdict in Proof |
| **Group** | Dispatch | One Group → one branch + agent (typically) |
| **Stage** | Pipeline progress | Five Stages per Run, in fixed order |
| **Run** | Session | One Run → one session dir → one Proof packet |

**Tasks** (todo items inside the agent's loop) are below the user's
surface. Internal-only. Never appear in Spec, Proof, MC, or CLI.

### How Features and Groups relate

Default rule: **one Feature per Group**, *unless file-ownership forces
grouping*. Concretely, at Compile time:

1. For each Feature, the compile agent emits a candidate `owned_paths`.
2. If two Features have overlapping `owned_paths`, they're merged into
   one Group.
3. Otherwise each Feature gets its own Group.
4. The compile agent may also override this with logical-coherence
   reasoning ("Comments and Reply share enough context that the same
   agent should build both") — but logical merging is the exception,
   file-overlap merging is the rule.

A Group's *logical* identity (name, description) is what the user
sees in spec review. Its *dispatch plan* (one or more execution units,
file-ownership rules) is internal.

### Brownfield routing for new Features

When the user adds a Feature to an existing project, Compile decides:

- **Modifies an existing Feature** → re-dispatch the existing Group
  with focused intent. Same agent identity, same branch base, surgical
  edits.
- **New Feature, no file overlap** → new Group, fresh agent.
- **New Feature, overlaps existing files** → new Group with extended
  `owned_paths` covering the overlapped files. Merge queue serializes
  the land.

Existing Groups are *not* re-built when a related Feature is added.
Targeted Feature implementation is the rule.

---

## 4. Retry / loop layers (only two)

### Layer 1 — Check loop

- **Where:** inside Build, per-Group.
- **Trigger:** a Group's `check_evidence` fails (RepoTestCheck,
  ApiProbe, StateInvariant, focused BrowserJourney).
- **Detection:** deterministic. Each Check has a clear pass/fail
  contract. No LLM judgment.
- **Retry:** the same Group's agent gets the failure narrative, edits,
  re-runs failing Checks.
- **Cap:** configurable, default `retries.check_loop.max_attempts_per_group = 3`.
- **Cost:** cheap. Local. No browser walkthrough of full product.

### Layer 2 — Audit loop

- **Where:** between Audit and Render.
- **Trigger:** Audit's LLM judge flags a Feature as failing or partial.
- **Detection:** LLM-judged. Holistic walkthrough verdict.
- **Retry:** the failing Feature is routed back to its Group's agent
  for one repair attempt; Audit re-runs *only the affected Features*.
- **Cap:** configurable, default
  `retries.audit_loop.max_repair_attempts_per_run = 1`,
  `retries.audit_loop.max_audit_passes_per_run = 2`.
- **Cost:** expensive. Browser walkthrough, LLM judgment, audit re-pass.

### No Layer 3

After Layer 2, if a Feature still fails, it lands in the Proof as
`partial` or `blocked` honestly. The Run completes. The user reads the
Proof and decides: accept the partial product, run a follow-up with
better guidance, or abort.

Quality findings (e.g. "hero section lacks visual distinction, 3/5
quality") are **informational**, never blocking. They appear in the
Proof; they do not trigger another retry.

### Severity ladder for findings

Quality findings (informational by default) need a severity ladder so
"my product has 8s page load and stale counts" doesn't slip past as
"passed":

| Severity | Meaning | Effect on verdict |
|---|---|---|
| `critical` | Feature passes its checks but is functionally unusable (e.g. 8s load time, stale state, broken UX) | Flips Feature verdict to `partial`; routes to Audit loop |
| `important` | Functional but suboptimal (small UX bug, missing edge case) | Surfaces in Proof under the Feature; verdict stays as audited |
| `polish` | Cosmetic (color, spacing, copy) | Surfaces in a "Polish suggestions" Proof section, never blocks |

Audit prompt produces severity-tagged findings; render renders them at
the appropriate visibility tier. Critical findings re-trigger Layer 2
repair within budget.

### Audit honesty contract

Walkthrough reviews surfaced three patterns that produce dishonest
verdicts. Each is fixed by an explicit field on Feature verdicts:

| Field | Values | Meaning |
|---|---|---|
| `evidence_completeness` | `full`, `proxy_only`, `partial` | Whether evidence directly verifies the Feature (`full`) vs verifies via a proxy (e.g. DB row exists for "delivered notification") |
| `coverage_confidence` | `high`, `medium`, `low` | How conclusive the evidence is. `low` = "this passed but evidence is suggestive, not conclusive" |
| `multi_actor_required` | bool | True if Feature inherently needs multiple browser sessions (DM delivery, cross-user follows, presence indicators) |

`multi_actor_required: true` Features cannot be verified `full`
completeness in v1 (single audit agent, single browser session). They
get `proxy_only` completeness with explicit narrative: "Verified
sender side + DB persistence; live multi-session delivery not directly
tested in v1." Honest partial > false complete.

### Audit fixtures

Multi-user products (Slack, Twitter) need pre-seeded fixture state
before audit walkthrough. Without this, audit wastes its budget creating
test users, follows, channels.

Spec gains an optional `audit_fixtures[]` block:

```
{
  "audit_fixtures": [
    {"kind": "user", "username": "alice@test", "role": "admin"},
    {"kind": "user", "username": "bob@test", "role": "member"},
    {"kind": "channel", "name": "general", "members": ["alice@test", "bob@test"]},
    {"kind": "follow", "follower": "alice@test", "followed": "bob@test"}
  ]
}
```

A new stage between Build and Audit — **Seed** — applies fixtures to the
live product before walkthrough. Seed runs are deterministic, idempotent,
and themselves checked (failed seed = blocked Run, not silent
proceed-with-empty-state).

### Audit modes (configurable)

Default: **whole-product audit at end of Run** with Feature anchors,
plus per-Feature deterministic Checks pre-merge inside Build.

Optional flags:

- `audit.walkthrough_per_feature: true` — audit produces per-Feature
  walkthrough segments (more expensive; needed if running many
  re-audits later).
- `audit.pre_merge_audit_groups: ["auth", "payments"]` — Groups in
  this list get a focused audit pre-merge. The merge queue blocks
  until that Group's Features pass a focused audit.

---

## 5. Configurable budgets

All retry counts, timeouts, cost caps, and audit modes live in
`otto.yaml` per project, with sane defaults from `otto/defaults.py`.

```yaml
# otto.yaml — project defaults
retries:
  check_loop:
    max_attempts_per_group: 3
    timeout_per_attempt_s: 1800
  audit_loop:
    max_repair_attempts_per_run: 1
    max_audit_passes_per_run: 2

budgets:
  total_repair_wall_s: 7200
  total_cost_usd: null   # null = no cap

audit:
  walkthrough_per_feature: false
  pre_merge_audit_groups: []

agents:
  default_provider: claude
  default_model: claude-sonnet-4-6
  per_group: {}   # advanced: override provider/model per Group id
```

CLI flags override per-run:

```
otto run "<intent>" \
  --max-check-attempts 5 \
  --max-audit-repairs 2 \
  --total-cost-usd 30 \
  --audit-walkthrough-per-feature
```

Spec-level overrides allowed for advanced users:

```json
{
  "groups": [
    {
      "id": "auth",
      "audit_pre_merge": true,
      "max_check_attempts": 5,
      ...
    }
  ]
}
```

**Rule for code:** no magic numbers anywhere except `otto/defaults.py`.
Every count/timeout/budget reads from config-with-defaults.

---

## 6. Pipeline stages

```
Compile → [Spec review gate] → Build → Audit → Render → Land
```

### Compile

LLM produces Spec from Intent. Or normalizes a user-supplied Spec.
Outputs `spec/spec.json`. May read existing project state in brownfield
mode (planned, not yet specified — see §9.4).

### Spec review gate

Optional, default-on for greenfield, default-off when user already
supplied concrete Features.

User can:
- Add / edit / remove Features
- Split / merge / rename Groups
- Add / remove Guardrails
- Approve, regenerate (recompile), or abort

Gate produces `spec/spec.json` (versioned: `spec-v1.json`, `spec-v2.json`
on regeneration).

### Build

For each Group, dispatch a long-lived agent in its own worktree on its
own branch. Agent's job: implement Features in the Group, run their
Checks, retry on failure (Check loop), produce a clean diff.

Per-Group output:
- `groups/<group-id>/branch` — git branch
- `groups/<group-id>/narrative.log` — agent trace
- `groups/<group-id>/check-evidence.jsonl` — per-Check results, tagged
  with `feature_id`
- `groups/<group-id>/cost.json` — tokens, dollars, wall

### Audit

One LLM pass on the integrated product (after all Groups have landed
or the merge queue declares a stable integrated state). The audit
agent walks the product, evidences each Feature against the spec,
emits a verdict per Feature.

Critical: **every walkthrough action is tagged with the Feature(s) it
evidences.** This is what enables per-Feature Proof.

Audit output:
- `audit/attempt-NN/walkthrough.jsonl` — line-per-action with
  `feature_ids[]`, `screenshot?`, `dom_snapshot?`, `narrative`
- `audit/attempt-NN/feature-verdicts.json` — per-Feature verdicts +
  evidence-ref lists
- `audit/attempt-NN/quality-findings.json` — informational findings
  with severity

### Render

Deterministic publish stage. Reads Spec + audit output + group logs +
state. Writes Proof packet:
- `proof/proof-packet.html` — whole-product front door
- `proof/proof-packet.json` — machine-readable
- `proof/features/<feature-id>/proof.html` — per-Feature mini-page
- `proof/features/<feature-id>/proof.json` — per-Feature machine
- `proof/assets/` — copied screenshots, DOM snapshots, audit clips

Pure function. No LLM. Re-runnable: `otto render <session-id>` to
update presentation without re-auditing.

### Land

Merge each Group's branch into target via eligibility-gated FIFO. Per
Group: refresh target, rebase, rerun checks, atomic land, post-land
recheck. On conflict or check fail: same Group's agent repairs in own
worktree.

Final state events emitted to `state.jsonl`:
- `run.finished` with `verdict ∈ {passed, partial, blocked}`

---

## 7. Proof granularity

### Whole-product Proof packet

`proof/proof-packet.html` — what the user lands on. Contents:

- Intent (verbatim)
- Verdict header: passed / partial / blocked, X/Y Features, quality score
- Feature list (primary surface):
  - Each Feature: verdict, one-line detail, expandable evidence
- Groups (secondary, expander below feature list):
  - Each Group: name, contained Feature ids, files changed, narrative
    log link, cost, wall, repair history
- Guardrails (verified — audit checked nothing violated them)
- Stage timeline: Compile → Build → Audit → Render → Land with
  durations and costs
- Spec amendments (if non-empty)
- Run metadata (collapsed)

### Per-Feature mini-packet

`proof/features/<feature-id>/proof.html`. Contents:

- Feature name + intent context (which Spec line produced it)
- Verdict + detail
- Evidence:
  - Browser walkthrough segment (timestamped, video-grid-style with
    screenshots and saved DOM)
  - Deterministic checks: per-check pass/fail, output excerpts
- Built in Group: name, files, diff link, repair history
- Audit narrative excerpt
- Spec context: was this added by Compile or by user during review?

### Per-Group narrative

Under `groups/<group-id>/`. Not Proof-level (these are *how* it was
built, not *what* was proven), but linked from Proof.

### Linking and embedding

- Whole-product packet embeds per-Feature blocks via anchors:
  `#feature-<id>` scrolls to that Feature's section in the same page.
- Per-Feature URL `/api/sessions/<id>/features/<feature-id>` returns
  the standalone mini-page; user can share that URL.
- The whole-product packet contains a link to each per-Feature page
  for "view this Feature in isolation."

---

## 8. Hybrid plan ownership

Same pipeline, two entry modes:

### Otto plans (default greenfield)

```
otto run "build me a doc editor with markdown and inline comments"
```

Compile derives full Spec from Intent. Spec review gate defaults to
on; user reads Otto's plan, edits, approves. Build runs.

### User plans (default brownfield)

User supplies concrete Features in a structured intent file:

```
# intent.md (user-supplied)

# Doc editor — image upload

## Features
- Drag-drop image upload, max 5MB, embed inline
- Server-side resize for images > 1MB

## Guardrails
- No video upload
```

Compile normalizes (no derivation, just structure validation). Spec
review gate defaults to off (user said what they wanted). Build runs.

### Mixed

Most common in practice. User says "add image upload" → Otto compiles
a Spec (one Group, one or two Features) → user reviews briefly →
Build runs.

---

## 9. Constraints

### 9.1 Source-of-truth discipline

- Anything MC shows for a Run that isn't in `proof-packet.json` is
  either (a) a derived metric, (b) a real-time progress field for
  in-flight Runs, or (c) a bug.
- If MC needs a field that doesn't exist in Proof, the renderer adds
  it; MC doesn't compute it independently.

### 9.2 Real evidence only

- Every metric MC shows must trace to a real file under
  `otto_logs/sessions/<id>/`.
- If a field doesn't exist for a session (partial / blocked), MC shows
  "—" not zeroes.
- No mock data, no placeholders.

### 9.3 Phase B/C compatibility

- Legacy `otto build`/`certify`/`improve` runs still readable in
  history during Phase A.
- Phase B: legacy commands route through the new stack. Phase C:
  legacy code, prompts, MC widgets, dataclasses deleted.
- New code paths must stand on their own without any legacy mount.

### 9.4 Brownfield compile mode (deferred spec)

When Otto runs against a project with existing code:
- Compile must read the working tree (not just Intent).
- Compile must diff against any existing `spec/spec.json` and only
  emit new/changed Features.
- Compile must respect file-level "leave it alone" markers (mechanism
  TBD — comment-based? `.otto/preserve` file?).
- Existing Groups carry forward; new Groups append.

This is acknowledged-needed but not yet specified. Tracked in §11
open items.

### 9.5b Out-of-scope products

Otto is not fit for systems-level products. Explicit out-of-scope
clause:

- Operating systems, kernels, hypervisors
- Web browsers, JavaScript runtimes, language compilers
- Database engines (storage layers, query planners)
- Embedded firmware, drivers
- Anything where "passes" requires verifying memory safety, sandboxing,
  spec compliance against external standards (HTML/CSS/JS specs,
  POSIX, etc.), or correctness under adversarial inputs

Compile detects out-of-scope intents heuristically (intent text contains
"browser," "kernel," "compiler," "OS," etc.) and emits a warning before
spending LLM cost. User can override; if they do, the proof packet
prominently notes "this is outside Otto's verified scope; treat verdict
as suggestive."

The browser walkthrough (`docs/review-walkthrough-browser.md`) is the
load-bearing analysis here. Audit instruments (BrowserJourney, ApiProbe,
StateInvariant, RepoTestCheck) cannot meaningfully verify a browser; the
proof would be dishonest by construction. Same logic for kernels, JS
engines, etc.

### 9.5c Adversarial criteria generation

Default Compile produces happy-path acceptance criteria. Real products
need adversarial coverage: double-like, like-deleted-tweet, race
conditions, malformed inputs. **Deferred to v2** — not blocking on
A0-A6.

When implemented: a separate Compile pass after the happy-path Compile
generates adversarial criteria as additional Features (or as additional
acceptance steps within existing Features, depending on cost/UX).
Audit verifies them like any other Feature. For v1, users can manually
add adversarial Features at the spec-review gate.

### 9.5d Cost cap default

`$5` was a doc-editor-scale default. Realistic per-Run budgets:

- Doc editor / shortener / kanban: $1-5
- Twitter clone / Slack clone: $20-50
- Brownfield iteration (one or two Features): $0.50-2

`otto.yaml` `budgets.total_cost_usd_default` is project-default. The
New Run dialog (wireframe screen 8) shows an estimate based on Spec
size before user clicks Start. Estimate formula derives from per-stage
cost averages tracked in `bench-results/`.

### 9.5e No hardcoded numbers (renumbered from §9.5)

All retry counts, timeouts, budgets, max sizes, max steps come from
`otto.yaml` with defaults from `otto/defaults.py`. No literal magic
numbers anywhere else.

---

## 10. CLI surface (target)

```
otto run [intent | --intent-file path] [options]
otto run --resume <session-id>
otto run --rerun-audit <session-id> [--feature <id> ...]
otto run --recompile <session-id>
otto render <session-id>
otto history
otto sessions <session-id>
otto setup
otto web [--project-launcher] [--projects-root <dir>]
otto replay <session-id>
```

Deprecated and deleted in Phase C: `otto build`, `otto certify`,
`otto improve`.

`otto run` flags (selection):
- `--intent-file path`
- `--project-kind {webapp,cli,library,api}`
- `--no-spec-review` (skip review gate)
- `--from-spec path` (skip compile, drive from existing spec)
- `--no-build` (compile only)
- `--max-check-attempts N`
- `--max-audit-repairs N`
- `--total-cost-usd N`
- `--audit-walkthrough-per-feature`
- `--audit-pre-merge-groups id1,id2`
- `--provider {claude,codex,...}`

---

## 11. Open items deferred

These came up but are not fully resolved. Tracked here so future
sessions don't re-derive them.

1. **Per-Feature audit cost vs whole-product audit cost.** Empirical
   measurement needed before committing to "per-Feature is expensive."
2. **Multi-Feature evidence cross-linking.** Walkthrough segments that
   evidence multiple Features — sketched as "list under each, mark
   'shared with X'" but not implemented.
3. **Per-Group provider/model surfacing.** Cheap to add to Proof; open
   question whether to surface by default.
4. **Brownfield compile mode** (§9.4). What does Compile read, how
   does it diff against existing Spec, what's the marker for "preserve
   this file."
5. **Spec-edit propagation during in-flight runs.** If user edits Spec
   mid-Run (rare), do dependent Groups get re-dispatched, or does the
   edit only apply to next Run?
6. **Concurrent Runs in the same project.** Forbidden today (single
   project lock). Should we support N parallel `otto run`s? If yes,
   how does the merge queue cope?

---

## 12. Verification plan

For the redesign to be considered shipped:

1. **Vocabulary refactor verified.** Single grep across the repo:
   `slice`, `capability`, `capability_verdict`, `certifier`, `story`,
   `stories_passed/tested` return zero hits in `otto/`, `tests/`,
   `docs/`. Existing files in `otto_logs/` and `bench-results/` are
   read-only history; not touched.

2. **`otto/defaults.py` exists and is the only place magic numbers
   live.** Grep for hardcoded retry counts in `otto/` returns matches
   only in `defaults.py` and `tests/`.

3. **Per-Feature Proof renders.** Run a fixture intent, assert
   `proof/features/<id>/proof.html` exists for every Feature, contains
   verdict + at least one evidence ref. Whole-product packet contains
   anchor links to all per-Feature pages.

4. **Audit walkthrough is Feature-tagged.** Each line of
   `audit/attempt-NN/walkthrough.jsonl` has `feature_ids[]` populated.

5. **Check loop and Audit loop tested independently.** Unit tests
   inject failing Checks (Layer 1) and failing Audit verdicts (Layer 2)
   and assert correct retry behavior with configurable caps.

6. **MC renders the new shape.** Click any Run row, drawer shows
   Feature list as primary, Groups as expander, stage timeline,
   per-Feature drilldown link. No legacy WARN noise. No Skipped
   Build/Certify phases.

7. **Microfeed parity bench.** End-to-end run produces a Spec with
   recognizable Features ("post creation," "RSS feed," etc.), all
   Features pass audit, Proof packet reads cleanly to a human, cost +
   wall within 1.5× / 1.2× of mono baseline (per the original plan
   step 11).

8. **Real-User-Audit (RUA).** Drive MC end-to-end in
   chrome-devtools against three different intents (greenfield webapp,
   greenfield CLI, brownfield "add a feature"), screenshot every
   panel state, check for misleading copy / dead links / empty values
   that should have data. Any RUA failure blocks merge.

9. **Codex implementation gate.** Before merging, dispatch
   `/codex-gate` with the implementation diff + this research.md.
   Codex finds bugs → Codex fixes them.

---

## 13. What this redesign deletes (Phase C, gated on bench)

- `otto/campaign.py` (codex-i2p)
- `otto/oracles.py` (codex-i2p) — logic ported to `otto/checks.py`
- `otto/product_contract.py` (both branches)
- `otto/queue/` — replaced by `otto run` orchestration
- `otto/checkpoint.py` legacy parts — replaced by `otto/state.py`
- `otto/cli.py` `build` / `certify` / `improve` subcommands
- `otto/mission_control/service.py:_review_packet` and friends —
  replaced by `otto/mission_control/run_view.py`
- `otto/web/client/src/components/inspector/RunInspector.tsx` (the
  2700-line beast) — replaced by `otto/web/client/src/components/run/`
- All "story" / "AC" / "stories_passed" code paths
- HistoryRow `domain` field once `otto run` covers all run types

---

## 14. What this redesign keeps (Phase A coexistence)

During Phase A:
- New code lives in: `otto/spec.py`, `otto/checks.py`, `otto/state.py`,
  `otto/build.py`, `otto/merge.py`, `otto/audit.py`, `otto/render.py`,
  `otto/defaults.py`
- New MC: `otto/mission_control/run_view.py`,
  `otto/web/client/src/components/run/`
- Legacy modules untouched until Phase B routes through the new stack
  and Phase C deletes them.

This means Phase A is purely additive in code. The detail-panel mess
that triggered this redesign (mc-i2p drawer showing legacy WARN noise
on i2p runs) is fixed not by patching the legacy panel but by routing
i2p runs to the new `run_view.py` + `<RunDrawer />` from day one.
Legacy runs keep using the legacy panel until Phase B.

---

# Modular Decomposition Field-Test Failure Research

Date: 2026-05-15T02:05:47Z

## Scope

Fix the v5 modular/decomposition path after Round 2 field tests:

- `04-mini-crm`: root emitted three children; all children reported pass; final verdict was `merge_blocked`.
- `05-blog-generator`: root emitted three children; all children reported pass; final verdict was `merge_blocked`.

Constraints from the request:

- Trust the agent. Minimize classification. Default repairable clean-deploy failures to a coding agent.
- No new validators or prompt-rule expansion.
- No provider routing changes.
- Do not touch the i2p monolithic path.
- Time-based validation matters more than unit tests; live rerun 04 and 05.

## Evidence Read

Artifacts inspected:

- `/Users/yuxuan/otto-projects/field-tests/20260515-012919/04-mini-crm/otto_logs/cross-sessions/task_graph.json`
- `/Users/yuxuan/otto-projects/field-tests/20260515-012919/04-mini-crm/field-test-otto.log`
- `/Users/yuxuan/otto-projects/field-tests/20260515-012919/04-mini-crm/otto_logs/sessions/*/lead/narrative.log`
- `/Users/yuxuan/otto-projects/field-tests/20260515-012919/05-blog-generator/otto_logs/cross-sessions/task_graph.json`
- `/Users/yuxuan/otto-projects/field-tests/20260515-012919/05-blog-generator/field-test-otto.log`
- `/Users/yuxuan/otto-projects/field-tests/20260515-012919/05-blog-generator/otto_logs/sessions/*/lead/narrative.log`

04 facts:

- Task graph: `root` emitted `v5-cb6494d893d7`, `v5-6825f5f82ade`, `v5-6cda4e78f2a9`.
- All three children have `verdict: pass`.
- Final field-test log reports:
  - `clean_deploy_port_busy [block]`: declared port `[19301]` already bound.
  - `clean_deploy_start_failed [block]`: `start.sh exited 127` with `python: command not found`.
- Git ancestry check showed all three `i2p/build/*` branch tips are ancestors of `main`.
- The frontend branch `i2p/build/v5-6825f5f82ade` has no unique frontend commit; its tip is a merge commit of the architect branch. The narrative says the FE agent found the frontend already complete and made no code changes.

05 facts:

- Task graph: `root` emitted `v5-361449e77ed0`, `v5-380ad5811f2c`, `v5-5805bd7c96b7`.
- All three children have `verdict: pass`.
- Final field-test log reports `clean_deploy_start_failed [block]`: `OSError: [Errno 48] Address already in use` from `http.server`.
- Git ancestry check showed all three `i2p/build/*` branch tips are ancestors of `main`.

## Relevant Code Paths

- `otto/v5_runner.py:_run_integration_smoke_preflight` runs `smoke_clean_deploy()` and serializes blocking `PreflightIssue`s.
- Root integration and subtree integration pass preflight payloads to the integration Lead, then run `smoke_clean_deploy()` again afterward.
- The clean-deploy smoke path is not wrapped in `PreflightRepairController`; therefore `clean_deploy_start_failed` and `clean_deploy_port_busy` never get the existing default agent/auto-fix loop.
- `PreflightRepairController.classify_preflight_issue()` already defaults unknown blocking failures to `agent`; it auto-fixes `port_busy`.
- `_run_preflight_repair_agent()` currently dispatches a focused Lead but does not runner-commit repair edits. A start.sh repair can pass in the dirty worktree but still fail to propagate through branch-based integration.
- `cleanup_stale_declared_ports()` currently runs once near pipeline start, before an architect-created `CHARTER.md` normally exists, so it often has no declared ports. It also kills all listeners on declared ports, which is too broad for user safety.
- `_merge_child_branch()` merges child build branches into the parent integration branch but does not explicitly verify the branch tip is an ancestor afterward. The observed 04/05 runs passed ancestry, but the existing test did not assert all children in a real task graph reach main after `_process_children`.

## Root-Cause Hypotheses

### H1: Clean-deploy smoke failures bypass the repair loop (root)

Supports:

- Field logs show blocking clean-deploy issues, not merge conflicts.
- `_run_integration_smoke_preflight()` only records issues.
- Root integration only dispatches the normal integration Lead, then downgrades to `merge_blocked` if post-smoke still blocks.
- `PreflightRepairController` is only used for checkout repair in this path.

Test:

- Simulate `smoke_clean_deploy()` returning `clean_deploy_start_failed`, then a focused repair edits `start.sh`; assert integration proceeds without invoking the broad integration Lead first and commits the repair.

### H2: Zombie port cleanup happens at the wrong layer and too early

Supports:

- Pipeline-start cleanup runs before the architect writes `CHARTER.md`, so there are no declared ports to clean.
- 04 field log still hit port 19301 busy after children passed.
- 05 hit address-in-use during the clean-deploy start.

Conflicts:

- Some port conflicts should be product bugs, not environment bugs, if `start.sh` fails to respect `$PORT`.

Test:

- Run clean-deploy repair loop with a `clean_deploy_port_busy` issue and assert it invokes the port cleanup auto-fix before rerunning smoke.
- Harden the cleanup helper to kill only Otto-owned project processes, not arbitrary listeners.

### H3: Branch propagation can be reported green without explicit ancestry invariant

Supports:

- The v6e bug class existed before: decomposed subtree integration work could stay on `i2p/integ/<id>` and never reach main.
- Existing tests exercise `merge_branch_into()` directly, but do not assert every child branch listed in the task graph is an ancestor of the parent/root integration after `_process_children`.
- Field-test interpretation was confused because a no-op child branch may not have a unique "v5 task" commit even when its branch tip is actually reachable from main.

Conflicts:

- Round 2 04 and 05 archived repos do have all child build branch tips as ancestors of main.

Test:

- Add a real `_process_children()` regression where multiple children pass, one child makes no unique code changes, and assert every pass child branch tip is an ancestor of `main`.
- Add a runner-side verification/logging helper so future failures surface as branch ancestry failures, not silent N-1 propagation.

## Plan Gate

Owned files:

- `otto/v5_runner.py`
- `otto/v5_preflight_repair.py`
- `otto/v5_clean_verify.py`
- Focused v5 tests under `tests/`
- This research/debug/plan/review trail

Risky assumptions and verification:

- Assumption: wrapping smoke preflight with `PreflightRepairController` is enough for both `start.sh` portability and port-busy failures.
  Verify: focused async tests plus live 04/05 reruns.
- Assumption: committing successful preflight repair edits via the integration commit allowlist will preserve fixes without tracking runtime garbage.
  Verify: test repaired `start.sh` is committed and `git status` clean.
- Assumption: branch propagation was not the direct Round 2 root cause, but missing invariant tests allowed confusion.
  Verify: ancestry checks for every child in tests and live rerun repos.
- Assumption: safe port cleanup belongs in the v5 clean-deploy repair path, not the field-test driver.
  Verify: cleanup filters to project/Otto-owned processes and clean-deploy reruns after cleanup.

System-level checks:

- Focused pytest for v5 integration preflight repair, branch propagation, and port cleanup.
- Ruff on touched files.
- `git diff --check`.
- Live `scripts/run_field_tests.py --scenario 04-mini-crm --parallel 1`.
- Live `scripts/run_field_tests.py --scenario 05-blog-generator --parallel 1`.
- After live runs: `git merge-base --is-ancestor` for all child branches versus `main`, `field-test-result.json` verdict and boot smoke HTTP status.

---

# Research: P1 Agentic-Native Router Defaults

Date: 2026-05-15
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`
Branch: `cc-i2p-2`

## Existing State

- `otto/v5_preflight.py` already maps scaffold `install_failed`,
  `build_failed`, `py_compile_failed`, `copy_failed`, and timeout kinds to
  blocking preflight issues. `no_npm` and `no_python` still map to warn/skip
  even though `verify_from_clean` emits them only when matching manifests
  exist.
- `otto/v5_preflight_repair.py` already treats `port_busy` no-op cleanup as
  an agent fallback, but filename and chmod deterministic shortcuts still log
  `repaired` even when they changed nothing.
- `otto/v5_clean_verify.cleanup_stale_declared_ports()` returns only a list of
  killed ports and does not report whether ports were actually freed.
- `otto/v5_runner._run_integration()` still sets `integration_cwd =
  integration_worktree or project_dir`, which can dispatch the integration
  Lead in the wrong tree after setup failure.
- `otto/v5_branching.merge_branch_into()` aborts unresolved source conflicts
  after deterministic/noise/structured merge attempts. It does not preserve a
  conflict packet for an agent repair.
- `otto/audit_loop.py` applies repair caps before every failing group gets a
  first repair attempt, can stop before a fix when audit pass cap is already
  reached, and silently excludes failing verdicts without a spec group.
- `otto/v5_context_slicer.py` falls back to full context for ambiguous scope.
  It writes `context_slice.json`, but there is no resolver hook and no
  explicit last-resort marker.
- `otto/build.py` treats out-of-scope writes as non-blocking
  `scope.warning` events. The amendment side-channel already runs before the
  scope check, so accepted amendments can legitimately clear the violation.

## Constraints

- Keep P2 over-classification untouched.
- Preserve deterministic shortcuts only when the shortcut actually changed
  state and the rerun oracle passes.
- For runtime/toolchain failures, blocking issue plus existing
  `PreflightRepairController` is the repair route.
- For merge conflicts, honor merge-conflict safety: provide both sides and do
  not use whole-file `--ours`/`--theirs` as the agent path.
- Tests must prove old behavior fails by leaving tests in place while
  reversing production-only changes, then reapplying the patch.

## Open Questions Resolved

- Scaffold failure item 1 is already enforced by P0 in this checkout:
  install/build/compile failures are `block` and include the clean verifier
  failure message.
- For `no_npm` / `no_python`, manifest presence is enough to establish the
  runtime is needed because `verify_from_clean` only emits those failures when
  `package.json` or `pyproject.toml` was found.
- Context-scope agent resolution cannot call an LLM from the pure slicer
  module without changing the runner contract. The smallest safe change is an
  explicit resolver hook plus an auditable `scope_resolution` record; runner
  fallback remains last-resort and logged when no resolver is supplied.

---

# Research: P2 Agentic-Native Over-Classification Hardening

Date: 2026-05-15
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`
Branch: `cc-i2p-2`

## Existing State

- `otto/repair_gates.py` uses term lists and methodology/surface heuristics to
  turn non-passing audit verdicts into proof gaps or browser-repro requests.
  That can suppress the agent + oracle repair lane after a detector already
  found a failing feature.
- `otto/browser_testing.py` classifies browser command families with substring
  checks against argv parts. `otto/checks.py` has a second Playwright detector
  with more substring logic for package scripts.
- `otto/spec_compile_flat.py` already reads typed `structured_output` result
  fields, but it first accepts assistant tool calls whose names merely look like
  structured-output aliases such as `submit_spec`.
- `otto/v5_verification_plan.py` skips the structured spec / CHARTER IA matrix
  with `required=False` whenever either side is missing, even for webapp specs.
- `otto/mcp_tools.py` writes scaffold build failures as `verdict:
  unverified`, which is a weaker signal than the canonical `partial` repair
  lane used elsewhere.

## Constraints

- Keep deterministic non-repairable classifications narrow and typed. Do not
  replace fuzzy blocklists with different fuzzy blocklists.
- Unknown or weakly classified audit failures should route to agent repair; the
  smoke/verify oracle is the gate.
- Browser command identity should live in one adapter. Unknown BrowserJourney
  commands must still run as real checks.
- Provider structured-output recovery should prefer typed result fields and the
  exact Claude `StructuredOutput` contract; tool-name aliases are not evidence.
- Missing structured IA should fail only for typed product kinds that require
  IA, currently v5 `project_kind: webapp`.

## Verification Plan

- Add `tests/test_v5_p2_hardening.py` with one regression per requested item.
- Run that file before production changes and capture the expected failures.
- Apply the scoped production patch and rerun the P2 tests.
- Run the requested P0/P1/leaf/smoke regressions, smoke tier, ruff, and
  basedpyright on touched files.

---

# Research: Pass 4 Brittleness Containment

Date: 2026-05-15
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`
Branch: `cc-i2p-2`

## Existing State

- `otto/queue/subtask.py` uses one `_TERMINAL_VERDICTS` set for two different
  jobs: anti-thrash non-runnability and dependency satisfaction. That keeps
  catastrophic children from redispatching, but it also lets downstream
  siblings run after a non-pass dependency.
- `otto/v5_runner.py:_build_decomp_runtime_context()` repeats the same
  "terminal verdicts are done" rule in the runtime prompt context.
- `otto/audit.py` has two false-green walkthrough edges: `no_op_walkthrough()`
  reports success with no artifacts, and synthesized webapp walkthroughs exit
  0 for "not-applicable" when a webapp has no runnable/static shape.
- `otto/checks.py:_malformed_check_evidence()` intentionally returns
  `passed=True` under the v2.1 design. It already marks
  `raw["malformed_check"]`, but `_compact_evidence()`, the evidence packet,
  and the prompt do not elevate that signal as "not proof".
- `otto/v5_branching.py:setup_child_worktree()` still returns `None` on setup
  failure and documents the caller fallback to `project_dir`.
- `otto/v5_runner._run_child()` still has a context-slicing fallback through
  `(child_worktree or project_dir)` and can proceed after child worktree setup
  failure.

## Constraints

- Preserve the anti-thrash mechanism: catastrophic/merge_blocked/unverified/raw
  partial tasks must not redispatch endlessly.
- Dependency satisfaction must match the P0 merge gate: only `pass` and
  `partial` with `review_state == "reviewed_partial"` satisfy dependents.
- Keep malformed per-check payloads non-slice-blocking per
  `docs/intent-to-product-v2-plan.md`; make them typed, loud, and unusable as
  proof.
- Production audit safety depends on the audit gate no longer treating missing
  or synthesized-not-applicable walkthroughs as success.
- The guardrail should be precise enough to fail on new brittle shapes without
  becoming a broad regex tax.

## Open Questions Resolved

- `checks.py` malformed evidence remains `passed=True` because v2.1 explicitly
  delegates product truth to the audit contract gate. The fix is to make
  `evidence_quality="malformed"` and `proof_usable=False` survive into the
  audit packet and prompt.
- Webapp synthesized "not-applicable" is not a successful walkthrough. It is a
  product/audit evidence gap and should cap a passing audit at least to
  partial.
- Child worktree setup failure is not a valid reason to run a child in the
  project root. The child should become `merge_blocked` before dispatch.

---

# Research: Round 6 Nested Integration Worktree Binding

Date: 2026-05-15
Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`
Branch: `cc-i2p-2`

## Live Evidence

- Field test:
  `/Users/yuxuan/otto-projects/field-tests/20260515-081853/08-data-platform/`.
- `field-test-otto.log` shows every grandchild merge into subtree
  `v5-212ea51688a9` failed with:
  `fatal: 'i2p/integ/v5-212ea51688a9' is already used by worktree at .../.worktrees/integ-v5-212ea51688a9`.
- `git worktree list --porcelain` confirms that
  `i2p/integ/v5-212ea51688a9` was legitimately checked out by the dedicated
  integration worktree while the root project worktree was on `main`.
- The 06 SaaS comparison avoided this exact failure because the nested
  integration branch was being mutated through whatever branch the root
  project worktree currently held. That ordering is non-general: if the
  integration branch is already bound to a linked worktree, a second checkout
  from the project root fails by Git design.

## Source Findings

- `otto/v5_branching.py:575` `merge_branch_into()` always runs
  `git checkout <target_branch>` in the caller's `project_dir`.
- `otto/v5_branching.py:749` `merge_child_into_integration()` is only a thin
  wrapper over that checkout-based primitive, so child-build to
  parent-integration merges inherit the same branch-binding bug.
- `otto/v5_runner.py:2623` `_setup_integration_worktree_once()` can create or
  reuse a dedicated integration worktree for a task's own integration branch.
  Once it does, later merges into that branch must operate in that owning
  worktree rather than trying to bind the same branch elsewhere.
- `otto/v5_runner.py:2895` restores `project_dir` after a nested integration,
  which means subsequent child merges cannot rely on the root worktree still
  being on the subtree integration branch.

## Related Cases

- `v5-e4696c23651d` is not the same Git checkout failure. Its logs show
  grandchild merges completed and `i2p/integ/v5-e4696c23651d` reached `main`;
  it remained `partial` because runner checks failed despite product tests
  passing.
- `v5-bc66f4349b3c` was emitted but never resolved because it depended on both
  subtrees. The blocked dependencies prevented dispatch; no separate orphaning
  mechanism was found.

## Constraints

- Do not special-case field-test 08 or task ids.
- Preserve fail-closed merge behavior: real conflicts still produce conflict
  packets and block/repair; no whole-file ours/theirs shortcuts.
- Merges into an integration branch should use the existing owning worktree
  when Git reports the target branch is bound there.
- Guard the merge primitive so concurrent nested child completions cannot
  mutate the same integration branch simultaneously.
