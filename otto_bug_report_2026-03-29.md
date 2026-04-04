# Otto Audit Bug Report

Date: 2026-03-29

Repo under audit:
- `https://github.com/logpie/otto`
- local clone used for code references: `/tmp/otto-audit`

Real-project E2E repos used:
- `/tmp/e2e-itsdangerous`
- `/tmp/e2e-click`
- `/tmp/e2e-p-limit`

Controls:
- `claude -p "Reply with exactly OK"` succeeded in this environment.
- `uv run python` plus a minimal `claude-agent-sdk` query succeeded.
- Early setup commands did hit sandbox restrictions, but the core E2E runs were executed after escalation outside the sandbox.
- The key failure was still reproducible outside the sandbox.
- Specifically:
  - `claude -p` worked
  - `uv run python` + direct `claude-agent-sdk` query worked
  - the same SDK query failed with `Not logged in · Please run /login` when run via Otto's installed runtime (`/tmp/otto-audit/.venv/bin/python` or `/tmp/otto-audit/.venv/bin/otto`)
- That makes the primary blocker an Otto/runtime/bootstrap issue in this environment, not a generic sandbox/network failure.

This report separates:
- E2E reproduced bugs
- static code/design bugs found during audit
- reproduction commands
- verification checks for fixes

## Executive Summary

Highest-signal issues:

1. Using the documented Codex launch path `uv run --project /path/to/otto-audit otto ...`, Otto successfully completed real single-task E2E runs on both Python and Node repos.
2. The strongest remaining real E2E bug is multi-task batch QA: on `click`, both tasks passed individually, merged successfully, and then the batch was rejected and rolled back after 3 retry rounds.
3. That batch-QA failure appears to be a real product bug: `_finalize_batch_qa_result()` treats any non-empty `regressions` list as fatal, even when the only regression is explicitly marked pre-existing.
4. The coding agent is launched with `cwd=project_dir`, not `task_work_dir`, so the core worktree-isolation guarantee is broken in code.
5. Dependency installation in worktrees is best-effort and unchecked, but Otto logs it as successful.
6. The architecture docs are not aligned with implementation around contradictory tasks and serial-vs-batch QA behavior.

## Environment Context

This audit was run from a Codex environment that initially enforced sandbox restrictions for some filesystem/network actions. Those restrictions affected setup steps such as:

- `git clone`
- `uv run ...` when `uv` tried to initialize cache outside writable roots

After escalation, the real E2E task runs and runtime probes were executed outside the sandbox.

Important interpretation:

- Some setup friction was sandbox-related.
- The main Otto failure was not.
- The decisive evidence is that adjacent control paths succeeded while Otto's installed runtime still failed:
  - `claude -p` succeeded
  - direct SDK query under `uv run python` succeeded
  - direct SDK query under Otto's installed venv/python failed with `Not logged in · Please run /login`

So the report should be read as:

- not "Otto failed only because the sandbox blocked it"
- and not "Otto is completely non-functional in Codex"
- but "the direct installed-runtime invocation was fragile in this environment, while the documented `uv run --project` path worked much better"

## Rerun Summary

After reading [codex-e2e-guide.md](/Users/yuxuan/work/cc-autonomous/docs/codex-e2e-guide.md), I reran the E2E suite using the documented invocation:

```bash
uv run --project /tmp/otto-audit otto ...
```

That materially changed the results.

Corrected E2E outcomes:

- Minimal synthetic repo: passed end-to-end
- Real Python repo (`itsdangerous`) single-task docs change: passed end-to-end
- Real Node repo (`p-limit`) single-task docs change: passed end-to-end
- Real Python repo (`click`) multi-task docs batch: failed only at integrated batch QA after 3 retry rounds and rollback

So the earlier "Otto is not operational at all" conclusion should be narrowed:

- Otto is operational for real single-task runs under the documented Codex launch path.
- The strongest remaining product failure from real E2E testing is the multi-task batch-QA/rollback behavior.

## E2E Reproductions

### E2E-RERUN-1: Minimal guide-based repo passes end-to-end

Repo:
- `/tmp/otto-guide-minimal`

Command:

```bash
uv run --project /tmp/otto-audit otto run "Add subtract(a, b) that returns a - b. Add tests."
```

Observed:
- prepare succeeded
- coding succeeded
- test passed
- QA passed
- merge succeeded

Final outcome:
- `1/1 tasks passed`
- integration gate passed

### E2E-RERUN-2: Real Python single-task repo passes end-to-end

Repo:
- `/tmp/e2e2-itsdangerous`

Command:

```bash
uv run --project /tmp/otto-audit otto run "Add one sentence to README.md near the introduction explaining that URLSafeSerializer is useful for signed data in links and cookies."
```

Observed:
- coding modified `README.md`
- test passed
- spec generation succeeded
- targeted QA succeeded with proof artifacts
- merge succeeded

Final outcome:
- `1/1 tasks passed`
- integration gate passed

Artifacts:
- `/tmp/e2e2-itsdangerous/otto_logs/adhoc-1774823181-13900/qa-proofs/proof-report.md`

### E2E-RERUN-3: Real Node single-task repo passes end-to-end

Repo:
- `/tmp/e2e2-p-limit`

Command:

```bash
uv run --project /tmp/otto-audit otto run "Add one sentence to readme.md noting that limit.map preserves input order in its returned results."
```

Observed:
- coding modified `readme.md`
- `npm test` passed in Otto verification/QA flow
- QA succeeded with proof artifacts
- merge succeeded

Final outcome:
- `1/1 tasks passed`
- integration gate passed

Artifacts:
- `/tmp/e2e2-p-limit/otto_logs/adhoc-1774823988-30228/qa-proofs/proof-report.md`

### E2E-RERUN-4: Real multi-task Python batch fails only at batch QA

Repo:
- `/tmp/e2e2-click`

Commands:

```bash
uv run --project /tmp/otto-audit otto add "Add one sentence to README.md noting that Click supports nested command groups."
uv run --project /tmp/otto-audit otto add "Add one sentence to docs/options.rst explaining that envvar defaults can be combined with prompts."
uv run --project /tmp/otto-audit otto run
```

Observed:
- planner succeeded
- plan was `1 batch(es), 2 tasks`
- both tasks passed prepare/coding/test individually
- both tasks merged successfully
- initial batch QA failed
- Otto retried both tasks through 3 full retry rounds
- retries mostly converged to "no changes needed"
- final batch QA still rejected the batch and Otto rolled it back

Final outcome:
- `0 passed  2 failed  of 2 tasks`
- run exited non-zero after rollback

Key artifact showing the bug:
- `/tmp/e2e2-click/otto_logs/batch-qa-20260329-153818/qa-proofs/proof-report.md`

That proof report says:
- `4/4 must items` passed
- integration finding passed
- full test suite passed
- only listed regression is explicitly marked `pre-existing`

Yet Otto still marks the batch failed and rolls it back.

### E2E-1: Single-task run fails immediately on a clean real Python repo

Status:
- superseded by the rerun above for normal Codex usage
- retained here as an environment/runtime caveat when Otto is invoked via the installed console script directly

Repo:
- `/tmp/e2e-itsdangerous`

Command:

```bash
/tmp/otto-audit/.venv/bin/otto run "Add one sentence to README.md near the introduction explaining that URLSafeSerializer is useful for signed data in links and cookies."
```

Observed:
- Otto created config and worktree normally.
- It spent 17.3s installing deps in the worktree.
- Spec agent and coding agent both failed immediately.
- Otto retried coding 4 times with zero cost and no actionable stderr.

Evidence:
- `/tmp/e2e-itsdangerous/otto_logs/adhoc-1774772144-2687/spec-agent.log`
- `/tmp/e2e-itsdangerous/otto_logs/orchestrator.log`
- `/tmp/e2e-itsdangerous/otto_logs/v4_events.jsonl`

Key observed log:

```text
Not logged in · Please run /login
ERROR: Command failed with exit code 1 (exit code: 1)
Error output: Check stderr output for details
```

Impact:
- Otto fails before any code or QA work on a clean real repo.
- Retries are wasted because the failure is infrastructure/auth, not task-specific.

### E2E-2: Multi-task plan/run silently degrades on a clean real Python repo

Status:
- superseded by the rerun above for normal Codex usage
- retained here as an environment/runtime caveat when Otto is invoked via the installed console script directly

Repo:
- `/tmp/e2e-click`

Commands:

```bash
/tmp/otto-audit/.venv/bin/otto add "Add one sentence to README.md noting that Click supports nested command groups."
/tmp/otto-audit/.venv/bin/otto add "Add one sentence to docs/options.rst explaining that envvar defaults can be combined with prompts."
/tmp/otto-audit/.venv/bin/otto plan
/tmp/otto-audit/.venv/bin/otto run
```

Observed:
- `otto plan` printed a planner failure and silently fell back to serial batches.
- `otto run` repeated the same planner fallback.
- Task 1 then failed after worktree setup and 4 coding retries.
- Replanner also failed and silently fell back to serial remaining plan.

Evidence:
- `/tmp/e2e-click/otto_logs/planner.log`
- `/tmp/e2e-click/otto_logs/orchestrator.log`
- `/tmp/e2e-click/otto_logs/v4_events.jsonl`

Key observed planner log:

```text
planner fallback
fallback trigger: planner failed (Command failed with exit code 1 ...)
final batch structure:
- batch 1: 4832b13efab3[strategy=direct, effort=high]
- batch 2: 1ad920dc594d[strategy=direct, effort=high]
```

Impact:
- Planner/replanner outages are hidden behind serial fallback.
- Users get degraded behavior without a clear diagnosis.
- Contradiction/dependency intelligence is bypassed in exactly the scenarios where it matters.

### E2E-3: `otto add` leaves repo visibly dirty after auto-init

Repo:
- `/tmp/e2e-click`

Commands:

```bash
/tmp/otto-audit/.venv/bin/otto add "..."
git status --short
git check-ignore -v otto.yaml tasks.yaml .tasks.lock otto_logs || true
```

Observed:
- `otto add` auto-created `otto.yaml`.
- `git status --short` showed:

```text
?? otto.yaml
```

- `tasks.yaml` and `.tasks.lock` were ignored.
- `otto.yaml` was not ignored.

Evidence:
- `.git/info/exclude` in `/tmp/e2e-click`

Impact:
- Otto's "first run" mutates the user's repo into a visibly dirty state.
- Preflight uses `git status --porcelain -uno`, so Otto ignores this internally while the user still sees a dirty tree.

### E2E-4: Node worktree bootstrap is expensive before any agent work

Repo:
- `/tmp/e2e-p-limit`

Command:

```bash
/tmp/otto-audit/.venv/bin/otto run "Add one sentence to readme.md noting that limit.map preserves input order in its returned results."
```

Observed:
- Otto spent 48.1s in worktree dependency installation before the run was interrupted.
- No task-level progress had been emitted yet.

Evidence:
- `/tmp/e2e-p-limit/otto_logs/orchestrator.log`

Impact:
- Failures that should be caught before agent launch can still incur heavy bootstrap time.
- The current flow is expensive on Node repos even for trivial docs tasks.

## Bugs

### BUG-1: Installed Otto console script fails SDK auth while `uv run python` succeeds

Severity:
- Medium

What happens:
- Direct execution via the installed venv interpreter or console script produces:
  `Not logged in · Please run /login`
- The same SDK query succeeds under `uv run python`.

Important scope note:
- This did not reproduce on the documented Otto launch path:
  `uv run --project /tmp/otto-audit otto ...`
- Treat this as an environment/bootstrap bug for Codex-style usage of the installed console script, not as proof that Otto itself cannot run.

Minimal repro:

Fails:

```bash
/tmp/otto-audit/.venv/bin/python - <<'PY'
import asyncio, os
from claude_agent_sdk import ClaudeAgentOptions, query

async def main():
    opts = ClaudeAgentOptions(
        permission_mode='bypassPermissions',
        cwd='/tmp/itsdangerous-sdk-wt',
        system_prompt={'type': 'preset', 'preset': 'claude_code'},
        setting_sources=['project'],
        env=dict(os.environ),
    )
    async for msg in query(prompt='Reply with exactly OK', options=opts):
        print(type(msg).__name__)
        if hasattr(msg, 'content'):
            print(msg.content)
        if hasattr(msg, 'is_error') and getattr(msg, 'is_error', False):
            print('RESULT_ERROR', getattr(msg, 'result', None))

asyncio.run(main())
PY
```

Succeeds:

```bash
cd /tmp/otto-audit
uv run python - <<'PY'
import asyncio
from claude_agent_sdk import ClaudeAgentOptions, query

async def main():
    opts = ClaudeAgentOptions(
        permission_mode='bypassPermissions',
        cwd='/tmp/itsdangerous-sdk-wt',
        system_prompt={'type': 'preset', 'preset': 'claude_code'},
    )
    async for msg in query(prompt='Reply with exactly OK', options=opts):
        print(type(msg).__name__)
        if hasattr(msg, 'content'):
            print(msg.content)

asyncio.run(main())
PY
```

Expected:
- The installed `otto` command should be able to launch the same SDK session successfully.

Actual:
- Console-script execution fails auth while `uv run python` succeeds.

Verification after fix:
- The direct venv-python repro above should produce `OK` with no auth error.
- `otto run "<simple README task>"` on `/tmp/e2e-itsdangerous` should reach coding/test/QA instead of dying before first tool call.

### BUG-2: Otto does not preflight agent auth/availability

Severity:
- Medium

What happens:
- Planner failures are hidden behind serial fallback.
- Coding/spec failures are retried up to max_retries even when the root cause is auth/runtime.
- The user gets generic `Command failed with exit code 1` instead of a clear actionable failure.

Important scope note:
- Under the documented `uv run --project` flow, this was not the dominant product issue anymore.
- The stronger remaining issue after rerun is BUG-9 below.

Evidence:
- `/tmp/e2e-click/otto_logs/planner.log`
- `/tmp/e2e-itsdangerous/otto_logs/adhoc-1774772144-2687/spec-agent.log`

Expected:
- Before installing deps or retrying tasks, Otto should perform a cheap agent health/auth check and stop with a precise message if agent launch is impossible.

Actual:
- Otto spends time in worktree setup, retries, and replanning before surfacing vague failures.

Likely fix shape:
- Add a run-level preflight agent probe.
- Classify auth/startup failures as infrastructure errors.
- Abort early instead of retrying task logic.

Verification after fix:
- In a broken-auth environment, `otto run` should fail once with a clear auth/startup diagnosis and no task retries.
- In a healthy environment, no behavior regression.

### BUG-9: Batch QA fails real multi-task runs on pre-existing regressions

Severity:
- High

Real repro:
- `/tmp/e2e2-click`

Observed behavior:
- planner succeeds
- both tasks pass individually
- both tasks merge successfully
- batch QA proof report shows all must items passing
- the only regression listed is explicitly marked pre-existing
- Otto still marks the batch failed, retries both tasks repeatedly, and finally rolls back

Key code evidence:
- `/tmp/otto-audit/otto/qa.py:1208-1247`

Relevant logic:

```python
overall_passed = (
    bool(qa_result.get("must_passed"))
    and not infrastructure_error
    and not missing_pairs
    and not integration_failed
    and not regressions
    and bool(test_suite_passed)
)
```

Problem:
- `not regressions` makes any non-empty regressions list fatal.
- The batch-QA prompt/output can include regressions whose `status` is `pre-existing`.
- In the Click repro, the proof report explicitly records the only regression as pre-existing and unrelated, but Otto still fails the batch.

Concrete evidence:
- `/tmp/e2e2-click/otto_logs/batch-qa-20260329-153818/qa-proofs/proof-report.md`
- `/tmp/e2e2-click/otto_logs/orchestrator.log`
- `/tmp/e2e2-click/tasks.yaml`

Expected:
- Pre-existing regressions should not fail a batch that otherwise satisfies all must items and integration checks.

Actual:
- Batch fails and is rolled back after 3 retry rounds.

Verification after fix:
- Re-run the Click multi-task repro.
- Expected result:
  - planner succeeds
  - both tasks merge
  - batch QA passes or at minimum ignores explicitly pre-existing regressions
  - final statuses are `passed`, not `failed`

### BUG-3: Coding agent runs in `project_dir`, not `task_work_dir`

Severity:
- High

Code evidence:
- `/tmp/otto-audit/otto/runner.py:692-699`

Relevant code:

```python
agent_opts = ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    cwd=str(project_dir),
    ...
)
```

Context:
- `coding_loop(... task_work_dir=worktree)` is supposed to execute inside the isolated worktree.
- Instead, the coding agent's cwd is the main repo.

Expected:
- Coding should happen inside the per-task worktree.

Actual:
- The coding agent is pointed at the main checkout.

Impact:
- Breaks the core isolation model.
- Risks modifying the main checkout during "isolated" task execution.
- Makes merge/test semantics conceptually wrong even if some prompts mention the worktree path.

Verification after fix:
- Add an integration test that creates a task, lets the agent write a sentinel file, and asserts:
  - the sentinel appears only in the task worktree before merge
  - the main checkout remains unchanged until merge

### BUG-4: `otto add` leaves `otto.yaml` unignored and visibly dirty

Severity:
- Medium

Evidence:
- `/tmp/e2e-click/.git/info/exclude`
- `/tmp/e2e2-click/.git/info/exclude`
- `git status --short` showing `?? otto.yaml`

Code evidence:
- `/tmp/otto-audit/otto/runner.py:191`
- `/tmp/otto-audit/otto/git_ops.py:23-30`

What happens:
- Otto auto-creates `otto.yaml`.
- It does not add `otto.yaml` to `.git/info/exclude`.
- `check_clean_tree()` also ignores untracked files entirely by using `git status --porcelain -uno`.

Expected:
- Either `otto.yaml` is treated as Otto-owned and ignored consistently, or Otto documents clearly that it creates a tracked config file.

Actual:
- Otto internally tolerates the file while leaving the repo visibly dirty to the user.

Verification after fix:
- After first `otto add` in a fresh repo, `git status --short` should remain empty unless user files changed.

### BUG-5: Worktree dependency install is best-effort but logged as success

Severity:
- Medium

Code evidence:
- `/tmp/otto-audit/otto/testing.py:56-123`
- `/tmp/otto-audit/otto/orchestrator.py:1849-1852`

What happens:
- `_install_deps()` ignores return codes from venv creation, `pip install`, and `npm install`.
- `_run_task_in_worktree()` logs `deps installed` unconditionally.

Expected:
- Dependency/bootstrap failures should be surfaced explicitly and classified as setup/infrastructure failures.

Actual:
- Bootstrap may partially fail while logs imply success.

Impact:
- False attribution to task logic.
- Wasted retries and misleading logs.

Verification after fix:
- Induce a guaranteed failing install command in a test fixture repo.
- Otto should fail with a setup/bootstrap error before coding starts.
- `orchestrator.log` should not say deps were installed successfully.

### BUG-6: Planner contradiction model is internally inconsistent with orchestrator/task state

Severity:
- Medium

Doc claim:
- contradictory tasks are "scheduled in separate batches (never drop)"

Code evidence:
- `/tmp/otto-audit/otto/planner.py:785-799`
- `/tmp/otto-audit/otto/planner.py:562-586`
- `/tmp/otto-audit/otto/orchestrator.py:275-300`
- `/tmp/otto-audit/otto/tasks.py:216-240`

What happens:
- Planner prompt and normalization both say contradictory tasks remain scheduled.
- `_plan_covers_pending()` rejects plans where planned tasks also appear in direct conflicts.
- Persisted planner conflicts are recomputed into `conflict` / `blocked` terminal task states.

Expected:
- One coherent model:
  - either contradictory tasks are runnable in separate batches
  - or they are excluded and surfaced as operator action items

Actual:
- Docs, planner, orchestrator, and task state machine disagree.

Verification after fix:
- Add a planner/orchestrator integration test with two explicit contradictory tasks.
- Assert one chosen behavior end-to-end, including user-visible status and plan output.

### BUG-7: Multi-task serial runs use batch QA, contrary to docs/diagram

Severity:
- Low to Medium

Doc claim:
- serial mode runs per-task QA inline

Code evidence:
- `/tmp/otto-audit/docs/architecture.md:23-30`
- `/tmp/otto-audit/otto/orchestrator.py:776-781`

What happens:
- For any run with more than one pending task, Otto sets `qa_mode = BATCH`, even if `max_parallel` is 1 and each batch executes serially.

Expected:
- Either docs should state this clearly, or implementation should match the diagram.

Verification after fix:
- A 2-task run with `max_parallel: 1` should either:
  - show documented batch QA behavior in docs
  - or perform true per-task QA if implementation changes

### BUG-8: Otto repo test/dev setup is incomplete

Severity:
- Low

Observed:

1. Before installing `pytest`, `uv run pytest -q` resolves to global `/opt/homebrew/bin/pytest`, which is misleading.
2. After installing `pytest`, the suite still fails collection due missing dev deps (`fastapi`, `pytest-asyncio` markers unresolved, etc.).

Repro:

```bash
cd /tmp/otto-audit
uv run which pytest
uv run pytest -q
```

Expected:
- One documented dev command should produce a runnable test environment.

Actual:
- Contributor test bootstrapping is incomplete.

Verification after fix:
- `uv sync --dev` or equivalent documented command should make `uv run pytest -q` pass collection at minimum.

## Architecture/Doc Drift

These are not separate runtime repros, but they should be updated or fixed:

1. `docs/architecture.md` calls itself source of truth, but contradiction handling and serial-vs-batch QA do not match implementation.
2. README advertises tier-0 QA skip semantics, but current `determine_qa_tier()` returns only 1 or 2.

Code evidence:
- `/tmp/otto-audit/otto/qa.py:100-188`
- `/tmp/otto-audit/README.md:18-24`

## Recommended Fix Order

1. Fix batch QA so explicitly pre-existing regressions do not fail integrated batches.
2. Fix coding cwd to `task_work_dir`.
3. Make Otto-owned file handling consistent for `otto.yaml`.
4. Make dependency installation fail-closed.
5. Reconcile planner contradiction semantics and update docs/tests.
6. Improve installed-runtime/bootstrap behavior for direct `otto` invocation in Codex-like environments.
7. Add a fast agent health/auth preflight for environments where agent startup is actually broken.

## Suggested Regression Checks

After fixes, rerun:

### Control checks

```bash
cd /tmp/otto-audit
uv run otto --help
uv run python - <<'PY'
import asyncio
from claude_agent_sdk import ClaudeAgentOptions, query
async def main():
    opts = ClaudeAgentOptions(permission_mode='bypassPermissions', cwd='/tmp/otto-audit', system_prompt={'type':'preset','preset':'claude_code'})
    async for msg in query(prompt='Reply with exactly OK', options=opts):
        pass
asyncio.run(main())
PY
```

### Real-repo checks

```bash
cd /tmp/e2e2-itsdangerous
uv run --project /tmp/otto-audit otto run "Add one sentence to README.md near the introduction explaining that URLSafeSerializer is useful for signed data in links and cookies."

cd /tmp/e2e2-click
rm -f tasks.yaml .tasks.lock otto.yaml
git status --short
uv run --project /tmp/otto-audit otto add "Add one sentence to README.md noting that Click supports nested command groups."
git status --short

cd /tmp/e2e2-click
uv run --project /tmp/otto-audit otto add "Add one sentence to README.md noting that Click supports nested command groups."
uv run --project /tmp/otto-audit otto add "Add one sentence to docs/options.rst explaining that envvar defaults can be combined with prompts."
uv run --project /tmp/otto-audit otto run

cd /tmp/e2e2-p-limit
uv run --project /tmp/otto-audit otto run "Add one sentence to readme.md noting that limit.map preserves input order in its returned results."
```

Expected post-fix:
- single-task Python repo passes
- single-task Node repo passes
- Click multi-task batch passes instead of rolling back on pre-existing regressions
- no visible dirty `otto.yaml` after Otto creates it
- worktree isolation honored during coding

## Full Setup From Empty Machine

Use this when reproducing from a fresh machine or clean temp workspace.

Prerequisites:
- `git`
- `uv`
- `python` compatible with Otto's `>=3.11` requirement via `uv`
- `node` and `npm` for the Node repo scenario
- `claude` CLI installed and authenticated

Suggested workspace:

```bash
mkdir -p /tmp/otto-repro
cd /tmp/otto-repro
```

### 1. Clone Otto and install it editable

```bash
git clone --depth 1 https://github.com/logpie/otto otto-audit
cd otto-audit

uv run python --version
uv pip install -e . pytest

uv run otto --help
claude -p "Reply with exactly OK"
```

### 2. Reproduce the installed-runtime auth mismatch

Control that should work:

```bash
cd /tmp/otto-repro/otto-audit

uv run python - <<'PY'
import asyncio
from claude_agent_sdk import ClaudeAgentOptions, query

async def main():
    opts = ClaudeAgentOptions(
        permission_mode='bypassPermissions',
        cwd='/tmp/otto-repro/otto-audit',
        system_prompt={'type': 'preset', 'preset': 'claude_code'},
    )
    async for msg in query(prompt='Reply with exactly OK', options=opts):
        if hasattr(msg, 'content'):
            print(msg.content)

asyncio.run(main())
PY
```

Probe that failed in this audit:

```bash
/tmp/otto-repro/otto-audit/.venv/bin/python - <<'PY'
import asyncio, os
from claude_agent_sdk import ClaudeAgentOptions, query

async def main():
    opts = ClaudeAgentOptions(
        permission_mode='bypassPermissions',
        cwd='/tmp/otto-repro/otto-audit',
        system_prompt={'type': 'preset', 'preset': 'claude_code'},
        setting_sources=['project'],
        env=dict(os.environ),
    )
    async for msg in query(prompt='Reply with exactly OK', options=opts):
        print(type(msg).__name__)
        if hasattr(msg, 'content'):
            print(msg.content)
        if hasattr(msg, 'is_error') and getattr(msg, 'is_error', False):
            print('RESULT_ERROR', getattr(msg, 'result', None))

asyncio.run(main())
PY
```

### 3. Clone the real-world test repos

```bash
cd /tmp/otto-repro

git clone --depth 1 https://github.com/pallets/itsdangerous e2e-itsdangerous
git clone --depth 1 https://github.com/pallets/click e2e-click
git clone --depth 1 https://github.com/sindresorhus/p-limit e2e-p-limit
```

### 4. Establish upstream baselines

Python repos:

```bash
cd /tmp/otto-repro/e2e-itsdangerous
uv run pytest -q

cd /tmp/otto-repro/e2e-click
uv run pytest -q
```

Node repo:

```bash
cd /tmp/otto-repro/e2e-p-limit
npm install --no-audit --no-fund
npm test
```

### 5. Single-task Otto repro on a real Python repo

```bash
cd /tmp/otto-repro/e2e-itsdangerous

uv run --project /tmp/otto-repro/otto-audit otto run "Add one sentence to README.md near the introduction explaining that URLSafeSerializer is useful for signed data in links and cookies."
```

Inspect:

```bash
find otto_logs -maxdepth 3 -type f | sort
sed -n '1,200p' otto_logs/orchestrator.log
sed -n '1,200p' otto_logs/*/spec-agent.log
tail -n 80 otto_logs/v4_events.jsonl
```

### 6. Multi-task Otto repro on a real Python repo

```bash
cd /tmp/otto-repro/e2e-click

uv run --project /tmp/otto-repro/otto-audit otto add "Add one sentence to README.md noting that Click supports nested command groups."
uv run --project /tmp/otto-repro/otto-audit otto add "Add one sentence to docs/options.rst explaining that envvar defaults can be combined with prompts."

git status --short
git check-ignore -v otto.yaml tasks.yaml .tasks.lock otto_logs || true

uv run --project /tmp/otto-repro/otto-audit otto plan
uv run --project /tmp/otto-repro/otto-audit otto run
```

Inspect:

```bash
cat tasks.yaml
cat otto.yaml
sed -n '1,240p' otto_logs/planner.log
sed -n '1,240p' otto_logs/orchestrator.log
tail -n 120 otto_logs/v4_events.jsonl
```

### 7. Node worktree bootstrap timing repro

```bash
cd /tmp/otto-repro/e2e-p-limit

uv run --project /tmp/otto-repro/otto-audit otto run "Add one sentence to readme.md noting that limit.map preserves input order in its returned results."
```

If it hangs or takes too long, interrupt and inspect:

```bash
sed -n '1,200p' otto_logs/orchestrator.log
tail -n 80 otto_logs/v4_events.jsonl
```

### 8. Worktree-isolation code check

This is static, but should be checked while reproducing:

```bash
cd /tmp/otto-repro/otto-audit
sed -n '692,699p' otto/runner.py
```

Current broken behavior in audited revision:

```python
agent_opts = ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    cwd=str(project_dir),
    ...
)
```
