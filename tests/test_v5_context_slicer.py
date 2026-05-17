from __future__ import annotations

import json
from pathlib import Path

from otto.v5_context_slicer import (
    ChildScope,
    slice_charter_for_child,
    slice_spec_for_child,
    write_context_slice_for_child,
)
from otto.v5_runner import _context_slicing_enabled


def _spec() -> dict[str, object]:
    return {
        "schema_version": 3,
        "project_kind": "webapp",
        "intent_claims": [
            {"id": "claim.issue_create", "text": "Create issues", "source_line": 1},
            {"id": "claim.report_export", "text": "Export reports", "source_line": 2},
        ],
        "core_entities": [
            {
                "id": "issue",
                "name": "Issue",
                "fields": [
                    {
                        "id": "issue.title",
                        "name": "title",
                        "type": "string",
                        "intent_claim_ids": ["claim.issue_create"],
                    }
                ],
                "states": ["empty", "open"],
                "primary_actions": [
                    {
                        "id": "issue.create",
                        "verb": "create",
                        "success_observable": "Issue appears",
                        "error_observable": "Inline error appears",
                        "intent_claim_ids": ["claim.issue_create"],
                    }
                ],
            },
            {
                "id": "report",
                "name": "Report",
                "fields": [
                    {
                        "id": "report.format",
                        "name": "format",
                        "type": "string",
                        "intent_claim_ids": ["claim.report_export"],
                    }
                ],
                "states": ["empty", "ready"],
                "primary_actions": [
                    {
                        "id": "report.export",
                        "verb": "export",
                        "success_observable": "CSV downloads",
                        "error_observable": "Export error appears",
                        "intent_claim_ids": ["claim.report_export"],
                    }
                ],
            },
        ],
        "behavior_journeys": [
            {
                "id": "create_issue",
                "role": "illustrative",
                "description": "User creates an issue.",
                "covers_primary_actions": ["issue.create"],
                "start_state": "empty_workspace",
                "entry_route": "/",
            }
        ],
    }


def _charter() -> str:
    ia = {
        "entry_states": [{"id": "empty", "route": "/", "expected": "Home"}],
        "routes": [{"id": "team.backlog", "path": "/", "key_text": "Backlog"}],
        "nav_surfaces": [{"id": "sidebar", "must_link_routes": ["team.backlog"]}],
        "action_surfaces": [
            {
                "id": "issue.create",
                "label": "Create issue",
                "surfaces": ["backlog.empty_state"],
                "target_route": "team.backlog",
            },
            {
                "id": "report.export",
                "label": "Export report",
                "surfaces": ["toolbar"],
                "target_route": "team.backlog",
            },
        ],
        "api_endpoints": [{"id": "issues.create", "method": "POST", "path": "/api/issues"}],
        "ws_events": [{"id": "issue.created", "direction": "server_to_client"}],
        "data_contracts": [{"id": "Issue", "fields": ["id", "title"]}],
    }
    return (
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps(ia, indent=2)
        + "\n```\n\n"
        "## Agent operating notes\n\n"
        "- Run `npm test` from the project root.\n\n"
        "## Issue Rationale\n\n"
        "Issues use optimistic creation because issue agents own backlog UX.\n\n"
        "## Reporting Rationale\n\n"
        "Reports use CSV export and are owned by reporting agents.\n"
    )


def test_slice_spec_keeps_entity_and_action_registries_but_filters_details() -> None:
    sliced = slice_spec_for_child(
        _spec(),
        ChildScope(child_id="child-1", task_intent="Build issue.create UI"),
    )

    assert [claim["id"] for claim in sliced["intent_claims"]] == ["claim.issue_create"]
    entities = {entity["id"]: entity for entity in sliced["core_entities"]}
    assert set(entities) == {"issue", "report"}
    assert entities["issue"]["fields"][0]["id"] == "issue.title"
    assert entities["issue"]["states"] == ["empty", "open"]
    assert entities["report"]["primary_actions"] == [{"id": "report.export"}]
    assert "fields" not in entities["report"]
    assert "states" not in entities["report"]


def test_slice_spec_ambiguous_scope_falls_back_to_full() -> None:
    original = _spec()

    sliced = slice_spec_for_child(
        original,
        ChildScope(child_id="child-1", task_intent="Polish the shell"),
    )

    assert sliced == original


def test_slice_charter_preserves_full_ia_contract_and_filters_rationale() -> None:
    sliced = slice_charter_for_child(
        _charter(),
        ChildScope(child_id="child-1", task_intent="Build issue.create UI"),
    )

    assert '"api_endpoints"' in sliced
    assert '"ws_events"' in sliced
    assert '"data_contracts"' in sliced
    assert '"report.export"' in sliced
    assert "Issue Rationale" in sliced
    assert "Reporting Rationale" not in sliced
    assert "Agent operating notes" in sliced


def test_write_context_slice_logs_decision_and_full_artifact_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    parent = tmp_path / "parent-session"
    child = tmp_path / "child-session"
    worktree = tmp_path / "worktree"
    parent_spec = parent / "spec" / "spec.json"
    child_spec = child / "spec" / "spec.json"
    charter = worktree / "CHARTER.md"
    parent_spec.parent.mkdir(parents=True)
    charter.parent.mkdir(parents=True)
    parent_spec.write_text(json.dumps(_spec()), encoding="utf-8")
    charter.write_text(_charter(), encoding="utf-8")

    result = write_context_slice_for_child(
        project_dir=project,
        child_session_dir=child,
        child_scope=ChildScope(child_id="child-1", action_ids=["issue.create"]),
        parent_spec_path=parent_spec,
        full_charter_path=charter,
        child_spec_path=child_spec,
    )

    audit = json.loads((child / "context_slice.json").read_text(encoding="utf-8"))
    assert audit["child_id"] == "child-1"
    assert audit["included_entities"] == ["issue"]
    assert audit["excluded_entities"] == ["report"]
    assert audit["included_intent_claims_n"] == 1
    assert audit["excluded_intent_claims_n"] == 1
    assert audit["fallback_to_full"] is False
    assert audit["artifacts"]["full_spec"] == str(parent_spec)
    assert "Full Artifact Index" in result.charter
    assert str(charter) in result.charter


def test_write_context_slice_logs_ambiguous_fallback(tmp_path: Path) -> None:
    parent = tmp_path / "parent-session"
    child = tmp_path / "child-session"
    worktree = tmp_path / "worktree"
    parent_spec = parent / "spec" / "spec.json"
    child_spec = child / "spec" / "spec.json"
    charter = worktree / "CHARTER.md"
    parent_spec.parent.mkdir(parents=True)
    charter.parent.mkdir(parents=True)
    parent_spec.write_text(json.dumps(_spec()), encoding="utf-8")
    charter.write_text(_charter(), encoding="utf-8")

    write_context_slice_for_child(
        project_dir=tmp_path,
        child_session_dir=child,
        child_scope=ChildScope(child_id="child-1", task_intent="Polish shell"),
        parent_spec_path=parent_spec,
        full_charter_path=charter,
        child_spec_path=child_spec,
    )

    audit = json.loads((child / "context_slice.json").read_text(encoding="utf-8"))
    assert audit["fallback_to_full"] is True
    assert "scope did not match" in audit["fallback_reason"]
    assert audit["included_entities"] == ["issue", "report"]
    assert audit["excluded_entities"] == []
    assert audit["artifacts"]["full_charter"] == str(charter)


def test_context_slicing_is_off_by_default_and_full_context_overrides_config() -> None:
    assert _context_slicing_enabled({}) is False
    assert _context_slicing_enabled({"v5_context_slicing": True}) is True
    assert _context_slicing_enabled({"context_slicing": {"enabled": True}}) is True
    assert _context_slicing_enabled({
        "context_slicing": {"enabled": True},
        "v5_context_slicing": False,
    }) is False
