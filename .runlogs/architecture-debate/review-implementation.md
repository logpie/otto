# Review — plan-v1.md, implementation feasibility

Scope: what a senior engineer cannot start coding from. Concrete gaps, not editorial.

---

## 1. Code-level dependencies the plan glosses over

### 1.1 Where does the Lead fit into `runner.py::run_pipeline`?

The plan never says. `run_pipeline` (otto/runner.py:131) is a fixed 7-phase chain (compile → seed → build → merge → audit → repair → render) with deeply intertwined state: `RunResult` (line 96), `shared_budget` threading (line 388), resume short-circuits (line 518), structural-block synthesis (line 520), Layer 2 repair bridge (line 582), pause/lifecycle journaling (line 432), and i2p-pointer writes (line 290, 681). The plan says `otto/lead.py` "replaces `build.py` group orchestration" but the Lead replaces *more*: build + merge + repair are all inside one Lead session. Concrete unanswered questions:

- Does `lead.py` plug in at the `_phase("build")` site (line 431) and bypass run_merge_queue + Layer 2? Or does it become a sibling of `run_pipeline` entirely?
- What happens to `shared_budget`, `resume_plan.landed_components`, `audit_resume_agent_session_id`, `layer2_resume_agent_sessions`? These are tied to Group/Feature ids that no longer exist.
- `_mark_i2p_run_active` / `_mark_i2p_run_complete` (line 1179, 1223) write `checkpoint.json`. With no spec/groups, what does spec_path/spec_hash become? Currently both are required.
- `_structural_blocked_ids` / `_audit_result_from_structural_blocks` (line 1716, 1738) read `BuildResult.blocked_ids`. Lead has no such ids. What's the new "structural blocker" signal?

### 1.2 Removing `_normalize_critical_shared_contract_scope`

`grep` confirms downstream consumers in `otto/build.py` and tests:

- `otto/build.py:678` `detect_critical_shared_contract_violations`
- `otto/build.py:691, 701, 2526` `collect_critical_shared_contract_deltas`
- `otto/build.py:3762, 3764` exports
- `tests/test_build.py:33, 625, 641, 671` direct calls

The plan deletes ~600 lines from `build.py` (4.1) but doesn't list the cascade. `tests/test_build.py` will fail import. Plan needs explicit list of test files to delete or rewrite.

### 1.3 SDK `can_use_tool` for write-time locks

The plan claims `otto/locks.py` will hook the SDK's `can_use_tool` callback to deny Write/Edit. Reading `otto/agent.py`:

- `AgentOptions.can_use_tool` (line 212) is a single `Callable[..., Any]`.
- It is ALREADY occupied by `_otto_can_use_tool_safety` (line 263). That callback only matches `Bash` (line 306) — Write/Edit are not currently inspected.
- `make_agent_options` unconditionally overwrites `can_use_tool` with the safety hook (line 262-263). The Lead must either (a) compose with the safety hook into a chain or (b) replace it. Plan doesn't say.
- The SDK callback signature is `async (tool_name, tool_input, context)` returning a `PermissionResultAllow`/`PermissionResultDeny` (lines 300-323). Locks need `agent_id` to distinguish callers — that is NOT in the signature. How does the lock know WHICH in-session subagent issued the tool call? The plan asserts `lock_paths(agent_id, globs)` but the SDK callback doesn't expose subagent identity. Resolution required before M2.
- Note also `agent.py:1605`: `can_use_tool` is dropped when `permission_mode == "bypassPermissions"`. `make_agent_options` defaults to `bypassPermissions` (line 247). This is currently overridden via `_SDKClaudeSDKClient` (line 1776) — but that branch needs verification with the new lock path.

### 1.4 Test agent — does today's audit write tests?

No. `otto/audit.py` only RUNS tests (the verifier). `otto/prompts/build.md:30-33` shows the **build** agent writes tests today. The plan says (1.3) "Build agent ... forbids touching `tests/`" and "Test agent ... writes tests + journeys." This means:

- `otto/prompts/build.md` must be cut roughly in half (steps 4, 8, 9 are test-agent-owned).
- The test agent must run AFTER the build agent commits but BEFORE audit. There is no slot for that today; merge runs after build, audit runs after merge.
- `tests/run_browser_journey.py`, `tests/browser_journeys/*` are referenced in `compile-spec.md` as build-side artifacts — these must move ownership.
- Plan does not specify: who initially scaffolds `tests/run_browser_journey.py` on a greenfield project? If build agent is forbidden, but no tests exist to extend, the test agent must create from scratch — but its prompt is described as "writes tests + journeys based on what it actually observes," not "creates the harness."

### 1.5 `otto.submit_subtask` / `otto.lock_paths` / `otto.verify` — exposure mechanism

Not specified. Options:

- SDK MCP (in-process via `create_sdk_mcp_server`) — but `feedback_inprocess_mcp.md` notes in-process MCP breaks with Agent tool. External MCP subprocess required.
- Hook-based — `can_use_tool` denial with structured error redirecting to a known tool name.
- Bash subprocess (`otto __submit_subtask ...`) — easiest, no SDK plumbing.

`grep -rn create_sdk_mcp_server otto/` returns zero hits — Otto has no MCP server today. Building one is a substantial sub-project, not a 100-LOC `otto/queue/subtask.py` (4.3 LOC estimate). Plan must pick one and own its costs.

### 1.6 Cross-task merge to `main`

`otto/merge_queue.py` (1983 lines) operates per-session: `landed_ids`, `blocked_ids` are Group ids within one BuildResult. The plan says (1.5, 4.2) it gets "rescoped to cross-task merge" — but:

- Today's queue runtime (`otto/queue/runner.py`, `otto/queue/runtime.py`) launches each task as its own process in its own worktree. There is no daemon that owns "main."
- Who runs the rebase loop in §1.5? The task's runner? A new merge-queue process? The watcher (`queue/runtime.py`, 275 lines, currently a readiness-marker library)?
- `git revert offending merge automatically` requires a single-writer. With concurrent tasks, the writer must be serialized — that's a new daemon, not in the LOC table.

---

## 2. Ordering / dependency violations between milestones

### 2.1 M1 schema changes

M1 adds `manifest_check.py` with the manifest concept and writes `tier-decision.json`. The plan claims "no architectural change yet," but:

- Does `spec.json` gain a top-level `manifest` block? Or is the manifest a sibling file? The plan never says. M1 needs the schema decision.
- `--tier` plumbing through `cli_run.py` (1669 lines) and `RunPayload` (web): grep shows `tier` does not exist anywhere in the web layer or CLI. RunPayload type is in `otto/web/i2p_routes.py` (line ~195 region) — needs a new field, default value, and migration for in-flight serialized payloads.
- M1's `tier-decision.json` is per-session, so **resume of a pre-M1 session** must tolerate its absence. `otto/resume.py:67` `ResumePlan` does not currently carry tier. Plan must specify the default-on-resume.

### 2.2 M2 + M3 cannot truly be parallel

M3 ships `otto.submit_subtask`. To do anything useful, Lead in M2 needs the tool surface or it'll have only inline + in-session subagents (which is what M2 promises). But:

- M3's queue extension also alters `QueueTask` schema (deps, group_id). M2 doesn't touch the schema. If M3 lands first, M2 is unaware. If M2 lands first, M3's schema-add is on top of an unmigrated queue. Order dependency exists.
- M2 lock semantics must support "child task with depends_on" because in-session subagents and emitted-tasks share the lock graph. The plan defers depth/cousin semantics to M4 (open question 2). M2's locks need a clear scope: single-session only? single-task only? Plan must specify before M2 starts.

### 2.3 M4 reuses `merge_queue.py` but M3 already extended it

The plan extends merge in M3 (project-level audit + auto-revert + fix-task) and again in M4 (rescope group→task). The two extensions interact: a fix-task spawned in M3 is itself a task in M4's merge queue. Plan needs to specify the order: M3 extends per-task post-merge audit + revert; M4 then *replaces* in-session merge with cross-task merge. That's a real refactor, not "rescope."

### 2.4 Bench gating bar (§6.2)

"T1 must match or beat today's pipeline" but `feedback_diagnose_from_real_logs.md`, `project_i2p_findings.md` say today's pipeline is broken on finance-true-web (the bug class M1 aims to prevent). What does "beat today's pipeline" mean concretely?

- Is the baseline today's HEAD or M1+today? The plan needs a frozen baseline tag.
- Verdict distribution comparison (6.2 metric "verdict distribution") changes if the verdict vocabulary changes between baselines (today: `pass | partial | blocked`; M2: pass/partial/unverified/merge_blocked/catastrophic). Direct compare is invalid.

---

## 3. Schema / data shape ambiguities

### 3.1 `lock_paths(agent_id, globs)` persistence

Sqlite schema not given. Required at minimum: `(agent_id TEXT, glob TEXT, expires_at TEXT, parent_agent_id TEXT, task_id TEXT)`. Indexed on glob lookups (write-path arrives as a concrete path; lock lookup is glob-match — that's NOT a sqlite-native operation, requires Python-side fnmatch over candidate rows). Performance budget per Write/Edit not stated.

### 3.2 `manifest.json` schema

Referenced in §1.7 but never defined. Needed for M1 (`manifest_check.py`):
- `modules: [{id, owned_paths: [glob], allowed_extension_paths: [glob], shared_contract: {paths: [glob], critical: bool}}]`?
- Or piggyback on existing `Spec.groups[].owned_paths` / `shared_contracts`?
- M1 says "Glob containment check for spec.json" — so M1 manifest_check operates on Spec, not a new manifest.json. M4's discovery agent emits manifest.json. Two formats, one validator? Plan must say.

### 3.3 Where verdicts live

Today: `summary.json` has `verdict`. `history.jsonl` (project-level cross-sessions, see otto/paths.py:62 + otto/queue/artifacts.py:240) has `verdict` too. Plan adds vocabulary `pass | partial | unverified | merge_blocked | catastrophic` (§3.1) and project state `green | degraded | quarantined` (§3.2). Required:

- New string values must be added to `AuditVerdict` enum in otto/audit.py:102 (currently only PASSED/PARTIAL/BLOCKED).
- Project state file path? otto/state.py is new; plan says "reads/writes per-project state file" but no path. Suggest `otto_logs/project-state.json` with explicit lock.
- Migration: existing `summary.json` writers (otto/render.py via `compose_proof_packet`) need updating.

### 3.4 `submit_subtask` response

Synchronous return: task_id only? Or block awaiting child verdict? Plan §1.2 "Lead emits a subtask, Lead exits." So fire-and-forget. Then `depends_on` must be expressible from inside the Lead's call; the Lead submitting B says `depends_on=[A_task_id]` — but if Lead emitted A in the same session, what's its id? The id is generated at enqueue time (otto/queue/ids.py `generate_task_id`). The plan needs to spell out: tool returns task_id immediately; Lead can chain by passing returned id into a subsequent submit_subtask call.

---

## 4. Behavior gaps / edge cases

- **Crash between emit and submit.** `enqueue_task` (otto/queue/enqueue.py:21) appends to queue.yml under fcntl.flock. Atomicity is guaranteed at file-write level. But if Lead calls submit_subtask, the MCP/subprocess succeeds, then Lead crashes BEFORE recording the returned task_id, you'll get duplicate tasks on resume. Plan must specify idempotency key (intent hash + parent_task_id?).
- **Test agent with no running product.** `otto.audit.py` calls `default_walkthrough_from_spec`. Test agent in plan reads "the running product." Who starts it? Today seed.py + spec's launch commands. Plan does not say; brownfield case is especially unclear.
- **Multiple Leads on same worktree.** Plan §1.1 says one task = one worktree, so this should not happen. But resume after crash: the same task resumed in a fresh process. Lock TTL says crashed locks release after 30 min. If a user resumes within 30 min, two Leads (the dead one's locks + the new one) coexist briefly. Resolution not specified.
- **Subagent failure.** "Subagent results return to Lead." SDK Agent tool returns a string. There is no failure protocol. Lead must parse the result text. Plan should commit to a structured marker (e.g., `SUBAGENT_RESULT: ok|error | detail=...`).
- **Lock TTL too short.** 30 min default. A Lead doing real work easily exceeds. "Reset on any tool use" (M2 §scope item 2) means heartbeat is implicit in tool calls. But if Lead is mid-thinking with no tool calls, TTL elapses and a sibling steals the lock. Concrete heartbeat mechanism needed; "any tool use" leaks the responsibility to the agent's pacing.
- **`state.py` API.** Plan never lists functions. Required by render, MC, runner: `read_project_state(project_dir) -> ProjectState`, `transition(project_dir, event) -> ProjectState`, `write_task_verdict(task_id, verdict)`. All needed before §3.2's state machine can be wired into MC.

---

## 5. Migration concerns

- **Test files referencing deleted code.** `tests/test_build.py` (lines 33, 625, 641, 671) imports the to-be-deleted `detect_critical_shared_contract_violations`. There are likely more — needs a comprehensive `grep` pass listed in the plan.
- **MC dashboard.** `otto/mission_control/service.py:1264` reads `cross-sessions/history.jsonl`. New verdict strings (`merge_blocked`, `unverified`, `catastrophic`) must render. Today's CSS/badge mapping likely lives in `otto/web/client/src/`. Plan §2.2 says "Verdict pill (pass/partial/...)" but doesn't enumerate the rendering changes; missing values will degrade to "unknown" in MC.
- **`resume.py` legacy branching.** Plan §4.5 says "old sessions resume in legacy mode (T2 default)." `otto/resume.py:67` `ResumePlan` is built by `build_resume_plan` (line 164). The branch decision must happen BEFORE `run_pipeline` is called. But `run_pipeline` is also the new Lead entry. So either resume short-circuits to the OLD pipeline (keep legacy intact), or Lead is taught to read legacy ResumePlan. Plan picks neither.
- **`history.jsonl` row schema.** Plan adds tier, mode, project_state. Today's row writers (otto/queue/artifacts.py:240, otto/runs/atomic_repair.py:21) must learn new fields. Old rows lack them — readers must defend with `.get(key, default)`. Mention nowhere.

---

## 6. "Open questions" that block specific milestones

- **Subagent budget slicing (§7.1, "by M5") blocks M2.** M2 ships subagent dispatch with no per-call budget. A runaway subagent burns the whole task budget. Today's `shared_budget.charge_cost` is post-hoc. Concrete pre-flight check is required *in M2*; deferring to M5 means M2 ships with a known runaway-cost class.
- **Recursive depth & lock semantics (§7.2, "before M4") blocks M2.** M2 ships locks. The plan says default depth = 2. With depth 2, Lead has children; children have grandchildren. M2 must commit to one of: (a) only the Lead can take locks; (b) every level can take locks but lock checks walk parent chain. Without that, M2's `agent_id` field design is undefined.
- **Test agent flaky DOM (§7.4, "by M5") blocks M2.** M2 ships test agent. Without a fallback, every flake = `unverified`. M2 needs at minimum: bounded selector retries inside test agent prompt, and an explicit fallback verdict.

---

## 7. Concrete questions a senior engineer hits hour 1

1. Is the new Lead the entrypoint, or is `run_pipeline` the entrypoint and Lead replaces just the build phase?
2. What's the exact entry-call signature: `await run_lead(task: QueueTask, project_dir, session_dir, config) -> RunResult`? Or a new result type?
3. Where does the spec live now? Is `spec/spec.json` written anymore? `otto/web/spec_review_routes.py`, `otto/runner.py:826`, `_set_lifecycle_best_effort` all read/write it.
4. Do `compile_spec`, `seed_fixtures`, `run_audit` keep their current signatures? If audit consumes a spec but Lead never compiles one, what does audit walk against?
5. `otto.yaml`'s `agents.{build,certifier,spec,fix}` block (otto/agent.py:231) — does T1 add `agents.lead`, `agents.test`, `agents.discovery`? Or reuse build/spec slots?
6. SDK `can_use_tool` collision — replace, chain, or split-by-tool? See agent.py:262.
7. How does the Lead surface `submit_subtask` to itself? MCP server? Bash shim? Decision drives ~3 days of plumbing.
8. Lock sqlite location — per-worktree (says plan) but worktrees nest, and a Lead can spawn an emitted-task that gets its OWN worktree. Whose locks does the child consult?
9. Project state file lock — how is `green → degraded` transition serialized when N tasks finish concurrently?
10. `--max-depth` enforcement — runtime counter passed via env var? Inherited via `OTTO_DEPTH`? Plan says nothing.
11. How does `otto run --resume` without a session id pick a session when M2 introduces task-level sessions distinct from project-level?
12. Does the queue runner (`otto/queue/runner.py:1539`) still own scheduling, or does the Lead-emit path bypass it for child tasks?

---

End. The plan describes intent well but is roughly half a design doc — the wire-protocol layer (SDK hooks, MCP exposure, sqlite schema, runner integration point, resume branching, queue schema additions, MC vocabulary mapping) is not implementable as written. Before M1 starts: pick the entrypoint, the SDK lock mechanism, and the manifest format; rewrite §4.1/§4.3 with explicit signature stubs and exhaustive deletion lists.
