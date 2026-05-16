# pyright: reportPrivateUsage=false
from __future__ import annotations

from otto import v5_runner


def _state(*, contract: dict[str, object], child_task_id: str = "foundation") -> dict[str, object]:
    path = str(contract["path"])
    return {
        "schema_version": 1,
        "parent_integration_branch": "main",
        "foundation_contracts": [contract],
        "touches": [
            {"child_task_id": child_task_id, "path": path},
            {
                "child_task_id": (
                    "feature-comments"
                    if child_task_id != "feature-comments"
                    else "foundation"
                ),
                "path": path,
            },
        ],
        "contributions": [
            {
                "child_task_id": child_task_id,
                "path": path,
                "line": "export function connect(workspaceId: string) {",
                "line_hash": "connect-v1",
                "source_branch": child_task_id,
                "base_ref": "base",
                "head_ref": child_task_id,
            }
        ],
    }


def test_semantic_contract_owner_accepts_compatible_behavioral_superset() -> None:
    path = "frontend/src/lib/ws.ts"
    state = _state(
        contract={
            "path": path,
            "owner_task_id": "foundation",
            "check": "semantic",
            "required_exports": ["connect"],
            "behavior_probes": ["openSocket(workspaceId, token)"],
        }
    )
    final_text_by_path = {
        path: (
            "export function connect(workspaceId: string, token?: string) {\n"
            "  return openSocket(workspaceId, token)\n"
            "}\n"
            "export function disconnect() {}\n"
        )
    }

    assert v5_runner._integration_union_missing_contributions(state, final_text_by_path) == []


def test_semantic_contract_blocks_when_behavior_probe_missing() -> None:
    path = "frontend/src/lib/ws.ts"
    state = _state(
        contract={
            "path": path,
            "owner_task_id": "foundation",
            "check": "semantic",
            "required_exports": ["connect"],
            "behavior_probes": ["openSocket(workspaceId, token)"],
        }
    )
    final_text_by_path = {
        path: (
            "export function connect(workspaceId: string, token?: string) {\n"
            "  return openSocket(workspaceId)\n"
            "}\n"
        )
    }

    missing = v5_runner._integration_union_missing_contributions(state, final_text_by_path)

    assert [item["line"] for item in missing] == [
        "export function connect(workspaceId: string) {"
    ]


def test_bound_contract_amendment_accepts_compatible_behavioral_superset() -> None:
    path = "frontend/src/lib/ws.ts"
    state = _state(
        contract={
            "path": path,
            "owner_task_id": "foundation",
            "check": "semantic",
            "required_exports": ["connect"],
            "behavior_probes": ["openSocket(workspaceId, token)"],
        },
        child_task_id="amend-ws",
    )
    state["contributors"] = {
        "amend-ws": {
            "task_role": "contract_amendment",
            "contract_amendment": {
                "contract_path": path,
                "owner_task_id": "foundation",
            },
        }
    }
    final_text_by_path = {
        path: (
            "export const connect = (workspaceId: string, token?: string) => {\n"
            "  return openSocket(workspaceId, token)\n"
            "}\n"
        )
    }

    assert v5_runner._integration_union_missing_contributions(state, final_text_by_path) == []


def test_non_owner_touch_to_semantic_contract_still_requires_exact_line_union() -> None:
    path = "frontend/src/lib/ws.ts"
    state = _state(
        contract={
            "path": path,
            "owner_task_id": "foundation",
            "check": "semantic",
            "required_exports": ["decorateConnection"],
            "behavior_probes": ["openSocket(workspaceId, token)"],
        },
        child_task_id="feature-comments",
    )
    final_text_by_path = {
        path: (
            "export function decorateConnection(workspaceId: string, token?: string) {\n"
            "  return openSocket(workspaceId, token)\n"
            "}\n"
        )
    }

    missing = v5_runner._integration_union_missing_contributions(state, final_text_by_path)

    assert [item["contributed_by"] for item in missing] == ["feature-comments"]
    assert [item["line"] for item in missing] == [
        "export function connect(workspaceId: string) {"
    ]


def test_literal_registry_path_still_requires_exact_additive_line_union() -> None:
    path = "frontend/src/App.tsx"
    state = {
        "schema_version": 1,
        "parent_integration_branch": "main",
        "foundation_contracts": [
            {
                "path": path,
                "owner_task_id": "routes-foundation",
                "check": "literal",
                "required_exports": ["registerRoute"],
                "behavior_probes": ["registerRoute('/issues', IssuesPage)"],
            }
        ],
        "touches": [
            {"child_task_id": "routes-a", "path": path},
            {"child_task_id": "routes-b", "path": path},
        ],
        "contributions": [
            {
                "child_task_id": "routes-a",
                "path": path,
                "line": "registerRoute('/issues', IssuesPage)",
                "line_hash": "route-issues",
                "source_branch": "routes-a",
                "base_ref": "base",
                "head_ref": "routes-a",
            }
        ],
    }
    final_text_by_path = {path: "registerRoute('/workspaces', WorkspacesPage)\n"}

    missing = v5_runner._integration_union_missing_contributions(state, final_text_by_path)

    assert [item["line"] for item in missing] == ["registerRoute('/issues', IssuesPage)"]
