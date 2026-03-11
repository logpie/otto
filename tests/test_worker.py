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
