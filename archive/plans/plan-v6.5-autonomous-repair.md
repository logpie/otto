# v6.5 Autonomous Repair Plan

Written: 2026-05-14

## Executive Summary

The previous read-only audit found that v6.5 readiness is blocked less by one-off bugs than by missing deterministic guardrails. The system needs a cheap, 0% LLM smoke matrix that exercises the exact failure classes before any live run burns provider time. The first priority is to pin subtree propagation, preflight repair, verdict tolerance, and artifact path safety with tests that fail on the old behavior and pass on the corrected behavior.

The P0 implementation adds:

- A `tests/smoke/` matrix for fast deterministic pre-run checks.
- Safe artifact slugging for runner-generated paths derived from model/spec prose.
- Forgiving verdict parsing plus one canonical rewrite retry for malformed verdict files.
- A deterministic `PreflightRepairController` wired into root and nested integration preflight.
- Definitive nested subtree propagation coverage for root -> frontend parent -> grandchildren -> main.

## Bug Class Catalog

1. **Nested subtree integration not propagated to main**
   - Impact: product files can pass inside `i2p/integ/<parent>` but never become reachable from root/main; root can claim pass while the real product is missing a subsystem.
   - Ranking: P0 correctness failure.
   - Contract: any descendant leaf commit that passed and integrated into a subtree must be reachable from the parent's integration branch, and root descendants must be reachable from `main`.

2. **Preflight failure has no autonomous repair loop**
   - Impact: deterministic failures such as busy ports, shell-script validation, TypeScript compile errors, and path errors get handed to long-running agents or cause terminal blocks without first trying the obvious bounded fix.
   - Ranking: P0 cost and reliability failure.
   - Contract: preflight failures are classified, repaired when deterministic, retried within caps, and escalated with an explicit reason when not repairable.

3. **Verdict schema brittleness**
   - Impact: agents can do the right work and write a plausible but non-canonical `verdict.json`; the runner discards it and marks the run unverified.
   - Ranking: P0 reliability failure.
   - Contract: known non-canonical success shapes are mapped to canonical verdicts; unknown malformed verdicts get one rewrite attempt, not an infinite loop.

4. **Runner-generated unsafe artifact paths**
   - Impact: prose-derived ids can exceed filesystem component limits or contain unsafe path characters. The v6c path failure around a 270-character `"curl verification: ..."` label is the concrete reproducer.
   - Ranking: P0 infrastructure failure.
   - Contract: every runner-created path component derived from prose goes through `safe_slug(label, max_len=48)` and gains a short hash when the label is modified or truncated.

5. **Port lifecycle cleanup**
   - Impact: stale app processes make integration runs fail or test the wrong app.
   - Ranking: P1 after the controller exists, because port-busy gets a P0 controller action now.

6. **Thin integration packet**
   - Impact: integration agents receive too little context to repair cross-subtree failures without rediscovery.
   - Ranking: P1.

7. **Leaf check matrix overreach**
   - Impact: leaf agents can be held responsible for integration-only behavior, creating false negatives and wasted repair.
   - Ranking: P2 unless it blocks the next live run.

## Pre-Run Test Matrix Design

The pre-run matrix has three tiers:

- **10s pre-launch checks**
  - Pure Python/unit smoke around path slugging, verdict parser mapping, repair classification caps, and raw-log path construction.
  - No browser, network, package installs, or LLM.

- **0% LLM smoke matrix**
  - `tests/smoke/`.
  - Uses real git repos and fake lead agents.
  - Exercises root -> nested subtree merge propagation, controller repair actions, malformed verdict handling, and path safety.
  - Intended command: `uv run --extra dev pytest tests/smoke/ -v`.

- **1-2 min proxy pipeline**
  - Existing focused v5 tests with fake agents and real git.
  - Intended command: `uv run --extra dev pytest tests/ -q -k "v5" --ignore=tests/integration`.
  - Proves the smoke fixes did not break broader v5 behavior.

## PreflightRepairController Design

New module: `otto/v5_preflight_repair.py`.

The controller receives a session directory, worktree path, original budget, and injectable repair callbacks. It appends every decision to `preflight-repair.jsonl` under the session directory. Every line has `_written_at`, attempt counters, failure fingerprint, issue kind, action, and outcome.

Caps:

- Maximum 2 attempts per failure kind.
- Maximum 3 total attempts per controller run.
- Agent repair spend must stay below 10% of the original budget.
- If the same normalized fingerprint appears twice, stop and escalate.
- No infinite retries; every terminal result is either `continued` or `escalated`.

Classification table:

| Failure kind | Signals | Action | Scope |
| --- | --- | --- | --- |
| `port_busy` | `clean_deploy_port_busy`, message mentions busy/bound port | Auto-fix | Kill only Otto-owned stale PIDs for declared/busy ports, then rerun preflight |
| `filename_too_long` | `filename_too_long`, `Errno 63`, `File name too long`, overlong prose path | Auto-fix | Rename unsafe generated path components with `safe_slug()` |
| `typescript_error` | `typescript_error`, `TS####`, `.ts`/`.tsx` error in compile output | Agent | Focused repair on mentioned TS/TSX paths |
| `script_valid_failed` | `script_valid_failed`, `clean_deploy_script_valid_failed` | Agent | Focused repair on `start.sh` only |
| `malformed_verdict` | non-canonical `verdict.json` not mappable | Agent retry | One canonical rewrite request with original verdict content |
| Other | unknown, repeated, over budget, over caps | Escalate | Clean terminal reason, no more retries |

## Prioritized Implementation Plan

### P0

1. Add `tests/smoke/` infrastructure and mark all smoke tests with `pytest.mark.smoke`.
   - Verify: `uv run --extra dev pytest tests/smoke/ -v`.

2. Add nested subtree propagation smoke test.
   - Verify: fake root + frontend parent + three grandchildren produce files whose commits are ancestors of `main`.

3. Add safe slugging for prose-derived runner path components.
   - Verify: 270-character `"curl verification: ..."` label produces a portable path component `<=48` chars, with no slash/special characters, and raw merge log dirs use that slug.

4. Add forgiving verdict parser and one canonical rewrite retry.
   - Verify: `{ "status": "success", "tests": {...}, "deliverables": [...] }` maps to canonical `verdict: pass`; unmappable malformed verdict triggers exactly one retry hook.

5. Add `PreflightRepairController` and wire it into integration preflight.
   - Verify: smoke fixtures for port busy, filename too long, TypeScript error, script validation failure, and malformed verdict all fire the intended repair path and continue or escalate deterministically.

### P1

- Tighten port ownership detection and surface process provenance in repair logs.
- Enrich integration packets with child branch, repair evidence, and preflight history.
- Add post-run artifact loading checks for `preflight-repair.jsonl` and raw log paths.

### P2

- Rebalance leaf vs integration verification matrix so leaves do local checks only and integration nodes own cross-product journeys.
- Expand smoke matrix to cover multi-level merge conflicts and degraded integration semantics.

## Test-First Discipline

Every P0 fix is paired with a smoke or focused regression test:

- Subtree propagation: `tests/smoke/test_nested_subtree_propagation.py`.
- Preflight repair classes: `tests/smoke/test_preflight_repair_fixtures.py`.
- Safe slugging: smoke test with the 270-character curl verification label.
- Verdict parser: smoke test for known non-canonical success shape and one retry cap.
- Controller caps/logging: smoke tests assert append-only `preflight-repair.jsonl` and deterministic escalation on repeated fingerprints.

The smoke matrix is load-bearing. It should be run before every live v6.5 run.

