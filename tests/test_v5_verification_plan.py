from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from otto.queue.task_graph import read_graph
from otto.spec_compile_flat import FlatSpec
from otto.v5_runner import run_v5_pipeline
from otto.v5_verification_plan import RunnerVerificationOutcome, validate_lead_verdict


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_git_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")


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


def _advisories_by_kind(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for advisory in plan.get("advisories", []):
        out.setdefault(advisory["kind"], []).append(advisory)
    return out


def _stub_text_advisory(project: Path, session: Path) -> dict[str, Any]:
    outcome = validate_lead_verdict(
        project_dir=project,
        worktree_dir=project,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )
    return _advisories_by_kind(outcome.verification_plan)["no_stub_text"][0]


def test_verification_plan_all_checks_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        matrix_scope="leaf",
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
        matrix_scope="leaf",
    )

    assert outcome.final_verdict == "pass"
    assert _advisories_by_kind(outcome.verification_plan)["action_has_test"][0]["id"] == "issue.create"


def test_route_grep_failure_is_advisory_and_does_not_downgrade_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(route_path="/missing-route"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        matrix_scope="leaf",
    )

    assert "route_resolves" not in _checks_by_kind(outcome.verification_plan)
    route_advisories = _advisories_by_kind(outcome.verification_plan)["route_resolves"]
    assert route_advisories[0]["status"] == "warn"
    assert route_advisories[0]["required"] is False
    assert route_advisories[0]["refs"]["path"] == "/missing-route"
    assert outcome.final_verdict == "pass"


def test_leaf_integration_only_scope_skips_unrelated_full_matrix(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(route_path="/missing-route"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        node_kind="leaf",
        matrix_scope="integration_only",
    )

    checks = _checks_by_kind(outcome.verification_plan)
    assert "route_resolves" not in checks
    assert checks["local_scope_check"][0]["status"] == "pass"
    assert outcome.verification_plan["full_matrix"] is False
    assert outcome.final_verdict == "pass"


def test_integration_node_runs_full_matrix_when_leaf_scope_skips(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(route_path="/missing-route"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        node_kind="integration",
        matrix_scope="integration_only",
    )

    assert "route_resolves" not in _checks_by_kind(outcome.verification_plan)
    route_advisories = _advisories_by_kind(outcome.verification_plan)["route_resolves"]
    assert route_advisories[0]["status"] == "warn"
    assert outcome.verification_plan["full_matrix"] is True
    assert outcome.final_verdict == "pass"


def test_deprecation_warnings_are_advisory_and_do_not_downgrade_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session)

    verdict = _verdict()
    verdict["test_output"] = (
        "tests passed\n"
        "DeprecationWarning: websockets.legacy is deprecated\n"
    )

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=verdict,
        initial_verdict="pass",
    )

    assert "deprecation_warnings" not in _checks_by_kind(outcome.verification_plan)
    advisory = _advisories_by_kind(outcome.verification_plan)["deprecation_warnings"][0]
    assert advisory["status"] == "warn"
    assert advisory["required"] is False
    assert advisory["refs"]["warnings"] == [
        "DeprecationWarning: websockets.legacy is deprecated"
    ]
    assert outcome.final_verdict == "pass"


@pytest.mark.asyncio
async def test_deprecation_telemetry_keeps_bug_b1_pass_and_structured_gates_still_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def deprecation_outcome(
        name: str,
        *,
        test_output: str = "",
        evidence_text: str = "",
    ) -> tuple[str, str, list[str]]:
        project = tmp_path / name / "project"
        session = tmp_path / name / "session"
        project.mkdir(parents=True)
        _passing_project(project, session)
        verdict = _verdict()
        if test_output:
            verdict["test_output"] = test_output
        if evidence_text:
            log_path = session / "test_output.log"
            _write(log_path, evidence_text)
            verdict["evidence"] = [str(log_path)]
        outcome = validate_lead_verdict(
            project_dir=project,
            worktree_dir=project,
            session_dir=session,
            agent_verdict=verdict,
            initial_verdict="pass",
        )
        advisory = _advisories_by_kind(outcome.verification_plan)["deprecation_warnings"][0]
        return outcome.final_verdict, str(advisory["status"]), list(advisory["refs"]["warnings"])

    bug_b1 = deprecation_outcome(
        "bug-b1",
        test_output=(
            "DeprecationWarning filtered — 7/7 tests pass with 0 warnings\n"
        ),
        evidence_text=(
            "/tmp/project/backend/.venv/lib/python3.11/site-packages/passlib/utils/__init__.py:854: "
            "DeprecationWarning: 'crypt' is deprecated and slated for removal\n"
        ),
    )
    observed["bug_b1_line_and_site_packages_warning_are_advisory"] = (
        bug_b1[0] == "pass"
        and bug_b1[1] == "warn"
        and bug_b1[2] == [
            "DeprecationWarning filtered — 7/7 tests pass with 0 warnings",
            "test_output.log: /tmp/project/backend/.venv/lib/python3.11/site-packages/passlib/utils/__init__.py:854: DeprecationWarning: 'crypt' is deprecated and slated for removal",
        ]
    )

    product_warning = deprecation_outcome(
        "product",
        evidence_text=(
            "backend/auth.py:38: DeprecationWarning: datetime.datetime.utcnow() is deprecated "
            "and scheduled for removal in a future version\n"
        ),
    )
    observed["product_warning_is_advisory"] = (
        product_warning[0] == "pass"
        and product_warning[1] == "warn"
        and "backend/auth.py" in product_warning[2][0]
    )

    failing_journey_project = tmp_path / "failing-journey" / "project"
    failing_journey_session = tmp_path / "failing-journey" / "session"
    failing_journey_project.mkdir(parents=True)
    _passing_project(failing_journey_project, failing_journey_session)
    failing_journey_verdict = _verdict(
        journeys=[{"id": "create_issue", "passed": False, "detail": "product test failed"}],
        test_output="FAILED tests/test_issue.py::test_create_issue",
    )
    failing_journey = validate_lead_verdict(
        project_dir=failing_journey_project,
        worktree_dir=failing_journey_project,
        session_dir=failing_journey_session,
        agent_verdict=failing_journey_verdict,
        initial_verdict="pass",
    )
    observed["genuine_product_failure_still_gates"] = (
        failing_journey.final_verdict == "partial"
        and failing_journey.journey_failures == ["create_issue"]
    )

    from otto import lead as lead_mod

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
        del phase_name, phase_label, timeout, project_dir
        session_dir = log_dir.parent
        _write(session_dir / "verdict.json", json.dumps(_verdict()))
        return "done", 0.0, "agent-session", {}

    def fake_validate(**kwargs: Any) -> RunnerVerificationOutcome:
        return RunnerVerificationOutcome(
            final_verdict="partial",
            verification_plan={"checks": []},
            runner_checks_summary=[
                {
                    "kind": "verdict_consistency",
                    "id": "intent_coverage",
                    "status": "fail",
                    "detail": "built claims overlap partial/skipped claims",
                }
            ],
            journey_failures=[],
        )

    monkeypatch.setattr("otto.agent.make_agent_options", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_agent)
    monkeypatch.setattr("otto.mcp_tools.create_otto_mcp_server", lambda **_k: object())
    monkeypatch.setattr("otto.v5_verification_plan.validate_lead_verdict", fake_validate)

    lead_session = tmp_path / "lead-session"
    lead_result = await lead_mod.run_lead(
        task_id="leaf",
        intent="leaf",
        project_dir=tmp_path / "lead-project",
        session_dir=lead_session,
        integration_branch="main",
        config={},
        kind="plan_or_inline",
    )
    lead_summary = json.loads((lead_session / "summary.json").read_text(encoding="utf-8"))
    observed["downgrade_reason_recorded"] = (
        lead_result.verdict == "partial"
        and bool(lead_result.failure_reason)
        and lead_result.failure_reason == lead_summary["failure_reason"]
        and "verdict_consistency" in lead_result.failure_reason
    )

    assert observed == {
        "bug_b1_line_and_site_packages_warning_are_advisory": True,
        "product_warning_is_advisory": True,
        "genuine_product_failure_still_gates": True,
        "downgrade_reason_recorded": True,
    }


@pytest.mark.asyncio
async def test_run_lead_passes_matrix_scope_to_runner_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import lead as lead_mod

    captured: list[tuple[str, str]] = []

    def fake_validate(**kwargs: Any) -> RunnerVerificationOutcome:
        captured.append((kwargs["node_kind"], kwargs["matrix_scope"]))
        return RunnerVerificationOutcome(
            final_verdict=kwargs["initial_verdict"],
            verification_plan={"checks": []},
            runner_checks_summary=[],
            journey_failures=[],
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
        del phase_name, phase_label, timeout, project_dir
        session_dir = log_dir.parent
        _write(session_dir / "verdict.json", json.dumps(_verdict()))
        return "done", 0.0, "agent-session", {}

    monkeypatch.setattr("otto.agent.make_agent_options", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_agent)
    monkeypatch.setattr("otto.mcp_tools.create_otto_mcp_server", lambda **_k: object())
    monkeypatch.setattr("otto.v5_verification_plan.validate_lead_verdict", fake_validate)

    config = {"verification_plan": {"matrix_scope": "integration_only"}}
    await lead_mod.run_lead(
        task_id="leaf",
        intent="leaf",
        project_dir=tmp_path,
        session_dir=tmp_path / "leaf-session",
        integration_branch="main",
        config=config,
        kind="plan_or_inline",
    )
    await lead_mod.run_lead(
        task_id="root",
        intent="integrate",
        project_dir=tmp_path,
        session_dir=tmp_path / "integration-session",
        integration_branch=None,
        config=config,
        kind="integration",
        child_summaries=[],
    )

    assert captured == [
        ("leaf", "integration_only"),
        ("integration", "integration_only"),
    ]


def test_page_grep_failure_is_advisory_and_does_not_downgrade_pass(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(route_path="/missing-pm-page"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        matrix_scope="leaf",
    )

    assert "page_resolves" not in _checks_by_kind(outcome.verification_plan)
    page_advisories = _advisories_by_kind(outcome.verification_plan)["page_resolves"]
    assert page_advisories[0]["id"] == "team.backlog"
    assert page_advisories[0]["status"] == "warn"
    assert page_advisories[0]["required"] is False
    assert outcome.final_verdict == "pass"


def test_endpoint_grep_failure_is_advisory(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write_contract(tmp_path, session, ia=_ia(endpoint_path="/api/missing"))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        matrix_scope="leaf",
    )

    assert "endpoint_resolves" not in _checks_by_kind(outcome.verification_plan)
    advisory = _advisories_by_kind(outcome.verification_plan)["endpoint_resolves"][0]
    assert advisory["status"] == "warn"
    assert advisory["required"] is False
    assert outcome.final_verdict == "pass"


def test_action_has_test_text_search_is_advisory(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    (tmp_path / "tests" / "issue_create.test.ts").unlink()

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        matrix_scope="leaf",
    )

    assert "action_has_test" not in _checks_by_kind(outcome.verification_plan)
    advisory = _advisories_by_kind(outcome.verification_plan)["action_has_test"][0]
    assert advisory["status"] == "warn"
    assert advisory["required"] is False
    assert outcome.final_verdict == "pass"


def test_mutating_action_feedback_text_search_is_advisory(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "App.tsx", "const a = 'issue.create';\n")

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
        matrix_scope="leaf",
    )

    assert "mutating_action_has_feedback" not in _checks_by_kind(outcome.verification_plan)
    advisory = _advisories_by_kind(outcome.verification_plan)["mutating_action_has_feedback"][0]
    assert advisory["status"] == "warn"
    assert advisory["required"] is False
    assert outcome.final_verdict == "pass"


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
        matrix_scope="leaf",
    )

    assert _checks_by_kind(outcome.verification_plan)["entity_has_empty_state"][0]["status"] == "fail"


def test_no_stub_text_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.tsx", "export function Copy() { return <button>TODO: implement</button>; }\n")

    check = _stub_text_advisory(tmp_path, session)

    assert check["status"] == "warn"
    assert check["required"] is False
    assert check["refs"]["offenders"] == ["src/copy.tsx:1"]


def test_no_stub_text_ignores_import_identifiers(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "Placeholder.tsx", "import { Placeholder } from 'react';\n")

    assert _stub_text_advisory(tmp_path, session)["status"] == "info"


def test_no_stub_text_ignores_comments(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.ts", "// TODO: refactor\nexport const label = 'Ready';\n")

    assert _stub_text_advisory(tmp_path, session)["status"] == "info"


def test_no_stub_text_ignores_build_artifacts(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "dist" / "index-abc.js", "console.error('TODO: implement generated code');\n")

    assert _stub_text_advisory(tmp_path, session)["status"] == "info"


def test_no_stub_text_ignores_otto_worktrees(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(
        tmp_path / ".worktrees" / "v5-foo" / "src" / "X.tsx",
        "export function X() { return <p>Lorem ipsum dolor sit amet</p>; }\n",
    )

    assert _stub_text_advisory(tmp_path, session)["status"] == "info"


def test_no_stub_text_flags_lorem_jsx_text(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.tsx", "export function Copy() { return <p>Lorem ipsum dolor sit amet</p>; }\n")

    check = _stub_text_advisory(tmp_path, session)

    assert check["status"] == "warn"
    assert check["required"] is False
    assert check["refs"]["offenders"] == ["src/copy.tsx:1"]


def test_no_stub_text_flags_toast_message(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "src" / "copy.ts", "toast.error(\"placeholder error message\");\n")

    check = _stub_text_advisory(tmp_path, session)

    assert check["status"] == "warn"
    assert check["required"] is False
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


def test_decisions_appended_legacy_missing_field_is_accepted(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=json.loads(json.dumps(_verdict())),
        initial_verdict="pass",
    )

    assert outcome.final_verdict == "pass"
    assert "decisions_broadcast" not in _checks_by_kind(outcome.verification_plan)


def test_shared_schema_change_without_decision_no_longer_downgrades(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _passing_project(tmp_path, session)
    _write(tmp_path / "shared" / "types.ts", "export type Issue = { id: string };\n")
    _init_git_repo(tmp_path)
    _write(
        tmp_path / "shared" / "types.ts",
        "export type Issue = { id: string; status: string };\n",
    )

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict=_verdict(),
        initial_verdict="pass",
    )

    assert outcome.final_verdict == "pass"
    assert "decisions_broadcast" not in _checks_by_kind(outcome.verification_plan)


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
async def test_v5_pipeline_preserves_agent_pass_when_only_route_grep_is_missing(
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
        result = await run_v5_pipeline(
            project_dir=project,
            intent="build tracker",
            config={"verification_plan": {"matrix_scope": "leaf"}},
        )

    assert result.verdict == "pass"
    graph = read_graph(project)
    assert graph["tasks"]["root"]["verdict"] == "pass"
    sessions = sorted((project / "otto_logs" / "sessions").glob("*"))
    session = sessions[-1]
    assert json.loads((session / "verdict.json").read_text())["verdict"] == "pass"
    summary = json.loads((session / "summary.json").read_text())
    assert summary["verdict"] == "pass"
    plan = json.loads((session / "verification_plan.json").read_text())
    assert not any(c["kind"] == "route_resolves" for c in plan["checks"])
    assert any(c["kind"] == "route_resolves" and c["status"] == "warn" for c in plan["advisories"])
