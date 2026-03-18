"""Validator: add-task-count — count and count_by_status methods."""

import sys
sys.path.insert(0, ".")

from taskflow import TaskStore


def test_count_method_exists(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    assert hasattr(store, "count")


def test_count_empty(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    assert store.count() == 0


def test_count_with_tasks(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Task 1")
    store.add("Task 2")
    assert store.count() == 2


def test_count_by_status(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Task 1")
    store.add("Task 2")
    store.update_status("Task 1", "done")
    assert store.count_by_status("todo") == 1
    assert store.count_by_status("done") == 1
    assert store.count_by_status("in_progress") == 0
