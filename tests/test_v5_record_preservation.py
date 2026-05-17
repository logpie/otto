"""Regression: record_task must not overwrite existing parent_task_id with None."""
from pathlib import Path

from otto.queue.task_graph import record_task, read_graph

def test_record_preserves_parent_when_re_registered(tmp_path: Path) -> None:
    project_dir = tmp_path
    record_task(project_dir, task_id="child1", intent="api", parent_task_id="root")
    g1 = read_graph(project_dir)
    assert g1["tasks"]["child1"]["parent_task_id"] == "root"
    # Re-register without parent_task_id (the child's own run_lead does this)
    record_task(project_dir, task_id="child1", intent="api")
    g2 = read_graph(project_dir)
    assert g2["tasks"]["child1"]["parent_task_id"] == "root", \
        f"parent was overwritten: {g2['tasks']['child1']}"

def test_record_preserves_integration_branch_when_re_registered(tmp_path: Path) -> None:
    project_dir = tmp_path
    record_task(project_dir, task_id="x", intent="i", parent_task_id="root",
                integration_branch="i2p/root/integration")
    record_task(project_dir, task_id="x", intent="i")
    g = read_graph(project_dir)
    assert g["tasks"]["x"]["integration_branch"] == "i2p/root/integration"
    assert g["tasks"]["x"]["parent_task_id"] == "root"


def test_record_preserves_child_context_scope_metadata(tmp_path: Path) -> None:
    project_dir = tmp_path
    record_task(
        project_dir,
        task_id="x",
        intent="i",
        parent_task_id="root",
        owned_paths=["frontend/src/issues/**"],
        action_ids=["issue.create"],
    )

    record_task(project_dir, task_id="x", intent="i")

    g = read_graph(project_dir)
    assert g["tasks"]["x"]["owned_paths"] == ["frontend/src/issues/**"]
    assert g["tasks"]["x"]["action_ids"] == ["issue.create"]
