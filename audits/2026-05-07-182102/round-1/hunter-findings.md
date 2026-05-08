# Hunter Findings

Multiple read-only agents reviewed provider, merge/audit, Mission Control, queue, and test surfaces.

## Provider

- Permission approval needed to be bounded to the active workspace.
- App-server token usage needed to prefer cumulative totals.
- Dollar-budget enforcement remains limited by missing provider cost metadata.

## Merge and Audit

- Cross-slice deterministic failures and missing feature audits were not hard caps on audit verdicts.
- Malformed project config was not failing the configured contract gate.
- Missing declared slice branches could fall back to committing dirty integration state.
- Existing worktree branches ignored requested `base_ref`.

## Queue and Mission Control

- Success manifest handling lost races against cancel, timeout, shutdown, and terminating states.
- Child process cwd changes were treated as identity failures.
- Mission Control run status flattened spec-review and paused states into misleading labels.

## Deferred

- Merge-all CTA/backend contract, destructive merge cleanup policy, raw Codex CLI bypass policy, and queue cancel/remove orphan cleanup were left as explicit follow-up risks.
