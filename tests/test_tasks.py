"""Tests for otto.tasks module."""

import threading
from pathlib import Path

import pytest
import yaml

from otto.tasks import (
    add_task,
    add_tasks,
    generate_key,
    load_tasks,
    save_tasks,
    update_task,
)


class TestGenerateKey:
    def test_returns_12_char_hex(self):
        key = generate_key(set())
        assert len(key) == 12
        assert all(c in "0123456789abcdef" for c in key)

    def test_unique_against_existing(self):
        existing = {generate_key(set()) for _ in range(100)}
        new_key = generate_key(existing)
        assert new_key not in existing


class TestLoadSaveTasks:
    def test_load_empty_file(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        tasks = load_tasks(path)
        assert tasks == []

    def test_round_trip(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        tasks = [{"id": 1, "key": "abc123def456", "prompt": "hello", "status": "pending"}]
        save_tasks(path, tasks)
        loaded = load_tasks(path)
        assert loaded == tasks


class TestAddTask:
    def test_adds_task_with_auto_id_and_key(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        task = add_task(path, "Build a login page")
        assert task["id"] == 1
        assert len(task["key"]) == 12
        assert task["prompt"] == "Build a login page"
        assert task["status"] == "pending"

    def test_increments_id(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        add_task(path, "First task")
        task2 = add_task(path, "Second task")
        assert task2["id"] == 2

    def test_custom_verify_and_retries(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        task = add_task(path, "Optimize", verify="python bench.py", max_retries=5)
        assert task["verify"] == "python bench.py"
        assert task["max_retries"] == 5


class TestUpdateTask:
    def test_updates_status(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        task = add_task(path, "Do something")
        updated = update_task(path, task["key"], status="running", attempts=1)
        assert updated["status"] == "running"
        assert updated["attempts"] == 1

    def test_raises_on_unknown_key(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        add_task(path, "Do something")
        with pytest.raises(KeyError):
            update_task(path, "nonexistent123", status="running")


class TestAddTaskSpec:
    def test_add_task_with_spec(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        task = add_task(tasks_path, "Add search", spec=["search is case-insensitive", "no matches returns empty list"])
        assert task["spec"] == ["search is case-insensitive", "no matches returns empty list"]
        tasks = load_tasks(tasks_path)
        assert tasks[0]["spec"] == ["search is case-insensitive", "no matches returns empty list"]

    def test_add_task_without_spec(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        task = add_task(tasks_path, "Fix typo")
        assert "spec" not in task


class TestAddTasksBatch:
    def test_add_tasks_batch(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Task A", "spec": ["criterion 1"]},
            {"prompt": "Task B", "spec": ["criterion 2"]},
            {"prompt": "Task C"},
        ]
        results = add_tasks(tasks_path, batch)
        assert len(results) == 3
        assert results[0]["id"] == 1
        assert results[1]["id"] == 2
        assert results[2]["id"] == 3
        tasks = load_tasks(tasks_path)
        assert len(tasks) == 3
        assert tasks[0]["spec"] == ["criterion 1"]
        assert tasks[1]["spec"] == ["criterion 2"]
        assert "spec" not in tasks[2]

    def test_add_tasks_appends_to_existing(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Existing task")
        batch = [{"prompt": "New task"}]
        results = add_tasks(tasks_path, batch)
        assert results[0]["id"] == 2
        tasks = load_tasks(tasks_path)
        assert len(tasks) == 2


class TestDependsOn:
    def test_add_tasks_with_depends_on(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Create User model"},
            {"prompt": "Add auth using User", "depends_on": [0]},
            {"prompt": "Add admin panel", "depends_on": [0, 1]},
        ]
        results = add_tasks(tasks_path, batch)
        assert results[0].get("depends_on") is None  # no deps → not stored
        assert results[1]["depends_on"] == [1]  # index 0 → id 1
        assert results[2]["depends_on"] == [1, 2]  # indices [0,1] → ids [1,2]

    def test_depends_on_self_ref_rejected(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Task A", "depends_on": [0]},
        ]
        with pytest.raises(ValueError, match="depends on itself"):
            add_tasks(tasks_path, batch)

    def test_depends_on_out_of_range_rejected(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Task A", "depends_on": [5]},
        ]
        with pytest.raises(ValueError, match="out of range"):
            add_tasks(tasks_path, batch)

    def test_depends_on_cycle_rejected(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Task A", "depends_on": [1]},
            {"prompt": "Task B", "depends_on": [0]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            add_tasks(tasks_path, batch)

    def test_depends_on_no_deps_is_fine(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Task A"},
            {"prompt": "Task B"},
        ]
        results = add_tasks(tasks_path, batch)
        assert "depends_on" not in results[0]
        assert "depends_on" not in results[1]

    def test_depends_on_persisted_in_yaml(self, tmp_git_repo):
        tasks_path = tmp_git_repo / "tasks.yaml"
        batch = [
            {"prompt": "Task A"},
            {"prompt": "Task B", "depends_on": [0]},
        ]
        add_tasks(tasks_path, batch)
        tasks = load_tasks(tasks_path)
        assert tasks[1]["depends_on"] == [1]


class TestConcurrentAccess:
    def test_concurrent_adds_dont_lose_data(self, tmp_git_repo):
        path = tmp_git_repo / "tasks.yaml"
        errors = []

        def add_one(i):
            try:
                add_task(path, f"Task {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_one, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        tasks = load_tasks(path)
        assert len(tasks) == 10
