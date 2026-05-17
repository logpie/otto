from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from otto.mcp_tools import submit_subtask_for_lead
from otto.queue.subtask import read_pending, take_ready
from otto.queue.task_graph import get_task, read_graph, record_task, set_verdict, update_task_metadata
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


def _ia_charter(contracts: object) -> str:
    ia = {
        "foundation_contracts": contracts,
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
        },
    }
    return (
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps(ia, indent=2)
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


def test_duplicate_submit_subtask_corrects_foundation_to_feature_role(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    first = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
        task_role="foundation",
    )
    second = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
        task_role="feature",
    )

    assert second == {"task_id": first["task_id"], "duplicate": True, "metadata_updated": True}
    task = get_task(tmp_path, str(first["task_id"])) or {}
    assert task["task_role"] == "feature"


def test_duplicate_foundation_submit_with_omitted_task_role_preserves_foundation(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    first = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
        task_role="foundation",
    )
    second = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
    )

    assert second == {"task_id": first["task_id"], "duplicate": True, "metadata_updated": False}
    task = get_task(tmp_path, str(first["task_id"])) or {}
    assert task["task_role"] == "foundation"


def test_ready_entry_reflects_corrected_duplicate_metadata(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    first = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
        task_role="foundation",
    )
    submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["frontend/src/features/auth/"],
        task_role="feature",
    )

    raw_pending = read_pending(tmp_path)
    assert raw_pending[0]["task_id"] == first["task_id"]
    assert raw_pending[0]["task_role"] == "foundation"
    assert raw_pending[0]["owned_paths"] == ["backend/auth.py"]

    ready = take_ready(tmp_path, completed_task_ids=set(), in_flight_task_ids=set())

    assert len(ready) == 1
    assert ready[0]["task_id"] == first["task_id"]
    assert ready[0]["task_role"] == "feature"
    assert ready[0]["owned_paths"] == ["frontend/src/features/auth/"]


def test_finalized_duplicate_with_changed_scope_is_structured_refused(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    first = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["backend/auth.py"],
        task_role="foundation",
    )
    set_verdict(tmp_path, str(first["task_id"]), "pass")

    second = submit_subtask_for_lead(
        project_dir=tmp_path,
        session_dir=session_dir,
        task_id="root",
        intent="Build auth",
        owned_paths=["frontend/src/features/auth/"],
        task_role="feature",
    )

    assert second["kind"] == "stale_duplicate_scope_refusal"
    task = get_task(tmp_path, str(first["task_id"])) or {}
    assert task["task_role"] == "foundation"
    assert task["owned_paths"] == ["backend/auth.py"]


def test_concurrent_duplicate_submit_does_not_double_create(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    def submit() -> dict[str, object]:
        return submit_subtask_for_lead(
            project_dir=tmp_path,
            session_dir=session_dir,
            task_id="root",
            intent="Build auth",
            owned_paths=["backend/auth.py"],
            task_role="feature",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: submit(), range(24)))

    task_ids = {result["task_id"] for result in results}
    assert len(task_ids) == 1
    graph_tasks = [
        task for task in read_graph(tmp_path)["tasks"].values()
        if task.get("parent_task_id") == "root"
    ]
    assert len(graph_tasks) == 1
    assert len(read_pending(tmp_path)) == 1


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


def test_ia_form_foundation_contracts_parse(tmp_path: Path) -> None:
    contracts = [{"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"}]
    (tmp_path / "CHARTER.md").write_text(_ia_charter(contracts), encoding="utf-8")

    parsed, findings = parse_foundation_contracts(tmp_path / "CHARTER.md")

    assert findings == []
    assert parsed == contracts


def test_empty_foundation_contracts_clear_stale_parent_contracts(tmp_path: Path) -> None:
    stale = [{"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"}]
    (tmp_path / "CHARTER.md").write_text(_charter([]), encoding="utf-8")
    record_task(tmp_path, task_id="parent", intent="parent", foundation_contracts=stale)

    parsed, findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id="parent",
    )

    assert findings == []
    assert parsed == []
    assert (get_task(tmp_path, "parent") or {})["foundation_contracts"] == []


def test_missing_charter_does_not_clear_stale_parent_foundation_contracts(tmp_path: Path) -> None:
    stale = [{"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"}]
    record_task(tmp_path, task_id="parent", intent="parent", foundation_contracts=stale)

    parsed, findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id="parent",
    )

    assert parsed == []
    assert any(f.kind == "foundation_contracts_charter_unreadable" for f in findings)
    assert (get_task(tmp_path, "parent") or {})["foundation_contracts"] == stale


def test_present_charter_with_removed_foundation_contracts_clears_stale_parent_contracts(
    tmp_path: Path,
) -> None:
    stale = [{"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"}]
    (tmp_path / "CHARTER.md").write_text("# CHARTER\n\nNo contracts.\n", encoding="utf-8")
    record_task(tmp_path, task_id="parent", intent="parent", foundation_contracts=stale)

    parsed, findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id="parent",
    )

    assert findings == []
    assert parsed == []
    assert (get_task(tmp_path, "parent") or {})["foundation_contracts"] == []


def test_malformed_foundation_contracts_do_not_clear_stale_parent_contracts(tmp_path: Path) -> None:
    stale = [{"path": "frontend/src/lib/ws.ts", "owner_task_id": "architect", "check": "semantic"}]
    (tmp_path / "CHARTER.md").write_text(
        "# CHARTER\n\n## Foundation Contracts\n\n```json\nnot-json\n```\n",
        encoding="utf-8",
    )
    record_task(tmp_path, task_id="parent", intent="parent", foundation_contracts=stale)

    parsed, findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id="parent",
    )

    assert parsed == []
    assert findings
    assert (get_task(tmp_path, "parent") or {})["foundation_contracts"] == stale


def test_missing_foundation_contracts_parse_empty_without_crash(tmp_path: Path) -> None:
    (tmp_path / "CHARTER.md").write_text("# CHARTER\n\nNo contracts.\n", encoding="utf-8")

    parsed, findings = parse_foundation_contracts(tmp_path / "CHARTER.md")

    assert parsed == []
    assert findings == []


def test_registry_path_declared_semantic_is_rejected(tmp_path: Path) -> None:
    contracts = [
        {"path": "frontend/src/App.tsx", "owner_task_id": "architect", "check": "semantic"}
    ]
    (tmp_path / "CHARTER.md").write_text(_charter(contracts), encoding="utf-8")

    parsed, findings = parse_foundation_contracts(tmp_path / "CHARTER.md")

    assert parsed == []
    assert any(f.kind == "foundation_contracts_registry_semantic_rejected" for f in findings)
