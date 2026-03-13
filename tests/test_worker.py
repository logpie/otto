import json

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
    assert task["heartbeat_at"] is not None


def test_pick_task_updates_file(tasks_file):
    from worker import pick_task
    pick_task(tasks_file, "w1")
    tasks = json.loads(tasks_file.read_text())
    t2 = next(t for t in tasks if t["id"] == "t2")
    assert t2["status"] == "in_progress"
    assert t2["worker"] == "w1"
    assert t2["heartbeat_at"] is not None


def test_requeue_stale_tasks_uses_heartbeat_and_clears_run_state(tmp_path):
    from worker import requeue_stale_tasks

    tasks = [
        {
            "id": "stale-heartbeat",
            "prompt": "stale heartbeat",
            "status": "in_progress",
            "started_at": "2099-01-01T00:00:00",
            "finished_at": None,
            "heartbeat_at": "2000-01-01T00:00:00",
            "worker": "w1",
            "attempts": 4,
            "cost_usd": 2.5,
            "session_id": "sess-stale",
            "last_error": "old error",
        },
        {
            "id": "healthy-heartbeat",
            "prompt": "healthy heartbeat",
            "status": "in_progress",
            "started_at": "2000-01-01T00:00:00",
            "finished_at": None,
            "heartbeat_at": "2099-01-01T00:00:00",
            "worker": "w2",
            "attempts": 3,
            "cost_usd": 1.0,
            "session_id": "sess-healthy",
        },
        {
            "id": "stale-started-at",
            "prompt": "fallback to started_at",
            "status": "in_progress",
            "started_at": "2000-01-01T00:00:00",
            "finished_at": None,
            "worker": "w3",
            "attempts": 2,
            "cost_usd": 0.5,
            "session_id": "sess-fallback",
            "last_error": "retry me",
        },
    ]
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps(tasks, indent=2))

    requeued = requeue_stale_tasks(tasks_file, stale_timeout=1800)

    assert requeued == 2
    updated = json.loads(tasks_file.read_text())
    stale_heartbeat = next(task for task in updated if task["id"] == "stale-heartbeat")
    healthy_heartbeat = next(task for task in updated if task["id"] == "healthy-heartbeat")
    stale_started_at = next(task for task in updated if task["id"] == "stale-started-at")

    assert stale_heartbeat["status"] == "pending"
    assert stale_heartbeat["worker"] is None
    assert stale_heartbeat["started_at"] is None
    assert stale_heartbeat["finished_at"] is None
    assert stale_heartbeat["heartbeat_at"] is None
    assert stale_heartbeat["attempts"] == 0
    assert stale_heartbeat["cost_usd"] == 0.0
    assert stale_heartbeat["session_id"] is None
    assert "last_error" not in stale_heartbeat

    assert healthy_heartbeat["status"] == "in_progress"
    assert healthy_heartbeat["worker"] == "w2"
    assert healthy_heartbeat["heartbeat_at"] == "2099-01-01T00:00:00"
    assert healthy_heartbeat["attempts"] == 3

    assert stale_started_at["status"] == "pending"
    assert stale_started_at["worker"] is None
    assert stale_started_at["started_at"] is None
    assert stale_started_at["heartbeat_at"] is None
    assert stale_started_at["attempts"] == 0
    assert stale_started_at["cost_usd"] == 0.0
    assert stale_started_at["session_id"] is None
    assert "last_error" not in stale_started_at


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
    assert "No verify.sh or test files found." in output


def test_run_verify_timeout(tmp_path):
    from worker import run_verify
    passed, output = run_verify(tmp_path, verify_cmd="sleep 10", timeout=1)
    assert passed is False
    assert "timed out" in output.lower()

from unittest.mock import MagicMock, patch

from claude_agent_sdk import ResultMessage


def _mock_result(session_id="sess-1", cost=0.05, subtype="success"):
    """Create a properly populated ResultMessage mock."""
    m = MagicMock(spec=ResultMessage)
    m.session_id = session_id
    m.total_cost_usd = cost
    m.subtype = subtype
    m.num_turns = 5
    m.duration_ms = 10000
    return m


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

    mock_result = _mock_result("sess-123", 0.05)

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

    mock_result = _mock_result("sess-456", 0.03)

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

    mock_result = _mock_result("sess-789", 0.02)

    async def mock_query(**kwargs):
        yield mock_result

    with patch("worker.query", side_effect=mock_query):
        result = await run_task(task_entry, tmp_path, max_retries=2)

    assert result["status"] == "failed"
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_run_task_skips_verification_on_agent_non_success(task_entry, tmp_path):
    from worker import run_task

    mock_result = _mock_result("sess-error", 0.01, "error_max_turns")

    async def mock_query(**kwargs):
        yield mock_result

    with patch("worker.query", side_effect=mock_query), patch(
        "worker.run_verify",
        side_effect=AssertionError("verification should be skipped"),
    ):
        result = await run_task(task_entry, tmp_path, max_retries=2)

    assert result["status"] == "failed"
    assert result["attempts"] == 2
    assert result["session_id"] == "sess-error"


@pytest.mark.asyncio
async def test_run_task_skips_verification_without_result_message(task_entry, tmp_path):
    from worker import run_task

    async def mock_query(**kwargs):
        if False:
            yield None

    with patch("worker.query", side_effect=mock_query), patch(
        "worker.run_verify",
        side_effect=AssertionError("verification should be skipped"),
    ):
        result = await run_task(task_entry, tmp_path, max_retries=2)

    assert result["status"] == "failed"
    assert result["attempts"] == 2


# -- generate_verify_script tests -------------------------------------------

def test_generate_verify_script_success(tmp_path):
    from worker import generate_verify_script

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout="grep -q 'hello' output.txt",
            stderr="",
            returncode=0,
        )
        result = generate_verify_script(
            tmp_path, "output.txt should contain hello", "Write hello to output.txt"
        )
    assert result == "grep -q 'hello' output.txt"
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "claude"
    assert "-p" in cmd


def test_generate_verify_script_strips_markdown_fences(tmp_path):
    from worker import generate_verify_script

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout="```bash\ngrep -q 'hello' output.txt\n```",
            stderr="",
            returncode=0,
        )
        result = generate_verify_script(
            tmp_path, "output.txt should contain hello", "Write hello"
        )
    assert result == "grep -q 'hello' output.txt"


def test_generate_verify_script_returns_none_on_failure(tmp_path):
    from worker import generate_verify_script

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="error", returncode=1)
        result = generate_verify_script(tmp_path, "check it", "do it")
    assert result is None


def test_generate_verify_script_returns_none_on_timeout(tmp_path):
    from worker import generate_verify_script
    import subprocess as sp

    with patch("subprocess.run", side_effect=sp.TimeoutExpired("cmd", 60)):
        result = generate_verify_script(tmp_path, "check it", "do it")
    assert result is None


def test_generate_verify_script_returns_none_when_cli_missing(tmp_path):
    from worker import generate_verify_script

    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = generate_verify_script(tmp_path, "check it", "do it")
    assert result is None



# -- Agent-written verify.sh tests ------------------------------------------

@pytest.mark.asyncio
async def test_run_task_uses_agent_written_verify_sh(tmp_path):
    """Agent writes verify.sh; run_verify picks it up via auto-detect."""
    from worker import run_task

    task = {
        "id": "vsh1",
        "prompt": "Create hello.txt",
        "verify_prompt": "",
        "verify_cmd": "",
        "status": "in_progress",
    }

    mock_result = _mock_result("sess-vsh", 0.02)

    async def mock_query(**kwargs):
        yield mock_result

    with patch("worker.query", side_effect=mock_query), \
         patch("worker.run_verify", return_value=(True, "ok")):
        result = await run_task(task, tmp_path, max_retries=3)

    assert result["status"] == "completed"
