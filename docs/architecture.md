# Otto Architecture

## Overview

Otto is ~11,000 lines of Python. It builds, certifies, and improves software
products using LLM agents — and runs many such jobs in parallel via a queue +
merge subsystem.

```
otto build "bookmark manager"          # build + certify + fix
otto certify                           # standalone verification
otto improve bugs                      # find and fix bugs
otto improve feature "search UX"       # suggest and implement improvements
otto improve target "latency < 100ms"  # optimize toward a metric
otto queue build/improve/certify ...   # enqueue parallel tasks (each in own worktree)
otto queue run                         # foreground watcher, dispatches up to N at a time
otto merge --all                       # land done branches into target (with conflict agent + post-merge certify)
```

Two subsystems built on top of the core build/certify/improve flows:

- **Queue** (`otto/queue/`): persistent, file-backed task list (`.otto-queue.yml`)
  + foreground watcher that spawns one `otto` subprocess per task into its own
  git worktree on its own branch. Crash-safe via PID-reuse-safe child tracking.
- **Merge** (`otto/merge/`): Python-driven `git merge --no-ff` orchestrator.
  Clean merges burn $0 (no LLM). When git can't auto-merge, otto commits
  marker-laden merges to preserve history, then invokes ONE agent session
  with full project context (Bash + test command + cross-branch context)
  to resolve every conflict globally. The agent self-corrects within its
  session via test-driven feedback. After merging, a triage agent emits a
  verification plan and the certifier re-runs the must-verify subset.

## System Diagram

```
┌─────────────────────────────────────────────────────┐
│                     CLI Layer                        │
│  cli.py (build, certify)  cli_improve.py (improve)  │
└──────────┬─────────────────────┬────────────────────┘
           │                     │
     ┌─────▼──────┐    ┌────────▼─────────┐
     │ Agent Mode │    │   Split Mode     │
     │ (default)  │    │   (--split)      │
     └─────┬──────┘    └────────┬─────────┘
           │                     │
     ┌─────▼──────────┐  ┌──────▼───────────────┐
     │ build_agentic  │  │ run_certify_fix_loop  │
     │ _v3()          │  │                       │
     │                │  │  ┌──► certify ──┐     │
     │  One agent     │  │  │              │     │
     │  session:      │  │  │  if fail:    │     │
     │  build/certify │  │  │   ┌──────┐   │     │
     │  /fix loop     │  │  └──┤ fix  ├───┘     │
     │                │  │     └──────┘          │
     └────────┬───────┘  └──────────┬────────────┘
              │                      │
        ┌─────▼──────────────────────▼─────┐
        │        Agent SDK Layer           │
        │  run_agent_with_timeout()        │
        │  • live logging                  │
        │  • timeout + orphan cleanup      │
        │  • retry on error                │
        │  • session_id for resume         │
        └─────────────┬───────────────────┘
                      │
        ┌─────────────▼───────────────────┐
        │     Claude Code / Codex CLI      │
        └──────────────────────────────────┘
```

## Build Flow (Agent Mode)

```
otto build "bookmark manager with tags"
│
├─ Load build.md prompt (explore → build → test → certify → fix → report)
├─ Pre-fill certifier prompt (certifier-thorough.md with {intent})
├─ Inject cross-run memory (if enabled)
│
└─ Single agent session ──────────────────────────────────────┐
     │                                                         │
     ├─ 1. Explore project, plan architecture                  │
     ├─ 2. Build code, write tests, commit                     │
     ├─ 3. Dispatch certifier subagent ───┐                    │
     │                                     │                    │
     │    Certifier (builder-blind):       │                    │
     │    ├─ Read project fresh            │                    │
     │    ├─ Install deps, start app       │                    │
     │    ├─ Test 5-10 user stories        │                    │
     │    └─ Report: PASS/FAIL per story   │                    │
     │                                     │                    │
     ├─ 4. Read findings ◄────────────────┘                    │
     ├─ 5. If FAIL: fix code, commit, re-dispatch certifier    │
     ├─ 6. Repeat until two consecutive PASSes                 │
     └─ 7. Report structured markers ─────────────────────────┘
                    │
                    ▼
        markers.py: parse STORY_RESULT, VERDICT, CERTIFY_ROUND
                    │
                    ▼
        Write: agent.log, proof-of-work.{json,html}, checkpoint.json
```

## Improve Flow (Split Mode)

```
otto improve bugs "error handling" --split -n 5
│
├─ Create improvement branch (improve/2026-04-17)
├─ Load config, set max_rounds=5
│
└─ Python-driven loop ──────────────────────────────────────┐
     │                                                       │
     │  ┌─── Round 1 ────────────────────────────────────┐   │
     │  │                                                 │   │
     │  │  Certify: fresh certifier agent session         │   │
     │  │  ├─ Load certifier-thorough.md                  │   │
     │  │  ├─ Test product, report findings               │   │
     │  │  └─ Parse results (markers.py)                  │   │
     │  │                                                 │   │
     │  │  If FAIL:                                       │   │
     │  │  Fix: fresh code agent session                  │   │
     │  │  ├─ Load code.md                                │   │
     │  │  ├─ Inject failures + previous attempts         │   │
     │  │  └─ Fix code, commit                            │   │
     │  │                                                 │   │
     │  │  Write checkpoint ──► checkpoint.json            │   │
     │  │  Write journal ──► build-journal.md              │   │
     │  │                                                 │   │
     │  └─────────────────────────────────────────────────┘   │
     │                                                       │
     │  Round 2, 3, ... (until PASS or max_rounds)           │
     └───────────────────────────────────────────────────────┘
```

## Improve Flow (Agent Mode — Default)

```
otto improve bugs "error handling" -n 5
│
├─ Create improvement branch
│
└─ Single agent session (improve.md prompt) ─────────────────┐
     │                                                        │
     ├─ 1. Explore project                                    │
     ├─ 2. Dispatch certifier subagent                        │
     ├─ 3. Read findings, fix, re-dispatch                    │
     ├─ 4. Repeat until two consecutive PASSes                │
     └─ 5. Report markers ───────────────────────────────────┘
                    │
     Agent IS the memory (single session, auto-compact)
```

## Queue Subsystem

```
                         ┌────────────────────┐
        otto queue build │   otto queue ls    │  otto queue rm
        otto queue improve│   otto queue show  │  otto queue cancel
        otto queue certify│                    │  otto queue cleanup
                         └─────────┬──────────┘
                                   │  (CLI commands write to file)
                                   ▼
              ┌────────────────────────────────────┐
              │  .otto-queue.yml         (queue)   │
              │  .otto-queue-state.json  (state)   │
              │  .otto-queue-commands.jsonl (cmds) │
              └────────┬───────────────────────────┘
                       │  (read by watcher every poll)
                       ▼
              ┌─────────────────────────┐
              │  otto queue run         │  ← foreground process (tmux pane)
              │  Runner._tick():        │
              │    drain_commands()     │
              │    load_queue()         │
              │    apply_command(...)   │
              │    enforce_timeouts()   │
              │    reap_children()      │
              │    dispatch_new()       │
              └────────┬────────────────┘
                       │  (Popen with PGID for safe cleanup)
              ┌────────▼─────────────────────────────────────┐
              │  Spawned otto subprocesses (concurrent ≤ N)  │
              │                                              │
              │  .worktrees/csv-export/    build/csv-...     │
              │  .worktrees/redesign/      build/redesign... │
              │  .worktrees/improve-bugs/  improve/...       │
              │                                              │
              │  Each writes:                                │
              │    otto_logs/builds/<id>/...                 │
              │    otto_logs/queue/<task-id>/manifest.json   │
              └──────────────────────────────────────────────┘
```

**Key invariants:**

- `.otto-queue.yml` is the only source of truth for what's enqueued.
  `state.json` tracks per-task status and child metadata (pid, pgid,
  start_time_ns, argv, cwd) so PID-reuse can't cause us to signal an
  unrelated process.
- Commands (`rm`, `cancel`) go through `.otto-queue-commands.jsonl` rather
  than mutating state directly — the watcher is the single writer.
- Each task gets its own git worktree at `.worktrees/<task-id>/` and its own
  branch (`build/<slug>-<date>` or `improve/...`). Bookkeeping files
  (`intent.md`, `otto.yaml`) are NOT committed to task branches —
  they're shared project state, not per-task work.
- Per-task wall-clock timeout (`queue.task_timeout_s`, default 1800s)
  SIGTERMs hung children so they free their concurrency slot.
- On watcher restart, in-flight tasks are either resumed (default,
  `on_watcher_restart: resume`) by reconciling state.json against live PIDs
  or marked failed (`fail`).

## Merge Subsystem

```
                  otto merge --all        otto merge build/x build/y
                  otto merge --target     otto merge --fast / --no-certify
                                ▼
                    ┌──────────────────────┐
                    │ orchestrator.run_merge│
                    └──────────┬───────────┘
                               ▼
              ┌─────────────────────────────────────┐
              │ Phase 1: sequential `git merge`     │
              │   for each branch:                  │
              │     git merge --no-ff               │
              │     if clean → outcome=merged       │
              │     else if --fast → bail           │
              │     else:                           │
              │       capture conflict diff (--merge)│
              │       capture raw file snapshots     │
              │       git add + git commit           │
              │       outcome=merged_with_markers    │
              └────────────────┬─────────────────────┘
                               ▼
              ┌─────────────────────────────────────┐
              │ Phase 2: ONE agent session          │
              │   prompt = all branches' intents +  │
              │            stories + diff + test cmd│
              │   tools  = Bash, Read, Edit, Write, │
              │            MultiEdit, Grep, Glob    │
              │   loop   = test-driven retry inside │
              │            the agent's session      │
              └────────────────┬─────────────────────┘
                               ▼
              ┌─────────────────────────────────────┐
              │ Phase 3: orchestrator validation    │
              │   (validate_post_agent — see below) │
              │   on fail → bail, user resolves     │
              │   on pass → git add + commit        │
              │             outcome=conflict_resolved│
              └────────────────┬─────────────────────┘
                               ▼
              ┌─────────────────────────────────────┐
              │ Phase 4: triage + cert               │
              │   collect_stories_from_branches      │
              │   triage_agent: must_verify /        │
              │                  skip_likely_safe /  │
              │                  flag_for_human      │
              │   certifier on must_verify subset    │
              │   (--no-certify skips this phase)    │
              └─────────────────────────────────────┘
```

**Validation guarantees** (`conflict_agent.validate_post_agent`):

1. Out-of-scope edits — `post_diff − pre_diff ⊆ expected_uu_files`
2. No new untracked files (build artifacts caught here; otto's
   `setup_gitignore.py` adds common patterns to keep this from tripping)
3. No conflict markers remain — direct content scan of `expected_uu_files`
   (markers live in committed files where `git diff --check` is blind);
   ANY column-zero `<<<<<<<` / `=======` / `>>>>>>>` line fails closed,
   including partial / mangled marker remnants
4. HEAD unchanged (agent didn't `commit` or `reset`)

**Branch outcome statuses** (`merge/state.py`):

| Status | Meaning |
|---|---|
| `merged` | Clean `git merge --no-ff` |
| `merged_with_markers` | Marker-laden merge commit (phase 1) |
| `conflict_resolved` | Agent resolved all phase-1 markers (phase 3) |
| `agent_giveup` | Agent failed validation, or post-stage git failure |
| `skipped` | Skipped per orchestrator policy |
| `pending` | Pre-flight state; should never appear in final state |

**Why one agent session, no orchestrator-level retry.** Test-driven retry
inside the agent's session is more powerful than re-rolling at the orchestrator
layer: the agent runs the project's test command, cross-references branches via
`git diff` / `git show`, and iterates until tests pass. P6 bench measured this
at 18min / $5.12 / 4 files vs 37min / $7.52 / 2 files for the prior per-conflict
approach — 2.1× faster, 32% cheaper, more files resolved cleanly.

**Codex provider rejected.** Codex does not reliably honor tool restrictions,
which would break the orchestrator's safety model for conflict resolution.
Conflict resolution requires `provider: claude` in `otto.yaml`.

## Certifier Modes

```
                    ┌─────────────┐
                    │   certify   │
                    └──────┬──────┘
                           │
         ┌─────────┬───────┼────────┬──────────┐
         ▼         ▼       ▼        ▼          ▼
     ┌───────┐ ┌───────┐ ┌──────┐ ┌────────┐ ┌───────┐
     │ fast  │ │ std   │ │ thor │ │ hill   │ │target │
     │       │ │       │ │ ough │ │ climb  │ │       │
     └───┬───┘ └───┬───┘ └──┬───┘ └───┬────┘ └───┬───┘
         │         │        │         │           │
      3-5 happy  full     adver-   suggest    measure
      paths,     verify   sarial,  features,  metric,
      inline,    + sub-   edge     UX gaps    require
      ~30s       agents   cases               METRIC_MET
                 ~2min    ~5min    ~3min      ~2min
```

## Checkpoint & Resume

Both modes write `otto_logs/checkpoint.json`. Agent mode uses `session_id`
for SDK-level resume; split mode replays from the last completed round.

```
otto improve bugs --split -n 50
│
├─ Pre-write ──► checkpoint.json {status:in_progress, phase:initial_build}
├─ Initial build (if not resuming)
│
├─ Round 1 starts ──► checkpoint {phase:certify, current_round:0}   (last COMPLETED = 0)
│   ├─ Certify
│   ├─ Fix (on FAIL)
│   └─ Round complete ──► checkpoint {phase:round_complete, current_round:1}
│
├─ Round 2 starts ──► checkpoint {phase:certify, current_round:1}
│   ├─ Certify
│   ├─ [CRASH or Ctrl+C]
│   └─ checkpoint {status:paused, phase:certify, current_round:1}
│
└─ otto improve bugs --split --resume
    └─ resolve_resume reads checkpoint → start_round = current_round + 1 = 2
       └─ Replays round 2's certify phase (not round 3 — the crashed phase
          didn't complete, so we don't skip it).
```

**Key invariants:**
- `current_round` = last FULLY completed round (start with 0, advance only
  after the round's fix phase finishes).
- `phase` distinguishes where a crash happened. `phase=""` (old checkpoints)
  is treated as "unknown — don't skip the initial build."
- Checkpoint writes are atomic: `checkpoint.json.tmp` + `os.replace()` so a
  concurrent reader never sees a half-written file. Read path never touches
  `.tmp` — it belongs to an in-flight writer.
- Agent mode writes an `in_progress` checkpoint BEFORE calling the SDK so a
  crash mid-session still has a resumable marker (session_id may be empty
  until the agent returns).
- Inner `build_agentic_v3` calls from `run_certify_fix_loop` pass
  `manage_checkpoint=False` so they don't stomp the outer loop's checkpoint.
- Command attribution is fine-grained: checkpoints record
  `improve.bugs`/`.feature`/`.target` not just `improve`, so a mismatch
  warning fires if you resume under a different subcommand.
- `otto improve target --resume` inherits the goal from the checkpoint and
  hard-fails if the prior run wasn't `improve.target`.

Error retry: certifier/build retried up to 2x on failure before moving on.

## Prompts

All prompts are markdown files — edit without touching Python.

```
otto/prompts/
  build.md                  Agent-mode build (explore→build→certify→fix loop)
  improve.md                Agent-mode improve (certify→fix loop, no build step)
  code.md                   Code-only (split-mode fix agent, no cert knowledge)
  certifier.md              Standard verification (subagents, screenshots)
  certifier-fast.md         Happy path smoke test (inline, ~30s)
  certifier-thorough.md     Adversarial (edge cases, code review)
  certifier-hillclimb.md    Product improvements (missing features, UX)
  certifier-target.md       Metric measurement (METRIC_VALUE/METRIC_MET)
  spec-light.md             Spec-gate generator (run_spec_agent)
  merger-conflict-agentic.md   Consolidated conflict resolver (single agent session)
  merger-triage.md          Post-merge verification-plan generator
```

## Key Modules

Run `find otto/ -name "*.py" -not -path "*__pycache__*" | xargs wc -l | sort -rn | head -20` for current LOC; the list below is a stable inventory by purpose, not a leaderboard.

| Module | Purpose |
|--------|---------|
| `pipeline.py` | `build_agentic_v3`, `run_certify_fix_loop`, PoW reports |
| `agent.py` | SDK abstraction, `run_agent_with_timeout`, provider switching |
| `cli.py` | `build`, `certify` commands |
| `cli_improve.py` | `improve` command group (bugs/feature/target), exit-code wiring |
| `cli_queue.py` | `queue` command group (build, improve, certify, run, ls, show, rm, cancel, cleanup) |
| `cli_merge.py` | `merge` command (and per-merge logging setup) |
| `certifier/__init__.py` | Standalone certifier, PoW generation, target-mode gate |
| `config.py` | Config loading, queue-section validation, auto-detection |
| `journal.py` | Build journal: round tracking, current state |
| `markers.py` | Parse STORY_RESULT/VERDICT/METRIC_MET from agent output |
| `checkpoint.py` | Atomic checkpoint read/write/clear, `resolve_resume`, `ResumeState` |
| `memory.py` | Cross-run certifier memory (opt-in) |
| `setup_gitignore.py` | Auto-managed `.gitignore` (queue runtime + common build artifacts) |
| `setup_gitattributes.py` | Auto-managed `.gitattributes` (merge=union for `intent.md`, merge=ours for `otto.yaml`) |
| `manifest.py` | Per-run manifest contract (queue or atomic mode) |
| `branching.py` | Slug + branch-name policy; `ensure_branch_for_atomic_command` |
| `worktree.py` | `git worktree add/remove` wrappers used by the queue runner |
| `queue/schema.py` | `.otto-queue.yml` + `state.json` + commands.jsonl read/write (atomic) |
| `queue/runner.py` | Foreground watcher: spawn / reap / cancel / timeout / lock |
| `queue/ids.py` | Slug-to-id rules, branch + worktree path generation |
| `merge/orchestrator.py` | `run_merge`, consolidated merge driver, post-merge verification |
| `merge/git_ops.py` | Thin git wrappers — `merge_no_ff`, `conflicted_files`, `diff_check`, … |
| `merge/conflict_agent.py` | Consolidated LLM resolver + `validate_post_agent` + `_files_with_markers` |
| `merge/triage_agent.py` | Post-merge verification-plan generator (must_verify / skip / flag) |
| `merge/stories.py` | Collect stories from merged branches (queue manifests + build manifests) |
| `merge/state.py` | `BranchOutcome`, per-merge `state.json`, status `Literal` |

## Error Handling

Centralized in `run_agent_with_timeout()`:
- **Timeout**: Derived from `RunBudget.for_call()` — the one timeout knob is
  `run_budget_seconds` (default 3600s), a wall-clock cap on the whole
  `otto build` / `otto certify` / `otto improve` invocation. Per-call timeouts
  shrink naturally as budget drains. `spec_timeout` (default 600s) is the
  only per-phase cap, applied inline in the spec agent call as
  `min(budget.remaining, spec_timeout)`. Orphan processes cleaned up.
- **Agent crash**: `AgentCallError` raised with preserved `session_id` from
  streaming state so `--resume` can continue the SDK conversation. Callers
  retry up to 2x for transient errors (not budget exhaustion).
- **No output**: No verdict markers → treated as FAIL.
- **KeyboardInterrupt**: Checkpoint written (status=paused, current phase
  recorded), re-raised.
- **Budget exhaustion**: Either pre-call (budget.exhausted()) or mid-call
  (AgentCallError from asyncio timeout) → `status=paused` checkpoint, exits
  non-zero. `otto build --resume` picks up from the recorded phase.

**Target-mode semantics:**
Target mode is invoked via `certifier_mode="target"` or
`config["_target"]`. The gate is strict: `result.passed is True` requires
BOTH story-level success AND `METRIC_MET: YES` from the certifier. A
target run where stories pass but the certifier omits `METRIC_MET:`
fails — in split mode with a fail-fast journal entry
(`"FAIL (certifier omitted METRIC_MET)"`) so the fix loop doesn't waste
a round on random guessing. Non-target modes (bugs/feature) never
consult `metric_met`.

`otto improve` exits non-zero on failure (matching `otto build`), so
CI wrappers can detect when the run didn't reach its goal.

## Observability

| Question | Where to look |
|----------|---------------|
| What was built/fixed? | `otto_logs/builds/<id>/agent.log` |
| Full agent trace? | `agent-raw.log` |
| Live tool calls? | `live.log` (timestamped) |
| Certifier results? | `otto_logs/certifier/*/proof-of-work.{json,html}` |
| Build history? | `otto history` or `run-history.jsonl` |
| Improve progress? | `build-journal.md`, `checkpoint.json` |
| Cost? | `checkpoint.json` → `total_cost` |
| Queue task status? | `otto queue ls` / `otto queue show <id>` / `.otto-queue-state.json` |
| Why didn't a queue task start? | `otto_logs/queue/watcher.log` + watcher stdout (spawn / reap / timeout / cancel events) |
| Per-task manifest? | `otto_logs/queue/<task-id>/manifest.json` |
| Merge orchestrator events? | `otto_logs/merge/merge.log` |
| Per-merge state + outcomes? | `otto_logs/merge/<merge-id>/state.json` |

## Data Flow

```
User intent
    │
    ▼
┌─────────┐     ┌──────────┐     ┌──────────┐
│  Build  │────▶│ Certify  │────▶│  Parse   │
│  Agent  │     │  Agent   │     │ Markers  │
└─────────┘     └──────────┘     └────┬─────┘
                                      │
              ┌───────────────────────┤
              │                       │
        ┌─────▼─────┐          ┌─────▼─────┐
        │   Logs    │          │  Reports  │
        │           │          │           │
        │ agent.log │          │ PoW.html  │
        │ live.log  │          │ PoW.json  │
        │ history   │          │ journal   │
        └───────────┘          └───────────┘
```
