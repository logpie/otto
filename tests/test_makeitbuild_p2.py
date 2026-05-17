from __future__ import annotations

from pathlib import Path

from otto.mcp_tools import submit_subtask_for_lead
from otto.queue.task_graph import read_graph, record_task
from otto.v5_capability_inventory import (
    persist_feature_owned_paths_from_charter,
    persist_foundation_contracts_from_charter,
)
from otto.v5_runner import _foundation_isolation_feedback


def test_root_feature_subtasks_may_omit_predicted_owned_paths(tmp_path: Path) -> None:
    session_dir = tmp_path / "otto_logs" / "sessions" / "root"
    session_dir.mkdir(parents=True)
    record_task(tmp_path, task_id="root", intent="build app")

    foundation = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build scaffold",
        task_role="foundation",
        owned_paths=["src/", "package.json"],
    )
    feature = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Add issue list",
        task_role="feature",
        depends_on=[foundation["task_id"]],
    )

    tasks = read_graph(tmp_path)["tasks"]
    assert tasks[feature["task_id"]]["task_role"] == "feature"
    assert tasks[feature["task_id"]]["depends_on"] == [foundation["task_id"]]
    assert tasks[feature["task_id"]]["owned_paths"] == []


def test_architect_charter_persists_authoritative_feature_partition(tmp_path: Path) -> None:
    record_task(tmp_path, task_id="root", intent="build app")
    record_task(
        tmp_path,
        task_id="foundation",
        intent="Build scaffold",
        parent_task_id="root",
        task_role="foundation",
        owned_paths=["src/shared/", "package.json"],
    )
    record_task(
        tmp_path,
        task_id="feature-a",
        intent="Add issue list",
        parent_task_id="root",
        task_role="feature",
        depends_on=["foundation"],
    )
    record_task(
        tmp_path,
        task_id="feature-b",
        intent="Add filters",
        parent_task_id="root",
        task_role="feature",
        depends_on=["foundation"],
    )
    (tmp_path / "CHARTER.md").write_text(
        """# Charter

## Information Architecture Contract

```json
{
  "foundation_contracts": [
    {"path": "src/shared/api.ts", "owner_task_id": "foundation", "check": "literal"}
  ],
  "registration_isolation": {
    "shared_registry_files": [
      {"path": "src/routes.ts", "owner_task_id": "foundation", "leaf_edit": "forbidden"}
    ],
    "leaf_extension_globs": ["src/features/*"]
  },
  "feature_owned_paths": {
    "feature-a": ["src/features/issues.tsx"],
    "feature-b": ["src/features/filters.tsx"]
  }
}
```
""",
        encoding="utf-8",
    )

    contracts, contract_findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id="root",
    )
    feature_paths, feature_findings = persist_feature_owned_paths_from_charter(
        tmp_path,
        parent_task_id="root",
    )
    tasks = read_graph(tmp_path)["tasks"]

    assert contract_findings == []
    assert feature_findings == []
    assert feature_paths == {
        "feature-a": ["src/features/issues.tsx"],
        "feature-b": ["src/features/filters.tsx"],
    }
    assert tasks["root"]["foundation_contracts"] == contracts
    assert tasks["feature-a"]["owned_paths"] == ["src/features/issues.tsx"]
    assert tasks["feature-b"]["owned_paths"] == ["src/features/filters.tsx"]
    assert _foundation_isolation_feedback(
        parent_task_id="root",
        architect_task_id="foundation",
        tasks=tasks,
        contracts=contracts,
    ) is None
