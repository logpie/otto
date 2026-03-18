# CC Autonomous v2 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bash-based worker (`ralph-loop.sh`) with a Python worker using `claude-agent-sdk` for session resume on retries, and redesign the web UI into a real productivity dashboard with SSE-based live updates.

**Architecture:** Python async worker (`worker.py`) picks tasks from the JSON queue, runs `claude-agent-sdk.query()` for each task, runs orchestrator-owned verification, and resumes the session on retry failures so the agent retains full context. The FastAPI manager spawns workers as subprocesses and pushes state to the frontend via Server-Sent Events. The frontend is extracted from the inline HTML string into separate static files with a redesigned dashboard layout.

**Tech Stack:** Python 3.11, claude-agent-sdk, FastAPI, uvicorn, vanilla HTML/CSS/JS, SSE

**Worktree:** All implementation work MUST happen in a git worktree to avoid interfering with the original code. Create with:
```bash
git worktree add .worktrees/v2 -b v2
```

---

## File Structure

### New files
| File | Responsibility |
|------|---------------|
| `worker.py` | Async worker loop: pick task, run claude-agent-sdk, verify, retry with session resume |
| `static/index.html` | Dashboard HTML structure |
| `static/style.css` | Dark theme styling |
| `static/app.js` | Frontend logic: SSE, task CRUD, worker controls, notifications |
| `tests/test_worker.py` | Unit tests for worker.py (queue helpers, prompt builder, verifier) |
| `tests/test_manager.py` | API endpoint tests for manager.py changes |

### Modified files
| File | Changes |
|------|---------|
| `manager.py` | Remove inline HTML, serve static files, spawn worker.py instead of ralph-loop.sh, add SSE endpoint, update task schema, remove verify.sh editor endpoints |

### Unchanged files
| File | Reason |
|------|--------|
| `ralph-loop.sh` | Kept as reference, not deleted |
| `start.sh` | Still runs manager.py |
| `demo-project/*` | Target project, not modified by orchestrator changes |

---

## Chunk 1: Worker Core

### Task 1: Install dependencies

**Files:**
- Modify: `.venv/` (install packages)

- [ ] **Step 1: Install claude-agent-sdk**

```bash
uv pip install claude-agent-sdk --python /Users/yuxuan/work/cc-autonomous/.venv/bin/python
```

- [ ] **Step 2: Install test dependencies**

```bash
uv pip install pytest pytest-asyncio httpx --python /Users/yuxuan/work/cc-autonomous/.venv/bin/python
```

- [ ] **Step 3: Verify installation**

```bash
/Users/yuxuan/work/cc-autonomous/.venv/bin/python -c "from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage; print('OK')"
```

Expected: `OK`

---

### Task 2: Worker — task queue helpers

**Files:**
- Create: `worker.py`
- Create: `tests/test_worker.py`

- [ ] **Step 1: Write failing tests for pick_task and update_task**

```python
# tests/test_worker.py
import json
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def tasks_file(tmp_path):
    """Create a temporary tasks.json with test data."""
    tasks = [
        {"id": "t1", "prompt": "do thing 1", "status": "completed",
         "created_at": "2026-01-01T00:00:00", "started_at": None,
         "finished_at": None, "worker": None, "attempts": 0},
        {"id": "t2", "prompt": "do thing 2", "status": "pending",
         "created_at": "2026-01-01T00:00:01", "started_at": None,
         "finished_at": None, "worker": None, "attempts": 0},
        {"id": "t3", "prompt": "do thing 3", "status": "pending",
         "created_at": "2026-01-01T00:00:02", "started_at": None,
         "finished_at": None, "worker": None, "attempts": 0},
    ]
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps(tasks, indent=2))
    return f


def test_pick_task_returns_first_pending(tasks_file):
    from worker import pick_task
    task = pick_task(tasks_file, "main")
    assert task is not None
    assert task["id"] == "t2"
    assert task["status"] == "in_progress"
    assert task["worker"] == "main"
    assert task["started_at"] is not None


def test_pick_task_updates_file(tasks_file):
    from worker import pick_task
    pick_task(tasks_file, "w1")
    tasks = json.loads(tasks_file.read_text())
    t2 = next(t for t in tasks if t["id"] == "t2")
    assert t2["status"] == "in_progress"
    assert t2["worker"] == "w1"


def test_pick_task_returns_none_when_no_pending(tasks_file):
    from worker import pick_task
    pick_task(tasks_file, "main")  # picks t2
    pick_task(tasks_file, "main")  # picks t3
    result = pick_task(tasks_file, "main")  # nothing left
    assert result is None


def test_update_task_sets_status(tasks_file):
    from worker import pick_task, update_task
    pick_task(tasks_file, "main")
    update_task(tasks_file, "t2", status="completed", attempts=1, cost_usd=0.05)
    tasks = json.loads(tasks_file.read_text())
    t2 = next(t for t in tasks if t["id"] == "t2")
    assert t2["status"] == "completed"
    assert t2["attempts"] == 1
    assert t2["cost_usd"] == 0.05
    assert t2["finished_at"] is not None


def test_update_task_no_finished_at_for_in_progress(tasks_file):
    from worker import update_task
    update_task(tasks_file, "t2", status="in_progress", attempts=1)
    tasks = json.loads(tasks_file.read_text())
    t2 = next(t for t in tasks if t["id"] == "t2")
    assert t2.get("finished_at") is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_worker.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'worker'`

- [ ] **Step 3: Implement pick_task and update_task**

```python
# worker.py
"""
CC Autonomous Worker v2 — Python worker using claude-agent-sdk.

Replaces ralph-loop.sh with:
- Session resume on retries (agent keeps context)
- Orchestrator-owned verification (agent never touches test harness)
- Cost tracking per task

Usage:
    python worker.py --tasks-file tasks.json --project-dir /path/to/project [options]
"""

import argparse
import asyncio
import fcntl
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

logger = logging.getLogger("worker")


# -- Task queue helpers -------------------------------------------------------

def _locked_task_rw(tasks_file: Path, mutator):
    """Read-modify-write tasks.json under a separate lockfile.

    Uses a dedicated .lock file so that flock operates on a stable inode
    (not the data file which gets replaced). Both worker and manager
    MUST use this same pattern to avoid lost writes.

    Args:
        tasks_file: Path to tasks.json
        mutator: callable(tasks: list[dict]) -> Any. Mutates tasks in place.
                 Return value is passed through.
    """
    import tempfile
    lock_path = tasks_file.with_suffix(".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = json.loads(tasks_file.read_text()) if tasks_file.exists() else []
        result = mutator(tasks)
        # Atomic write: temp file + os.replace
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(tasks_file.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(tasks, f, indent=2)
            os.replace(tmp_path, str(tasks_file))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return result


def pick_task(tasks_file: Path, worker_name: str = "main") -> dict | None:
    """Atomically pick the first pending task and mark it in_progress."""
    picked = [None]

    def _pick(tasks):
        for task in tasks:
            if task["status"] == "pending":
                task["status"] = "in_progress"
                task["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                task["worker"] = worker_name
                task.setdefault("attempts", 0)
                picked[0] = dict(task)  # snapshot
                return
    _locked_task_rw(tasks_file, _pick)
    return picked[0]


def update_task(tasks_file: Path, task_id: str, **updates) -> None:
    """Atomically update a task's fields."""
    def _update(tasks):
        for task in tasks:
            if task["id"] == task_id:
                task.update(updates)
                if updates.get("status") in ("completed", "failed"):
                    task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                break
    _locked_task_rw(tasks_file, _update)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_worker.py -v
```

Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add worker.py tests/test_worker.py
git commit -m "feat: add worker.py with task queue helpers (pick_task, update_task)"
```

---

### Task 3: Worker — prompt builder and verifier

**Files:**
- Modify: `worker.py` (add build_prompt, run_verify)
- Modify: `tests/test_worker.py` (add tests)

- [ ] **Step 1: Write failing tests for build_prompt**

```python
# Append to tests/test_worker.py

def test_build_prompt_first_attempt():
    from worker import build_prompt
    task = {"prompt": "Fix the bug", "verify_prompt": "", "verify_cmd": ""}
    result = build_prompt(task, attempt=1)
    assert "Fix the bug" in result
    assert "attempt" not in result.lower()  # No retry language on first attempt
    assert "NEVER ask questions" in result


def test_build_prompt_first_attempt_with_verify_prompt():
    from worker import build_prompt
    task = {"prompt": "Fix the bug", "verify_prompt": "tests must pass",
            "verify_cmd": ""}
    result = build_prompt(task, attempt=1)
    assert "tests must pass" in result
    assert "VERIFICATION GOAL" in result


def test_build_prompt_retry_includes_error():
    from worker import build_prompt
    task = {"prompt": "Fix the bug", "verify_prompt": "", "verify_cmd": ""}
    result = build_prompt(task, attempt=2, verify_error="AssertionError: 1 != 2")
    assert "attempt 2" in result
    assert "AssertionError: 1 != 2" in result
    assert "VERIFICATION ERROR" in result
    assert "Fix the bug" in result


def test_build_prompt_legacy_verify_field():
    """Old tasks have 'verify' instead of 'verify_prompt'."""
    from worker import build_prompt
    task = {"prompt": "Do it", "verify": "latency < 500ms"}
    result = build_prompt(task, attempt=1)
    assert "latency < 500ms" in result
```

- [ ] **Step 2: Write failing tests for run_verify**

```python
# Append to tests/test_worker.py

def test_run_verify_with_explicit_cmd(tmp_path):
    from worker import run_verify
    passed, output = run_verify(tmp_path, verify_cmd="echo 'all good'")
    assert passed is True
    assert "all good" in output


def test_run_verify_cmd_failure(tmp_path):
    from worker import run_verify
    passed, output = run_verify(tmp_path, verify_cmd="echo 'bad' && exit 1")
    assert passed is False
    assert "bad" in output


def test_run_verify_auto_detect_pytest(tmp_path):
    """When no verify_cmd, falls back to detecting test files."""
    from worker import run_verify
    test_file = tmp_path / "test_example.py"
    test_file.write_text("def test_ok(): assert True")
    passed, output = run_verify(tmp_path)
    assert passed is True


def test_run_verify_no_tests(tmp_path):
    """When no verify_cmd and no test files, pass by default."""
    from worker import run_verify
    passed, output = run_verify(tmp_path)
    assert passed is True
    assert "No verification" in output


def test_run_verify_timeout(tmp_path):
    from worker import run_verify
    passed, output = run_verify(tmp_path, verify_cmd="sleep 10", timeout=1)
    assert passed is False
    assert "timed out" in output.lower()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_worker.py -v -k "build_prompt or run_verify"
```

Expected: FAIL with `ImportError` (functions don't exist yet)

- [ ] **Step 4: Implement build_prompt**

```python
# Append to worker.py after update_task

# -- Prompt builder -----------------------------------------------------------

# NOTE: The old ralph-loop.sh included "VERIFICATION AUTHORING RULES" telling the
# agent to write/update verify.sh. Those are intentionally dropped in v2 because
# verification is now orchestrator-owned — the agent never touches the test harness.
_AUTONOMY_RULES = """\
RULES:
- NEVER ask questions or present options. Make the best decision and do it.
- If something fails, fix it yourself. Try alternative approaches.
- If an API is down or unreachable, switch to a working alternative.
- After making changes, run the tests/app yourself to verify it works.
- Do NOT stop until you have verified your changes work end-to-end.
- Then commit with a descriptive message.
- Do NOT add result caching or memoization to pass speed tests. Optimize the actual code path."""


def build_prompt(task: dict, attempt: int = 1, verify_error: str = "") -> str:
    """Build the prompt for Claude, including retry context if applicable."""
    # Support both old 'verify' field and new 'verify_prompt' field
    verify_goal = task.get("verify_prompt") or task.get("verify", "")
    verify_section = ""
    if verify_goal:
        verify_section = f"\nVERIFICATION GOAL:\n{verify_goal}\n"

    if attempt == 1:
        return (
            f"You are running autonomously with NO human in the loop.\n\n"
            f"{_AUTONOMY_RULES}\n"
            f"{verify_section}\n"
            f"TASK: {task['prompt']}"
        )
    else:
        return (
            f"You are running autonomously with NO human in the loop. "
            f"This is attempt {attempt}.\n\n"
            f"Your previous attempt FAILED verification. "
            f"Here is the error output:\n\n"
            f"--- VERIFICATION ERROR ---\n"
            f"{verify_error}\n"
            f"--- END ERROR ---\n\n"
            f"{_AUTONOMY_RULES}\n"
            f"{verify_section}\n"
            f"ORIGINAL TASK: {task['prompt']}"
        )
```

- [ ] **Step 5: Implement run_verify**

```python
# Append to worker.py after build_prompt

# -- Verification runner ------------------------------------------------------

def run_verify(
    project_dir: Path,
    verify_cmd: str | None = None,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Run verification command. Returns (passed, output).

    Priority:
    1. Explicit verify_cmd if provided
    2. Project verify.sh if it exists (backward compat with v1)
    3. Auto-detect test files (pytest, then unittest)
    4. Pass by default if nothing configured

    NOTE: The orchestrator runs verify.sh, but the agent does NOT write it.
    Existing verify.sh scripts from v1 are still honored.
    """
    if not verify_cmd:
        # Check for existing verify.sh (backward compat)
        verify_script = project_dir / "verify.sh"
        if verify_script.exists() and os.access(verify_script, os.X_OK):
            verify_cmd = "bash verify.sh"
        else:
            # Auto-detect test files (including one level of subdirectories)
            test_files = (
                list(project_dir.glob("test_*.py"))
                + list(project_dir.glob("*_test.py"))
                + list(project_dir.glob("*/test_*.py"))
                + list(project_dir.glob("*/*_test.py"))
            )
            if test_files:
                verify_cmd = f"{sys.executable} -m pytest -x --tb=short"
            else:
                return True, "No verification configured"

    try:
        result = subprocess.run(
            verify_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_dir),
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Verification timed out after {timeout}s"
    except Exception as e:
        return False, f"Verification error: {e}"
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_worker.py -v
```

Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add worker.py tests/test_worker.py
git commit -m "feat: add prompt builder and verification runner to worker"
```

---

### Task 4: Worker — main async loop with session resume

**Files:**
- Modify: `worker.py` (add run_task, worker_loop, main)
- Modify: `tests/test_worker.py` (add async tests)

- [ ] **Step 1: Write failing test for run_task with mocked SDK**

```python
# Append to tests/test_worker.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import ResultMessage


@pytest.fixture
def task_entry():
    return {
        "id": "test1",
        "prompt": "Fix the bug",
        "verify_prompt": "",
        "verify_cmd": "echo 'pass'",
        "status": "in_progress",
    }


@pytest.mark.asyncio
async def test_run_task_passes_on_first_attempt(tasks_file, task_entry, tmp_path):
    from worker import run_task

    mock_result = MagicMock(spec=ResultMessage)
    mock_result.session_id = "sess-123"
    mock_result.total_cost_usd = 0.05
    mock_result.subtype = "success"

    async def mock_query(**kwargs):
        yield mock_result

    with patch("worker.query", side_effect=mock_query):
        result = await run_task(task_entry, tmp_path, max_retries=3)

    assert result["status"] == "completed"
    assert result["attempts"] == 1
    assert result["cost_usd"] == 0.05
    assert result["session_id"] == "sess-123"


@pytest.mark.asyncio
async def test_run_task_resumes_session_on_retry(tasks_file, task_entry, tmp_path):
    from worker import run_task

    # First attempt: verification fails
    task_entry["verify_cmd"] = "exit 1"
    call_count = 0
    captured_options = []

    mock_result = MagicMock(spec=ResultMessage)
    mock_result.session_id = "sess-456"
    mock_result.total_cost_usd = 0.03
    mock_result.subtype = "success"

    async def mock_query(*, prompt, options=None):
        nonlocal call_count
        call_count += 1
        captured_options.append(options)
        if call_count >= 2:
            # Make verify pass on second attempt
            task_entry["verify_cmd"] = "echo 'fixed'"
        yield mock_result

    with patch("worker.query", side_effect=mock_query):
        result = await run_task(task_entry, tmp_path, max_retries=3)

    assert result["status"] == "completed"
    assert result["attempts"] == 2
    # Second call should have resume set
    assert captured_options[1].resume == "sess-456"


@pytest.mark.asyncio
async def test_run_task_fails_after_max_retries(task_entry, tmp_path):
    from worker import run_task

    task_entry["verify_cmd"] = "exit 1"

    mock_result = MagicMock(spec=ResultMessage)
    mock_result.session_id = "sess-789"
    mock_result.total_cost_usd = 0.02
    mock_result.subtype = "success"

    async def mock_query(**kwargs):
        yield mock_result

    with patch("worker.query", side_effect=mock_query):
        result = await run_task(task_entry, tmp_path, max_retries=2)

    assert result["status"] == "failed"
    assert result["attempts"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_worker.py -v -k "run_task"
```

Expected: FAIL with `ImportError` (run_task doesn't exist yet)

- [ ] **Step 3: Implement run_task**

```python
# Append to worker.py after run_verify

# -- Task runner with session resume ------------------------------------------
# NOTE: claude_agent_sdk imports are at the top of the file.

async def run_task(
    task: dict,
    project_dir: Path,
    max_retries: int = 3,
    tasks_file: Path | None = None,
    logs_dir: Path | None = None,
) -> dict:
    """Run a single task with verify-fix loop and session resume.

    If tasks_file is provided, persists state after each attempt so the
    UI shows live progress (attempts, cost, session_id) via SSE.

    If logs_dir is provided, writes per-task log to logs_dir/{task_id}.log
    so the dashboard Log button shows task-specific output.

    NOTE on cost: ResultMessage.total_cost_usd is per-query (not cumulative
    across resumed sessions), so we sum across attempts.
    """
    session_id = None
    verify_error = ""
    total_cost = 0.0

    # Per-task log file for the dashboard Log button
    task_log_handler = None
    if logs_dir:
        task_log_path = logs_dir / f"{task['id']}.log"
        task_log_handler = logging.FileHandler(task_log_path, mode="a")
        task_log_handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(task_log_handler)
        # Write attempt separator so all attempts are visible in one log
        logger.info(f"{'='*60}")
        logger.info(f"TASK {task['id']} — START")
        logger.info(f"{'='*60}")

    try:
        return await _run_task_inner(
            task, project_dir, max_retries, tasks_file,
            session_id, verify_error, total_cost,
        )
    finally:
        if task_log_handler:
            logger.removeHandler(task_log_handler)
            task_log_handler.close()


async def _run_task_inner(
    task, project_dir, max_retries, tasks_file,
    session_id, verify_error, total_cost,
):
    for attempt in range(1, max_retries + 1):
        logger.info(f"Task {task['id']}: attempt {attempt}/{max_retries}")

        # Persist attempt count immediately so UI shows live progress
        if tasks_file:
            update_task(tasks_file, task["id"], attempts=attempt)

        prompt = build_prompt(task, attempt, verify_error)

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            max_turns=50,
            max_budget_usd=5.0,
            cwd=str(project_dir),
            setting_sources=["project"],
        )
        if session_id and attempt > 1:
            options.resume = session_id

        result_msg = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result_msg = message
        except Exception as e:
            logger.error(f"  SDK error: {e}")
            verify_error = f"Claude Code process error: {e}"
            if tasks_file:
                update_task(tasks_file, task["id"],
                            last_error=str(e), cost_usd=total_cost)
            continue

        if result_msg:
            session_id = result_msg.session_id
            if result_msg.total_cost_usd:
                total_cost += result_msg.total_cost_usd
            logger.info(
                f"  Claude finished: {result_msg.subtype}, "
                f"cost=${result_msg.total_cost_usd or 0:.4f}"
            )
            # Persist cost and session_id after each attempt
            if tasks_file:
                update_task(tasks_file, task["id"],
                            cost_usd=total_cost, session_id=session_id)
        else:
            logger.warning("  No ResultMessage received from SDK")

        # Run verification
        verify_cmd = task.get("verify_cmd") or None
        verify_timeout = task.get("verify_timeout", 120)
        passed, output = run_verify(project_dir, verify_cmd, timeout=verify_timeout)

        if passed:
            logger.info("  Verification PASSED")
            return {
                "status": "completed",
                "attempts": attempt,
                "cost_usd": total_cost,
                "session_id": session_id,
            }

        verify_error = output
        logger.info(f"  Verification FAILED: {output[:200]}")
        if tasks_file:
            update_task(tasks_file, task["id"], last_error=output[:500])

    return {
        "status": "failed",
        "attempts": max_retries,
        "cost_usd": total_cost,
        "session_id": session_id,
    }
```

- [ ] **Step 4: Implement worker_loop and CLI entry point**

```python
# Append to worker.py after run_task

# -- Main loop ----------------------------------------------------------------

async def worker_loop(
    tasks_file: Path,
    project_dir: Path,
    worker_name: str = "main",
    poll_interval: int = 30,
    max_tasks: int = 0,
    max_retries: int = 3,
    logs_dir: Path | None = None,
) -> None:
    """Main worker loop: pick tasks, execute, verify, repeat."""
    task_count = 0

    logger.info(f"Worker '{worker_name}' starting")
    logger.info(f"  Tasks file: {tasks_file}")
    logger.info(f"  Project dir: {project_dir}")
    logger.info(f"  Max retries: {max_retries}")

    while True:
        task = pick_task(tasks_file, worker_name)
        if not task:
            logger.info(f"No pending tasks. Waiting {poll_interval}s...")
            await asyncio.sleep(poll_interval)
            continue

        task_count += 1
        logger.info(f"Task #{task_count} - ID: {task['id']}")
        logger.info(f"  Prompt: {task['prompt'][:100]}")

        # Per-task max_retries overrides worker default; sanitize for bad stored values
        try:
            effective_retries = max(1, min(int(task.get("max_retries", max_retries)), 10))
        except (ValueError, TypeError):
            effective_retries = max_retries
        result = await run_task(
            task, project_dir, effective_retries,
            tasks_file=tasks_file, logs_dir=logs_dir,
        )

        update_task(
            tasks_file,
            task["id"],
            status=result["status"],
            attempts=result["attempts"],
            cost_usd=result.get("cost_usd", 0),
            session_id=result.get("session_id"),
        )

        logger.info(
            f"Task {task['id']}: {result['status']} "
            f"(attempt {result['attempts']}/{max_retries}, "
            f"${result.get('cost_usd', 0):.4f})"
        )

        if max_tasks > 0 and task_count >= max_tasks:
            logger.info(f"Reached max tasks ({max_tasks}). Exiting.")
            break

    logger.info("Worker loop finished.")


# -- CLI entry point ----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CC Autonomous Worker v2")
    parser.add_argument("--tasks-file", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--worker-name", default="main")
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file))

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # logs_dir defaults to same directory as tasks_file / "logs"
    logs_dir = args.tasks_file.parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    asyncio.run(
        worker_loop(
            tasks_file=args.tasks_file,
            project_dir=args.project_dir,
            worker_name=args.worker_name,
            poll_interval=args.poll_interval,
            max_tasks=args.max_tasks,
            max_retries=args.max_retries,
            logs_dir=logs_dir,
        )
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run all tests**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_worker.py -v
```

Expected: All tests PASS

- [ ] **Step 6: Verify CLI entry point parses args**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python worker.py --help
```

Expected: Shows usage with all options

- [ ] **Step 7: Commit**

```bash
git add worker.py tests/test_worker.py
git commit -m "feat: add async task runner with session resume and CLI entry point"
```

---

## Chunk 2: Manager Backend Updates

### Task 5: Manager — update task schema and clean up endpoints

**Files:**
- Modify: `manager.py` (update task schema, remove verify.sh editor endpoints)
- Create: `tests/test_manager.py`

- [ ] **Step 1: Write failing tests for updated task API**

```python
# tests/test_manager.py
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_tmp(tmp_path):
    """Create a test app with temporary tasks file."""
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("[]")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    import manager
    manager.TASKS_FILE = tasks_file
    manager.LOGS_DIR = logs_dir
    manager.BASE_DIR = tmp_path
    return TestClient(manager.app)


def test_create_task_with_new_fields(app_with_tmp):
    resp = app_with_tmp.post("/api/tasks", json={
        "prompt": "Fix the bug",
        "verify_prompt": "tests must pass",
        "verify_cmd": "pytest -x",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["prompt"] == "Fix the bug"
    assert data["verify_prompt"] == "tests must pass"
    assert data["verify_cmd"] == "pytest -x"
    assert data["cost_usd"] == 0.0
    assert data["session_id"] is None


def test_create_task_backward_compat(app_with_tmp):
    """Old clients sending 'verify' field should still work."""
    resp = app_with_tmp.post("/api/tasks", json={
        "prompt": "Do something",
        "verify": "latency < 500ms",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["verify_prompt"] == "latency < 500ms"


def test_verify_script_endpoints_removed(app_with_tmp):
    resp = app_with_tmp.get("/api/verify-script")
    assert resp.status_code == 404

    resp = app_with_tmp.post("/api/verify-script", json={})
    assert resp.status_code in (404, 405)

    resp = app_with_tmp.post("/api/verify-run", json={})
    assert resp.status_code in (404, 405)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_manager.py -v
```

Expected: FAIL (old schema, endpoints still exist)

- [ ] **Step 3: Update task schema in manager.py**

In `manager.py`, update the `add_task` endpoint:

First, update `save_tasks()` in manager.py to use atomic writes (matching worker.py's pattern). Replace the existing `save_tasks` (around line 37-39) with:

```python
def _locked_task_rw_manager(mutator):
    """Same transaction pattern as worker's _locked_task_rw.

    All manager mutations (add, delete, retry) MUST use this instead of
    separate load_tasks()/save_tasks() to prevent lost updates under
    concurrent access.
    """
    import fcntl
    import tempfile
    lock_path = TASKS_FILE.with_suffix(".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = json.loads(TASKS_FILE.read_text()) if TASKS_FILE.exists() else []
        result = mutator(tasks)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(TASKS_FILE.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(tasks, f, indent=2)
            os.replace(tmp_path, str(TASKS_FILE))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return result
```

Then replace the task dict creation in `add_task()` (around line 59-69) with:

```python
@app.post("/api/tasks")
async def add_task(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, 400)

    # Support both old 'verify' field and new split fields
    verify_prompt = (
        body.get("verify_prompt", "").strip()
        or body.get("verify", "").strip()
    )
    verify_cmd = body.get("verify_cmd", "").strip()

    try:
        max_retries = int(body.get("max_retries", 3))
        max_retries = max(1, min(max_retries, 10))  # clamp to [1, 10]
    except (ValueError, TypeError):
        return JSONResponse({"error": "max_retries must be an integer 1-10"}, 400)

    task = {
        "id": uuid.uuid4().hex[:8],
        "prompt": prompt,
        "verify_prompt": verify_prompt,
        "verify_cmd": verify_cmd,
        "max_retries": max_retries,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "started_at": None,
        "finished_at": None,
        "worker": None,
        "attempts": 0,
        "cost_usd": 0.0,
        "session_id": None,
    }

    def _add(tasks):
        tasks.append(task)
    _locked_task_rw_manager(_add)
    return task
```

- [ ] **Step 4: Remove verify.sh editor endpoints**

Delete the following endpoints from `manager.py`:
- `GET /api/verify-script` (around line 201-208)
- `POST /api/verify-script` (around line 211-221)
- `POST /api/verify-run` (around line 224-246)

Keep `GET /api/verify-status` — it's still useful to know if a project has tests.

Also update `retry_task` and `delete_task` to use `_locked_task_rw_manager`:
```python
@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    def _retry(tasks):
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "pending"
                t["started_at"] = None
                t["finished_at"] = None
                t["worker"] = None
                t["attempts"] = 0
                t["cost_usd"] = 0.0
                t["session_id"] = None
                t.pop("last_error", None)
                break
    _locked_task_rw_manager(_retry)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    def _delete(tasks):
        tasks[:] = [t for t in tasks if t["id"] != task_id]
    _locked_task_rw_manager(_delete)
    return {"ok": True}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_manager.py -v
```

Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add manager.py tests/test_manager.py
git commit -m "feat: update task schema with verify_prompt/verify_cmd/cost, remove verify.sh endpoints"
```

---

### Task 6: Manager — SSE endpoint for live updates

**Files:**
- Modify: `manager.py` (add SSE endpoint)
- Modify: `tests/test_manager.py` (add SSE test)

- [ ] **Step 1: Write failing test for SSE endpoint**

```python
# Append to tests/test_manager.py

def test_sse_endpoint_returns_event_stream(app_with_tmp):
    """SSE endpoint should return text/event-stream content type."""
    with app_with_tmp.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Read first event
        for line in resp.iter_lines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                assert "tasks" in data
                assert "workers" in data
                break
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_manager.py::test_sse_endpoint_returns_event_stream -v
```

Expected: FAIL (endpoint doesn't exist)

- [ ] **Step 3: Implement SSE endpoint**

Add `import asyncio` and `from fastapi.responses import StreamingResponse` to the **import block at the top** of `manager.py`. Then add the endpoint after the existing API endpoints:

```python
# (imports go at top of file)
# import asyncio
# from fastapi.responses import StreamingResponse


@app.get("/api/events")
async def events():
    """Server-Sent Events endpoint for live task/worker updates."""
    async def event_stream():
        last_data = ""
        try:
            while True:
                tasks = load_tasks()
                workers_status = {}
                for name, proc in list(workers.items()):
                    poll = proc.poll()
                    workers_status[name] = {
                        "pid": proc.pid,
                        "status": "running" if poll is None else "exited",
                        "code": poll,
                    }
                current_data = json.dumps(
                    {"tasks": tasks, "workers": workers_status},
                    default=str,
                )
                if current_data != last_data:
                    yield f"data: {current_data}\n\n"
                    last_data = current_data
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/test_manager.py -v
```

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add manager.py tests/test_manager.py
git commit -m "feat: add SSE endpoint for live task/worker updates"
```

---

### Task 7: Manager — serve static files and spawn worker.py

**Files:**
- Modify: `manager.py` (static file serving, worker spawning, remove inline HTML)
- Create: `static/index.html` (placeholder)

- [ ] **Step 1: Create placeholder static/index.html**

```bash
mkdir -p static
```

```html
<!-- static/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CC Autonomous v2</title>
</head>
<body>
<h1>CC Autonomous v2</h1>
<p>Loading...</p>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Update manager.py to serve static files**

Replace the module-level `app = FastAPI(...)` and `index()` function with an app factory:

```python
from fastapi.staticfiles import StaticFiles


def create_app(
    base_dir: Path | None = None,
    tasks_file: Path | None = None,
    logs_dir: Path | None = None,
) -> FastAPI:
    """App factory. All mutable state lives on app.state, not module globals.

    Routes access config via request.app.state so multiple app instances
    (e.g., in tests) are fully isolated.
    """
    _base = base_dir or BASE_DIR
    _tasks = tasks_file or TASKS_FILE
    _logs = logs_dir or LOGS_DIR

    app = FastAPI(title="CC Autonomous")

    # Store config and runtime state on app.state
    app.state.base_dir = _base
    app.state.tasks_file = _tasks
    app.state.logs_dir = _logs
    app.state.workers = {}        # name -> Popen
    app.state.worker_dirs = {}    # name -> resolved project_dir

    # Mount static files with resolved path at creation time
    static_dir = _base / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (app.state.base_dir / "static" / "index.html").read_text()

    # ... register all other routes on `app` here ...
    # Routes read from request.app.state.tasks_file, request.app.state.workers, etc.
    # Use APIRouter for route definitions, then app.include_router(router).
    return app


# Module-level app for uvicorn
app = create_app()
```

Then update the test fixture:
```python
@pytest.fixture
def app_with_tmp(tmp_path):
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("[]")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>v2</body></html>")

    import manager
    test_app = manager.create_app(
        base_dir=tmp_path, tasks_file=tasks_file, logs_dir=logs_dir
    )
    return TestClient(test_app)
```

NOTE: All route handlers must access state via `request.app.state` (not module globals). For example:
```python
@router.get("/api/tasks")
def get_tasks(request: Request):
    tasks_file = request.app.state.tasks_file
    return json.loads(tasks_file.read_text()) if tasks_file.exists() else []
```
This is a significant refactor of manager.py but ensures test isolation and eliminates module-level mutation.

- [ ] **Step 3: Update worker spawning to use worker.py**

Replace the `start_worker` endpoint body with:

```python
@app.post("/api/workers/start")
async def start_worker(request: Request):
    body = await request.json()
    name = body.get("name", "main")
    project_dir = str(Path(body.get("project_dir", str(BASE_DIR))).resolve())
    max_retries = body.get("max_retries", 3)

    # Clean up exited workers from worker_dirs
    for wname in list(worker_dirs):
        if wname not in workers or workers[wname].poll() is not None:
            worker_dirs.pop(wname, None)

    if name in workers and workers[name].poll() is None:
        return JSONResponse({"error": f"Worker '{name}' already running"}, 400)

    # Prevent multiple workers on the same project_dir (they'd race on files)
    for wname, proc in workers.items():
        if proc.poll() is None and worker_dirs.get(wname) == project_dir:
            return JSONResponse(
                {"error": f"Worker '{wname}' already running on {project_dir}"},
                400,
            )

    log_path = LOGS_DIR / f"worker-{name}.log"
    cmd = [
        sys.executable,
        str(BASE_DIR / "worker.py"),
        "--tasks-file", str(TASKS_FILE),
        "--project-dir", project_dir,
        "--worker-name", name,
        "--max-retries", str(max_retries),
        "--log-file", str(log_path),
    ]

    # Add `worker_dirs: dict[str, str] = {}` next to the existing `workers` dict
    # at module level in manager.py.
    # NOTE: Worktree support is intentionally dropped in v2. Worker.py sets
    # cwd via ClaudeAgentOptions(cwd=project_dir). For parallel workers,
    # pass different project_dir values pointing to separate worktrees.

    # Worker handles its own logging via --log-file. Do NOT also redirect
    # stdout/stderr to the same file (would duplicate log lines).
    # Redirect to DEVNULL instead — all useful output goes through the logger.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
    )
    workers[name] = proc
    worker_dirs[name] = project_dir  # Track for duplicate-project check
    return {"name": name, "pid": proc.pid, "status": "started"}
```

Add `import sys` at the top if not already present.

- [ ] **Step 4: Verify the server starts and serves the placeholder**

```bash
cd /Users/yuxuan/work/cc-autonomous && timeout 3 .venv/bin/python -c "
import uvicorn
from manager import app
uvicorn.run(app, host='127.0.0.1', port=18420, log_level='error')
" &
sleep 1
curl -s http://127.0.0.1:18420/ | head -5
kill %1 2>/dev/null
```

Expected: Shows the placeholder HTML with `CC Autonomous v2`

- [ ] **Step 5: Commit**

```bash
git add manager.py static/index.html
git commit -m "feat: serve static files, spawn worker.py instead of ralph-loop.sh"
```

---

## Chunk 3: Frontend Redesign

### Task 8: Frontend — HTML structure

**Files:**
- Create: `static/index.html` (full layout)

- [ ] **Step 1: Write the complete HTML structure**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="CC Auto">
<title>CC Autonomous</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<!-- Status bar -->
<header id="statusBar">
  <h1>CC Autonomous</h1>
  <div id="statusSummary" class="status-summary">Connecting...</div>
</header>

<!-- Project config (collapsible) -->
<details class="section" id="projectSection" open>
  <summary>Project</summary>
  <div class="section-body">
    <input type="text" id="projectDir" placeholder="Project directory (absolute path)">
    <div class="info-row" id="verifyInfo">
      <span class="label">Verification:</span>
      <span class="value" id="verifyDetail">set project dir above</span>
    </div>
  </div>
</details>

<!-- Task input -->
<div class="section" id="inputSection">
  <div class="task-input-wrap">
    <textarea id="taskInput" placeholder="Describe the task..." rows="2"></textarea>
    <button id="addTaskBtn" class="btn primary" onclick="addTask()">Add</button>
  </div>
  <details class="advanced-toggle">
    <summary>Advanced</summary>
    <div class="advanced-fields">
      <input type="text" id="verifyPromptInput"
             placeholder="Verify goal (e.g. 'E2E latency < 500ms')">
      <input type="text" id="verifyCmdInput"
             placeholder="Verify command (e.g. 'pytest -x')">
      <div class="field-row">
        <label>Max retries:</label>
        <input type="number" id="maxRetriesInput" value="3" min="1" max="10" class="small-input">
      </div>
    </div>
  </details>
</div>

<!-- Workers -->
<div class="section">
  <div class="section-header">
    <h2>Workers</h2>
    <div class="worker-actions">
      <button class="btn primary small" onclick="startWorker()">Start</button>
      <button class="btn danger small" onclick="stopAllWorkers()">Stop All</button>
    </div>
  </div>
  <div id="workerList" class="worker-list">
    <div class="empty-state">No workers running</div>
  </div>
</div>

<!-- Task list -->
<div class="section">
  <div class="section-header">
    <h2>Tasks</h2>
    <div class="task-filters">
      <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
      <button class="filter-btn" data-filter="active" onclick="setFilter('active')">Active</button>
      <button class="filter-btn" data-filter="done" onclick="setFilter('done')">Done</button>
    </div>
  </div>
  <div id="taskList" class="task-list"></div>
</div>

<!-- Git log (collapsible) -->
<details class="section" id="gitSection">
  <summary>Git Log</summary>
  <div class="section-body">
    <pre id="gitLog" class="git-log">set project dir above</pre>
  </div>
</details>

<!-- Toast container -->
<div id="toasts" class="toast-container"></div>

<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add static/index.html
git commit -m "feat: add HTML structure for v2 dashboard"
```

---

### Task 9: Frontend — CSS styling

**Files:**
- Create: `static/style.css`

- [ ] **Step 1: Write the complete CSS**

```css
/* static/style.css — CC Autonomous v2 dark theme */

* { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #1c2128;
  --border: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --text-muted: #6e7681;
  --accent-blue: #58a6ff;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-yellow: #d29922;
  --accent-purple: #bc8cff;
  --radius: 8px;
  --radius-sm: 6px;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  padding: 0 16px 32px;
  max-width: 720px;
  margin: 0 auto;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* --- Header / Status bar --- */
header {
  position: sticky;
  top: 0;
  background: var(--bg-primary);
  border-bottom: 1px solid var(--border);
  padding: 12px 0;
  margin-bottom: 16px;
  z-index: 100;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

header h1 {
  font-size: 18px;
  font-weight: 600;
  color: var(--accent-blue);
}

.status-summary {
  font-size: 13px;
  color: var(--text-secondary);
}

.status-summary .count {
  font-weight: 600;
}

.status-summary .cost {
  color: var(--accent-green);
}

/* --- Sections --- */
.section {
  margin-bottom: 16px;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.section-header h2 {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.section-body {
  padding-top: 8px;
}

details.section > summary {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  cursor: pointer;
  padding: 4px 0;
  list-style: none;
}

details.section > summary::before {
  content: '\25B6';
  display: inline-block;
  margin-right: 6px;
  font-size: 10px;
  transition: transform 0.15s;
}

details.section[open] > summary::before {
  transform: rotate(90deg);
}

/* --- Inputs --- */
input[type="text"],
input[type="number"],
textarea {
  width: 100%;
  padding: 10px 12px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 14px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s;
}

input:focus, textarea:focus {
  border-color: var(--accent-blue);
}

textarea {
  resize: vertical;
  min-height: 56px;
}

.small-input {
  width: 60px;
}

/* --- Buttons --- */
.btn {
  padding: 8px 16px;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.15s;
  white-space: nowrap;
}

.btn:active { opacity: 0.7; }
.btn.primary { background: #238636; color: #fff; }
.btn.danger { background: #da3633; color: #fff; }
.btn.secondary { background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); }
.btn.small { padding: 4px 12px; font-size: 12px; }
.btn.tiny { padding: 3px 8px; font-size: 11px; }

/* --- Task input --- */
.task-input-wrap {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.task-input-wrap textarea {
  flex: 1;
}

.task-input-wrap .btn {
  margin-top: 1px;
  padding: 10px 20px;
}

.advanced-toggle {
  margin-top: 6px;
}

.advanced-toggle > summary {
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
  list-style: none;
}

.advanced-toggle > summary::before {
  content: '+ ';
}

.advanced-toggle[open] > summary::before {
  content: '- ';
}

.advanced-fields {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
}

.advanced-fields input {
  font-size: 13px;
  padding: 8px 10px;
}

.field-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.field-row label {
  font-size: 13px;
  color: var(--text-secondary);
  white-space: nowrap;
}

/* --- Info row --- */
.info-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
}

.info-row .label { color: var(--text-muted); }
.info-row .value { color: var(--text-secondary); }

/* --- Worker list --- */
.worker-actions {
  display: flex;
  gap: 6px;
}

.worker-list .empty-state {
  color: var(--text-muted);
  font-size: 13px;
  padding: 8px 0;
}

.worker-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  margin-bottom: 4px;
  font-size: 13px;
}

.worker-card .worker-name { font-weight: 500; }
.worker-card .worker-meta { color: var(--text-secondary); flex: 1; }

/* --- Badges --- */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.4;
}

.badge.pending { background: #1f2937; color: #9ca3af; }
.badge.in_progress { background: #1e3a5f; color: var(--accent-blue); }
.badge.completed { background: #0d2818; color: var(--accent-green); }
.badge.failed { background: #3d1418; color: var(--accent-red); }
.badge.running { background: #1e3a5f; color: var(--accent-blue); }
.badge.exited { background: #1f2937; color: #9ca3af; }

/* --- Task filters --- */
.task-filters {
  display: flex;
  gap: 2px;
  background: var(--bg-tertiary);
  border-radius: var(--radius-sm);
  padding: 2px;
}

.filter-btn {
  padding: 4px 10px;
  border: none;
  border-radius: 4px;
  font-size: 12px;
  color: var(--text-secondary);
  background: transparent;
  cursor: pointer;
  transition: all 0.15s;
}

.filter-btn.active {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

/* --- Task cards --- */
.task-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
  transition: border-color 0.15s;
}

.task-card:hover {
  border-color: #484f58;
}

.task-card.in_progress {
  border-left: 3px solid var(--accent-blue);
}

.task-card.failed {
  border-left: 3px solid var(--accent-red);
}

.task-card.completed {
  border-left: 3px solid var(--accent-green);
}

.task-prompt {
  font-size: 14px;
  word-break: break-word;
  line-height: 1.4;
}

.task-verify-goal {
  font-size: 12px;
  color: var(--accent-yellow);
  margin-top: 4px;
}

.task-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-muted);
}

.task-meta .cost {
  color: var(--accent-green);
}

/* Progress bar for attempts */
.attempt-bar {
  display: inline-flex;
  gap: 3px;
  align-items: center;
}

.attempt-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--border);
}

.attempt-dot.done { background: var(--accent-green); }
.attempt-dot.fail { background: var(--accent-red); }
.attempt-dot.active { background: var(--accent-blue); animation: pulse 1.5s infinite; }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.task-actions {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

/* --- Log box --- */
.log-box {
  background: var(--bg-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px;
  margin-top: 8px;
  max-height: 400px;
  overflow-y: auto;
  font-family: 'SF Mono', 'Menlo', 'Monaco', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-secondary);
}

/* --- Git log --- */
.git-log {
  font-family: 'SF Mono', 'Menlo', monospace;
  font-size: 12px;
  color: var(--text-muted);
  white-space: pre;
  overflow-x: auto;
  max-height: 150px;
  overflow-y: auto;
  padding: 8px 0;
}

/* --- Toasts --- */
.toast-container {
  position: fixed;
  bottom: 16px;
  right: 16px;
  display: flex;
  flex-direction: column-reverse;
  gap: 8px;
  z-index: 200;
  max-width: 320px;
}

.toast {
  padding: 10px 16px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  color: #fff;
  animation: slideIn 0.2s ease-out;
  cursor: pointer;
}

.toast.success { background: #238636; }
.toast.error { background: #da3633; }
.toast.info { background: #1f6feb; }

@keyframes slideIn {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}

/* --- Responsive --- */
@media (max-width: 480px) {
  body { padding: 0 12px 24px; }
  header h1 { font-size: 16px; }
  .task-input-wrap { flex-direction: column; }
  .task-input-wrap .btn { align-self: flex-end; }
}

/* --- Project dir input --- */
#projectDir {
  margin-bottom: 0;
}

/* --- Empty states --- */
.task-list:empty::after {
  content: 'No tasks yet';
  color: var(--text-muted);
  font-size: 13px;
  padding: 16px 0;
  display: block;
  text-align: center;
}
```

- [ ] **Step 2: Commit**

```bash
git add static/style.css
git commit -m "feat: add dark theme CSS for v2 dashboard"
```

---

### Task 10: Frontend — JavaScript

**Files:**
- Create: `static/app.js`

- [ ] **Step 1: Write the complete JavaScript**

```javascript
// static/app.js — CC Autonomous v2 frontend

const API = '';
let currentFilter = 'all';
let openLogs = new Set();
let evtSource = null;
let lastTasks = [];
let lastWorkers = {};

// -- Initialization ----------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  // Restore project dir from localStorage
  const saved = localStorage.getItem('cc-auto-project-dir');
  if (saved) document.getElementById('projectDir').value = saved;

  document.getElementById('projectDir').addEventListener('input', e => {
    localStorage.setItem('cc-auto-project-dir', e.target.value);
    debounce(refreshProjectInfo, 500)();
  });

  // Enter to submit task
  document.getElementById('taskInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      addTask();
    }
  });

  // Request notification permission
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  // Start SSE
  connectSSE();
  refreshProjectInfo();
});


// -- SSE Connection ----------------------------------------------------------

function connectSSE() {
  if (evtSource) evtSource.close();

  evtSource = new EventSource(API + '/api/events');

  evtSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    const oldTasks = lastTasks;
    lastTasks = data.tasks || [];
    lastWorkers = data.workers || {};

    // Detect task completions/failures for notifications
    for (const task of lastTasks) {
      const old = oldTasks.find(t => t.id === task.id);
      if (old && old.status === 'in_progress' && task.status === 'completed') {
        notify('success', `Task completed: ${task.prompt.slice(0, 60)}`);
      } else if (old && old.status === 'in_progress' && task.status === 'failed') {
        notify('error', `Task failed: ${task.prompt.slice(0, 60)}`);
      }
    }

    renderStatusBar();
    renderWorkers();
    renderTasks();
  };

  evtSource.onerror = () => {
    document.getElementById('statusSummary').textContent = 'Disconnected. Reconnecting...';
  };
}


// -- Rendering ---------------------------------------------------------------

function renderStatusBar() {
  const counts = { pending: 0, in_progress: 0, completed: 0, failed: 0 };
  let totalCost = 0;
  for (const t of lastTasks) {
    counts[t.status] = (counts[t.status] || 0) + 1;
    totalCost += t.cost_usd || 0;
  }

  const parts = [];
  if (counts.pending) parts.push(`<span class="count">${counts.pending}</span> pending`);
  if (counts.in_progress) parts.push(`<span class="count">${counts.in_progress}</span> running`);
  if (counts.completed) parts.push(`<span class="count">${counts.completed}</span> done`);
  if (counts.failed) parts.push(`<span class="count">${counts.failed}</span> failed`);
  if (totalCost > 0) parts.push(`<span class="cost">$${totalCost.toFixed(2)}</span>`);

  document.getElementById('statusSummary').innerHTML = parts.join(' · ') || 'No tasks';
}

function renderWorkers() {
  const el = document.getElementById('workerList');
  const entries = Object.entries(lastWorkers);
  if (entries.length === 0) {
    el.innerHTML = '<div class="empty-state">No workers running</div>';
    return;
  }

  el.innerHTML = entries.map(([name, w]) => {
    // Find the task this worker is working on
    const currentTask = lastTasks.find(
      t => t.worker === name && t.status === 'in_progress'
    );
    const taskInfo = currentTask
      ? `working on <em>${esc(currentTask.prompt.slice(0, 40))}</em>`
      : '';

    return `
      <div class="worker-card">
        <span class="badge ${w.status}">${w.status}</span>
        <span class="worker-name">${esc(name)}</span>
        <span class="worker-meta">${taskInfo}</span>
        ${w.status === 'running'
          ? `<button class="btn danger tiny" onclick="stopWorker('${esc(name)}')">Stop</button>`
          : ''}
      </div>`;
  }).join('');
}

function renderTasks() {
  const el = document.getElementById('taskList');
  let tasks = lastTasks.slice().reverse();

  // Apply filter
  if (currentFilter === 'active') {
    tasks = tasks.filter(t => t.status === 'pending' || t.status === 'in_progress');
  } else if (currentFilter === 'done') {
    tasks = tasks.filter(t => t.status === 'completed' || t.status === 'failed');
  }

  el.innerHTML = tasks.map(t => {
    const attempts = t.attempts || 0;
    const maxRetries = t.max_retries || 3;
    const cost = t.cost_usd ? `$${t.cost_usd.toFixed(3)}` : '';
    const elapsed = t.started_at ? formatElapsed(t.started_at, t.finished_at) : '';
    const verifyGoal = t.verify_prompt || t.verify || '';

    // Build attempt dots
    let attemptDots = '';
    if (t.status === 'in_progress' || t.status === 'failed' || attempts > 0) {
      const dots = [];
      for (let i = 1; i <= maxRetries; i++) {
        if (attempts === 0 && t.status === 'in_progress' && i === 1)
          dots.push('<span class="attempt-dot active"></span>');
        else if (i < attempts) dots.push('<span class="attempt-dot done"></span>');
        else if (i === attempts && t.status === 'in_progress')
          dots.push('<span class="attempt-dot active"></span>');
        else if (i === attempts && t.status === 'failed')
          dots.push('<span class="attempt-dot fail"></span>');
        else if (i <= attempts && t.status === 'completed')
          dots.push('<span class="attempt-dot done"></span>');
        else dots.push('<span class="attempt-dot"></span>');
      }
      attemptDots = `<span class="attempt-bar">${dots.join('')}</span>`;
    }

    return `
      <div class="task-card ${t.status}">
        <div class="task-prompt">${esc(t.prompt)}</div>
        ${verifyGoal ? `<div class="task-verify-goal">Verify: ${esc(verifyGoal)}</div>` : ''}
        <div class="task-meta">
          <span class="badge ${t.status}">${t.status}</span>
          ${attemptDots}
          ${cost ? `<span class="cost">${cost}</span>` : ''}
          ${elapsed ? `<span>${elapsed}</span>` : ''}
          ${t.worker ? `<span>${esc(t.worker)}</span>` : ''}
          <span style="color:var(--text-muted)">${t.id}</span>
        </div>
        <div class="task-actions">
          <button class="btn secondary tiny" onclick="toggleLog('${t.id}')">Log</button>
          ${['failed', 'completed'].includes(t.status)
            ? `<button class="btn primary tiny" onclick="retryTask('${t.id}')">Retry</button>`
            : ''}
          <button class="btn danger tiny" onclick="deleteTask('${t.id}')">Delete</button>
        </div>
        <div class="log-box" id="log-${t.id}" style="display:none"></div>
      </div>`;
  }).join('');

  // Re-open logs that were open
  for (const id of openLogs) {
    const logEl = document.getElementById('log-' + id);
    if (logEl) {
      logEl.style.display = 'block';
      refreshLog(id);
    }
  }
}


// -- Actions -----------------------------------------------------------------

async function addTask() {
  const input = document.getElementById('taskInput');
  const prompt = input.value.trim();
  if (!prompt) return;

  const verifyPrompt = document.getElementById('verifyPromptInput').value.trim();
  const verifyCmd = document.getElementById('verifyCmdInput').value.trim();
  const maxRetries = parseInt(document.getElementById('maxRetriesInput').value) || 3;

  await fetch(API + '/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt,
      verify_prompt: verifyPrompt,
      verify_cmd: verifyCmd,
      max_retries: maxRetries,
    }),
  });

  input.value = '';
  document.getElementById('verifyPromptInput').value = '';
  document.getElementById('verifyCmdInput').value = '';
  notify('info', 'Task added');
}

async function deleteTask(id) {
  await fetch(API + '/api/tasks/' + id, { method: 'DELETE' });
}

async function retryTask(id) {
  await fetch(API + '/api/tasks/' + id + '/retry', { method: 'POST' });
  notify('info', 'Task queued for retry');
}

async function startWorker(name) {
  const projectDir = document.getElementById('projectDir').value.trim();
  if (!projectDir) {
    notify('error', 'Set project directory first');
    return;
  }
  if (!name) name = 'main';
  await fetch(API + '/api/workers/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, project_dir: projectDir }),
  });
  notify('info', `Worker '${name}' started`);
}

async function stopWorker(name) {
  await fetch(API + '/api/workers/' + name + '/stop', { method: 'POST' });
  notify('info', `Worker '${name}' stopped`);
}

async function stopAllWorkers() {
  for (const name of Object.keys(lastWorkers)) {
    await fetch(API + '/api/workers/' + name + '/stop', { method: 'POST' });
  }
  notify('info', 'All workers stopped');
}

function setFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });
  renderTasks();
}


// -- Log viewer --------------------------------------------------------------

async function toggleLog(id) {
  const el = document.getElementById('log-' + id);
  if (!el) return;
  if (el.style.display === 'block') {
    el.style.display = 'none';
    openLogs.delete(id);
  } else {
    el.style.display = 'block';
    openLogs.add(id);
    await refreshLog(id);
  }
}

async function refreshLog(id) {
  const el = document.getElementById('log-' + id);
  if (!el || el.style.display === 'none') return;
  try {
    const res = await fetch(API + '/api/tasks/' + id + '/log');
    const data = await res.json();
    el.textContent = data.log || '(empty)';
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.textContent = '(error loading log)';
  }
}


// -- Project info ------------------------------------------------------------

async function refreshProjectInfo() {
  const dir = document.getElementById('projectDir').value.trim();
  if (!dir) {
    document.getElementById('verifyDetail').textContent = 'set project dir above';
    return;
  }
  try {
    const res = await fetch(API + '/api/verify-status?project_dir=' + encodeURIComponent(dir));
    const data = await res.json();
    const el = document.getElementById('verifyDetail');
    if (data.type === 'verify.sh') {
      el.textContent = 'verify.sh found';
      el.style.color = 'var(--accent-green)';
    } else if (data.type === 'auto-tests') {
      el.textContent = 'Tests: ' + data.detail;
      el.style.color = 'var(--accent-yellow)';
    } else {
      el.textContent = data.detail;
      el.style.color = 'var(--accent-red)';
    }
  } catch (e) {
    document.getElementById('verifyDetail').textContent = 'error checking project';
  }

  // Also refresh git log
  try {
    const res = await fetch(API + '/api/git-log?project_dir=' + encodeURIComponent(dir));
    const data = await res.json();
    document.getElementById('gitLog').textContent = data.log || '(empty)';
  } catch (e) {}
}


// -- Notifications -----------------------------------------------------------

function notify(type, message) {
  // In-page toast
  const container = document.getElementById('toasts');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toast.onclick = () => toast.remove();
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);

  // Browser notification for task completion/failure
  if ((type === 'success' || type === 'error') &&
      'Notification' in window &&
      Notification.permission === 'granted') {
    new Notification('CC Autonomous', { body: message });
  }
}


// -- Utilities ---------------------------------------------------------------

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function formatElapsed(start, end) {
  const s = new Date(start);
  const e = end ? new Date(end) : new Date();
  const diff = Math.floor((e - s) / 1000);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
  return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
}

let debounceTimers = {};
function debounce(fn, ms) {
  return (...args) => {
    clearTimeout(debounceTimers[fn.name]);
    debounceTimers[fn.name] = setTimeout(() => fn(...args), ms);
  };
}
```

- [ ] **Step 2: Verify frontend loads correctly**

Start the server and open in browser:
```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python manager.py &
sleep 2
curl -s http://localhost:8420/ | grep "CC Autonomous"
curl -s http://localhost:8420/static/style.css | head -3
curl -s http://localhost:8420/static/app.js | head -3
kill %1 2>/dev/null
```

Expected: All three return content. HTML contains "CC Autonomous", CSS starts with comment, JS starts with comment.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat: add JavaScript for v2 dashboard with SSE and notifications"
```

---

### Task 11: Integration smoke test

**Files:**
- Modify: `tests/test_manager.py` (add smoke tests)

- [ ] **Step 1: Write integration smoke test**

```python
# Append to tests/test_manager.py

def test_full_flow_smoke(app_with_tmp):
    """Smoke test: create task, read it back, verify schema."""
    # Create
    resp = app_with_tmp.post("/api/tasks", json={
        "prompt": "Test task",
        "verify_prompt": "it works",
        "verify_cmd": "echo ok",
    })
    assert resp.status_code == 200
    task = resp.json()
    task_id = task["id"]

    # List
    resp = app_with_tmp.get("/api/tasks")
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["verify_prompt"] == "it works"
    assert tasks[0]["verify_cmd"] == "echo ok"
    assert tasks[0]["cost_usd"] == 0.0
    assert tasks[0]["session_id"] is None

    # Retry
    resp = app_with_tmp.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200

    # Delete
    resp = app_with_tmp.post("/api/tasks", json={"prompt": "Second task"})
    id2 = resp.json()["id"]
    resp = app_with_tmp.delete(f"/api/tasks/{id2}")
    assert resp.status_code == 200
    resp = app_with_tmp.get("/api/tasks")
    assert len(resp.json()) == 1


def test_index_serves_static_html(app_with_tmp):
    """Verify index serves the static HTML file."""
    import manager
    static_path = manager.BASE_DIR / "static"
    static_path.mkdir(exist_ok=True)
    (static_path / "index.html").write_text("<html><body>v2</body></html>")

    resp = app_with_tmp.get("/")
    assert resp.status_code == 200
    assert "v2" in resp.text


def test_worker_start_uses_worker_py(app_with_tmp):
    """Verify worker start spawns worker.py, not ralph-loop.sh."""
    import manager
    from unittest.mock import patch, MagicMock

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.poll.return_value = None

    with patch("manager.subprocess.Popen", return_value=mock_proc) as mock_popen:
        resp = app_with_tmp.post("/api/workers/start", json={
            "name": "test",
            "project_dir": "/tmp/test",
        })
        assert resp.status_code == 200
        cmd = mock_popen.call_args[0][0]
        assert "worker.py" in cmd[1]
        assert "ralph-loop.sh" not in " ".join(cmd)
```

- [ ] **Step 2: Run all tests**

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python -m pytest tests/ -v
```

Expected: All tests PASS

- [ ] **Step 3: Manual verification checklist**

Verify by starting the server and checking in a browser:

```bash
cd /Users/yuxuan/work/cc-autonomous && .venv/bin/python manager.py
```

Then open `http://localhost:8420` and verify:
- [ ] Status bar shows "No tasks" at top right
- [ ] Project dir input is present and restores from localStorage
- [ ] Task textarea accepts input, Enter submits
- [ ] Advanced section expands to show verify_prompt, verify_cmd, max_retries
- [ ] Worker Start/Stop buttons are visible
- [ ] Task filter tabs (All/Active/Done) work
- [ ] Git Log section is collapsible
- [ ] SSE connection establishes (check Network tab for `/api/events`)
- [ ] Adding a task shows a toast notification and the task appears
- [ ] Task card shows prompt, badge, attempt dots
- [ ] Log button toggles log view
- [ ] On mobile viewport (DevTools), layout adapts

- [ ] **Step 4: Commit**

```bash
git add tests/test_manager.py
git commit -m "test: add integration smoke tests for v2 API and UI"
```

---

## Verification Criteria

After all tasks are complete, verify:

1. **Worker starts and picks tasks:**
   ```bash
   .venv/bin/python worker.py --tasks-file tasks.json --project-dir demo-project --max-tasks 1
   ```
   Should pick a pending task, run claude-agent-sdk, verify, and update status.

2. **Session resume works on retry:** Check that the second attempt's `options.resume` is set to the session_id from the first attempt (visible in worker logs).

3. **SSE delivers live updates:** Open browser, start worker from UI, watch task status change in real-time without page refresh.

4. **Cost tracking:** After a task completes, the task card should show the dollar cost.

5. **Old tasks.json is compatible:** The existing tasks with `verify` field should still render correctly in the new UI (backward compat via `verify_prompt || verify`).

6. **ralph-loop.sh is untouched:** `git diff ralph-loop.sh` shows no changes.

---

## Plan Review

### Round 1 — Codex
- [FIXED] tasks.json concurrency: manager writes unlocked — added shared lockfile pattern (`_locked_task_rw` + `.lock` file) for both worker and manager
- [FIXED] Verification regresses verify.sh — restored verify.sh as priority 2 in `run_verify`, expanded test discovery to subdirs
- [FIXED] max_retries stored but not enforced per task — worker reads `task["max_retries"]` and overrides default
- [FIXED] Task logs broken (no per-task log files) — added per-task `FileHandler` lifecycle in `run_task`
- [FIXED] SSE won't show retry progress — `run_task` persists state incrementally after each attempt
- [FIXED] Multi-worker same-project footgun — added `worker_dirs` tracking with duplicate-project check
- [FIXED] SDK cost double-counting — documented per-query semantics, used `MagicMock(spec=ResultMessage)` in tests
- [FIXED] Crash recovery incomplete — `retry_task` resets all run-derived fields (cost_usd, session_id, last_error)
- [FIXED] Duplicate logging — worker owns all file logging, manager redirects subprocess to DEVNULL
- [NOTED] Static file test brittleness — deferred to app factory (addressed in round 2)

### Round 2 — Codex
- [FIXED] Manager mutations lose updates under concurrency — all mutations use `_locked_task_rw_manager()` transactions
- [FIXED] App factory test isolation — `create_app()` stores all state on `app.state`, routes access via `request.app.state`
- [FIXED] max_retries not validated on worker side — worker sanitizes with `int()`, clamp to [1,10], fallback

### Round 3 — Codex
- [FIXED] Per-task logs truncate on retry — changed to `mode="a"` (append) with attempt separators

### Round 4 — Codex
- APPROVED. No new issues.
