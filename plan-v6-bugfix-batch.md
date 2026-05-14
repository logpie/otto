# c80b3b Clarification

1. c80b3b's 22-minute wall time was more a smell in the decomposition logic
   than evidence that the scope was optimally inlined. Inline can be correct
   for a tightly coupled scaffold or atomic migration, but a 22-minute child
   usually means too much work crossed the dispatch boundary as one unit. The
   deeper distinction is timing: splitting inside that child may serialize if
   those sub-children all depend on a scaffold the child has not created yet;
   splitting at root with an architect-first scaffold lets independent feature
   leaves start after shared contracts exist. So both earlier statements were
   partly true: late child-local decomposition would likely serialize, but the
   root should probably have pushed shared setup into the architect and fanned
   out smaller leaves sooner.

2. Yes. A child that consistently runs over 20 minutes should be treated as a
   decomposition feedback signal, not as normal success. The lead prompt's DAG
   rule should ask the root Lead to refactor the parent decomposition before
   dispatch: move shared architecture, route shells, data models, ports,
   dependency installation, and test harness setup into the architect scaffold;
   then emit smaller children that each own a feature/action slice with clear
   `owned_paths` and `action_ids`. The exception is an indivisible atomic change
   where parallel leaves would edit the same file set or require a single
   ordered migration; in that case the parent must say why the large child is
   intentionally serial.

3. Add both prompt pressure and runner feedback:

   Prompt language:
   - "Target leaf child size: one coherent feature/action slice, normally 5-10
     minutes of wall time. Integration nodes verify/repair merged state; they
     should not become feature-build monoliths."
   - "If a proposed child owns both scaffold/contracts and multiple product
     features, split it: architect owns scaffold/contracts first; leaves own
     feature/action implementation after the architect."
   - "If a proposed child mentions more than two primary actions, more than one
     subsystem, frontend plus backend plus tests, or vague umbrella wording such
     as 'build the app UI', do not emit it as a leaf. Break it into explicit
     leaves with owned paths and action IDs."
   - "If you keep a child that you expect to exceed 15 minutes, include an
     explicit indivisibility rationale: which files/contracts force
     serialization, what would conflict if split, and why the architect cannot
     pre-scaffold more of it."

   Runner heuristic:
   - Record `fat_child_duration` when any non-integration child exceeds 20
     minutes, including task id, parent id, duration, intent, owned paths, and
     action ids.
   - Feed that warning into the parent/root decomposition review on retry or
     the next run: "this shape produced a >20 minute leaf; refactor the parent
     DAG unless indivisible."
   - Add a cheap pre-dispatch lint for root-emitted children: warn when a leaf
     has no `owned_paths`/`action_ids`, contains umbrella words ("all",
     "entire", "full UI", "frontend and backend"), or spans multiple subsystem
     path prefixes. Do not hard-block initially; surface it as a structured
     warning so we learn without preventing legitimate coupled work.

# Plan

Codex peer gate note: the `mcp__codex__codex` tool required by the local
`codex-gate` workflow is not available here, and this dispatch explicitly says
not to split work across Codex calls. I will keep a file trail here and verify
with local regression tests.

## P1.1 Global concurrency lease

Change `otto/v5_runner.py`: add a per-run shared dispatch lease object and pass
it through nested `_process_children()` calls. A scheduler acquires a task id
before creating `_run_child()`, and releases in a `finally`-style completion
path. The lease tracks active task ids to prevent duplicate dispatch even when
two scheduler loops see the same pending entry.

Verify: add an async regression that runs two concurrent `_process_children()`
calls with `max_parallel=3`, a slow fake child runner, and six pending tasks;
assert active count never exceeds 3 and each task id dispatches once.

## P1.2 Decomposed-child summaries

Change `_build_child_summaries()` in `otto/v5_runner.py`: when a direct child is
still `pending_children`, reconstruct the summary from that child's descendant
integration results. If no integration session/result exists yet, preserve the
legacy `pending_children` summary.

Verify: fixture graph where child A is `pending_children` and A's subchildren
landed via A's integration result; root summaries must report A's integrated
verdict/coverage instead of stale planning verdict.

## P1.3 Root integration preflight injection

Change root integration in `run_v5_pipeline()` to use the same
`_run_integration_smoke_preflight()` payload path as subtree integration before
calling `run_lead(kind="integration")`, and run the matching post-agent
preflight/downgrade path.

Verify: root integration test with failing `smoke_clean_deploy` proves
`preflight_result` reaches the integration prompt/call.

## P1.4 Matrix scope call-site regression

Current `lead.py` already passes `verification_plan.matrix_scope`; add a
runner-facing regression so this wiring cannot silently regress. Keep backward
compat default as full matrix for legacy configs.

Verify: leaf `run_lead()` with `matrix_scope=integration_only` writes only local
scope checks; integration `run_lead()` with the same config runs the full
matrix.

## P2.5 Toolchain preflight propagation

Change `_process_children()` around architect pass: make toolchain preflight
guarded by a per-architect retry key instead of one boolean for the whole
subtree, run it after the actual architect child completes, log timing, and
emit manifest/install-dir propagation details. Keep symlink propagation in
`_run_child()` as the child-side inheritance point.

Verify: runner fixture where architect emits a frontend manifest; fake
toolchain preflight creates `frontend/node_modules`; a child worktree receives
the shared install dir symlink and a toolchain timing event is emitted.

## P2.6 Port-busy preflight hard block

Change `otto/v5_preflight.py`: map `verify_from_clean(...).failure_kind ==
"port_busy"` to `severity="block"` for `clean_deploy_port_busy`. This is the
simplest correct option because clean-deploy cannot be trusted when declared
ports are already bound.

Verify: unit test that `smoke_clean_deploy()` returns block severity for a
busy declared port.

## P2.7 Skipped intent report

Change `otto/lead.py`: after any child/integration result lands, inspect
`verify_result.intent_coverage.skipped`; if non-empty, write/append
`<session_dir>/skipped_report.md` with timestamp, task id, verdict, and skipped
items. Do not auto-spawn recovery tasks.

Verify: direct unit test for the report writer that proves skipped coverage
becomes an operator-visible manual follow-up report.

## P2.8 CHARTER prose cap split

Change `otto/prompts/lead.md`: separate IA JSON from prose explicitly; IA JSON
has no line cap, CHARTER prose target becomes <=300 lines. Change
`otto/v5_capability_inventory.py` constant and warning detail to report total
lines, IA JSON lines, and prose lines.

Verify: IA/coherence tests assert the prompt language and structured warning
detail include the prose/IA split.

## P3.9 Deprecation warnings

Change `otto/v5_verification_plan.py`: add a deterministic check that flags
deprecation warnings in `test_output`, `stdout`, `stderr`, journey details, or
evidence log files when a verdict claims pass. Also update the lead prompt to
tell agents to fix or downgrade on deprecation warnings.

Verify: unit test where passing verdict with `DeprecationWarning` or
`deprecated` warning output downgrades to partial.

## Test cadence

After P1 fixes: `uv run --extra dev pytest tests/ -q -k "v5 or spec_compile or runner or branching" --ignore=tests/integration`

After P2/P3 fixes: run the same command again. Then run targeted files for any
new tests if the broad filter misses them.

## Implementation Notes

- P1.1 implemented with a shared per-run `_DispatchLease` passed through nested
  `_process_children()` calls.
- P1.2 implemented by reconstructing stale `pending_children` summaries from
  subtree integration results, with recursive descendant fallback.
- P1.3 implemented by injecting root integration pre/post
  `smoke_clean_deploy` payloads through the same prompt field as subtree
  integrations.
- P1.4 pinned with a `run_lead()` call-site regression for
  `verification_plan.matrix_scope`.
- P2.5 implemented by running architect toolchain preflight per architect/retry
  key, logging duration, propagating install dirs, and verifying child
  inheritance.
- P2.6 implemented by making `clean_deploy_port_busy` a blocking preflight
  issue.
- P2.7 implemented with timestamped append-only
  `<session_dir>/skipped_report.md` generation for skipped intent coverage.
- P2.8 implemented with uncapped IA JSON, prose target <=300 lines, and
  coherence warning details that split total/prose/IA JSON lines.
- P3.9 implemented with a `deprecation_warnings` runner check and lead prompt
  language requiring agents to fix or downgrade on deprecation warnings.

## Verification Run

- `uv run python -m py_compile otto/v5_runner.py otto/lead.py otto/v5_verification_plan.py otto/v5_capability_inventory.py otto/v5_preflight.py tests/test_v5_phase2.py tests/test_v5_architect_retry.py tests/test_v5_integration_worktree.py tests/test_v5_preflight.py tests/test_v5_skipped_report.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py` passed.
- Repository search confirmed the old 500-line CHARTER-cap wording and the
  warning-level `clean_deploy_port_busy` mapping are gone.
- `uv run --extra dev pytest tests/test_v5_phase2.py tests/test_v5_architect_retry.py tests/test_v5_integration_worktree.py tests/test_v5_preflight.py tests/test_v5_skipped_report.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py -q` passed: 77 passed.
- `uv run --extra dev pytest tests/ -q -k "v5 or spec_compile or runner or branching" --ignore=tests/integration` passed: 568 passed, 2061 deselected.
- `uv run ruff check otto/v5_runner.py otto/lead.py otto/v5_verification_plan.py otto/v5_capability_inventory.py otto/v5_preflight.py tests/test_v5_phase2.py tests/test_v5_architect_retry.py tests/test_v5_integration_worktree.py tests/test_v5_preflight.py tests/test_v5_skipped_report.py tests/test_v5_ia_contract.py tests/test_v5_verification_plan.py` passed.

Deferred/unfixable: no product fixes deferred. The local Codex peer gate remains
unavailable in this session because the `mcp__codex__codex` tool is not present
and the dispatch requested no extra Codex calls.
