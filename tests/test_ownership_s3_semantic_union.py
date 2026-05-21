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


def test_semantic_no_probes_trusts_owner_audit_F4() -> None:
    """Audit F-4: a `check: semantic` contract with NO probes is the explicit
    'trust the owner' mode. Previously this fell back to literal
    line-preservation, contradicting the documented semantics. Now: when the
    owner contributes a line that doesn't survive in the union but the contract
    declares no probes, the union guard exempts (operator-visible advisory
    surfaces this elsewhere)."""
    path = "backend/tests/conftest.py"
    state = _state(
        contract={
            "path": path,
            "owner_task_id": "foundation",
            "check": "semantic",
            # No required_exports, no behavior_probes — under-specified.
        }
    )
    # The owner's original `connect()` line is gone from the final text:
    final_text_by_path = {
        path: (
            "# conftest.py — evolved fixtures\n"
            "import pytest\n"
            "@pytest.fixture\n"
            "def db_session(): ...\n"
        )
    }

    # Pre-audit-F-4: this would flag the missing 'connect' line as a
    # union-incomplete violation. Post-F-4: trust the owner; exempt.
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


def test_phase_e_shared_path_without_contract_is_advisory_not_gate() -> None:
    """Audit F-5 Phase E: a shared path touched by multiple children but with
    NO foundation_contract entry no longer demotes the merge. The architect's
    missing declaration is surfaced via _integration_union_undeclared_shared_paths
    as an advisory; Phase B's _foundation_isolation_feedback catches the
    declared-partition cases BEFORE features dispatch.

    Linkboard 2026-05-21 reproduction (post-Phase-B catches this earlier;
    this is the defense in depth)."""
    from otto.v5.merge import _integration_union_undeclared_shared_paths

    path = "frontend/src/pages/BookmarksPage.tsx"
    state = {
        "schema_version": 1,
        "parent_integration_branch": "main",
        "foundation_contracts": [],  # NOT declared!
        "touches": [
            {"child_task_id": "foundation", "path": path},
            {"child_task_id": "feature-a", "path": path},
        ],
        "contributions": [
            {
                "child_task_id": "foundation",
                "path": path,
                "line": "<p>Loading…</p>",  # foundation's stub line
                "line_hash": "loading-stub",
                "source_branch": "foundation",
                "base_ref": "base",
                "head_ref": "foundation",
            }
        ],
    }
    # Feature A's overwrite — foundation's stub line is gone:
    final_text_by_path = {
        path: "export function BookmarksPage() { return <Bookmarks /> }\n"
    }

    # Pre-Phase-E: this flagged the missing stub line → child marked partial.
    # Post-Phase-E: no contract → no demote (operator can tighten CHARTER if
    # they want to gate this; the advisory makes the under-declaration visible).
    assert v5_runner._integration_union_missing_contributions(state, final_text_by_path) == []
    # And the advisory exposes the unclaimed shared path for visibility:
    assert _integration_union_undeclared_shared_paths(state) == [path]


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
