"""Validator: add-task-dependencies — dependency graph with cycle detection."""

import sys
sys.path.insert(0, ".")

from taskflow import TaskStore


def test_add_dependency(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Build")
    store.add("Test")
    store.add_dependency("Test", "Build")

    task = store.get_by_title("Test")
    assert "Build" in task.depends_on


def test_can_start_no_deps(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Standalone")
    assert store.can_start("Standalone") is True


def test_can_start_blocked(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Build")
    store.add("Test")
    store.add_dependency("Test", "Build")
    assert store.can_start("Test") is False


def test_can_start_unblocked(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Build")
    store.add("Test")
    store.add_dependency("Test", "Build")
    store.update_status("Build", "done")
    assert store.can_start("Test") is True


def test_get_ready_tasks(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Build")
    store.add("Test")
    store.add("Deploy")
    store.add_dependency("Test", "Build")
    store.add_dependency("Deploy", "Test")

    ready = store.get_ready_tasks()
    titles = [t.title for t in ready]
    assert "Build" in titles
    assert "Test" not in titles
    assert "Deploy" not in titles


def test_cycle_detection(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("A")
    store.add("B")
    store.add("C")
    store.add_dependency("B", "A")
    store.add_dependency("C", "B")

    import pytest
    with pytest.raises(ValueError):
        store.add_dependency("A", "C")  # Would create A→B→C→A cycle
