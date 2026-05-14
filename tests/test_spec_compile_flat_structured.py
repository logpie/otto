from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.spec_compile_flat import (
    FlatSpec,
    StructuredSpecValidationError,
    _read_structured_output_tool_input,
    _run_compile,
    load_flat_spec,
    validate_structured_spec,
)


def _valid_spec() -> FlatSpec:
    return FlatSpec(
        project_kind="webapp",
        product_overview={
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
        intent_claims=[
            {"id": "claim.issue_create", "text": "Members can create issues", "source_line": 1},
            {"id": "claim.form_feedback", "text": "Forms show feedback", "source_line": 2},
        ],
        core_entities=[
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
                        "success_observable": "Issue appears in the backlog",
                        "error_observable": "Inline error explains the missing title",
                        "intent_claim_ids": ["claim.issue_create"],
                    }
                ],
            }
        ],
        cold_start_states=[
            {"id": "unauthenticated", "name": "Unauthenticated"},
            {"id": "empty_workspace", "name": "Empty workspace"},
        ],
        permissions=[{"id": "member", "name": "Member", "gates": ["issue.create"]}],
        quality_constraints=[
            {
                "id": "quality.form_feedback",
                "text": "All forms have user-visible feedback",
                "intent_claim_ids": ["claim.form_feedback"],
            }
        ],
        behavior_journeys=[
            {
                "id": "create_issue_from_home",
                "role": "illustrative",
                "description": "A visitor reaches /, signs in, creates an issue, and sees it in the backlog.",
                "covers_primary_actions": ["issue.create"],
                "start_state": "unauthenticated",
                "entry_route": "/",
            }
        ],
    )


def test_valid_structured_flat_spec_passes() -> None:
    assert validate_structured_spec(_valid_spec(), strict=True) == []


def test_validate_structured_spec_accepts_dict_from_disk() -> None:
    spec = _valid_spec()
    roundtripped = json.loads(json.dumps(asdict(spec)))

    warnings = validate_structured_spec(roundtripped, strict=True)

    assert warnings == []
    assert roundtripped["product_overview"]["top_level_pages"][0]["id"] == "team.backlog"


def test_product_overview_json_roundtrip_validates() -> None:
    spec = _valid_spec()
    roundtripped = json.loads(json.dumps(asdict(spec)))

    assert roundtripped["schema_version"] == 3
    assert roundtripped["product_overview"]["primary_navigation"]["sidebar"] == ["team.backlog"]
    assert validate_structured_spec(roundtripped, strict=True) == []


def test_product_overview_required_for_webapp() -> None:
    spec = _valid_spec()
    spec.product_overview = {}

    with pytest.raises(StructuredSpecValidationError, match="product_overview is required"):
        validate_structured_spec(spec, strict=True)


def test_product_overview_top_level_pages_must_be_listed() -> None:
    spec = _valid_spec()
    spec.product_overview["top_level_pages"] = []

    with pytest.raises(StructuredSpecValidationError, match="top_level_pages"):
        validate_structured_spec(spec, strict=True)


def test_phases_cross_reference_primary_actions() -> None:
    spec = _valid_spec()
    spec.product_overview["phases"][0]["covers_primary_action_ids"] = ["issue.archive"]

    with pytest.raises(StructuredSpecValidationError, match="issue.archive"):
        validate_structured_spec(spec, strict=True)


def test_sidebar_references_must_resolve_to_top_level_pages() -> None:
    spec = _valid_spec()
    spec.product_overview["primary_navigation"]["sidebar"] = ["team.archive"]

    with pytest.raises(StructuredSpecValidationError, match="team.archive"):
        validate_structured_spec(spec, strict=True)


def test_uncovered_intent_claim_fails() -> None:
    spec = _valid_spec()
    spec.intent_claims.append({"id": "claim.audit_log", "text": "Audit log exists", "source_line": 3})

    with pytest.raises(StructuredSpecValidationError, match="claim.audit_log"):
        validate_structured_spec(spec, strict=True)


def test_unreferenced_primary_action_fails() -> None:
    spec = _valid_spec()
    spec.core_entities[0]["primary_actions"].append({
        "id": "issue.delete",
        "verb": "delete",
        "success_observable": "Issue disappears",
        "error_observable": "Permission error is visible",
        "intent_claim_ids": ["claim.issue_create"],
    })

    with pytest.raises(StructuredSpecValidationError, match="issue.delete"):
        validate_structured_spec(spec, strict=True)


def test_webapp_requires_root_cold_start_journey() -> None:
    spec = _valid_spec()
    spec.behavior_journeys[0]["start_state"] = "authenticated_seeded_workspace"
    spec.behavior_journeys[0]["entry_route"] = "/app"

    with pytest.raises(StructuredSpecValidationError, match="entry_route '/'"):
        validate_structured_spec(spec, strict=True)


def test_legacy_flat_spec_loads_and_warns(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "intent": "legacy",
        "project_kind": "webapp",
        "behavior_journeys": [{"id": "legacy", "description": "User sees the app."}],
    }))

    spec = load_flat_spec(path)
    warnings = validate_structured_spec(spec, strict=False)

    assert spec.intent_claims == []
    assert spec.core_entities == []
    assert any("structured spec fields are absent" in warning for warning in warnings)


@pytest.mark.asyncio
async def test_run_compile_recovers_large_result_from_messages_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_text = "compile-result-payload-" + ("x" * 33_000)
    payload = asdict(_valid_spec())
    payload["quality_constraints"][0]["text"] = large_text
    result_text = json.dumps(payload)
    assert len(result_text) > 32_000

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: object):
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            json.dumps({"type": "result", "subtype": "success", "result": result_text}) + "\n",
            encoding="utf-8",
        )
        return "", 0.0, "compile-session", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    recovered = await _run_compile(
        "compile prompt",
        SimpleNamespace(max_turns=1),
        tmp_path / "compile-agent",
        tmp_path,
    )

    assert recovered == result_text
    parsed = json.loads(recovered)
    assert parsed["quality_constraints"][0]["text"] == large_text


@pytest.mark.asyncio
async def test_run_compile_prefers_result_structured_output_over_prose_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = asdict(_valid_spec())
    payload["core_entities"][0]["name"] = "Structured result entity"

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: object):
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "Product contract emitted as prose.",
                "structured_output": payload,
            }) + "\n",
            encoding="utf-8",
        )
        return "legacy text should not be used", 0.0, "compile-session", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    recovered = await _run_compile(
        "compile prompt",
        SimpleNamespace(max_turns=1),
        tmp_path / "compile-agent",
        tmp_path,
    )

    parsed = json.loads(recovered)
    assert parsed["core_entities"][0]["name"] == "Structured result entity"


@pytest.mark.asyncio
async def test_run_compile_prefers_structured_output_tool_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = asdict(_valid_spec())
    payload["intent_claims"][0]["text"] = "structured tool payload wins"

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: object):
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "type": "assistant",
                    "blocks": [
                        {
                            "type": "tool_use",
                            "id": "toolu_structured",
                            "name": "StructuredOutput",
                            "input": payload,
                        }
                    ],
                }),
                json.dumps({
                    "type": "result",
                    "subtype": "success",
                    "result": "Product contract emitted. This is a prose summary, not JSON.",
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        return "legacy text should not be used", 0.0, "compile-session", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    recovered = await _run_compile(
        "compile prompt",
        SimpleNamespace(max_turns=1),
        tmp_path / "compile-agent",
        tmp_path,
    )

    parsed = json.loads(recovered)
    assert parsed["intent_claims"][0]["text"] == "structured tool payload wins"
    assert parsed["behavior_journeys"][0]["id"] == payload["behavior_journeys"][0]["id"]


def test_structured_output_tool_input_accepts_submit_spec_alias(tmp_path: Path) -> None:
    payload = asdict(_valid_spec())
    messages = tmp_path / "messages.jsonl"
    messages.write_text(
        json.dumps({
            "type": "assistant",
            "blocks": [
                {
                    "type": "tool_use",
                    "id": "toolu_submit",
                    "name": "submit_spec",
                    "input": payload,
                }
            ],
        }) + "\n",
        encoding="utf-8",
    )

    recovered = _read_structured_output_tool_input(messages)

    assert recovered is not None
    assert recovered["project_kind"] == "webapp"
    assert recovered["core_entities"][0]["id"] == "issue"


@pytest.mark.asyncio
async def test_run_compile_falls_back_to_returned_text_without_messages_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = asdict(_valid_spec())
    payload["permissions"][0]["name"] = "Returned text fallback"
    result_text = "Here is the JSON:\n" + json.dumps(payload) + "\nDone."

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: object):
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            json.dumps({"type": "assistant", "blocks": [{"type": "text", "text": "working"}]}) + "\n",
            encoding="utf-8",
        )
        return result_text, 0.0, "compile-session", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    recovered = await _run_compile(
        "compile prompt",
        SimpleNamespace(max_turns=1),
        tmp_path / "compile-agent",
        tmp_path,
    )

    parsed = json.loads(recovered)
    assert parsed["permissions"][0]["name"] == "Returned text fallback"
