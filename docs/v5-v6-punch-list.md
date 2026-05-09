# Otto v6 Punch List

Things deliberately not in v5, with rationale for deferral and pointers
to where they connect.

## Architectural

### Recursive in-session subagent dispatch (depth ≥ 2)
**State:** verified broken in SDK 0.1.50 via
`/tmp/sdk-smoke/test_depth_thorough.py`. Subagents cannot dispatch other
subagents via Task. v5 routes recursion through the queue
(`mcp__otto__submit_subtask` → fresh top-level `query()`); verified at
depth 3 in `test_queue_recursion.py`.

**v6 question:** if SDK adds depth-2 in-session, do we use it for
tightly-coupled work, or stick with queue recursion?

### Watcher integration
**State:** v5 has its own async runner (`otto/v5_runner.py`). The
existing watcher (`otto/queue/runtime.py`) does not yet schedule v5 tasks.
This means `otto queue submit "<intent>"` doesn't currently reach v5.

**v6 work:** extend the existing watcher's main loop to also poll
`v5_pending.jsonl` and spawn v5 child tasks via `otto v5 run --queue-task=<id>`.
Estimated ~250 LOC. Tested by submitting tasks via `otto queue submit` and
verifying they appear in `task_graph.json`.

### Multi-user concurrent submitters
**State:** untested. v5_runner is in-process; two MC users running
`otto v5 run` against the same project would each spin up their own
runner. Cross-task merge has fcntl on git operations, but the queue's
v5_pending.jsonl write-coordination is unverified at high concurrency.

**v6 work:** stress test 4+ concurrent submitters; harden any race seen.

### Per-task isolation under conflict resolution
**State:** when a child's branch fails to merge into the parent's
integration branch, v5 marks `merge_blocked` and continues. Plan-v5 §3
described the option to "resume task's Lead via SDK session_id" with
the conflict markers in scope; not implemented. Today's behavior: child
worktree preserved, user can resolve manually.

**v6 work:** add a "conflict resolution Lead" that's spawned with the
conflict context and the task's prior session_id resumed; cap at 2
attempts.

## Verifier / audit

### Audit's LLM-judge integration
**State:** v5's verifier (`otto/lead_verify.py`) runs the project's native
test suite (npm/pytest/cargo/go) plus a browser journey runner if one
exists. It does NOT invoke the existing LLM judge in `otto/audit.py`. For
trivial-to-medium products, native tests are sufficient; for large
products (browser-engine class), the LLM judge's holistic check matters.

**v6 work:** wire `lead_verify.run_verify_for_lead` to call
`otto/audit.run_audit` when `--strict-audit` is set or when the project
has no native tests. ~80 LOC.

### Cumulative project intent
**State:** each v5 task is independent. There's no "project's
INTENT.md" that accumulates user requirements across tasks. Plan-v5 §6
explicitly punted.

**v6 work:** maintain `INTENT.md` at project root; each task either
reads from it (if no inline intent) or appends its intent to it.
Audit at root then verifies against the cumulative intent.

### Mid-run intent amendment
**State:** snapshotted at session start, immutable for the run. Plan-v5 §6.

**v6 work:** detect changes to `intent.md` mid-run; in supervised mode,
prompt user to confirm; in autopilot, defer to a follow-up task.

## UI

### MC tree view
**State:** the run drilldown still shows the v4 stage list. Plan-v5 §7.2
called for a tree-view component showing the task graph (parent → children).
Not implemented.

**v6 work:** React component reading `task_graph.json` via existing API,
rendering as expandable tree with verdict pills, cost, drill-down to
proof packet.

### First-level review modal
**State:** `--review-first-decomp` flag works; `otto v5 review` CLI works.
Plan-v5 §7.3 called for an MC modal that pops when root has emitted
children in supervised mode, allowing accept / edit / replace.

**v6 work:** modal component + WebSocket from watcher to MC for
notifications.

### Build form additions
**State:** today's build form in MC submits via `RunPayload` to the v4
pipeline. Plan-v5 §7.1 called for tier dropdown + manual tasks input.
Not implemented.

**v6 work:** extend build form; add `tier` and `tasks` fields to
`RunPayload`; route to `otto v5 run` instead of `otto run` when tier is
set.

## Cleanup

### Move v4 group/contract synthesis to legacy/
**State:** `otto/spec_compile.py` group synthesis (~800 LOC),
`otto/build.py` group orchestration (~600 LOC),
`otto/build.py::detect_critical_shared_contract_violations` (~600 LOC),
`otto/repair_gates.py` (~300 LOC) all still active. Today's `otto run`
goes through them.

**v6 work:** move to `otto/legacy/`; update imports; gate behind a
`--legacy` CLI flag; document migration path. Coupled to bench results.

### Today's pipeline removal
**State:** `otto run` (v4 pipeline) and `otto v5 run` (v5 pipeline)
coexist. Plan-v5 §10 definition-of-done required `otto run` to route to
v5 by default after bench validates v5.

**v6 work:** flip the default; preserve `--legacy` for one release; then
delete.

## Operations

### Crash recovery
**State:** Otto's existing checkpoint/resume works for v4. v5's runner
writes `summary.json` per task per philosophy invariant. Resume of a v5
session was not exercised under crash conditions.

**v6 work:** kill v5_runner mid-run; verify `otto v5 run --resume <session>`
picks up the task graph and continues. Smoke test.

### Concurrency stress test
**State:** unit tests verify task_graph.json writes are atomic under 4
threads. Live LLM concurrency (2+ `otto v5 run` against same project) is
not tested.

**v6 work:** stress test 4 concurrent live runs; profile contention.

### Provider fallback under live conditions
**State:** unit-tested via mocks. Live codex 402 → claude swap is
plausible but not exercised end-to-end with real provider failures.

**v6 work:** schedule a planned codex-credit-exhaustion test.

## Bench

### 5 reference projects
**State:** plan-v5 §6.6 required bench against finance-dashboard,
microblog, ops-dashboard, acme-expense, brownfield SAML. v5 has been
live-tested on:
- ✅ CSV→JSON CLI (trivial; passed in 99s, $0.17)
- ✅ TODO web app (medium; passed in 174s, $0.50)
- ⚠️ Finance-dashboard live test: in progress / pending verification

The other 3 reference projects are pending.

**v6 work:** finish the bench matrix; publish cost / time / pass-rate.

## Documentation

### Operator runbook
**State:** docs/v5-tier-guide.md and docs/v5-verdict-reference.md exist.
docs/v5-v6-punch-list.md (this file) exists. No operator runbook for
common failures (provider auth, disk full, watcher crashed, etc.).

**v6 work:** ~200 lines of "if X happens, do Y" troubleshooting.

### Architecture deep-dive
**State:** plan-v5 + plan-v5-tabletop in `.runlogs/architecture-debate/`
serve as the design doc. No condensed "how does v5 work" reference for
new contributors.

**v6 work:** `docs/v5-architecture.md` summarizing the recursive Lead +
queue + audit-at-each-merge model in ~5 pages.
