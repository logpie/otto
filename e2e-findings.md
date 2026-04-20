# E2E Findings — `otto queue` + `otto merge`

Append-only log. Each finding has: scenario → symptom → diagnosis → fix
(commit ref) → verification.

## Format

```
### [F#] <short title> — scenario <id>

**Symptom**: what failed
**Diagnosis**: root cause
**Fix**: what changed; commit <sha>
**Verification**: how I confirmed it
**Observability improvement**: any new log line / instrumentation
```

---

### [F1] Manifest path mismatch between watcher and spawned otto — pre-flight code read

**Symptom**: Code review of `_finalize_task_from_manifest` and `manifest_path_for`
shows the watcher reads `<main_project>/otto_logs/queue/<task-id>/manifest.json`
but the spawned `otto build` runs with `cwd=<worktree>` and writes its
manifest to `<worktree>/otto_logs/queue/<task-id>/manifest.json`. In real
queue runs, the watcher would never find the manifest and every task would
be marked failed with `exited 0 but no manifest written`.

**Diagnosis**: `otto/cli.py` line 698: `project_dir = Path.cwd()`. When
spawned by the queue runner, cwd is the worktree, not the main project.
`write_manifest` uses that as project_dir and writes inside the worktree.
The watcher's `_finalize_task_from_manifest` uses `self.project_dir` (the
main project) → mismatch.

The existing test fixture in `tests/test_queue_runner.py` masks this by
setting `OTTO_PROJECT_DIR` in the test process env (which Popen inherits)
and having the fake-otto.sh write to that env-var path. The runner never
sets `OTTO_PROJECT_DIR` in production.

**Fix**: Runner now sets `OTTO_QUEUE_PROJECT_DIR=<main>` when spawning
children. `manifest_path_for` reads that env var when `OTTO_QUEUE_TASK_ID`
is set and uses it instead of the caller's `project_dir`. Result: both
sides resolve to the same `<main>/otto_logs/queue/<task-id>/manifest.json`.

Test fixture renamed `OTTO_PROJECT_DIR` → `OTTO_QUEUE_PROJECT_DIR` and a
new test asserts the runner sets this env var on spawn.

**Verification**: `pytest tests/test_queue_runner.py tests/test_manifest.py`
+ E2E scenario A1 with real `otto queue` end-to-end.

**Observability improvement**: `_finalize_task_from_manifest` now logs the
exact manifest path it expected when it can't find one. Was: "exited 0 but
no manifest written"; now: "exited 0 but no manifest at <path>".

---

### [F2] `otto queue run` requires otto.yaml but other queue commands don't — UX inconsistency — A1

**Symptom**: `otto queue build "X"` succeeds in a repo with no otto.yaml,
adding the task to the queue. But `otto queue run --concurrent 1` exits
with rc=2 and message "otto.yaml not found. Run `otto setup` first." The
task sits in the queue forever, the user has no idea why.

**Diagnosis**: `cli_queue.py:queue.run` had a hard `if not config_path.exists()`
gate. But `load_config()` already returns defaults when the file is absent,
and `_enqueue` (used by build/improve/certify) doesn't check. So the gate
was dead defensive code that broke the otherwise-consistent "defaults
everywhere" UX.

**Fix**: Removed the gate. Watcher now uses defaults (concurrent=3,
worktree_dir=".worktrees", on_watcher_restart="resume") when otto.yaml is
absent — matching `otto queue build`.

**Verification**: A1 reaches `done` end-to-end without otto.yaml.

---

### [F3] `otto merge` requires otto.yaml — same UX inconsistency as F2 — B4, B8

**Symptom**: `otto merge --all --no-certify` and `otto merge --all --fast`
both fail with rc=2 + "otto.yaml not found" in repos that use defaults.

**Diagnosis**: Same hard gate pattern as F2, this time in `otto/cli_merge.py`.

**Fix**: Removed the gate. Falls through to `load_config()` defaults.

**Verification**: B4, B8 progress past the otto.yaml check.

---

### [F4] otto's bookkeeping not auto-gitignored — `otto merge` fails on dirty tree — B4, B8

**Symptom**: After `otto queue build` runs, `otto merge --all --no-certify`
fails with `working tree must be clean before merge (uncommitted changes
detected)`. The "dirty" files are otto's own bookkeeping: `.otto-queue.yml`,
`otto_logs/`, `.worktrees/`, `.otto-queue-state.json`, etc. — none of which
the user wants in their repo.

**Diagnosis**: Otto creates these artifacts during normal operation but had
no mechanism to add them to `.gitignore`. Result: every queued task taints
the working tree from git's perspective, every merge precondition fails.

**Fix**:
1. New module `otto/setup_gitignore.py` with `ensure_gitignore(project_dir)`
   that idempotently appends otto's runtime patterns under an `# Otto
   runtime (auto-managed; safe to edit comments)` header.
2. New `first_touch_bookkeeping(project_dir, config)` in `otto/config.py`
   that calls `ensure_gitignore` AND `setup_gitattributes.install` (the
   merge driver setup), then auto-commits the changed files in a single
   `chore(otto): set up runtime bookkeeping (.gitignore, .gitattributes)`
   commit so the working tree stays clean.
3. Wired into `otto queue build|improve|certify` (`_enqueue`) and `otto merge`.
4. Auto-commit is gated: skipped if the user has other staged work
   (would otherwise bundle their changes into a chore commit).
5. Tests in `tests/test_setup_gitignore.py`.

Without this, every new otto user would need to run `otto setup` first AND
manually commit `.gitignore` before any merge could succeed. Now: zero
manual steps.

**Verification**: B4 + B8 pass end-to-end (no `otto setup` required).
`tests/test_setup_gitignore.py` covers fresh / preserve-existing / idempotent
/ partial-overlap.

**Observability improvement**: `setup_gitignore` logs the patterns added
and the auto-commit message at INFO level (visible with --verbose). Failure
modes (other staged files, git not present) log WARNING with the exact
reason instead of silently skipping.

---

### [F5] `otto merge` requires `.gitattributes` from `otto setup` — same root cause as F4 — B4, B8

**Symptom**: After F4 was fixed, `otto merge` next failed with
`.gitattributes precondition failed: Missing required `.gitattributes`
rules: intent.md merge=union, otto.yaml merge=ours / Run `otto setup` to
install`. Same UX failure: a brand-new user can't merge without manual
setup.

**Diagnosis**: `setup_gitattributes.assert_setup` is a hard precondition in
the merge orchestrator, but `ensure_bookkeeping_setup` (which installs
those rules) was only called from `otto setup`, not from queue/merge entry
points.

**Fix**: Bundled into `first_touch_bookkeeping` from F4. `_enqueue` and
`cli_merge` now call `first_touch_bookkeeping`, which installs the
`.gitattributes` rules (and `merge.ours` driver) and auto-commits.

**Verification**: B4 + B8 now pass without manual `otto setup`.

---

### [F6] `--cleanup-on-success` was a no-op — flag plumbed but never used — B10

**Symptom**: `otto merge --all --no-certify --cleanup-on-success` reports
"Merge complete" but the worktree of the merged task is still present in
`git worktree list`.

**Diagnosis**: `MergeOptions.cleanup_on_success` is defined and threaded
from CLI → orchestrator, but `orchestrator.run_merge` never reads it.
Half-finished feature.

**Fix**: Implemented `_cleanup_worktrees_for_merged_tasks()` and called from
both the no-cert and cert paths in `run_merge`. Looks up each merged
branch's task via `queue_lookup`, runs `git worktree remove --force`,
preserves the underlying branch (default git behavior). Errors are logged
WARNING (best-effort post-merge cleanup); failures don't fail the merge
result the user already saw succeed.

**Verification**: B10 now passes — worktree removed, branch preserved.

**Observability improvement**: orchestrator logs `cleanup-on-success: removed
worktrees for [<task-ids>]` at INFO; per-task failures log WARNING with
the git error.

---

### [F7] Watcher logger had no handlers — spawn/reap events invisible to user — observed during real LLM C1 run

**Symptom**: Running `otto queue run` shows the spawned otto's stdout
interleaved but no information about the watcher itself: when it dispatched
each task, when it reaped, what cost it observed, how long it took. The
`logger.info(...)` calls in `runner.py` (and there are many: spawn, reap,
cancel, terminate, reconcile, ECHILD-deferred reap) all go nowhere because
no handler is attached to `otto.queue.runner`.

User-impact: a user who runs `otto queue run` and sees their task seemingly
hang has no way to tell whether the watcher spawned it (vs. silently
queued it). When investigating a stuck task, they see only the spawned
otto's output — no watcher metadata. Debugging is guesswork.

**Diagnosis**: `cli_queue.run` never called `logging.basicConfig` or
attached any handler. Library convention is to leave that to the application
entry point — but the entry point forgot.

**Fix**: New `_install_runner_logging(project_dir, *, quiet)` in cli_queue.
Always installs a file handler at `otto_logs/queue/watcher.log` (append,
ISO-8601 timestamps, INFO level). Unless `--quiet`, also installs a stdout
handler with short `[HH:MM:SS] message` format so events stream live.
Stamped events:

    [02:32:26] spawned add-a-calculator: pid=2241, branch=build/add-...
    [02:32:28] reaped add-a-calculator: done (cost=$0.42, duration=1.0s)
    [02:32:28] SIGTERM: immediate shutdown

The file handler in `otto_logs/queue/watcher.log` survives across watcher
restarts (append mode) — useful for diagnosing "what happened during last
night's run" without scrollback.

**Verification**: A1 with `OTTO_E2E_KEEP=1` shows both files populated
correctly. New `--quiet` flag gives users an opt-out.

---

### [F8] Merge orchestrator + agents had no log handler — same bug as F7, different namespace — observed during real LLM C2/C3 runs

**Symptom**: After F7 fixed the queue runner, the merge orchestrator,
conflict-agent, and triage-agent all still logged into the void. The user
sees the high-level "Merging..." → "Merge complete" CLI banners but no
record of:
- which branches the orchestrator started/completed
- conflict-agent attempts, retries, validation failures
- triage-agent reasoning / story coverage decisions

**Diagnosis**: `otto.merge.*` and `otto.cli_merge` loggers had no handlers
attached.

**Fix**: New `_install_merge_logging(project_dir)` in cli_merge.py. Attaches
a single FileHandler to the parent `otto.merge` logger (which the children
inherit) and to `otto.cli_merge`. Output goes to
`otto_logs/merge/merge.log` (append, ISO-8601, INFO+). Idempotent across
reruns in the same Python process.

**Verification**: real merge run in /tmp/otto-real-cert wrote
"merge merge-... starting: target=main, branches=[...]" to the log file.

---

## Real-LLM Set C results

Three real-LLM scenarios run ad-hoc (not yet automated since the harness
still depends on capturing live cost; the underlying CLI paths work):

**C1 - single real build via queue → merge --no-certify --cleanup-on-success**
- Cost: $0.47 build + $0 merge = $0.47 total
- Time: 75s build + ~3s merge
- Result: calc.py with add(a,b) committed; branch merged into main; worktree
  cleaned up; first-touch bookkeeping (.gitignore, .gitattributes) auto-
  committed cleanly.

**C2 - 2 parallel real builds → merge --all (with conflict agent)**
- Cost: 2×$0.19 builds + $0.24 conflict agent = $0.62 total
- Time: ~30s/build + ~15s conflict resolution
- Setup: Both builds touch math.py with different functions
- Result: branch `add` merged clean; branch `mul` triggered conflict agent;
  agent merged both functions correctly into math.py with both
  `add(a,b)` and `multiply(a,b)`. Validation passed (no scope creep, no
  diff --check failures, HEAD unchanged). Commit landed cleanly.
- conflict-agent.log captured the agent's Read/Write tool calls per file.

**C3 - 2 parallel real builds → merge --all (with triage + cert)**
- Cost: 2×$0.15 builds + $0 triage (trivial — no stories yet) = $0.29 total
- Result: clean merge; triage emitted "no stories collected; nothing to
  verify" plan; cert skipped because plan was empty. This is correct — the
  fast/no-qa builds didn't produce STORY_RESULT lines, so there's nothing
  to verify. Triage cost $0 because it short-circuited on empty input.

**Total real-LLM cost**: ~$1.40 across 5 builds + 1 conflict resolution +
1 triage.

---

### [F9] `otto merge --all` silently skips improves that "failed" cert — observed during P1 benchmark

**Symptom**: P1 benchmark queues 3 parallel `otto improve feature -n 1`
runs against the same TODO CLI. Each improve makes real commits adding
its feature (priority/duedate/tags), but the certifier finds 7 unrelated
gaps and exits status="failure" because cert didn't pass within the round
limit. `otto merge --all` then sees 0 done tasks and silently merges only
the base build — the user's 3 improves' work is invisible to `--all`.

**Diagnosis**: `_resolve_branches(all_done_queue_tasks=True)` filters by
`status == "done"` only. There's no facility for "merge anything queued
that has a branch with commits" (the realistic improves-fail-cert case).

**Workaround**: pass branch names explicitly. The conflict agent merged
all 3 cleanly (1 clean + 2 conflict_resolved), producing a working CLI
with all 9 commands. Total merge cost: $1.35.

**Recommendations** (deferred — would be follow-up work):
1. `otto merge --all-with-branches` (or `--include-failed`) opt-in flag
   that merges any task whose branch has commits, regardless of status.
2. When `--all` finds 0 done tasks but N failed tasks have branches,
   print: `0 done tasks to merge; 3 failed tasks have branches with
   commits — use 'otto merge <branch> ...' to merge them.`

**Verification**: working `todo.py` post-salvage at
`/var/folders/.../bench-p1-poh9vxro/todo.py` (now 145 lines, all features).

---

### [F10] Watcher has no per-task timeout — hung child blocks queue indefinitely — observed during P3 base build

**Symptom**: P3 base build (Flask bookmark API) completed its commit at
178s but the otto build subprocess hung for 8+ more minutes with 0% CPU
and no log output. Watcher kept it as "running" (heartbeat alive). I
manually killed an orphan Flask process and the build immediately
completed normally.

**Root cause**: The build agent had executed an inline shell script that
ended with `pkill -f "python3 app.py"; wait 2>/dev/null; echo "done"`.
The Flask server was launched via `/opt/homebrew/.../Python app.py`
(capital P, no "3"), so `pkill -f "python3 app.py"` didn't match it.
The Flask child stayed alive, so `wait` blocked forever, so the bash
command never returned to the SDK, so the SDK never returned to otto
build, so the watcher kept polling status=running indefinitely.

**Two distinct issues**:

1. **Otto's coding-agent prompt doesn't instruct the agent to use robust
   process kill patterns.** The fix is in the agent's system prompt
   (warn about pkill -f matching by full command line, recommend
   killing by PID captured at launch time). NOT a parallel-otto bug;
   pre-existing.

2. **Queue watcher has no per-task timeout.** A hung subprocess can
   block its slot forever. Recommendation: add `queue.task_timeout_s`
   config (default e.g. 30 min for build, 60 min for improve) and have
   the reaper SIGTERM tasks that exceed it. Mark them `failed` with
   `reason="timed out after Xm"`.

**Workaround used**: I killed the orphan Flask process manually
(`kill 41341`); the build then completed successfully with status=done,
cost=$0.81. P3 then advanced into phase 2 normally.

**Recommendation**: implement per-task timeout. Defer instructing the
agent on shell scripting (separate concern, not parallel-otto's domain).

**Verification**: After my manual kill, P3 base reached `done` and phase
2 dispatched. P3 phase 2 (parallel improves) running normally.

**Status (post-F10 fix)**: Watcher now SIGTERMs tasks past
`task_timeout_s` (default 1800s, configurable via `queue.task_timeout_s`).
`task_timeout_s: null` or `0` disables enforcement (escape hatch). Tested
with 3 unit tests in `tests/test_queue_runner.py`. Implemented in
commit bdda328b.

---

### [F11] Bench helper looked for `branch` field on state.json (it's only in queue.yml) — observed during P5 bench salvage path

**Symptom**: P5 bench's fallback salvage merge ran with empty branches list:
"merge: rc=2, ... Specify branches/task ids, or pass --all to merge all done
queue tasks." Even though there were 3 failed improves with real branches.

**Diagnosis**: `bench_runner._run_complex_bench` had:
```python
all_branches = [t.get("branch") for t in queue_state(repo)["tasks"].values() if t.get("branch")]
```
But state.json only stores `status`, `started_at`, `child`, etc. — never
`branch` (that's in queue.yml). So the list was always empty.

**Fix needed (deferred — bench is a one-off)**: read branches from
`load_queue(repo)` not state. Workaround: I manually invoked the salvage
merge with explicit branch names from the queue.yml.

**Lesson**: scripts that mix queue.yml + state.json data should use the
proper API (`load_queue`, `load_state`) instead of treating them as
interchangeable.

---

## Real-Product Bench Results (P4-P6 — complex multi-module products)

Three new benchmarks designed to exceed the simple-product bar (P1-P3):

**P4 — Flask multi-module API (auth + users + posts)**
- Base: $1.14, 3.1 min — Flask + sqlite3 + JWT auth, 3 endpoint families
- Improves (3 in parallel, `-n 2`): comments + tags + likes
- Improve outcomes: 1 passed cert (comments), 2 failed cert with real commits
- Phase 2 wall (parallel): 9.3 min (max of 3 improves)
- Initial merge --all (1 done improve): cert PASSED in 2.6 min
- Salvage merge (2 failed improves): conflict agent resolved both
  ($2.60 + $1.19 = $3.79 conflict cost)
- **Final cost**: $13.29 total, all features merged + working
- **Final app**: 62 routes/functions in app.py (auth + user CRUD + posts +
  comments + tags + likes + edit/moderate + pagination + search + many extras
  the agent added autonomously)

**P5 — Markdown blog SSG (build pipeline)**
- Base: $1.03, 2.9 min — Jinja2 templates, markdown renderer, 2 example posts
- Improves (3 in parallel, `-n 2`): tag-pages + RSS + search
- All 3 improves failed cert in `-n 2` rounds (the build pipeline is
  hard to certify automatically — output is HTML files)
- Improve cost: $9.62 total ($3.67 + $2.70 + $3.25)
- Salvage merge: ALL 3 branches conflicted on blog.py + templates;
  conflict agent doing real 3-way merge (in progress at write time —
  observed conflict-agent log showing massive Write payloads taking
  10+ minutes per branch).

**P6 — Inventory management CLI (SQLite + multi-feature)**
- Base: $0.62, 2.1 min — SQLite-backed CLI with 6 commands
- Improves (3 in parallel, `-n 2`): categories + suppliers + alerts
- 2 passed cert (categories, alerts), 1 failed (suppliers)
- Phase 2 wall (parallel): 12.6 min
- Merge --all on 2 done improves: **conflict agent ran 13.3 min** doing a
  large 3-way merge of inv.py + .gitignore + README.md ($2.41 cost)
- cert PASSED on the merged result
- **Final cost**: $12.77, 28.7 min wall
- **Final inv.py**: 18 commands (6 base + categories/suppliers/alerts/etc.)

### Key takeaway from P4-P6

The conflict agent's value is most evident on complex multi-file merges:
P6 took 13 minutes of conflict-agent work to merge categories + alerts
into the same inv.py, but the result PASSED full cert. Without the agent
the user would face manual conflict resolution across multiple files —
easily an hour of human time.

P5 demonstrated the agent's worst case: 3 branches all editing the same
build pipeline → very large 3-way merges → 10+ min per Write per branch.
Still cheaper than human merge time, but a single conflict can dominate
total benchmark time.

For complex products, `-n 2` improve rounds is still not enough for cert
to pass reliably (P4: 1/3 passed; P5: 0/3; P6: 2/3). Either tune `-n`
higher (more cost per improve) or accept the salvage-merge pattern as
standard (cheap explicit-branch merge captures the work).

---

### [F12] Conflict agent: disallowed `Write` to force patch-style `Edit` — COUNTERINTUITIVE RESULT

**Motivation**: P5 bench showed the conflict agent generating a single
`Write` call that regenerated entire files — measured ~10 min per branch,
86% of which was streaming unchanged bytes. Expected `Edit`-only to be
3-5× faster.

**Implementation** (commit 58568078):
1. Added `"Write"` to `disallowed_tools` alongside `"Bash"` in
   `conflict_agent.py`.
2. Rewrote `otto/prompts/merger-conflict.md` with explicit "Patch regions,
   don't rewrite files" framing + tool discipline section.
3. Regression test in `tests/test_merge_conflict_agent.py`.

**Actual measured impact (P6 rerun salvage, 2 branches × 3 conflict files)**:

| | Baseline (Write) | F12 (Edit only) |
|---|---|---|
| Tools used per branch | 1 Write + 3-5 Reads | 2-4 Edits + 4-6 Greps + 6-8 Reads |
| Branch 1 wall time | ~10 min | ~22 min |
| Wall time per tool call | Write ≈ 10 min (token stream) | Edit ≈ 30-60s; Read/Grep ≈ 5s |
| Total round trips | 1 big | 12-18 small |
| Drift risk on unchanged code | real | near-zero |
| Final diff reviewability | entire file re-written | hunk-level patches |

**Counterintuitive outcome**: F12 is **SLOWER per branch**, not faster.
Reason: each Edit is a separate LLM round-trip with its own "thinking"
phase. 12 small round-trips with 30-60s thinking each beats 1 big
round-trip with 5 min of thinking + 5 min of token-streaming in wall
time terms. The agent also "verifies" between edits (Grep to check no
markers remain) — caution adds calls.

**Trade-offs**:
- F12 pro: safety (no drift), patch-level git diffs, human-reviewable
  merges, smaller API output tokens per call (maybe lower cost per token
  but more calls)
- F12 con: ~2× slower wall time per branch
- Cost: roughly comparable (more calls × smaller payload vs fewer calls
  × larger payload) — need more data to be confident

**Recommendation**: keep F12 despite the speed regression. Safety and
diff-quality are worth the wall-time cost. For a single conflict on a
small file, Edit is fast. For complex multi-file merges, neither
approach is "fast" — both take 10-25 min. The real wins would come from:
- Running conflict resolution per-file in parallel (N agents for N
  conflict files)
- Disabling extended thinking for the conflict agent (this is a
  mechanical task; extended reasoning is overkill)

Deferred both as F13/F14 for future investigation.

---

### [F12.REVERT] F12 reverted after measured 2-3× regression on P6 rerun

**Decision**: revert F12 (`Write` re-allowed in conflict agent, prompt
restored to original Edit/Write/MultiEdit framing). Drift prevention
remains via post-agent `validate_post_agent` (out-of-scope file modify
detection + HEAD-unchanged check).

**Why revert** (from real audit of conflict-agent.log):
- Per-branch wall time: F12 22-26 min vs baseline ~10 min (2-3× slower)
- Per-branch cost: F12 $7-11 vs baseline $2-3 (3-4× more expensive)
- Root cause **is not** "tool-call overhead per round-trip" (my initial
  guess). Audit of branch-2 timeline:
  - 583s (9.7 min) gap of pure thinking before first Edit
  - 117s + 82s additional thinking gaps mid-execution
  - Edit bursts themselves were fast (5-15s/call)
- The pattern: F12 prompt's "verify each edit" instruction made the agent
  enter a **plan→execute batch→verify→re-plan** loop. Each "re-plan"
  triggered another extended-thinking phase. Write-based agent does
  one plan + one execute, with no re-planning loop.

**Audit-verified vs guessed**:
- I guessed: "many small Edit round-trips, each with thinking overhead"
- Actual: "few large thinking phases regardless of edit count" — the
  prompt design caused multiple re-planning cycles, not the tool choice

**Better directions identified, but not implemented now**:
1. Post-Write structural drift check in orchestrator: compare unchanged
   regions of the agent's output to the pre-merge content; flag any
   drift outside the conflict markers. Gets safety without the
   round-trip cost.
2. Per-file parallel conflict agents: N concurrent agents, one per
   conflict file. Could 2-3× speed up multi-file conflicts. More
   orchestrator complexity.
3. Different prompt framing: "plan ALL edits upfront, do one MultiEdit
   per file, single final verification grep" — try to keep Edit
   semantics but force a single planning phase. Risky (model may not
   comply).

Reverted in a single commit; tests/test_merge_conflict_agent.py asserts
`Write` is NOT in disallowed_tools (with comment explaining the history
to prevent re-introduction).

**Lesson**: I predicted the F12 speedup based on a model of "Edit is
small, Write is big, smaller is faster." Real data showed thinking time
dominates, and the prompt's caution instructions amplified it. Always
audit real logs before claiming an optimization works.

---

## Build/improve vs conflict vs triage vs certifier — agent runtime audit

User question: "what are the differences besides prompts?" Comprehensive
audit:

ALL four agent types route through the same code path:
```
caller → make_agent_options(project_dir, config) → run_agent_with_timeout
       → _query_claude → claude_agent_sdk.query
       → spawns bundled `claude` CLI subprocess
```

Same factory, same model, same effort default (None → CLI default),
same permission_mode (bypassPermissions), same setting_sources
(["project"]), same env, same Claude Code system prompt preset.

The ONLY runtime differences:

| Agent | Disallowed tools | capture_tool_output | Resume | Retry |
|---|---|---|---|---|
| build | none | **True** | yes | inner certify-loop (in-session) |
| improve | none | **True** | yes | inner certify-loop (in-session) |
| certifier | none | False | no | none (caller decides) |
| triage | none | False | no | up to N (orchestrator) |
| conflict | **["Bash"]** | False | no | up to 2 (orchestrator) |

`capture_tool_output=True` for build/improve so the parent can parse
`STORY_RESULT:` lines emitted by the certifier subagent. Other agents
parse the agent's text output directly.

`disallowed_tools=["Bash"]` on the conflict agent is the only structural
isolation — prevents the agent from running `git` or other shell
commands that could escape the orchestrator's commit control.

**Conclusion**: the conflict agent IS NOT slower because of any runtime
difference vs build/improve. Same model, same SDK, same CLI, same
defaults. The slowness is entirely from prompt design + workload
characteristics (multi-file 3-way merge requires deep reasoning per
file, regardless of tool choice).






**End-to-end coverage**:
- ✅ queue dispatch → real `otto build` → manifest → reap (F1 path)
- ✅ first-touch bookkeeping auto-commit (F4, F5)
- ✅ clean merge → cleanup-on-success (F6)
- ✅ real conflict → real conflict agent → validation → commit
- ✅ post-merge triage agent (trivial input edge)
- ✅ watcher logging visible (F7)
- ✅ merge logging visible (F8)






