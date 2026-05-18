from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from otto.spec_compile_flat import FlatSpec
from otto.v5_capability_inventory import (
    build_inventory,
    check_coherence,
    parse_information_architecture_contract,
    validate_information_architecture_contract,
)


def _write_charter(project: Path, ia: dict[str, Any] | None) -> None:
    if ia is None:
        text = "# CHARTER\n\n## Agent operating notes\n\n- Tests: `npm run test`\n"
    else:
        text = (
            "# CHARTER\n\n"
            "## Information Architecture Contract\n\n"
            "```json\n"
            + json.dumps(ia, indent=2)
            + "\n```\n\n"
            "## Agent operating notes\n\n- Tests: `npm run test`\n"
        )
    (project / "CHARTER.md").write_text(text)


def _ia() -> dict[str, Any]:
    return {
        "entry_states": [{"id": "unauthenticated", "route": "/", "expected": "Home"}],
        "routes": [{"id": "team.backlog", "path": "/", "key_text": "Backlog"}],
        "nav_surfaces": [{"id": "sidebar", "must_link_routes": ["team.backlog"]}],
        "action_surfaces": [
            {
                "id": "issue.create",
                "label": "Create issue",
                "surfaces": ["backlog.empty_state", "keyboard.C", "command_palette"],
                "target_route": "team.backlog",
            }
        ],
        "api_endpoints": [{"id": "issues.create", "method": "POST", "path": "/api/issues"}],
        "ws_events": [{"id": "issue.created", "direction": "server_to_client"}],
        "data_contracts": [{"id": "Issue", "fields": ["id", "title"]}],
        "empty_states": [{"entity": "issue", "list_route": "team.backlog", "cta_present": True}],
        "settings_sections": [{"id": "account", "path": "/settings/account"}],
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


def _spec() -> dict[str, Any]:
    return {
        "project_kind": "webapp",
        "product_overview": {
            "top_level_pages": [{"id": "team.backlog", "purpose": "triage issues"}],
            "primary_navigation": {"sidebar": ["team.backlog"]},
        },
        "core_entities": [
            {
                "id": "issue",
                "primary_actions": [{"id": "issue.create"}],
            }
        ],
    }


def _spec_dataclass() -> FlatSpec:
    return FlatSpec(
        project_kind="webapp",
        product_overview={
            "top_level_pages": [{"id": "team.backlog", "purpose": "triage issues"}],
            "primary_navigation": {"sidebar": ["team.backlog"]},
        },
        core_entities=[
            {
                "id": "issue",
                "primary_actions": [{"id": "issue.create"}],
            }
        ],
    )


def test_parse_information_architecture_contract_extracts_json(tmp_path: Path) -> None:
    payload = _ia()
    _write_charter(tmp_path, payload)

    parsed = parse_information_architecture_contract(tmp_path / "CHARTER.md")

    assert parsed is not None
    assert parsed["routes"][0]["id"] == "team.backlog"


def test_missing_ia_fails_webapp_coherence(tmp_path: Path) -> None:
    _write_charter(tmp_path, None)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(f.kind == "missing_ia_contract" for f in findings)


def test_bad_action_target_route_fails(tmp_path: Path) -> None:
    payload = _ia()
    payload["action_surfaces"][0]["target_route"] = "missing.route"  # type: ignore[index]
    _write_charter(tmp_path, payload)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(f.kind == "ia_unknown_target_route" for f in findings)


def test_bad_surface_kind_fails(tmp_path: Path) -> None:
    payload = _ia()
    payload["action_surfaces"][0]["surfaces"] = ["mystery.portal"]  # type: ignore[index]
    _write_charter(tmp_path, payload)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(f.kind == "ia_unknown_surface" for f in findings)


def test_bad_registration_isolation_contract_fails(tmp_path: Path) -> None:
    payload = _ia()
    payload["registration_isolation"] = {
        "policy": "manual_append",
        "shared_registry_files": [{"path": "frontend/src/App.tsx", "leaf_edit": True}],
        "leaf_extension_globs": [],
    }
    _write_charter(tmp_path, payload)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(f.kind == "route_registration_isolation_contract_invalid" for f in findings)


def test_missing_action_surface_for_spec_action_fails(tmp_path: Path) -> None:
    payload = _ia()
    payload["action_surfaces"] = []
    _write_charter(tmp_path, payload)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(f.kind == "ia_missing_action_surface" and f.reference == "issue.create" for f in findings)


def test_missing_product_overview_page_route_warns(tmp_path: Path) -> None:
    payload = _ia()
    payload["routes"] = []
    _write_charter(tmp_path, payload)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(
        f.kind == "ia_missing_product_page_route" and f.reference == "team.backlog"
        for f in findings
    )


def test_missing_sidebar_page_nav_link_warns(tmp_path: Path) -> None:
    payload = _ia()
    payload["nav_surfaces"] = []
    _write_charter(tmp_path, payload)

    findings = validate_information_architecture_contract(tmp_path, spec=_spec())

    assert any(
        f.kind == "ia_missing_sidebar_nav_link" and f.reference == "team.backlog"
        for f in findings
    )


def test_ia_contract_accepts_dict_from_roundtripped_spec(tmp_path: Path) -> None:
    _write_charter(tmp_path, _ia())
    roundtripped = json.loads(json.dumps(asdict(_spec_dataclass())))

    findings = validate_information_architecture_contract(tmp_path, spec=roundtripped)

    assert not any(f.kind == "ia_missing_action_surface" for f in findings)


def test_check_coherence_includes_ia_findings(tmp_path: Path) -> None:
    _write_charter(tmp_path, None)
    inv = build_inventory(tmp_path)

    findings = check_coherence(tmp_path, inv, spec=_spec())

    assert any(f.kind == "missing_ia_contract" for f in findings)


def test_architect_prompt_keeps_concise_ia_contract_requirement() -> None:
    prompt = Path("otto/prompts/lead.md").read_text(encoding="utf-8")

    assert "Information Architecture Contract" in prompt
    assert "registration_isolation" in prompt
    assert "file-local" in prompt
    assert "Do not restate JSON in paragraphs" in prompt


def test_architect_prompt_prefers_short_charter_without_slicer_dependency() -> None:
    prompt = Path("otto/prompts/lead.md").read_text(encoding="utf-8")

    assert "Keep prose short" in prompt
    assert "operational facts" in prompt
    assert "SCOPED CONTEXT" in prompt


def test_coherence_warning_reports_prose_ia_split(tmp_path: Path) -> None:
    ia = _ia()
    prose = "\n".join(f"- prose line {index}" for index in range(305))
    text = (
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps(ia, indent=2)
        + "\n```\n\n"
        "## Agent operating notes\n\n"
        + prose
        + "\n"
    )
    (tmp_path / "CHARTER.md").write_text(text, encoding="utf-8")
    inv = build_inventory(tmp_path)

    findings = check_coherence(tmp_path, inv, spec=_spec())

    cap = next(f for f in findings if f.kind == "charter_prose_over_line_cap")
    assert "prose lines" in cap.detail
    assert "IA JSON/fence lines" in cap.detail
    assert "Prose target is <= 300 lines" in cap.detail
    assert "IA JSON is uncapped" in cap.detail
