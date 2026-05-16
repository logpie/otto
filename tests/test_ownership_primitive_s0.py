from __future__ import annotations

import json
from pathlib import Path

from otto.mcp_tools import submit_subtask_for_lead
from otto.queue.task_graph import get_task, read_graph, record_task, update_task_metadata
from otto.v5_capability_inventory import (
    parse_foundation_contracts,
    persist_foundation_contracts_from_charter,
)
from otto.v5_runner import _parent_task_id_for_child


def _charter(contracts: list[dict[str, object]]) -> str:
    ia = {
        "registration_isolation": {
            "policy": "file_local_auto_discovery",
            "shared_registry_files": [
                {
                    "path": "frontend/src/App.tsx",
                    "discovers": "frontend/src/features/*/routes.tsx",
                    "leaf_edit": False,
                }
            ],
            "leaf_extension_globs": ["frontend/src/features/*/routes.tsx"],
        }
    }
    return (
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps(ia, indent=2)
        + "\n```\n\n"
        "## Foundation Contracts\n\n"
        "```json\n"
        + json.dumps(contracts, indent=2)
        + "\n```\n"
    )


def test_task_role_and_foundation_contracts_round_trip(tmp_path: Path) -> None:
    contracts = [
        {
            "path": "frontend/src/lib/ws.ts",
            "owner_task_id": "architect",
            "check": "semantic",
            "required_exports": ["connectWorkspace"],
            "behavior_probes": ["connects using workspace id"],
        }
    ]

    record_task(
        tmp_path,
        task_id="architect",
        intent="Architect scaffold",
        parent_task_id="root",
        task_role="foundation",
        foundation_contracts=contracts,
    )

    assert (get_task(tmp_path, "architect") or {})["task_role"] == "foundation"
    assert (read_graph(tmp_path)["tasks"]["architect"])["foundation_contracts"] == contracts


def test_record_task_preserves_s0_and_unrelated_metadata(tmp_path: Path) -> None:
    contracts = [{"path": "api/auth.py", "owner_task_id": "architect", "check": "semantic"}]
    record_task(
        tmp_path,
        task_id="architect",
        intent="Architect scaffold",
        parent_task_id="root",
        task_role="foundation",
        foundation_contracts=contracts,
    )
    update_task_metadata(tmp_path, "architect", custom_marker={"keep": True})

    record_task(tmp_path, task_id="architect", intent="Architect scaffold")

    task = get_task(tmp_path, "architect") or {}
    assert task["task_role"] == "foundation"
    assert task["foundation_contracts"] == contracts
    assert task["custom_marker"] == {"keep": True}


def test_duplicate_submit_subtask_updates_changed_role_and_owned_paths(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    first = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
        task_role="feature",
    )
    second = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py", "frontend/src/features/auth/"],
        task_role="foundation",
    )

    assert second["duplicate"] is True
    assert second["metadata_updated"] is True
    task = get_task(tmp_path, str(first["task_id"])) or {}
    assert task["task_role"] == "foundation"
    assert task["owned_paths"] == ["backend/auth.py", "frontend/src/features/auth/"]


def test_charter_foundation_contracts_persist_on_decomposition_parent(tmp_path: Path) -> None:
    contracts = [{"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"}]
    (tmp_path / "CHARTER.md").write_text(_charter(contracts), encoding="utf-8")
    record_task(tmp_path, task_id="parent", intent="parent", integration_branch="main")
    record_task(tmp_path, task_id="architect", intent="Architect scaffold", parent_task_id="parent")

    parent_id = _parent_task_id_for_child(tmp_path, "architect", "main")
    parsed, findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id=parent_id,
    )

    assert findings == []
    assert parsed == contracts
    parent = get_task(tmp_path, "parent") or {}
    architect = get_task(tmp_path, "architect") or {}
    assert parent["foundation_contracts"] == contracts
    assert architect["foundation_contracts"] == []


def test_registry_path_declared_semantic_is_rejected(tmp_path: Path) -> None:
    contracts = [
        {"path": "frontend/src/App.tsx", "owner_task_id": "architect", "check": "semantic"}
    ]
    (tmp_path / "CHARTER.md").write_text(_charter(contracts), encoding="utf-8")

    parsed, findings = parse_foundation_contracts(tmp_path / "CHARTER.md")

    assert parsed == []
    assert any(f.kind == "foundation_contracts_registry_semantic_rejected" for f in findings)
