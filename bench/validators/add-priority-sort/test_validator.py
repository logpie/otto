"""Validator: add-priority-sort — sorting by priority, status, generic."""

import sys
sys.path.insert(0, ".")

from taskflow import TaskStore


def test_list_by_priority(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Low task", priority="low")
    store.add("High task", priority="high")
    store.add("Med task", priority="medium")

    result = store.list_by_priority()
    titles = [t.title for t in result]
    assert titles.index("High task") < titles.index("Med task")
    assert titles.index("Med task") < titles.index("Low task")


def test_list_by_status(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Task 1")
    store.add("Task 2")
    store.update_status("Task 1", "done")

    todo = store.list_by_status("todo")
    assert len(todo) == 1
    assert todo[0].title == "Task 2"

    done = store.list_by_status("done")
    assert len(done) == 1
    assert done[0].title == "Task 1"


def test_list_sorted_by_title(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    store.add("Zebra")
    store.add("Apple")
    store.add("Mango")

    result = store.list_sorted("title")
    titles = [t.title for t in result]
    assert titles == ["Apple", "Mango", "Zebra"]


def test_list_sorted_invalid_key(tmp_path):
    store = TaskStore(str(tmp_path / "t.json"))
    import pytest
    with pytest.raises(ValueError):
        store.list_sorted("invalid_key")
