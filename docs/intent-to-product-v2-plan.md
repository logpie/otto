# Otto v2 — Implementation Plan

Date: 2026-05-03
Branch: `cc-i2p-2` (v1) → series of PRs cut from `main` after v1 merges
Source: `docs/intent-to-product-v2.md` (findings + design)
Provider for all real-cost runs: `--provider claude` (Sonnet)

## Framing

v1 is the working frame. v2 is **seven independent PRs**, each
implementing one principle from the v2 design doc. Each PR is
**bench-validated** — Microfeed bench must reach v1 parity (5/5) or
better before merging.

The order is deliberate: each step is **independently shippable** and
each unlocks the next. The first three are highest-leverage and lowest-
risk; steps 4–7 are larger and gated on bench evidence from earlier
steps.

```
v1 (current PR)
  │
  ├─ v2.1  Permissive parser + F2 generalization        [smallest]
  ├─ v2.2  Tiered mutable spec + amendment API
  ├─ v2.3  Auto-collapse for tiny products
  ├─ v2.4  Pluggable build agents
  ├─ v2.5  Parallel build execution
  ├─ v2.6  Per-capability audit verdicts
  └─ v2.7  Session continuity                           [largest]
```

Each step is its own branch off `main`, its own PR, its own bench
run. No long-lived feature branches.

---

## v2.1 — Permissive parser

**Why first**: highest leverage, lowest risk. Closes the entire class
of cheap-fail rounds (R3, R5, R8, R17, R22, R26 shape). Pure parser
rewrite; no behavior changes downstream.

### Scope

Replace strict schema validation in `otto/spec_compile.py:389
validate_spec` with a permissive parser that:

- **Coerces**: missing `intent` → `""`; missing `project_kind` → `"webapp"`;
  missing `slices` → `[]`; non-array `slices` → `[slices]`; non-dict
  `amendments` entries → `{"reason": str(entry), "actor": "compile-agent",
  "ts": now()}`; non-string `slice.id` → slugified str(); etc.
- **Warns** for departures from recommended shape (logged to journal,
  surfaced in proof packet, NEVER blocks).
- **Hard rejects** only on truly unusable input: no slices AND no intent,
  or schema_version mismatch where the format genuinely cannot be parsed.
- **Generalizes F2 to all check kinds**: a `state_invariant.expression`
  that isn't Python, an `api_probe` with malformed URL, a `pytest_check`
  with a missing test name — none should slice-block. The check-run
  layer reports informational PASS or FAIL with a clear diagnostic; the
  audit's contract gate remains the source of truth.

### Files

- **Modify**: `otto/spec_compile.py`
  - Replace `_validate_against_schema` with `_coerce_to_spec` that returns
    `(Spec, list[ValidationWarning])`.
  - `validate_spec` becomes thin: parse → list warnings (never errors).
  - Drop the strict per-kind JSON schemas; keep them as `recommended_shape`
    documentation only.
- **Modify**: `otto/checks.py`
  - Already started (F2 fix landed). Extend the same pattern to all
    `CheckKind` runners: any malformed payload → informational PASS with
    warning, never slice-blocking SyntaxError/ValidationError.
- **New**: `otto/spec_warnings.py` — `ValidationWarning` dataclass +
  warning collector + journal sink.
- **Delete**: per-kind JSON schemas under `otto/schemas/*.json` (if they
  exist as separate files); inline them as advisory dicts in
  `otto/spec_compile.py`.

### Tests

`tests/test_spec_permissive.py` (new):
- Coercion: `slices` as a single dict → wrapped in list.
- Coercion: amendment as a string → wrapped in dict with synthesized fields.
- Coercion: `state_invariant.expression` as prose → check runs as
  informational; spec parses fine.
- Coercion: missing `project_kind` → defaults to webapp.
- Hard reject: empty body → ValidationError.
- Warning surfaces: malformed amendment generates exactly one warning;
  spec is still usable.

### Verification

1. Unit: `uv run pytest -q tests/test_spec_permissive.py` — 100% pass.
2. Existing: `uv run pytest -q` — 161 v1 tests still pass (parser is
   strictly more permissive; nothing should break).
3. Bench: `OTTO_ALLOW_REAL_COST=1 uv run python scripts/bench_microfeed_i2p.py
   --mode new --timeout-s 3600 --provider claude` — reaches 5/5 parity
   (audit PASSED, hidden+browser evaluators PASS, 0 slices blocked,
   wall ≤1500s, cost within v1 envelope).
4. Replay: pick the failed R26 messages.jsonl, re-run the parser on its
   compile-spec output. Should produce a usable Spec with one warning
   (state_invariant prose) instead of slice-blocking.

### Bounds & risks

- **Risk**: permissive parsing might mask real bugs. Mitigation:
  warnings are surfaced in the proof packet and journal so humans can
  see what was coerced. The audit's contract gate remains the truth.
- **LOC**: ~250 LOC + ~150 LOC tests.
- **Wall time to ship**: ~1 day implementation + 1 bench round.

---

## v2.2 — Tiered mutable spec + amendment API

**Why second**: this is the load-bearing v2 architectural change.
v2.3–v2.7 all assume mutable spec.

### Scope

Implement the three-tier mutability design from `docs/intent-to-product-v2.md`
"Safe mutability" section.

- **Tier 1 (Bedrock)**: `intent`, `intent_hash`. Hash computed at
  session start, stored in `<session>/spec/intent.lock`. Every
  `persist_spec` call verifies the hash; mismatch → hard reject + journal
  event + run blocks.
- **Tier 2 (Locked)**: `project_kind`, `done_means`, `non_goals`,
  `cross_slice_checks`, `test_command`, `slice.id`. Compile sets them
  once; agents cannot amend; user-only edit through spec-review gate.
- **Tier 3 (Slice-local)**: a slice's `deps`, `owned_paths`, `tasks`,
  `shared_scaffold` (collective), `slice.checks` (append-only).
  Mutable via `request_amendment()` API.

### Files

- **New**: `otto/spec_amend.py`
  - `IntentLock` dataclass (intent, intent_hash, session_id).
  - `request_amendment(spec, slice_id, change, reason, trigger_event_id)
    -> Result[Spec, AmendmentError]` — validates tier rules, hash chain,
    trigger event existence.
  - `verify_amendment_chain(spec) -> ChainVerification` — used at
    end-of-run by audit.
- **Modify**: `otto/spec_compile.py`
  - `Spec` dataclass gains `intent_hash: str` and per-field `mutable_by`
    metadata (Tier 1/2/3).
  - `persist_spec` checks tier-1 invariant on every write.
- **Modify**: `otto/journal.py`
  - Stable event IDs (`"<session-id>-<seq>"`). Existing events get them.
  - New event types: `amendment.requested`, `amendment.applied`,
    `amendment.rejected`, `intent.lock.violated`.
- **Modify**: `otto/build.py`
  - Build-agent prompt gains awareness of `request_amendment` as a tool
    (the agent can request a dep add when it hits a scope warning, instead
    of silently violating).
  - `BuildAgentTools` gets `request_amendment` MCP tool.
- **Modify**: `otto/audit.py`
  - At end-of-run, call `verify_amendment_chain` and `review_amendments`
    (LLM, scoped to amendment chain only — not full spec). Suspicious
    chains cap verdict at PARTIAL.

### Tests

`tests/test_spec_amend.py` (new):
- Tier-1 violation: attempt to write spec with mutated intent → blocked.
- Tier-2 violation: agent calls `request_amendment(target="project_kind")`
  → rejected with reason.
- Tier-3 ok path: agent adds a dep with valid trigger event → applied,
  hash chain extends.
- Append-only checks: agent attempts to remove a check → rejected.
- Hash chain break: manually corrupt spec.json → next persist_spec catches.
- Audit chain review: amendment with no trigger event → flagged.

`tests/integration/test_amendment_loop.py` (new, real LLM):
- Inject a spec where slice A's deps don't include slice B, but A
  needs B's helper. Build agent should `request_amendment` (deps_add=B)
  when it hits the scope warning. Verify amendment lands and slice
  proceeds.

### Verification

1. Unit + integration tests pass.
2. Bench: Microfeed at parity (5/5), no regressions.
3. Bench under deliberate attack: seed a spec with an attack-shape
   (e.g., compile produces wrong deps); verify the build agent
   self-corrects via amendment instead of failing.
4. Bench under inverse attack: write a malicious build agent prompt
   that tries to weaken its own checks; verify rejection.

### Bounds & risks

- **Risk**: agents might abuse the amendment API to declare success.
  Mitigation: defense-in-depth (D1 contract test + D2 private evaluators
  + D3 chain review). All three already have v1 equivalents or are
  small additions.
- **LOC**: ~400 LOC + ~150 LOC tests.
- **Wall time**: ~3 days implementation + 2 bench rounds (parity + attack).

---

## v2.3 — Auto-collapse for tiny products

**Why third**: independent of v2.2; small win on small products.
Reduces the ceremony tax for trivial inputs.

### Scope

When the compile agent produces `len(slices) <= 1`, the pipeline
auto-collapses:

- Build phase = single agent invocation against the entire repo.
- Merge queue = no-op (single slice → branch lands directly).
- Audit = unchanged (still verifies).
- Render = simplified packet (no per-slice grid; just the audit
  walkthrough + checks).

### Files

- **Modify**: `otto/pipeline.py` — top-level dispatcher detects
  `len(spec.slices) <= 1` and routes to `_run_singleshot()`.
- **New**: `otto/pipeline_singleshot.py` — collapsed flow.
- **Modify**: `otto/render.py` — single-slice template branch.

### Tests

`tests/test_singleshot.py`:
- Compile produces 0 slices (just intent + structure) → singleshot path.
- Compile produces 1 slice → singleshot path.
- Compile produces 2+ slices → multi-slice path (existing behavior).

`tests/integration/test_tiny_intent.py` (real LLM):
- Intent: "a single Python script that prints 'hello world'." Verify
  singleshot path runs end-to-end in <2 minutes.

### Verification

1. Unit + integration pass.
2. Bench: Microfeed (multi-slice) still 5/5.
3. New tiny-intent bench: < 2 min wall, audit PASSED.

### Bounds & risks

- **Risk**: misclassifying a multi-slice product as tiny. Mitigation:
  the dispatch is on the spec output of compile, which is observable.
  Visible in journal.
- **LOC**: ~150 LOC + ~80 LOC tests.
- **Wall time**: ~1.5 days.

---

## v2.4 — Pluggable build agents

**Why fourth**: independent of v2.2/v2.3. Reduces LLM cost and variance
for deterministic build steps.

### Scope

Generalize "slice build = LLM call" to "slice build = any callable
that produces a diff."

```python
@dataclass
class BuildAgent:
    kind: Literal["llm", "bash", "template", "codemod", "package_install"]
    payload: dict[str, Any]

# Spec.slices[i].builder: BuildAgent | None  (None = LLM default)
```

Built-in builders:
- `llm`: existing v1 path (default).
- `bash`: runs a shell script in the worktree.
- `template`: copies a directory tree, with token substitution.
- `codemod`: applies a libcst/jscodeshift transform.
- `package_install`: runs `pip install` / `npm install` / `uv add`.

### Files

- **New**: `otto/builders/` package
  - `__init__.py` — `dispatch_builder(slice, spec, project_dir)`.
  - `llm.py` — wraps existing `otto/build.py` LLM path.
  - `bash.py`, `template.py`, `codemod.py`, `package_install.py`.
- **Modify**: `otto/spec_compile.py` — `Slice.builder` field added (optional).
- **Modify**: `otto/build.py` — `_run_slice` dispatches to builders.

### Tests

`tests/test_builders.py`:
- Each builder kind: happy path + error path.
- Composite: a multi-slice spec with mixed builder kinds.

`tests/integration/test_pluggable_build.py` (real LLM, but only
where `kind=llm` slices need it):
- Intent: "Flask app with a CLI helper." Compile produces 2 slices,
  one `llm` (Flask routes) + one `template` (CLI scaffold from a
  click template). Both land. Audit PASSED.

### Verification

1. Unit + integration pass.
2. Bench: Microfeed unchanged (all slices stay `kind=llm`); 5/5.
3. Cost check: a synthetic spec with 50% `kind=template` slices runs
   at <60% the cost of the all-LLM equivalent.

### Bounds & risks

- **Risk**: deterministic builders fail in ways agents would have
  recovered from (e.g., a template doesn't fit the project). Mitigation:
  builder failures fall through to an LLM repair loop (one retry, LLM
  builder).
- **LOC**: ~600 LOC + ~250 LOC tests (includes 4 builder implementations).
- **Wall time**: ~5 days.

---

## v2.5 — Parallel build execution

**Why fifth**: depends on v2.2 (mutable spec) for amendment-driven
self-correction during conflicts. Big wall-time win.

### Scope

Use the dep DAG. Slices with no shared deps run concurrently. Each
slice in its own worktree; merge queue serializes the merge step but
build can be parallel.

### Files

- **Modify**: `otto/build.py`
  - Replace sequential `for slice in topological_sort(spec.slices)` with
    `asyncio.gather` over a readiness queue.
  - Concurrency cap: `min(spec.parallel_max, n_slices_ready)` — default
    parallel_max=4.
- **Modify**: `otto/merge_queue.py`
  - Already FIFO; reaffirm: build can finish in any order, merge stays
    serial per `(project, target_branch)`.
- **Modify**: `otto/journal.py`
  - Concurrent writers: serialize via existing append lock.

### Tests

`tests/test_build_parallel.py`:
- Linear DAG (A→B→C): runs sequentially.
- Diamond DAG (A→{B,C}→D): B and C run concurrently.
- Independent slices: all run concurrently.
- Cap respected: 5 ready slices, parallel_max=2 → 2 at a time.

`tests/integration/test_parallel_microfeed.py` (real LLM):
- Microfeed (multi-slice). Verify wall time drops materially vs. v1
  sequential baseline.

### Verification

1. Unit + integration pass.
2. Bench: Microfeed at 5/5 with wall time ≤ 60% of v1 baseline.
3. No flakiness: run the bench 3× back-to-back; all 3 pass.

### Bounds & risks

- **Risk**: more concurrent slices → more merge conflicts at shared
  scaffolds. Mitigation: amendment API (v2.2) lets agents self-correct
  by requesting dep additions or `shared_scaffold` declarations.
- **Risk**: race conditions in journal/checkpoint. Mitigation: existing
  append lock + audit replay on resume.
- **LOC**: ~200 LOC + ~150 LOC tests.
- **Wall time**: ~3 days + 3 bench rounds.

---

## v2.6 — Per-capability audit verdicts

**Why sixth**: largely orthogonal to others; UI-heavy. Improves user
experience but doesn't affect build reliability.

### Scope

Audit emits per-capability (per-slice or per-`done_means` item)
verdicts instead of a single PASSED/PARTIAL/BLOCKED label.

- `Verdict.overall` → enum `passed/partial/blocked` (kept for
  back-compat).
- `Verdict.capabilities` → `list[CapabilityVerdict]` with
  `(name, status, evidence_refs, narrative)`.

The proof packet renders both: top-line summary + per-capability grid
with drill-down.

### Files

- **Modify**: `otto/audit.py`
  - Audit prompt updated to ask for per-capability judgment.
  - Output schema gains `capabilities` list.
- **Modify**: `otto/render.py`
  - HTML template: capability grid + per-capability evidence section.
- **Modify**: `otto/web/client/src/components/audit/` (Mission Control)
  - Per-capability tab.

### Tests

`tests/test_audit_capabilities.py`:
- Audit on a spec with 3 slices, 2 working + 1 broken → overall=partial,
  capabilities=[ok, ok, blocked].
- Render: HTML contains all 3 capability cards.

`tests/browser/test_audit_view.py`:
- Navigate to audit tab; verify per-capability grid renders.

### Verification

1. Unit + browser pass.
2. Bench: Microfeed at 5/5; capability list contains 5 entries (one
   per slice).

### Bounds & risks

- **Risk**: audit prompt drift. Mitigation: prompt versioned + regression
  test fixture.
- **LOC**: ~300 LOC + ~200 LOC tests + ~400 LOC TS.
- **Wall time**: ~4 days.

---

## v2.7 — Session continuity

**Why last**: largest-scope architectural change; depends on stable
v2.1–v2.6.

### Scope

A new session can read the latest landed spec + audit verdict + proof
packet from a previous session, treat them as input, build incrementally.

- `otto run --continue <session-id>` (or auto-detect from CWD's
  `otto_logs/sessions/latest`).
- Compile agent gets prior spec + audit findings as context; produces
  amendments rather than fresh decomposition.
- Build agents see prior code; only changed/new slices rebuild.
- Audit compares against prior verdict; produces incremental report.

### Files

- **New**: `otto/session_chain.py`
  - `load_prior_session(path) -> PriorContext`
  - `compile_amendments(prior, new_intent) -> list[Amendment]`
- **Modify**: `otto/cli_run.py` — `--continue` flag.
- **Modify**: `otto/spec_compile.py` — `compile_spec_incremental`.
- **Modify**: `otto/audit.py` — incremental audit mode.

### Tests

`tests/test_session_chain.py`:
- Two sessions in sequence: first lands a 3-slice product; second adds
  a 4th slice via incremental intent. Verify only slice 4 rebuilds.

`tests/integration/test_session_chain_e2e.py` (real LLM):
- Microfeed in two sessions: first build feeds; second build adds
  comments. Verify second session reuses first's app shell.

### Verification

1. Unit + integration pass.
2. Bench (new): two-session Microfeed runs, second session is 50%
   faster than first.

### Bounds & risks

- **Risk**: prior spec/audit drift confuses the compile agent.
  Mitigation: explicit `prior_context` field passed in; not implicit.
- **Risk**: huge user-experience surface. Mitigation: ship v2.7 as
  experimental flag (`--experimental-continue`); promote after dogfood.
- **LOC**: ~700 LOC + ~300 LOC tests.
- **Wall time**: ~7 days.

---

## Cumulative bench discipline

Every PR must:

1. Pass v1's existing 161 unit tests.
2. Add its own unit + integration tests (counts above).
3. Pass Microfeed bench at 5/5 parity (cost ≤ v1 envelope).
4. **Optional but recommended**: Codex review via `/codex-gate`
   (when credits available).

If a step's bench fails, **iterate within that step** — don't pile
fixes onto the next PR. v1's R3–R26 history shows that one-piece-at-a-
time gives the cleanest signal.

---

## Total budget estimate

| Step | LOC (impl) | LOC (test) | Wall (days) |
|---|---|---|---|
| v2.1 | 250 | 150 | 1 |
| v2.2 | 400 | 150 | 3 |
| v2.3 | 150 | 80 | 1.5 |
| v2.4 | 600 | 250 | 5 |
| v2.5 | 200 | 150 | 3 |
| v2.6 | 700 | 200 | 4 |
| v2.7 | 700 | 300 | 7 |
| **Total** | **3000** | **1280** | **~24.5 days** |

Plus ~10 bench runs across the series at ~$10–30 each ≈ $200 real-cost
budget.

---

## Decision points (where to stop)

If after v2.1+v2.2 the bench reliability is already high (≥80% pass
rate across 10 consecutive Microfeed runs), evaluate whether v2.4 and
v2.7 are needed. They're optimization/expansion, not reliability. v2.3
and v2.6 ship cheaply and are user-visible — keep them.

v2.5 (parallel build) is the highest-effort wall-time win. Its value
depends on whether users hit the wall-time ceiling in practice.

---

## Open questions deferred to v2.x execution

- **Concurrency cap default**: 4 might be too high for memory; revisit
  with bench data.
- **Builder dispatch in compile agent**: should compile suggest
  builders (`builder=template` for scaffold slices), or always default
  to LLM and let human edit? Defer until v2.4 has real usage.
- **Intent-hash collision**: SHA-256 is fine; document the algorithm
  in the spec format.
- **Mid-session intent change**: tier-1 says immutable. If user
  genuinely needs to change intent mid-session, what's the recovery?
  Leaning toward: drop the session, start fresh with new intent.
  Document; don't engineer until needed.

---

## Acknowledgements

This plan is the implementation reading of `docs/intent-to-product-v2.md`
(design + findings). The principle that everything maps back to:

> Whenever we tried to constrain the agent's output, the system got
> more brittle. Whenever we relaxed and trusted post-hoc verification,
> reliability went up.

Each v2 step is one application of that lesson.
