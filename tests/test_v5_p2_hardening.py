from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from otto.browser_testing import classify_browser_command, validate_agent_browser_command
from otto.mcp_tools import _run_scaffold_certification
from otto.repair_gates import NO_REPAIR, REPAIR_NOW, repair_gate_for_verdict
from otto.spec_compile_flat import (
    _read_structured_output_tool_input,
    _run_compile,
)
from otto.v5_verification_plan import validate_lead_verdict


def _flat_spec_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 3,
        "intent": "issue tracker",
        "project_kind": "webapp",
        "product_overview": {
            "one_liner": "Issue tracker for teams.",
            "primary_users": [{"id": "member", "description": "triages work"}],
            "top_level_pages": [{"id": "team.backlog", "purpose": "triage issues"}],
            "primary_navigation": {"sidebar": ["team.backlog"]},
            "out_of_scope": [],
            "phases": [],
        },
        "intent_claims": [{"id": "claim.issue", "text": "Create issues", "source_line": 1}],
        "core_entities": [
            {
                "id": "issue",
                "name": "Issue",
                "fields": [{"id": "issue.title", "intent_claim_ids": ["claim.issue"]}],
                "states": ["empty", "open"],
                "primary_actions": [
                    {
                        "id": "issue.create",
                        "verb": "create",
                        "success_observable": "Issue appears",
                        "error_observable": "Error appears",
                        "intent_claim_ids": ["claim.issue"],
                    }
                ],
            }
        ],
        "cold_start_states": [{"id": "empty", "entity_id": "issue", "cta_action_id": "issue.create"}],
        "permissions": [],
        "quality_constraints": [],
        "behavior_journeys": [],
    }
    payload.update(overrides)
    return payload


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _charter() -> str:
    return (
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps({
            "routes": [{"id": "team.backlog", "path": "/", "key_text": "Issues"}],
            "action_surfaces": [{"id": "issue.create"}],
            "api_endpoints": [],
        })
        + "\n```\n"
    )


def test_repair_gate_defaults_detector_findings_to_agent_repair() -> None:
    for payload in (
        {
            "feature_id": "layout",
            "verdict": "partial",
            "detail": "The screenshot looks sparse.",
            "surface": "screenshot",
            "methodology": "visual-only",
            "evidence_refs": ["home.png"],
        },
        {
            "feature_id": "filters",
            "verdict": "partial",
            "detail": "The dashboard filter was checked by fetching HTML.",
            "surface": "DOM",
            "methodology": "http-request",
            "evidence_refs": ["curl /dashboard"],
        },
        {"feature_id": "search", "verdict": "blocked", "detail": ""},
    ):
        decision = repair_gate_for_verdict(payload)
        assert decision.action == REPAIR_NOW
        assert decision.actionable is True


def test_repair_gate_keeps_typed_non_actionable_provider_failures_out_of_repair() -> None:
    decision = repair_gate_for_verdict(
        {
            "feature_id": "checkout",
            "verdict": "blocked",
            "failure_kind": "provider_auth_exhausted",
            "detail": "provider refused the request",
        }
    )

    assert decision.action == NO_REPAIR
    assert decision.actionable is False
    assert "provider" in decision.reason


def test_browser_tool_adapter_rejects_substring_impostors_without_preflight_skip() -> None:
    command = ["my-agent-browser-wrapper", "open", "http://127.0.0.1:3000"]

    assert classify_browser_command(command) == "other"
    assert validate_agent_browser_command(command) is None


def test_browser_tool_adapter_uses_explicit_script_table(tmp_path: Path) -> None:
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"browser": "playwright test tests/browser.spec.ts"}}),
    )

    assert classify_browser_command(["npm", "run", "browser"], cwd=tmp_path) == "playwright"


def test_compile_uses_typed_structured_output_result_not_prose(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    payload = _flat_spec_payload()
    payload["intent_claims"][0]["text"] = "typed result field wins"

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: Any):
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            json.dumps({"type": "result", "subtype": "success", "result": "not json"}) + "\n",
            encoding="utf-8",
        )
        return "not json either", 0.0, "compile-session", {"structured_output": payload}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    recovered = asyncio.run(
        _run_compile(
            "compile prompt",
            SimpleNamespace(max_turns=1, provider="codex"),
            tmp_path / "compile-agent",
            tmp_path,
        )
    )

    assert json.loads(recovered)["intent_claims"][0]["text"] == "typed result field wins"


def test_compile_ignores_fuzzy_structured_tool_name_alias(tmp_path: Path) -> None:
    messages = tmp_path / "messages.jsonl"
    messages.write_text(
        json.dumps({
            "type": "assistant",
            "blocks": [
                {
                    "type": "tool_use",
                    "name": "submit_spec",
                    "input": _flat_spec_payload(),
                }
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    assert _read_structured_output_tool_input(messages) is None


def test_webapp_missing_structured_spec_fails_verification(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _write(
        session / "spec" / "spec.json",
        json.dumps({"schema_version": 3, "project_kind": "webapp", "behavior_journeys": []}),
    )
    _write(tmp_path / "CHARTER.md", _charter())

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict={"verdict": "pass", "summary": "done", "journeys": []},
        initial_verdict="pass",
    )

    structured = [
        check
        for check in outcome.verification_plan["checks"]
        if check["kind"] == "structured_contract_present"
    ][0]
    assert structured["status"] == "fail"
    assert structured["required"] is True
    assert outcome.final_verdict == "partial"


def test_webapp_missing_ia_contract_fails_verification(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _write(session / "spec" / "spec.json", json.dumps(_flat_spec_payload()))

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict={"verdict": "pass", "summary": "done", "journeys": []},
        initial_verdict="pass",
    )

    structured = [
        check
        for check in outcome.verification_plan["checks"]
        if check["kind"] == "structured_contract_present"
    ][0]
    assert structured["status"] == "fail"
    assert structured["required"] is True
    assert outcome.final_verdict == "partial"


def test_non_webapp_legacy_contract_stays_skippable(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _write(
        session / "spec" / "spec.json",
        json.dumps({"schema_version": 3, "project_kind": "cli", "behavior_journeys": []}),
    )

    outcome = validate_lead_verdict(
        project_dir=tmp_path,
        worktree_dir=tmp_path,
        session_dir=session,
        agent_verdict={"verdict": "pass", "summary": "done", "journeys": []},
        initial_verdict="pass",
    )

    structured = [
        check
        for check in outcome.verification_plan["checks"]
        if check["kind"] == "structured_contract_present"
    ][0]
    assert structured["status"] == "skipped"
    assert structured["required"] is False
    assert outcome.final_verdict == "pass"


def test_scaffold_certification_nonzero_exit_is_partial(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    session = tmp_path / "session"

    result = asyncio.run(
        _run_scaffold_certification(
            project_dir=project,
            session_dir=session,
            build_command="false",
            summary="would have been pass",
        )
    )

    assert result["verdict"] == "partial"
    written = json.loads((session / "verify" / "verify-result.json").read_text(encoding="utf-8"))
    assert written["verdict"] == "partial"
