# Otto Review Handoff

Date: 2026-03-28
Scope: `otto/` only
Goal: give Claude Code a concrete, fix-oriented report with the highest-risk bugs first, plus stronger E2E scenarios.

## Executive Summary

The highest-risk issues are not syntax or unit-test failures. They are behavioral bugs in the orchestration layer:

1. `preflight_checks()` mutates user state before Otto has established that it is safe to run.
2. `preflight_checks()` creates unrelated commits on `main`.
3. `replan()` drops dependency metadata and can parallelize tasks that must stay serialized.
4. QA can accept a partial verdict file as authoritative success after an exception.
5. Startup recovery resets `merged` tasks to `pending`, which can rerun already-landed work.
6. Failure cleanup can rewind `tasks.yaml`, erasing previously recorded task outcomes.

The current test suite passes, which means several of these bugs are encoded behavior or untested edge cases rather than regressions caught by CI.

## Critical Bugs

### 1. Preflight can silently stash user work and switch branches

Files:
- [otto/runner.py:149](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L149)

What happens:
- Otto tries `git checkout <default_branch>` before proving the repo is safe to touch.
- If checkout fails, it runs `git stash --include-untracked`, retries checkout, and only then checks whether the tree is clean.
- A failed Otto run can therefore still move the user from a feature branch to `main`, and can also stash work without explicit consent.

Why this is bad:
- This violates least surprise.
- It mutates the repo before Otto has even started task execution.
- It can hide user work in a stash entry and make the working context appear to "disappear."

Observed runtime reproduction:
- I ran `preflight_checks()` against a temp repo on branch `feature` with a committed divergence from `main` plus an uncommitted change.
- Result:
  - Otto returned success code `0` because there were no pending tasks.
  - Current branch changed from `feature` to `main`.
  - A new stash entry was created: `stash@{0}: WIP on feature: ...`.

Fix direction:
- Make preflight read-only until validation passes.
- Never auto-stash user work in Otto.
- If current branch is not `default_branch`, either fail with a clear message or require an explicit flag.
- If the working tree is dirty, fail before any checkout attempt.

Suggested tests:
- Add an integration test proving preflight does not change branch, does not create stash entries, and does not mutate tracked files on failure.

### 2. Preflight auto-commits `.gitignore` changes onto `main`

Files:
- [otto/runner.py:206](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L206)

What happens:
- During preflight, Otto appends framework-derived ignore entries to `.gitignore`.
- It stages the file and creates a real commit: `otto: update .gitignore for build artifacts`.
- This happens before any task work, and can happen even when there are no pending tasks.

Why this is bad:
- It creates unrelated history on `main`.
- It mixes environment/bootstrap concerns with task execution.
- It can surprise users by adding commits that are not tied to requested work.

Observed runtime reproduction:
- I ran `preflight_checks()` on a clean temp npm repo with no pending tasks.
- Result:
  - A new commit was created on `main`: `otto: update .gitignore for build artifacts`.
  - `.gitignore` was modified with `node_modules/`, `.next/`, `dist/`, `build/`, `coverage/`, `.turbo/`.

Fix direction:
- Move this behavior into explicit setup, e.g. `otto setup` or `otto init`.
- Do not create commits automatically in `otto run`.
- If runtime ignore coverage is still needed, use `.git/info/exclude` only.

Suggested tests:
- Add an integration test proving `preflight_checks()` does not create commits in a clean repo.

### 3. Replan loses dependency edges and can produce invalid parallel execution

Files:
- [otto/planner.py:934](/Users/yuxuan/work/cc-autonomous/otto/planner.py#L934)

What happens:
- `replan()` rebuilds the remaining task list as `{"key": task_plan.task_key}` only.
- `_normalize_plan()` reconstructs explicit dependency analysis from task dicts, but it needs `id` and `depends_on`.
- Because replan strips that data, a valid serial dependency can be forgotten during replan normalization.

Why this is bad:
- Otto can replan dependent tasks into the same batch.
- That creates false parallelism and downstream merge/integration failures.
- This is a correctness bug, not just a cleanup issue.

Observed runtime reproduction:
- I directly compared `_normalize_plan()` with:
  - full tasks including `id` and `depends_on`
  - key-only tasks matching the current `replan()` code path
- Result:
  - with dependencies present: `[['a'], ['b']]`
  - with key-only placeholders: `[['a', 'b']]`

Fix direction:
- Preserve original remaining task dicts when replanning.
- Do not down-convert remaining tasks to key-only placeholders.
- Add a regression test covering replan on an explicit dependency chain.

Suggested tests:
- Replan after one batch failure in a three-task dependency chain and assert that the remaining dependent tasks stay serialized.

### 4. QA can trust a partial verdict file after agent failure

Files:
- [otto/qa.py:872](/Users/yuxuan/work/cc-autonomous/otto/qa.py#L872)

What happens:
- If the QA agent raises an exception, Otto checks whether the verdict file exists.
- If it can parse JSON containing only `"must_passed"`, it returns that partial verdict as authoritative.
- The rest of the schema may be missing, including `must_items`.

Why this is bad:
- A crashed QA agent can leave behind partial output that Otto treats as success.
- This weakens the main safety guarantee of the QA phase.

Fix direction:
- Require complete schema before trusting the verdict file.
- Validate that every `[must]` item has a corresponding `must_items` entry.
- If the file is partial, treat it as infrastructure failure or QA failure, not success.

Suggested tests:
- QA writes `{ "must_passed": true }`, then throws.
- Assert Otto does not accept that as a passing QA result.

### 5. Startup recovery resets `merged` tasks back to `pending`

Files:
- [otto/runner.py:240](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L240)
- [tests/test_orchestrator.py:1382](/Users/yuxuan/work/cc-autonomous/tests/test_orchestrator.py#L1382)

What happens:
- Preflight treats `merged` as a stale transient state and resets it to `pending`.

Why this is bad:
- `merged` means code has already landed on `main`.
- Resetting it to `pending` can rerun already-applied work.
- That risks duplicate commits, duplicated side effects, or incorrect planning state.

Fix direction:
- Only reset genuinely transient states like `running`, `verified`, and `merge_pending`.
- Do not reset `merged`.
- Update the existing test that currently expects this incorrect behavior.

Suggested tests:
- A task in `merged` state should remain `merged` after startup recovery.

### 6. Serial task execution can rewind tracked `tasks.yaml` and corrupt queue state

Files:
- [otto/git_ops.py:562](/Users/yuxuan/work/cc-autonomous/otto/git_ops.py#L562)
- [otto/git_ops.py:216](/Users/yuxuan/work/cc-autonomous/otto/git_ops.py#L216)

What happens:
- On task failure, `_cleanup_task_failure()` calls `_restore_workspace_state()`.
- `_restore_workspace_state()` runs `git reset --hard` in the project repo.
- If `tasks.yaml` is tracked in git, that hard reset rewinds the queue file to the last committed version.
- Otto then updates only the current task to `failed`, which erases previously persisted outcomes from earlier tasks in the same run.

Why this is bad:
- Queue state is corrupted by cleanup.
- Earlier task failures can silently disappear from `tasks.yaml`.
- A later restart can rerun tasks that already failed in the previous run.

Observed live runtime reproductions:
- Failure case:
  - I ran a real serial `otto run` on a disposable 3-task repo where the underlying agent path failed.
  - Otto reported `0 passed  3 failed`.
  - After the run, `tasks.yaml` showed only the last task as `failed`; the first two had been reset to `pending`.
- Success case:
  - I ran a real serial `otto run` on a disposable 3-task repo where all three tasks passed.
  - Otto reported `3/3 tasks passed`.
  - After the run, `tasks.yaml` showed only the last task as `passed`; the first two were reset to `pending`.

Interpretation:
- This is not just a failure-cleanup bug.
- In serial mode, tracked queue state is being reverted during workspace restoration / branch cleanup across tasks.
- The live telemetry and per-task summaries recorded the correct pass/fail results even when `tasks.yaml` did not.

Fix direction:
- Never hard-reset tracked Otto state files.
- Either keep `tasks.yaml` untracked runtime state only, or exclude it from workspace reset logic.
- `_restore_workspace_state()` should restore user code without rolling back the queue database.

Suggested tests:
- Multi-task run with three deterministic failures.
- Assert that all three remain `failed` in `tasks.yaml` after the run.
- Multi-task serial run with three deterministic passes.
- Assert that all three remain `passed` in `tasks.yaml` after the run.

## Important Bugs

### 7. Proof replay script is not shell-safe

Files:
- [otto/qa.py:364](/Users/yuxuan/work/cc-autonomous/otto/qa.py#L364)

What happens:
- `_write_regression_script()` inserts raw commands into:
  - `echo "Running: ..."`
  - then the raw command itself

Why this is bad:
- If a command contains shell substitution like `` `...` `` or `$(...)`, the label line can execute unintended shell expansion while merely printing the label.
- This makes replay artifacts less trustworthy and less portable.

Fix direction:
- Use `printf '%s\n'` with shell-escaped strings.
- Prefer `shlex.quote()` for labels or avoid reprinting the raw command in an eval-sensitive context.

### 8. Startup behavior is incorrectly validated by tests

Files:
- [tests/test_orchestrator.py:1382](/Users/yuxuan/work/cc-autonomous/tests/test_orchestrator.py#L1382)

What happens:
- The suite explicitly expects `merged` tasks to be reset to `pending`.

Why this matters:
- This means fixing the runtime bug requires updating tests, not just code.

Fix direction:
- Treat this as a behavior correction, not a regression.

### 9. Agent auth failures are surfaced opaquely in Otto’s UI

Files:
- [otto/runner.py:1625](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L1625)
- [otto/planner.py:747](/Users/yuxuan/work/cc-autonomous/otto/planner.py#L747)

What happens:
- When the underlying Claude subprocess is not authenticated, the actual assistant output is `Not logged in · Please run /login`.
- Otto surfaces this to users mainly as `Command failed with exit code 1 (exit code: 1) Error output: Check stderr output for details`.

Why this matters:
- Operators see an unhelpful generic error.
- Planner and coding failures look like ordinary command failures instead of an auth/bootstrap issue.
- Otto retries coding attempts even though the failure is non-retryable.

Observed live runtime reproduction:
- Direct `query()` call returned:
  - assistant text: `Not logged in · Please run /login`
  - result message marked error
- Otto run/output only showed generic command-failed text.

Fix direction:
- Special-case auth/setup failures from the SDK and surface them explicitly.
- Do not spend retry budget on non-retryable login/bootstrap errors.

## Lower-Priority Issues Worth Fixing

### 10. Duplicate `_subprocess_env` import in `runner.py`

Files:
- [otto/runner.py:12](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L12)
- [otto/runner.py:51](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L51)

Notes:
- `runner.py` imports `_subprocess_env` from both `otto.agent` and `otto.testing`.
- It is harmless today because one is re-exported from the other, but it is sloppy and confusing.

### 11. Exception swallowing is too broad in several critical paths

Examples:
- [otto/qa.py:869](/Users/yuxuan/work/cc-autonomous/otto/qa.py#L869)
- [otto/orchestrator.py:1577](/Users/yuxuan/work/cc-autonomous/otto/orchestrator.py#L1577)
- [otto/runner.py:1388](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L1388)

Notes:
- Some of these are intentionally best-effort.
- Some are hiding correctness failures in state transitions and cleanup.
- They should be audited and narrowed.

## Executed Runtime Reproductions

These were run against actual Otto functions in disposable git repos.

### Reproduction A: dirty feature branch

Scenario:
- Repo has `main` and `feature`
- `feature` has an extra commit
- working tree on `feature` is dirty
- call `preflight_checks()`

Observed behavior:
- branch changed to `main`
- stash entry created
- Otto returned `0` when there were no pending tasks

Bug observed:
- preflight mutates user state before safety checks are complete

### Reproduction B: clean repo with framework manifest

Scenario:
- clean temp repo with `package.json`
- call `preflight_checks()`

Observed behavior:
- `.gitignore` changed
- new commit created on `main`: `otto: update .gitignore for build artifacts`

Bug observed:
- Otto creates unrelated commits during run preflight

### Reproduction C: replan dependency normalization

Scenario:
- compare `_normalize_plan()` with and without explicit dependency metadata

Observed behavior:
- with dependency metadata: tasks serialized correctly
- with key-only placeholders: tasks become parallelizable

Bug observed:
- `replan()` is structurally capable of violating explicit dependency constraints

### Reproduction D: serial-mode baseline false infrastructure failure

Scenario:
- simple Python repo
- tests are runnable in principle
- Otto auto-detects `pytest`
- baseline runs before any dependency/project installation

Observed behavior:
- Otto failed in `prepare` with `baseline tests fail — infrastructure issue`
- captured error was an import/collection failure in baseline test execution

Bug observed:
- serial-mode baseline validation is too early and can misclassify normal project setup/import issues as infrastructure failure

Likely root cause:
- `run_task_v45()` runs baseline before the disposable-worktree install path used by verification

Relevant file:
- [otto/runner.py:1625](/Users/yuxuan/work/cc-autonomous/otto/runner.py#L1625)

### Reproduction E: live multi-task run with real Otto logs

Scenario:
- disposable repo with 3 tasks:
  - case-insensitive search
  - search filters depending on search
  - dark mode toggle
- real `otto run`

Observed behavior:
- planner LLM failed and Otto fell back to serial plan
- each coding task failed immediately because the underlying Claude subprocess was not logged in
- Otto retried each task 3 times anyway
- final console summary said all 3 tasks failed
- `tasks.yaml` persisted only the last task as failed; earlier failed tasks were left pending

Bugs observed:
- opaque auth failure reporting
- non-retryable failures still consume retry budget
- tracked queue state corruption via failure cleanup reset

Observed logs:
- `planner.log` showed planner fallback after auth failure
- `orchestrator.log` showed serial execution and replans
- `v4_events.jsonl` and `pilot_results.jsonl` recorded repeated coding failures
- `tasks.yaml` did not match the run summary

### Reproduction F: live serial multi-task success with incorrect persisted task states

Scenario:
- disposable repo with 3 additive tasks touching the same module and test file
- planner classified all pairs as `ADDITIVE`
- Otto executed 3 serial batches
- all 3 tasks passed

Observed behavior:
- console summary: `3/3 tasks passed`
- per-task `task-summary.json`: all 3 tasks marked `passed`
- `v4_events.jsonl`: all 3 emitted `task_merged`
- `tasks.yaml`: only the last task remained `passed`; earlier tasks were back to `pending`

Bug observed:
- serial-mode queue state persistence is incorrect even on successful runs

### Reproduction G: live parallel multi-task success

Scenario:
- disposable repo with 2 clearly independent tasks touching different modules and different tests
- planner classified the pair as `INDEPENDENT`
- Otto executed one parallel batch with `max_parallel: 2`

Observed behavior:
- planner output correctly grouped both tasks into one parallel batch
- both tasks ran concurrently in separate worktrees
- both tasks merged successfully
- `tasks.yaml` correctly persisted both tasks as `passed`

Takeaway:
- planner parallelization worked in this case
- queue-state corruption appears tied to serial-mode task execution / workspace restoration, not all Otto runs

## High-Value E2E Test Sets To Add

These are meant to be realistic enough to expose orchestration failures, not just unit bugs.

### Test Set 1: Auth + Ownership + UI Batch

Project shape:
- small FastAPI or Express app with:
  - auth middleware
  - per-user resources
  - React or HTMX frontend

Tasks:
1. Add login-required filtering to `/items`
2. Add “my items” sidebar count
3. Add item deletion flow
4. Add audit log export

Why this set matters:
- mixes API changes, ownership constraints, UI integration, and deletion semantics
- good for testing planner dependency analysis and batch QA integration

What to assert:
- planner does not parallelize tasks with explicit `depends_on`
- batch QA catches cross-task regressions like sidebar count not respecting auth scope
- retry feedback is task-specific
- merged tasks are not reset to pending on restart

Logs to inspect:
- `otto_logs/orchestrator.log`
- per-task `qa-tier.log`
- per-task `qa-proofs/proof-report.md`
- `planner.log`

### Test Set 2: Parallel Same-File Additive Tasks

Project shape:
- medium Python service with one large `service.py`

Tasks:
1. Add pagination to `list_orders`
2. Add filtering to `list_orders`
3. Add sort order support to `list_orders`

Why this set matters:
- same-file additive work is where false “independent” classifications become merge conflict factories

What to assert:
- planner serializes additive tasks
- if replanning occurs, serialization is preserved
- no same-file additive tasks are reintroduced into the same batch

Logs to inspect:
- `planner.log` for relationship labels
- `orchestrator.log` for merge/reapply paths

### Test Set 3: Existing-Code / No-Changes Verification

Project shape:
- repo where the requested feature already exists, but only partially

Tasks:
1. “Add dark mode toggle”
2. “Ensure dark mode preference persists”

Why this set matters:
- exercises `_handle_no_changes()`
- forces QA to distinguish “already exists” from “agent made no changes and guessed”

What to assert:
- no-change path waits for spec generation
- QA rejects incomplete existing behavior
- partial QA verdict files do not produce false pass

Logs to inspect:
- per-task `qa-agent.log`
- `qa-proofs/proof-report.md`
- `live-state.json`

### Test Set 4: Crash / Resume / Restart Safety

Project shape:
- any repo with 2-3 tasks and durable candidate refs

Procedure:
1. start Otto
2. kill it after one task is merged and another is `merge_pending`
3. restart Otto

Why this set matters:
- validates recovery semantics, especially stale-state handling

What to assert:
- `running` and `merge_pending` recover correctly
- `merged` does not get reset to `pending`
- already-landed code is not rerun

Logs to inspect:
- `orchestrator.log`
- `tasks.yaml` before/after restart

### Test Set 5: Batch QA Failure With Selective Retry

Project shape:
- full-stack app with API + UI

Tasks:
1. Add backend search endpoint
2. Add frontend search box
3. Add keyboard shortcuts for search modal

Injected failure:
- batch QA finds that frontend calls wrong query param or fails on keyboard interaction

Why this set matters:
- checks whether failed-task targeting is correct
- checks whether good tasks keep their status and bad tasks get focused retry feedback

What to assert:
- only failed task keys get reset/retried
- feedback is not empty
- successful tasks do not regress to `pending`

## Suggested Fix Order

1. Fix preflight side effects:
   - no auto-stash
   - no branch mutation before validation
   - no auto-commit in `otto run`
2. Fix `replan()` to preserve dependency metadata.
3. Fix QA verdict validation so partial files cannot pass.
4. Fix stale-state recovery so `merged` is not reset.
5. Add regression tests for all of the above.
6. Clean up lower-priority slop afterward.

## Test Status

Local suite result:
- `UV_CACHE_DIR=.uv-cache uv run pytest`
- result: `477 passed in 63.89s`

Interpretation:
- the suite is broad, but it currently misses or normalizes several orchestration correctness bugs above.

Live Otto runtime status:
- I was able to execute real Otto commands on disposable repos and inspect actual Otto logs/state files.
- After rerunning outside the sandbox, live planner/coding execution worked.
- Successful live runs completed for:
  - one-off single-task repo
  - serial multi-task repo
  - parallel multi-task repo
- The unauthenticated Claude subprocess issue was specific to the sandboxed environment, not the normal terminal context.

## Detailed Reproduction And Fix Verification

Use disposable repos only. Do not run these against this repo.

### Bug 1: Preflight silently stashes user work and switches branches

How to reproduce:

```bash
tmpdir=$(mktemp -d /tmp/otto-preflight-stash-XXXXXX)
cd "$tmpdir"
git init -b main
git config user.email test@example.com
git config user.name Test
printf 'main v1\n' > README.md
git add README.md
git commit -m init

git checkout -b feature
printf 'feature committed\n' > README.md
git commit -am "feature commit"
printf 'feature dirty\n' > README.md
printf 'tasks: []\n' > tasks.yaml

UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous python - <<'PY'
from pathlib import Path
from otto.runner import preflight_checks
repo = Path(".")
code, pending = preflight_checks(
    {"default_branch": "main", "verify_timeout": 60, "max_retries": 1},
    repo / "tasks.yaml",
    repo,
)
print("code=", code)
PY

git branch --show-current
git stash list
git status --short
```

Buggy behavior today:
- branch changes from `feature` to `main`
- a stash entry is created
- user work is moved even though Otto should only have validated and exited

How to verify the fix:
- rerun the exact repro
- expected:
  - branch remains `feature`
  - `git stash list` is unchanged / empty
  - tracked dirty state remains in place
  - Otto exits with a clear non-zero error explaining that the repo must be clean / on the correct branch

### Bug 2: Preflight auto-commits `.gitignore` onto `main`

How to reproduce:

```bash
tmpdir=$(mktemp -d /tmp/otto-preflight-gitignore-XXXXXX)
cd "$tmpdir"
git init -b main
git config user.email test@example.com
git config user.name Test
cat > package.json <<'EOF'
{"name":"x","scripts":{"test":"echo ok"}}
EOF
printf '# test\n' > README.md
printf 'tasks: []\n' > tasks.yaml
git add package.json README.md
git commit -m init

UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous python - <<'PY'
from pathlib import Path
from otto.runner import preflight_checks
repo = Path(".")
code, pending = preflight_checks(
    {"default_branch": "main", "verify_timeout": 60, "max_retries": 1},
    repo / "tasks.yaml",
    repo,
)
print("code=", code)
PY

git log --oneline -3
cat .gitignore
```

Buggy behavior today:
- a new commit appears: `otto: update .gitignore for build artifacts`

How to verify the fix:
- rerun the repro
- expected:
  - no new commit is created
  - if ignore updates are still needed, they happen only in `.git/info/exclude` or via explicit setup
  - `git log --oneline -3` still shows only `init`

### Bug 3: Replan loses dependency metadata

How to reproduce:

```bash
UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous python - <<'PY'
from otto.planner import ExecutionPlan, Batch, TaskPlan, _normalize_plan

plan = ExecutionPlan(batches=[Batch(tasks=[TaskPlan(task_key='a'), TaskPlan(task_key='b')])])
with_deps = [
    {'id': 1, 'key': 'a'},
    {'id': 2, 'key': 'b', 'depends_on': [1]},
]
without_deps = [
    {'key': 'a'},
    {'key': 'b'},
]
print('with deps:', [[tp.task_key for tp in batch.tasks] for batch in _normalize_plan(plan, with_deps).batches])
print('without deps:', [[tp.task_key for tp in batch.tasks] for batch in _normalize_plan(plan, without_deps).batches])
PY
```

Buggy behavior today:
- with dependency metadata: `[['a'], ['b']]`
- without dependency metadata: `[['a', 'b']]`

How to verify the fix:
- rerun the repro after changing `replan()`
- expected:
  - replanning path preserves original dependency information
  - normalization of replanned tasks still returns `[['a'], ['b']]`
- also add an integration test:
  - start with 3 tasks where task 3 depends on task 2
  - force a replan after batch 1
  - assert tasks 2 and 3 are never placed in the same batch

### Bug 4: QA accepts partial verdict files

How to reproduce:
- easiest path is a unit/integration test around `_run_qa_prompt()` / `run_qa_agent_v45()`
- force the agent path to:
  1. write a verdict file containing only `{"must_passed": true}`
  2. then throw an exception before producing complete `must_items`

Expected buggy behavior today:
- Otto accepts the partial file as authoritative and returns a passing-style verdict shape

Suggested test shape:

```python
partial = {"must_passed": True}
# write partial JSON to verdict_file
# then raise Exception("simulated qa crash")
# assert current code returns must_passed=True
```

How to verify the fix:
- expected after fix:
  - partial file is rejected
  - result is either:
    - `infrastructure_error=True`, or
    - `must_passed=False`
  - and no passing result is emitted unless:
    - verdict JSON is complete
    - all required `must_items` are present

### Bug 5: Startup resets `merged` back to `pending`

How to reproduce:

```bash
tmpdir=$(mktemp -d /tmp/otto-merged-reset-XXXXXX)
cd "$tmpdir"
git init -b main
git config user.email test@example.com
git config user.name Test
printf 'x\n' > README.md
git add README.md
git commit -m init
cat > tasks.yaml <<'EOF'
tasks:
  - id: 1
    key: merged-task
    prompt: Already merged
    status: merged
EOF

UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous python - <<'PY'
from pathlib import Path
from otto.runner import preflight_checks
repo = Path(".")
code, pending = preflight_checks(
    {"default_branch": "main", "verify_timeout": 60, "max_retries": 1},
    repo / "tasks.yaml",
    repo,
)
print("code=", code)
print((repo / "tasks.yaml").read_text())
PY
```

Buggy behavior today:
- `merged` becomes `pending`

How to verify the fix:
- expected:
  - `merged` remains `merged`
  - `pending` list returned by preflight does not include that task

### Bug 6: Serial-mode `tasks.yaml` state corruption

This is the most important live reproduction.

How to reproduce:

```bash
tmpdir=$(mktemp -d /tmp/otto-serial-state-XXXXXX)
cd "$tmpdir"
git init -b main
git config user.email test@example.com
git config user.name Test

cat > tasklib.py <<'EOF'
from dataclasses import dataclass

@dataclass
class Task:
    id: int
    title: str
    done: bool = False

def create_task(task_id: int, title: str) -> Task:
    return Task(id=task_id, title=title)

def list_titles(tasks: list[Task]) -> list[str]:
    return [task.title for task in tasks]
EOF

mkdir -p tests
cat > tests/test_tasklib.py <<'EOF'
import unittest
from tasklib import Task, create_task, list_titles

class TaskLibTests(unittest.TestCase):
    def test_create_task(self):
        task = create_task(1, 'write docs')
        self.assertEqual(task.id, 1)
        self.assertEqual(task.title, 'write docs')
        self.assertFalse(task.done)

    def test_list_titles(self):
        tasks = [Task(1, 'a'), Task(2, 'b', done=True)]
        self.assertEqual(list_titles(tasks), ['a', 'b'])

if __name__ == '__main__':
    unittest.main()
EOF

cat > otto.yaml <<'EOF'
default_branch: main
max_retries: 1
verify_timeout: 300
max_parallel: 2
test_command: python -m unittest discover -s tests -p 'test_*.py'
skip_qa: true
EOF

cat > tasks.yaml <<'EOF'
tasks:
  - id: 1
    key: task-111111111111
    prompt: Add complete_task(task) that marks a task as done and add tests covering repeated completion.
    status: pending
  - id: 2
    key: task-222222222222
    prompt: Add list_tasks(tasks, done=None) that returns all tasks by default and filters by completion status when done is True or False. Add tests.
    status: pending
  - id: 3
    key: task-333333333333
    prompt: Add export_tasks_csv(tasks, path) that writes id,title,done columns to a CSV file and returns the path. Add tests for file contents.
    status: pending
EOF

git add .
git commit -m init

UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous otto run

echo '--- tasks.yaml ---'
cat tasks.yaml
echo '--- summaries ---'
for f in otto_logs/task-*/task-summary.json; do echo FILE:$f; cat "$f"; done
```

Buggy behavior today:
- console summary says all 3 tasks passed
- each per-task `task-summary.json` says `status: passed`
- but `tasks.yaml` retains only the last task as `passed`
- earlier tasks are back to `pending`

How to verify the fix:
- rerun the repro
- expected:
  - `tasks.yaml` shows all 3 tasks as `passed`
  - `tasks.yaml` matches:
    - console summary
    - `task-summary.json`
    - `v4_events.jsonl`
- also verify a serial all-fail run:
  - all failed tasks remain `failed` in `tasks.yaml`

### Bug 7: Serial baseline false infrastructure failure

How to reproduce:

```bash
tmpdir=$(mktemp -d /tmp/otto-baseline-XXXXXX)
cd "$tmpdir"
git init -b main
git config user.email test@example.com
git config user.name Test
cat > pyproject.toml <<'EOF'
[project]
name = "calcapp"
version = "0.1.0"
requires-python = ">=3.11"
EOF
cat > calc.py <<'EOF'
def add(a, b):
    return a + b
EOF
mkdir -p tests
cat > tests/test_calc.py <<'EOF'
from calc import add

def test_add():
    assert add(2, 3) == 5
EOF
git add .
git commit -m init

UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous otto run "Add a subtract(a, b) helper and tests for negative numbers"
```

Buggy behavior today:
- Otto can fail in `prepare` with `baseline tests fail — infrastructure issue`
- this happens before the later verification/install behavior has a chance to normalize the environment

How to verify the fix:
- expected:
  - either baseline uses the same environment/setup guarantees as verification, or
  - baseline is skipped/degraded for projects that are not yet install-normalized
  - Otto should proceed into coding instead of dying in `prepare`

### Live parallel-planning verification

Use this to verify the planner still parallelizes real independent work after fixes:

```bash
tmpdir=$(mktemp -d /tmp/otto-parallel-check-XXXXXX)
cd "$tmpdir"
git init -b main
git config user.email test@example.com
git config user.name Test

cat > searchlib.py <<'EOF'
def contains(items: list[str], query: str) -> list[str]:
    return [item for item in items if query in item]
EOF

cat > themelib.py <<'EOF'
def toggle_theme(current: str) -> str:
    return 'dark' if current == 'light' else 'light'
EOF

mkdir -p tests
cat > tests/test_searchlib.py <<'EOF'
import unittest
from searchlib import contains

class SearchTests(unittest.TestCase):
    def test_contains(self):
        self.assertEqual(contains(['Alpha', 'beta'], 'a'), ['Alpha', 'beta'])
EOF

cat > tests/test_themelib.py <<'EOF'
import unittest
from themelib import toggle_theme

class ThemeTests(unittest.TestCase):
    def test_toggle_theme(self):
        self.assertEqual(toggle_theme('light'), 'dark')
        self.assertEqual(toggle_theme('dark'), 'light')
EOF

cat > otto.yaml <<'EOF'
default_branch: main
max_retries: 1
verify_timeout: 300
max_parallel: 2
test_command: python -m unittest discover -s tests -p 'test_*.py'
skip_qa: true
EOF

cat > tasks.yaml <<'EOF'
tasks:
  - id: 1
    key: task-aaaa1111bbbb
    prompt: Add contains_case_insensitive(items, query) to searchlib.py and tests covering mixed-case matching.
    status: pending
  - id: 2
    key: task-cccc2222dddd
    prompt: Add set_theme(current, target) to themelib.py and tests covering light and dark targets.
    status: pending
EOF

git add .
git commit -m init

UV_CACHE_DIR=/Users/yuxuan/work/cc-autonomous/.uv-cache \
uv run --project /Users/yuxuan/work/cc-autonomous otto run

sed -n '1,200p' otto_logs/planner.log
cat tasks.yaml
```

What success looks like:
- planner log says the pair is `INDEPENDENT`
- final batch structure is one batch with both tasks
- console/logs show both tasks starting together
- `tasks.yaml` ends with both tasks as `passed`
