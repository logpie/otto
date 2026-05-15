# Proposal A — Dynamic, Agent-Driven Decomposition

## Thesis

The lead Claude agent is the architect. Otto provides workspace mechanics, tools, verification, repair coordination. Decomposition is a runtime decision the lead makes and revises — never frozen up-front from intent text.

The finance-dashboard run failed because `otto/spec_compile.py` produced a static decomposition (foundation owns `tests/run_browser_journey.py` + `tests/browser/test_shell.*`; each downstream group owns `tests/browser/test_<feature>.*`) and `otto/build.py::detect_scope_violations` policed agents against it — 11 `group.attempt.failed` + 6 `scope.critical` events before the watcher killed the run at 36m54s. The fix is not better validators. The fix is to delete the up-front commitment.

---

## 1. Pipeline Diagram

```
intent.md ──► otto/runner.py (worktree, locks, browser, verifier setup)
                │
                ▼
          LEAD AGENT (Claude, full Agent SDK)
            tools: Read/Write/Edit/Bash/Glob/Grep/Agent/TeamCreate
                   + otto.verify, otto.checkpoint, otto.lock_paths
                │
        ┌───────┼─────────────┐  (runtime decision; may not happen)
        ▼       ▼             ▼
     SUB-LEAD  SUB-LEAD     WORKER  (own worktree; recursive to depth cap)
                │
                ▼
        otto.verify ──► proof-packet.{html,json} ──► product
```

No `spec_compile` in the hot path. No merge_queue. The lead owns sequencing.

---

## 2. User Controls

CLI flags on `otto run` (all mirrored as `defaults:` in `otto.yaml`):

| Flag | Effect |
|---|---|
| `--mode autopilot` (default) | Lead picks decomposition, depth, parallelism within caps. No pauses. |
| `--mode supervised` | Lead must `otto.checkpoint(reason)` before spawning subagents, merging, finalizing. MC shows plan diff and awaits approval. |
| `--mode solo` | `Agent`/`TeamCreate` removed from tool list. Single-agent build. |
| `--max-parallel N` (default 4) | Hard cap on concurrent sub-agents at any depth. Enforced by `otto.lock_paths` refusing further worktree allocations. |
| `--decomp-depth N` (default 2) | Sub-leads allowed to depth N. At cap, `Agent` returns "depth exhausted, do it yourself." |
| `--no-decompose` | Alias for `--mode solo`. |
| `--budget-usd N` / `--budget-min N` | Otto injects a finalize-now tool result on the lead's next turn when crossed. |
| `--review-gate spec\|plan\|merge\|none` | When MC opens review threads. Default `plan` in supervised, `none` in autopilot. |

---

## 3. Scenario Walkthroughs

**Small (1-page todo app).** Lead reads intent, writes `App.tsx + storage.ts + a test` serially, calls `otto.verify` (browser harness loads page, ticks a todo, reloads, asserts persistence). No `Agent` calls. With `--mode solo` the same path runs with the Agent tool removed from its tool list.

**Medium (finance dashboard).** Lead reads intent, sees 6 product surfaces. It builds the shared store (`financeStore.ts`) and routing shell synchronously — the lead correctly recognizes this must exist before any feature works (precisely what the static spec got wrong). Then it `TeamCreate`s one team and dispatches 3 parallel workers via `Agent`: `transactions`, `insights+budgets` (coupled via category aggregation), `csv-io`. The lead — not Otto — declares each worker's writable paths in its prompt and acquires them via `otto.lock_paths(worker_id, [...])`. When `insights` needs a new `financeStore` selector, it returns "needs upstream" instead of touching foundation; the lead applies the change and re-dispatches. After all return, the lead runs the full browser journey. Parallelism: 3 wide, depth 1.

**Large (browser engine / IDE).** Lead gets `--decomp-depth 4`. Top-level lead splits into rendering / networking / JS-runtime sub-leads, each itself a Claude agent with `Agent`/`TeamCreate` access, recursing one or two more levels. `otto.lock_paths` is the global concurrency arbiter — locks are namespaced by glob and refcounted, so a sub-tree owns a subtree without Otto knowing the schema. Verification is hierarchical: each sub-lead runs its sub-tree's tests + integration probe; root lead runs full system test once.

**Brownfield (100k LOC).** `otto run --intent "add SAML login"`. Lead's first action is `Glob`+`Grep` to map auth code (work currently done by `spec_compile.py` brownfield prompts; we move it into the lead). It decides this is small-surface and runs solo. For "add multi-tenant" the same lead might decompose by layer (data model, middleware, UI) with `lock_paths` on directories it already mapped. Ownership comes from observation of the real tree, not from imagining one from intent text.

---

## 4. What Dies / What Stays

**Deleted.**
- `otto/spec_compile.py` (5190 lines), `otto/spec_amend.py`, `otto/spec_warnings.py`, `otto/spec_state.py`, `otto/spec_schemas/` — no spec object to compile, validate, or amend.
- `otto/merge_queue.py` (1983 lines) — no frozen groups to FIFO-merge. Lead merges as it goes.
- `otto/build.py::detect_scope_violations` + `detect_dependency_scope_extensions` + `FAILED_SCOPE` (~600 lines) — replaced by lock-based prevention.
- `otto/repair_gates.py` — group-level repair gating.
- Prompts: `compile-spec*.md`, `build-agent-static-policy.md`, `build-merge-repair.md`.

**Gutted but retained.**
- `otto/build.py` shrinks to a "drive lead, stream events, capture cost" loop. Group/Slice dataclasses go.
- `otto/runner.py` becomes setup → lead → verify → render. No spec-gate, no merge stage.
- `otto/audit.py` becomes the verifier shim that renders `proof-packet.{html,json}` from the lead's verify calls.
- `otto/prompts/build.md` rewritten (§7).

**Unchanged.** `otto/worktree.py`, `branching.py`, `checkpoint.py`, `resume.py`, `budget.py`, `token_usage.py`, `observability.py`, `journal.py`, `logstream.py`, `redaction.py`, `paths.py`, `browser_testing.py`, `testing.py`, `certifier/*` (now exposed as `otto.verify`), `mission_control/`, `web/`, `cli_*.py`, `queue/`, `runs/`, `history.py`.

---

## 5. Verification Without External Scope Enforcement

Three mechanisms keep the lead honest:

1. **Lock-based prevention, not detection.** `otto.lock_paths(agent_id, globs)` writes a row into a worktree-scoped sqlite lockfile. Sub-agents' `Write`/`Edit` tools are wrapped to fail-fast if the path matches another agent's lock. This is what `detect_scope_violations` should have been: enforced at the file-write boundary, not as a post-commit audit.

2. **`otto.verify` is the only PASS gate.** Lifted from `otto/certifier/*` + `otto/browser_testing.py`. Takes behavioral claims and runs them with browser/CLI/HTTP probes plus screenshot/video evidence. Returns structured PASS/FAIL. The lead cannot fake this — the harness produces the evidence.

3. **Repo-state diff probes.** After each sub-agent returns, the wrapper diffs the worktree and refuses commit if files outside its declared locks were modified. The only "scope check" left, enforced against the lead's own declarations.

---

## 6. Failure Modes

**Lead context overflow.** Claude Code preset handles native compaction. `otto/checkpoint.py` snapshots the full message stream after every tool call so a fresh lead resumes mid-build with the journal as input. Depth and parallel caps bound tool fanout.

**Sub-agent failure cascades.** Sub-agents return exit codes; the lead decides retry vs. re-decompose vs. abort. Otto does not auto-retry. A crashing sub-agent releases its locks via the wrapper's `finally`.

**Concurrent conflict on shared file.** Cannot happen at write time — the lock wrapper fails the second writer with "agent X holds lock on path P." The lead must serialize.

**Cost overrun.** `otto/budget.py` polls cost; at threshold it injects a tool result on the lead's next turn: "BUDGET_EXCEEDED — finalize." Sub-agents inherit a budget slice from the `Agent` call.

**Partial product on hard failure.** `otto.verify` always runs against whatever is on disk, even on lead crash. Proof packet renders with `verdict: partial` listing what worked.

**Resume after crash.** `otto/resume.py` resumes the lead's message stream from the last checkpoint. Locks reconstruct from the sqlite file in the worktree.

---

## 7. Build Prompt Sketch (replaces `otto/prompts/build.md`)

```
You are the lead engineer. You own the architecture. Intent is in
./intent.md. Worktree root is your CWD.

Work alone or with subagents. Choose based on actual surface area you
discover, not on intent text alone.

TOOLS BEYOND CLAUDE CODE DEFAULTS
- otto.lock_paths(agent_id, globs[]) — call before dispatching a writing
  subagent. Conflicts return error; serialize.
- otto.verify(claims[]) — runs browser/CLI/HTTP probes, captures
  screenshots/video, returns PASS/FAIL. Your only quality gate.
- otto.checkpoint(reason) — required in supervised mode before spawning
  subagents, merging, or finalizing.
- Agent / TeamCreate — Claude-native. Subagents inherit a budget slice.
  Depth cap: {decomp_depth}. Parallel cap: {max_parallel}.

SUBAGENT CONTRACT
You write each subagent's prompt. It MUST list:
- exact writable paths (you must hold those locks)
- behaviors the subagent must verify before returning
- upstream interfaces it depends on, with file:symbol refs
A subagent that needs upstream changes returns "needs upstream: <desc>"
rather than touching upstream code. You apply the change, re-dispatch.

PROCESS
1. Read intent. Glob/Grep the tree if any.
2. Sketch architecture. If surface is small or coupling is high, solo.
   Do not decompose for its own sake.
3. Build. Re-decompose mid-flight if the work shape changes.
4. otto.verify. Iterate until PASS or budget exhausted.
5. Write product-handoff.json.

HARD RULES
- No fake evidence. otto.verify is the only PASS signal.
- No silent scope cuts. Dropped features go in product-handoff.json
  under "deferred" with reason.
- Honor your own locks. The wrapper rejects violations regardless.
- On budget pressure: finalize most-complete slice, no new subagents.
```

---

## 8. What I Am Not Confident About

- **The lead is now load-bearing for correctness.** Current Otto compensates for weak planning with rigid validators. A bad lead model regresses harder. Bench must confirm dynamic decomposition is at least as good as static across the i2p suite, not just on finance where static is known broken.
- **Cross-worktree shared dependencies.** `shared_contracts` handled lockfiles/generated code crudely but explicitly. The lead now discovers those couplings on its own. Greenfield TS projects with `pnpm-lock.yaml` are a known sharp edge.
- **Recursive sub-leads at depth 3+ are unverified.** The OS-scale claim is theoretical. No bench evidence Claude reliably plans 3-deep architectures.
- **Resume semantics for in-flight subagent trees.** A crash mid-fanout leaves orphaned worktrees + locks. Recovery contract (kill all descendants? resume each?) is undecided.
- **Supervised mode UX.** Showing a plan diff for a mutating tree is harder than showing one frozen spec. MC needs visualizations we have not designed.
- **Cost variance.** Dynamic decomposition is less predictable than static. Budget guardrails matter more; we have less data on where the lead over-fans.
