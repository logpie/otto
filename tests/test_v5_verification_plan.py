from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from otto.queue.task_graph import read_graph
from otto.spec_compile_flat import FlatSpec
from otto.v5_runner import run_v5_pipeline
from otto.v5_verification_plan import validate_lead_verdict


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _spec() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "intent": "issue tracker",
        "project_kind": "webapp",
        "product_overview": {
            "one_liner": "Issue tracker for teams to triage and ship work.",
            "primary_users": [
                {"id": "engineer", "description": "owns issues day-to-day"},
                {"id": "team_lead", "description": "plans cycles and triages backlog"},
            ],
            "top_level_pages": [
                {
                    "id": "team.backlog",
                    "purpose": "plan and triage upcoming work",
                    "primary_users": ["engineer", "team_lead"],
                }
            ],
            "primary_navigation": {
                "sidebar": ["team.backlog"],
                "command_palette": ["issue.create"],
            },
            "out_of_scope": ["native mobile app"],
            "phases": [
                {
                    "id": "must_have",
                    "rationale": "core issue creation loop",
                    "covers_primary_action_ids": ["issue.create"],
                }
            ],
        },
        "intent_claims": [{"id": "claim.issue_create", "text": "Create issues", "source_line": 1}],
        "core_entities": [
            {
                "id": "issue",
                "name": "Issue",
                "fields": [{"id": "issue.title", "intent_claim_ids": ["claim.issue_create"]}],
                "states": ["empty", "open"],
                "primary_actions": [
                    {
                        "id": "issue.create",
                        "verb": "create",
                        "success_observable": "Issue appears in backlog",
                        "error_observable": "Inline validation error appears",
                        "intent_claim_ids": ["claim.issue_create"],
                    }
                ],
            }
        ],
        "cold_start_states": [{"id": "unauthenticated"}],
        "permissions": [{"id": "member", "gates": ["issue.create"]}],
        "quality_constraints": [],
        "behavior_journeys": [
            {
                "id": "create_issue",
                "role": "illustrative",
                "description": "User creates an issue.",
                "covers_primary_actions": ["issue.create"],
                "start_state": "unauthenticated",
                "entry_route": "/",
            }
        ],
    }


def _spec_dataclass() -> FlatSpec:
    spec = _spec()
    return FlatSpec(
        intent=str(spec["intent"]),
        project_kind=str(spec["project_kind"]),
        product_overview=spec["product_overview"],
        intent_claims=spec["intent_claims"],
        core_entities=spec["core_entities"],
        cold_start_states=spec["cold_start_states"],
        permissions=spec["permissions"],
        quality_constraints=spec["quality_constraints"],
        behavior_journeys=spec["behavior_journeys"],
    )


def _ia(*, route_path: str = "/", endpoint_path: str = "/api/issues", cta: bool = True) -> dict[str, Any]:
    return {
        "entry_states": [{"id": "unauthenticated", "route": "/", "expected": "Backlog"}],
        "routes": [{"id": "team.backlog", "path": route_path, "key_text": "Backlog"}],
        "nav_surfaces": [{"id": "sidebar", "must_link_routes": ["team.backlog"]}],
        "action_surfaces": [
            {
                "id": "issue.create",
                "label": "Create issue",
                "surfaces": ["backlog.empty_state", "keyboard.C", "command_palette"],
                "target_route": "team.backlog",
            }
        ],
        "api_endpoints": [{"id": "issues.create", "method": "POST", "path": endpoint_path}],
        "ws_events": [],
        "data_contracts": [{"id": "Issue", "fields": ["id", "title"]}],
        "empty_states": [{"entity": "issue", "list_route": "team.backlog", "cta_present": cta}],
        "settings_sections": [],
    }


def _write_contract(project: Path, session: Path, *, ia: dict[str, Any] | None = None) -> None:
    _write(session / "spec" / "spec.json", json.dumps(_spec()))
    if ia is not None:
        _write(
            project / "CHARTER.md",
            "# CHARTER\n\n## Information Architecture Contract\n\n```json\n"
            + json.dumps(ia, indent=2)
            + "\n```\n",
        )


def _passing_project(project: Path, session: Path) -> None:
    _write_contract(project, session, ia=_ia())
    _write(project / "src" / "App.tsx", "export const routes = ['/', 'team.backlog'];\nconst a = 'issue.create'; toast.success('Created');\n")
    _write(project / "api" / "issues.py", "router.post('/api/issues')\n")
    _write(project / "tests" / "issue_create.test.ts", "it('issue.create works', () => {})\n")


def _verdict(**overrides: Any) -> dict[str, Any]:
    payload = {
        "verdict": "pass",
        "journeys": [{"id": "create_issue", "passed": True}],
        "intent_coverage": {"built": ["issue create"], "partial": [], "skipped": []},
        "summary": "passed",
        "evidence": [],
    }
    payload.update(overrides)
    return payload


def _checks_by_kind(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for check in plan["checks"]:
        out.setdefault(check["kind"], []).append(check)
    return out


def _no_stub_check(project: Path, session: Path) -> dict[str, Any]:
    outcome = validate_lead_verdict(
        project_dir=project,
        worktree_dir=project,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )
    return _checks_by_kind(outcome.verification_plan)["no_stub_text"][0]


def test_verification_plan_all_checks_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert outcome.final_verdict == "pass"
    assert (session / "verification_plan.json").exists()
    assert all(c["status"] != "fail" for c in outcome.verification_plan["checks"])


def test_verification_plan_accepts_dict_from_roundtripped_spec(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    roundtripped = json.loads(json.dumps(asdict(_spec_dataclass())))
    _write(session / "spec" / "spec.json", json.dumps(roundtripped))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert outcome.final_verdict == "pass"
    assert _checks_by_kind(outcome.verification_plan)["action_has_test"][0]["id"] == "issue.create"


def test_route_resolves_failure_downgrades_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(route_path="/missing-route"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    route_checks = _checks_by_kind(outcome.verification_plan)["route_resolves"]
    assert route_checks[0]["status"] == "fail"
    assert outcome.final_verdict == "partial"


def test_check_matrix_page_resolves(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(route_path="/missing-pm-page"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    page_checks = _checks_by_kind(outcome.verification_plan)["page_resolves"]
    assert page_checks[0]["id"] == "team.backlog"
    assert page_checks[0]["status"] == "fail"
    assert outcome.final_verdict == "partial"


def test_endpoint_resolves_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(endpoint_path="/api/missing"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert _checks_by_kind(outcome.verification_plan)["endpoint_resolves"][0]["status"] == "fail"


def test_action_has_test_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    (tmp_path / "tests" / "issue_create.test.ts").unlink()

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert _checks_by_kind(outcome.verification_plan)["action_has_test"][0]["status"] == "fail"


def test_mutating_action_feedback_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "App.tsx", "const a = 'issue.create';\n")

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert _checks_by_kind(outcome.verification_plan)["mutating_action_has_feedback"][0]["status"] == "fail"


def test_entity_empty_state_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(cta=False))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert _checks_by_kind(outcome.verification_plan)["entity_has_empty_state"][0]["status"] == "fail"


def test_no_stub_text_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.tsx", "export function Copy() { return <button>TODO: implement</button>; }\n")

    check = _no_stub_check(tmp_path, session)

    assert check["status"] == "fail"
    assert check["refs"]["offenders"] == ["src/copy.tsx:1"]


def test_no_stub_text_ignores_import_identifiers(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "Placeholder.tsx", "import { Placeholder } from 'react';\n")

    assert _no_stub_check(tmp_path, session)["status"] == "pass"


def test_no_stub_text_ignores_comments(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.ts", "// TODO: refactor\nexport const label = 'Ready';\n")

    assert _no_stub_check(tmp_path, session)["status"] == "pass"


def test_no_stub_text_ignores_build_artifacts(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "dist" / "index-abc.js", "console.error('TODO: implement generated code');\n")

    assert _no_stub_check(tmp_path, session)["status"] == "pass"


def test_no_stub_text_ignores_otto_worktrees(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(
        tmp_path / ".worktrees" / "v5-foo" / "src" / "X.tsx",
        "export function X() { return <p>Lorem ipsum dolor sit amet</p>; }\n",
    )

    assert _no_stub_check(tmp_path, session)["status"] == "pass"


def test_no_stub_text_flags_lorem_jsx_text(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.tsx", "export function Copy() { return <p>Lorem ipsum dolor sit amet</p>; }\n")

    check = _no_stub_check(tmp_path, session)

    assert check["status"] == "fail"
    assert check["refs"]["offenders"] == ["src/copy.tsx:1"]


def test_no_stub_text_flags_toast_message(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.ts", "toast.error(\"placeholder error message\");\n")

    check = _no_stub_check(tmp_path, session)

    assert check["status"] == "fail"
    assert check["refs"]["offenders"] == ["src/copy.ts:1"]


def test_verdict_consistency_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(intent_coverage={"built": ["audit log"], "partial": [{"feature": "audit log"}], "skipped": []}),
        initial_verdict="pass",
    )

    assert _checks_by_kind(outcome.verification_plan)["verdict_consistency"][0]["status"] == "fail"


def test_missing_passed_journey_downgrades_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(journeys=[]),
        initial_verdict="pass",
    )

    assert outcome.final_verdict == "partial"
    assert outcome.journey_failures == ["create_issue"]


@pytest.mark.asyncio
async def test_v5_pipeline_downgrades_agent_pass_and_preserves_input_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "src" / "App.tsx", "export const routes = ['/'];\nconst a = 'issue.create'; toast.success('Created');\n")
    _write(project / "api" / "issues.py", "router.post('/api/issues')\n")
    _write(project / "tests" / "issue_create.test.ts", "it('issue.create works', () => {})\n")
    _write(
        project / "CHARTER.md",
        "# CHARTER\n\n## Information Architecture Contract\n\n```json\n"
        + json.dumps(_ia(route_path="/missing-route"), indent=2)
        + "\n```\n",
    )

    async def fake_compile(**kwargs: Any) -> FlatSpec:
        session_dir = Path(kwargs["session_dir"])
        spec = _spec()
        _write(session_dir / "spec" / "spec.json", json.dumps(spec))
        return FlatSpec(
            intent=kwargs["intent"],
            project_kind="webapp",
            product_overview=spec["product_overview"],
            intent_claims=spec["intent_claims"],
            core_entities=spec["core_entities"],
            cold_start_states=spec["cold_start_states"],
            permissions=spec["permissions"],
            quality_constraints=spec["quality_constraints"],
            behavior_journeys=spec["behavior_journeys"],
        )

    async def fake_agent(
        _prompt: str,
        _options: Any,
        *,
        log_dir: Path,
        phase_name: str,
        phase_label: str,
        timeout: int,
        project_dir: Path,
    ) -> tuple[str, float, str, dict[str, Any]]:
        session_dir = log_dir.parent
        _write(session_dir / "verdict.json", json.dumps(_verdict()))
        return "done", 0.0, "agent-session", {}

    with patch("otto.v5_runner.compile_flat_spec", new=fake_compile):
        monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_agent)
        result = await run_v5_pipeline(project_dir=project, intent="build tracker", config={})

    assert result.verdict == "partial"
    graph = read_graph(project)
    assert graph["tasks"]["root"]["verdict"] == "partial"
    sessions = sorted((project / "otto_logs" / "sessions").glob("*"))
    session = sessions[-1]
    assert json.loads((session / "verdict.json").read_text())["verdict"] == "pass"
    summary = json.loads((session / "summary.json").read_text())
    assert summary["verdict"] == "partial"
    plan = json.loads((session / "verification_plan.json").read_text())
    assert any(c["kind"] == "route_resolves" and c["status"] == "fail" for c in plan["checks"])
