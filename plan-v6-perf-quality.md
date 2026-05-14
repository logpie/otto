# v6 Performance and Quality Plan

Date: 2026-05-14
Scope: v5 pipeline only. Do not touch the i2p monolithic path. Do not start live runs during planning or batch implementation unless explicitly approved.

This plan sequences the 10 high-priority punch-list items into deployable batches. The objective is to fix correctness first, then reduce wasted wall-clock time, while validating with deterministic fixtures and existing sc4/sc6 artifacts before paying for another full live iTracker build.

## Hard constraints

- **Provider stays on Claude.** Do not flip the default compile provider to codex, and do not add a config switch that prepares such a flip. Earlier draft of Batch 6 included "provider default flip prepared behind config" — that's REMOVED. Claude is the default and only investigated provider for spec compile in v6. Codex remains for review/consultation only (via mcp__codex__codex), not for first-class spec emission.
- v5 hierarchical only; i2p monolithic path untouched.
- UI work (3 spec-viz entries in punch list) is explicitly out of scope.
- No live runs initiated by Codex during implementation; user gates each batch validation.

## Sequencing Recommendation

### Batch 1 - Shift-left script correctness and integration repair input

Items: #2, #1.

Why first: this addresses the sc6 product-breaking failure class directly. `script_valid` catches the bad `start.sh` near architect time; preflight injection still gives the integration agent a defense-in-depth repair path if architect-time validation misses something.

Ship criteria:
- Architect post-pass preflight fails fast on root-owned shell portability issues.
- Integration preflight runs against the resolved integration worktree and is visible in the integration prompt.
- Integration reruns clean deploy after agent edits and downgrades honestly if still red.

### Batch 2 - Verification layers and decision broadcast enforcement

Items: #6, #3.

Why second: after shift-left script validation exists, the verification matrix can move out of leaves without losing important coverage. Decisions enforcement belongs here because it is another runner-side verdict discipline layer.

Ship criteria:
- Leaf verification no longer runs the full IA matrix for unrelated routes/endpoints.
- Integration verification still runs the full structured contract matrix.
- `decisions_appended` is part of the v5 verdict contract, with legacy verdicts accepted.
- Cross-subsystem modifications without a matching `decisions.md` entry downgrade to `partial` with a precise runner diagnostic.

### Batch 3 - Compile cache and compile observability

Items: #4 and the metrics-first part of #7.

Why third: cache is low-risk and can eliminate most repeated compile cost. Metrics must ship with it so we can decide whether Claude output explosion remains urgent after cache hits.

Ship criteria:
- Same intent + prompt + provider + model + Otto version reuses the cached spec.
- Any key change misses safely.
- Compile attempts write timing/size/usage metadata: start, end, first assistant token, prompt bytes, output bytes, validation retries, provider, model, cache hit/miss.

### Batch 4 - Shared toolchain preflight and critical-path DAG prompt

Items: #8, #5.

Why fourth: the prompt rule for inlining versus sub-decomposing depends on the real cost model. Once shared installs are actually preflighted once and propagated, child setup cost drops and the DAG rule can focus on semantic dependency depth rather than install overhead.

Ship criteria:
- Architect-time runner preflight installs/syncs shared toolchains once where manifests require it.
- Children inherit install dirs through existing symlink propagation.
- Lead prompt forbids `tests-only final child` and requires restructuring or inlining when the proposed child critical path exceeds 2 build stages.

### Batch 5 - Context shrink for children

Items: #10, #9.

Why fifth: CHARTER size cap is the smaller prompt-only reduction; per-child slicing is larger and should be built against the post-cap shape. They should ship together because the user-visible success metric is smaller child prompt context without starving children of cross-reference data.

Ship criteria:
- Architect prompt treats IA JSON as contract and caps prose to rationale-only, target about 500 CHARTER lines.
- Child sessions receive a scoped spec + CHARTER slice plus cross-reference index.
- Full unsliced spec/CHARTER remain available in session artifacts for audit, integration, and backward compatibility.

### Batch 6 - Compile output caps (Claude-only)

Items: remaining #7 (output caps only; provider routing dropped per hard constraint).

Why last: output caps should be informed by Batch 3 metrics and Batch 5 context changes. Goal is to make Claude's spec compile faster + cheaper by reducing duplicative output, NOT to flip the provider.

Ship criteria:
- Spec prompt/schema caps reduce duplication: `intent_claims <= 30`, terse IDs (not verbose repeated prose), representative journeys (not exhaustive), lower-priority detail stored as notes rather than matrix-driving rows.
- Validation still rejects structurally incomplete specs.
- Claude spec compile timing measurably improves vs Batch 3 baseline (target: cut Claude compile from ~10 min to ~5 min for iTracker-class intents).
- NO config switch added for provider routing. Claude remains the only first-class spec compile provider in v6.

## Per-Item Plan

| Item | Target Files | Change Scope | Tests | Risk | Effort | Dependencies |
| --- | --- | --- | --- | --- | --- | --- |
| #1 Inject preflight result into integration agent | `otto/v5_runner.py`, `otto/lead.py`, `otto/prompts/lead-integration.md`, `tests/test_v5_integration_worktree.py` | Resolve integration worktree before smoke, run `smoke_clean_deploy()` there, classify issues into structured preflight payload, pass into prompt, rerun after agent, downgrade if still failing | Fake `_run_integration()` with stub smoke and stub `run_lead`; assert prompt includes failure data, smoke cwd is integration worktree, post-agent smoke reruns, repeat failure downgrades | Medium: misclassifying fixable versus escalate could allow too much integration editing | M | Best paired with #2 so `script_valid_failed` is a known class |
| #2 Shell script + scaffold portability validation | `otto/v5_clean_verify.py`, `otto/v5_preflight.py`, `otto/v5_runner.py`, `tests/test_v5_clean_verify.py` or new focused test file | Add `script_valid` step for root-owned `*.sh`: shebang, executable bit, `bash -n`, bash-4 feature detection, dynamic PORT_CONFLICT branch, CHARTER table port parsing | Temp repos with `${service^^}`, missing shebang, non-executable script, CHARTER table ports, busy `API_PORT` branch. Existing sc6 `start.sh` should fail without starting services | Medium: dynamic script exec must avoid launching full app; use busy-port branch only and short timeout | M | None; enables #1 defense-in-depth |
| #3 Enforce decisions.md broadcast | `otto/prompts/lead.md`, `otto/prompts/lead-integration.md`, `otto/lead.py`, `otto/v5_verification_plan.py`, tests for verdict validation | Promote decisions responsibility near top of prompt, extend verdict schema with `decisions_appended`, add runner heuristic for shared/wire paths without entries | Prompt snapshot assertions; verdicts without field remain accepted; temp repo modifying shared files with no `decisions.md` entry downgrades; matching entry passes | Medium: path heuristic false positives can annoy agents; start with conservative shared-path list | M | Pairs with #6 because both are runner downgrade layers |
| #4 Cache spec compile by intent hash | `otto/spec_compile_flat.py`, maybe `otto/paths.py` or `otto/observability.py`, `tests/test_spec_compile_flat_structured.py` | Add cache key `(intent hash, rendered compile prompt hash, provider, model, Otto version, schema version)`, copy cached `spec.json` into current session, record provenance | Monkeypatch `_run_compile`; two identical calls invoke once; prompt/provider/model/version/schema changes miss; corrupt cache ignored; legacy no-cache path unchanged | Low/medium: stale cache if key omits schema/prompt/version; make key verbose and no cross-version reuse | M | None; metrics part of #7 should ship in same batch |
| #5 Critical-path-aware sub-decomposition | `otto/prompts/lead.md`, prompt tests | Add DAG critical path rule, forbid tests-only final child, tell architect to pre-wire shared contracts so leaves fan out; instruct inline if dependent chain cannot be flattened | Prompt snapshot/static test; optional fake MCP child graph unit if recorder exists | Medium: prompt-only control may not reliably change behavior; avoid overcorrecting to giant inline blobs | S | #8 changes cost model and should land first within same batch |
| #6 Full check matrix only at integration nodes | `otto/v5_verification_plan.py`, `otto/lead.py`, `tests/test_v5_verification_plan.py` | Add scope/mode parameter. Leaf mode: cheap checks only (`no_stub_text`, `verdict_consistency`, own test/journey evidence). Integration mode: full IA matrix | Existing all-check tests become integration-mode tests; new leaf-mode test proves route/endpoint misses outside leaf do not downgrade; legacy absent spec still skips | Medium: leaves may miss early contract drift; Batch 1/#2 and integration matrix cover final gate | M | Should account for #2's `script_valid` layer |
| #7 Claude compile output explosion | `otto/spec_compile_flat.py`, `otto/agent.py` or `otto/logstream.py` only if needed, `otto/config.py` if routing later, tests | Phase A: metrics artifact. Phase B: caps for intent claims, terse IDs, representative journeys, lower-priority notes. Phase C: optional Codex default after approval | Metrics parser against sc6 messages fixture; schema validation for `intent_claims <= 30`; fake provider configs for route decision; no default flip test until approved | High: caps can underrepresent complex intents; provider default changes product behavior | L | #4 first to know urgency; #10/#9 before final caps assessment |
| #8 Toolchain pre-flight in shared worktree | `otto/v5_runner.py`, `otto/v5_clean_verify.py` if sharing helper logic, tests around propagation/preinstall | Complete existing partial work: run `npm ci/install`, `uv sync`/explicit venv install, and Playwright install once post-architect; preserve existing symlink propagation | Mock subprocess calls by manifest shape; assert one install per subsystem, child symlink paths created, failures are logged honestly and block only when required | Medium: installs can mutate lockfiles or take network; do not silently commit dependency artifacts | M | #5 should use the updated cost model |
| #9 Per-child context slicing | `otto/v5_runner.py`, `otto/lead.py`, new `otto/v5_context_slice.py`, tests | Build scoped spec/CHARTER slice by child intent, owned paths, action IDs, IA routes/endpoints; provide cross-reference index and full artifact path | Fixture based on sc6 spec/CHARTER; slice for backend child includes relevant API/data contracts, excludes unrelated pages; integration/root receive full context; old specs pass through full | High: over-slicing starves child agents and causes drift | L | #10 first, then #9 in same batch |
| #10 Architect CHARTER output size cap | `otto/prompts/lead.md`, prompt tests | Make IA JSON the contract, prose rationale-only, target about 500 lines, no duplicate restatement of JSON fields | Prompt snapshot; sc4/sc6 CHARTER fixture analysis used as baseline metric, no LLM run required | Low/medium: prompt-only and may reduce useful human rationale if too blunt | S | Helps #9 but does not depend on it |

## Interaction Decisions

- #4 and #7: do #4 plus #7 metrics first, before caps or routing. If cache hit rate is high, compile speed matters mainly for first runs; still add metrics because first-run live builds remain expensive. Defer provider default routing until after metrics and one final comparative run.
- #6 and #2: define layers explicitly. Architect layer runs `script_valid` plus scaffold build. Leaf layer runs cheap local checks and verdict consistency. Integration layer runs clean deploy plus full IA matrix. This preserves coverage while moving noisy matrix checks out of leaves.
- #1 and #2: ship together in Batch 1. `script_valid` makes the sc6 class rarer, but #1 remains required because integration may still encounter env, import, permission, or cross-child glue failures that only exist after merge.
- #5 and #8: complete #8 first within Batch 4, then adjust #5 prompt. Once installs are shared, the prompt should discourage chains for semantic reasons, not because each child has setup overhead.
- #9 and #10: implement #10 first within Batch 5, then #9. CHARTER cap reduces the full artifact; slicing reduces what each child sees. Both are needed for meaningful context reduction.
- #3 and #6: implement in the same batch. Decisions entries are a lightweight cross-subsystem trace that can catch drift before the integration matrix turns it into a late failure. The runner downgrade should complement, not replace, integration checks.

## Validation Strategy

No batch should require a fresh full live iTracker build. Use the following validation ladder.

### Batch 1 validation

- Unit: `uv run pytest -q tests/test_v5_integration_worktree.py tests/test_v5_architect_retry.py tests/test_v5_preflight.py --maxfail=5`
- New focused tests for `script_valid` in temp repos.
- Artifact replay: run the new script validator against a copy of `/Users/yuxuan/otto-projects/v5-itracker-sc6-213910/start.sh` and assert it reports `${service^^}` as bash-4-incompatible. This should not start the full app.
- Near-live: fake integration worktree with a deliberately broken `start.sh`; integration agent is stubbed, not provider-backed.

### Batch 2 validation

- Unit: `uv run pytest -q tests/test_v5_verification_plan.py tests/test_v5_step0b_recovery.py --maxfail=5`
- New tests for leaf versus integration verification modes.
- New tests for `decisions_appended` backward compatibility and downgrade behavior.
- Artifact replay: run verification-plan construction against an sc6 session/spec/CHARTER fixture in leaf mode and confirm unrelated IA route/endpoint misses do not produce leaf false-fail noise.

### Batch 3 validation

- Unit: spec compile cache tests with `_run_compile` monkeypatched.
- Metrics test: feed sc6 `messages.jsonl` into the metrics extractor and assert first assistant elapsed, output tokens, total tokens, duration, and prompt/output byte fields are populated.
- No provider call required. A cache hit should be provable with call counts and current-session `input-provenance.json`.

### Batch 4 validation

- Unit: manifest-driven toolchain preflight tests with mocked `subprocess.run`.
- Existing tests: `uv run pytest -q tests/test_v5_playwright_preinstall.py tests/test_v5_subtree_propagation.py tests/test_prompt_group_vocabulary.py --maxfail=5`
- Prompt snapshot: `lead.md` contains critical-path and no-tests-only-child rules.
- Near-live: temp repo with `frontend/package.json` and `api/pyproject.toml`; no LLM. Assert preflight creates/uses install dirs once and child worktree symlinks them.

### Batch 5 validation

- Unit: context slicing tests using sc6 `spec.json` and `CHARTER.md` fixtures. Assert slice bytes are materially smaller and required cross-references remain.
- Prompt snapshot: architect instructions include IA JSON contract, rationale-only prose, and target CHARTER size.
- Backward compatibility: legacy spec or missing IA falls back to unsliced full context.

### Batch 6 validation

- Unit: schema validation rejects over-cap outputs or trims/canonicalizes them according to the chosen design.
- Metrics comparison: use Batch 3 metrics artifact to compare pre/post output bytes and row counts on cached/replayed specs.
- Provider routing: do not flip default during unit validation. Prepare config support and document the switch.

### Final validation after all batches

Run one paid, full live validation only after Class A and Class B are both shipped. Preferred shape:
- Reuse the iTracker intent for apples-to-apples comparison against sc6.
- Capture wall-clock, compile cache hit/miss, compile metrics, child critical path, number of leaf false-fail checks, decisions entries by writer, script_valid outcome, and final clean deploy.
- If provider routing is under consideration, run one controlled compile-only A/B first rather than two full builds.

## Backward Compatibility

- Legacy `verdict.json` without `decisions_appended` must parse and validate.
- Legacy specs without schema v3 structured fields must keep the existing skip behavior in `v5_verification_plan.py`.
- Missing `CHARTER.md`, missing `decisions.md`, and missing `start.sh` must skip gracefully, not crash.
- Existing `smoke_clean_deploy()` event names should remain stable; add fields rather than renaming event kinds.
- Spec cache must be opt-in by exact key match and ignored if malformed. No cross-version reuse.
- Context slicing must preserve the full original spec/CHARTER in session artifacts and should fall back to full context for unknown/legacy shapes.
- Provider/model overrides in `otto.yaml` and CLI must continue to take precedence over any default-routing change.
- Toolchain preflight must not require network success for projects that do not declare those toolchains.

## Risk Callouts

- ~~Provider default routing to Codex for compile should require explicit confirmation~~ **RESOLVED via hard constraint: provider stays on Claude in v6. Drop this risk.**
- Dynamic `start.sh` validation is powerful but risky. It must force the PORT_CONFLICT branch with a busy port, short timeout, and cleanup, rather than executing arbitrary full launch logic.
- Per-child context slicing has the highest correctness risk. The first implementation should be conservative, keep full artifact paths visible, and fall back to full context when unsure.
- Decisions enforcement can generate false positives if the shared-path heuristic is too broad. Start with shared schemas/types/config/wire files, not every file outside a child directory.
- Moving the full matrix to integration nodes improves noise and speed but delays some feedback. Keep leaf local checks and final integration matrix mandatory.
- Toolchain preflight may mutate lockfiles or local install state. It should log commands and results with timestamps and should not stage or commit dependency artifacts.

## Out Of Scope

The three UI punch-list entries are explicitly deferred:
- Spec visualization - PM PRD layer.
- Spec visualization - engineering layer.
- Spec diagrams.

No Mission Control UI work belongs in these batches unless a later explicit request adds it.

## Implementation Gate Requirements

Before implementing Batch 1, run the required Plan Gate through `codex-gate` if available in that session. Before completing or merging each non-trivial batch, run the Implementation Gate and append findings to `review.md` per repo instructions. This plan intentionally stops before implementation.

## Implementation notes for Dispatch 2

Date: 2026-05-14

- Gate availability: the local `codex-gate` skill was present, but the `mcp__codex__codex` tool was not available in this session. Plan/implementation gates could not be invoked through MCP; validation was local only.
- Batch 2: `verification_plan.matrix_scope` now supports the backward-compatible default `leaf` (full matrix at every node) and opt-in `integration_only` (leaf local checks, integration full matrix). Leaf local mode keeps `no_stub_text`, `verdict_consistency`, local test/journey evidence, and decisions enforcement; integration nodes still run the full IA matrix.
- Batch 2: `decisions_appended` is accepted in `verdict.json` while legacy verdicts without the field remain valid. Runner enforcement uses a narrow git-status heuristic over changed shared schema/type/wire files only; matching `decisions.md` entries prevent downgrade.
- Batch 3: spec compile cache keys include intent hash, rendered `compile-spec-flat` prompt hash, provider, model, flat-spec schema version, and Otto version. Cache entries live under `otto_logs/cross-sessions/spec-cache/<key-hash>/`; malformed entries miss safely. `otto v5 run --no-cache` disables lookup/store for diagnostics.
- Batch 3: `compile_metrics.json` is written at the v5 session root for both cache hits and misses with timestamps, first assistant token timestamp when available, prompt/output byte counts, tokens, validation retries, provider, model, and cache hit/miss.
- Batch 4: architect-time shared toolchain preflight now runs manifest-driven `npm ci/install`, `uv sync`, and Playwright browser install once in the architect worktree, logs command/timing data under `otto_logs/preflight/toolchain-preflight-<architect>-attempt-N.json`, then reuses existing install-dir propagation for child worktrees.
- Batch 4: the lead prompt now rejects child DAGs with a critical path longer than two build stages unless the chain is flattened or inlined, and explicitly forbids a tests-only final child.

## Findings during implementation

- No deferred out-of-scope findings for Dispatch 2.
- Dispatch 3 gate availability: the local `codex-gate` skill was present, but
  the `mcp__codex__codex` tool was still unavailable in this session. Plan and
  implementation gates could not be invoked through MCP; validation was local
  only.
- Batch 5: the lead prompt now states that the IA JSON block is the CHARTER
  contract, prose is rationale-only, and total CHARTER should target <= 500
  lines without trimming contract data. The coherence gate now warns when
  non-IA CHARTER prose exceeds the 500-line target.
- Batch 5: child context slicing is implemented in `otto/v5_context_slicer.py`
  and remains off by default. Opt-in runs can use `--slice-context`; `--full-context`
  is the explicit escape hatch. Slice decisions are written to each child
  session's `context_slice.json` with included/excluded entities, claim counts,
  fallback state, and full artifact paths.
- Batch 6: the compile-spec-flat prompt now caps `intent_claims` at <= 30,
  asks for terse IDs, representative journeys, and lower-priority detail in
  `quality_constraints[].note`. The structured spec validator warns, rather
  than fails, when `intent_claims` exceeds the cap.
- Dispatch 3 lint cleanup: repo-wide ruff exposed mechanical pre-existing
  v5 lint issues outside the batch files. They were fixed without behavioral
  changes so the requested ruff gate could pass.
- No deferred out-of-scope findings for Dispatch 3.
