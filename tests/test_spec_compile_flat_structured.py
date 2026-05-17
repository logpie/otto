from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from otto.spec_compile_flat import (
    FlatSpec,
    INTENT_CLAIMS_MAX,
    PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS,
    SCHEMA_VERSION,
    SpecContractRepairExhaustedError,
    StructuredSpecValidationError,
    _PROMPT_TEMPLATE,
    _cleanup_root_spec_artifacts,
    _read_structured_output_tool_input,
    _run_compile,
    compile_flat_spec,
    load_flat_spec,
    validate_structured_spec,
)


def _ui_pass_model() -> dict[str, object]:
    observable = {
        "kind": "persisted_data_visible",
        "primary_action_id": "issue.create",
        "description": "The created issue title appears in the backlog after submit.",
        "text": "Fix login",
    }
    return {
        "start_state": "unauthenticated",
        "setup": [],
        "actions": [
            {
                "id": "issue.create",
                "state_changing": True,
                "role": "button",
                "name": "Create issue",
                "covers_primary_actions": ["issue.create"],
                "success_observables": [observable],
                "network_expectations": [],
            }
        ],
        "success_observables": [observable],
        "ready_policy": {"route": "/", "wait_for": "interactive"},
        "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
        "network_expectations": [],
        "final_dom_assertions": [observable],
    }


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
                "verification_level": "ui",
                "pass_model": _ui_pass_model(),
            }
        ],
    )


def _itracker_comment_mention_payload(pass_model: dict[str, object]) -> dict[str, Any]:
    payload = asdict(_valid_spec())
    payload["product_overview"]["one_liner"] = "Issue tracker with mention inbox notifications."
    payload["core_entities"][0] = {
        "id": "comment",
        "name": "Comment",
        "fields": [
            {
                "id": "comment.body",
                "name": "body",
                "type": "string",
                "intent_claim_ids": ["claim.issue_create"],
            }
        ],
        "states": ["draft", "posted"],
        "primary_actions": [
            {
                "id": "comment.mention",
                "verb": "send",
                "success_observable": "Mentioned user receives an inbox notification tied to the issue comment.",
                "error_observable": "Inline error explains why the mention cannot be sent.",
                "intent_claim_ids": ["claim.issue_create"],
            }
        ],
    }
    payload["behavior_journeys"][0].update({
        "id": "comment_mention_inbox",
        "description": (
            "A teammate comments with @maria on Fix login and Maria sees the "
            "mention in her inbox."
        ),
        "covers_primary_actions": ["comment.mention"],
        "start_state": "authenticated_seeded_workspace",
        "entry_route": "/",
        "verification_level": "ui",
        "pass_model": pass_model,
    })
    return payload


def _weak_comment_mention_pass_model() -> dict[str, object]:
    tautological = {
        "kind": "text_visible",
        "description": "Comment text appears.",
        "text": "@maria can you review Fix login?",
    }
    return {
        "start_state": "authenticated_seeded_workspace",
        "setup": [],
        "actions": [
            {
                "id": "comment.mention",
                "state_changing": True,
                "role": "button",
                "name": "Post comment",
                "covers_primary_actions": ["comment.mention"],
                "success_observables": [tautological],
                "network_expectations": [],
            }
        ],
        "success_observables": [tautological],
        "ready_policy": {"route": "/", "wait_for": "interactive"},
        "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
        "network_expectations": [],
        "final_dom_assertions": [tautological],
    }


def _strong_comment_mention_pass_model() -> dict[str, object]:
    observable = {
        "kind": "persisted_data_visible",
        "primary_action_id": "comment.mention",
        "description": (
            "After posting a comment that mentions Maria, Maria's inbox shows "
            "a Fix login notification from that comment."
        ),
        "text": "Fix login",
    }
    return {
        "start_state": "authenticated_seeded_workspace",
        "setup": [],
        "actions": [
            {
                "id": "comment.mention",
                "state_changing": True,
                "role": "button",
                "name": "Post comment",
                "covers_primary_actions": ["comment.mention"],
                "success_observables": [observable],
                "network_expectations": [],
            }
        ],
        "success_observables": [observable],
        "ready_policy": {"route": "/", "wait_for": "interactive"},
        "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 5000},
        "network_expectations": [],
        "final_dom_assertions": [observable],
    }


def _strict_union_type_paths(schema: Any, path: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(schema, dict):
        raw_type = schema.get("type")
        if isinstance(raw_type, list):
            paths.append(path)
        for key, value in schema.items():
            paths.extend(_strict_union_type_paths(value, f"{path}.{key}"))
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            paths.extend(_strict_union_type_paths(item, f"{path}[{index}]"))
    return paths


def test_valid_structured_flat_spec_passes() -> None:
    assert validate_structured_spec(_valid_spec(), strict=True) == []


def test_behavior_journey_role_is_not_in_schema_or_prompt() -> None:
    assert '"role": "illustrative"' not in _PROMPT_TEMPLATE
    assert "behavior_journeys[{jid}].role" not in _PROMPT_TEMPLATE


def test_compile_prompt_contains_v6_output_cap_guidance() -> None:
    assert "intent_claims cap <= 30" in _PROMPT_TEMPLATE
    assert "terse and stable" in _PROMPT_TEMPLATE
    assert "representative" in _PROMPT_TEMPLATE
    assert "critical flows" in _PROMPT_TEMPLATE
    assert "Build agents can reason from context" in _PROMPT_TEMPLATE


def test_compile_prompt_forbids_spec_file_writes() -> None:
    assert "StructuredOutput" in _PROMPT_TEMPLATE
    assert "Do not" in _PROMPT_TEMPLATE
    assert "`product-contract.json`" in _PROMPT_TEMPLATE
    assert "`spec.json`" in _PROMPT_TEMPLATE
    assert "project-root spec file" in _PROMPT_TEMPLATE


def test_root_spec_artifact_cleanup_is_allowlisted_and_idempotent(tmp_path: Path) -> None:
    removed_names = {
        "product-contract.json",
        "product_contract.json",
        "spec.json",
        "flat-spec.json",
        "otto_spec.json",
    }
    for name in removed_names:
        (tmp_path / name).write_text('{"unused": true}\n', encoding="utf-8")
    keep = tmp_path / "legitimate-config.json"
    keep.write_text('{"keep": true}\n', encoding="utf-8")

    removed = _cleanup_root_spec_artifacts(tmp_path)
    removed_again = _cleanup_root_spec_artifacts(tmp_path)

    assert {path.name for path in removed} == removed_names
    assert removed_again == []
    assert not any((tmp_path / name).exists() for name in removed_names)
    assert keep.read_text(encoding="utf-8") == '{"keep": true}\n'


@pytest.mark.asyncio
async def test_compile_flat_spec_removes_root_spec_artifacts_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_options(_project_dir: Path, _config: dict[str, object], *, agent_type: str | None = None):
        assert agent_type == "spec"
        return SimpleNamespace(
            max_turns=1,
            provider="claude",
            model="claude-sonnet-test",
        )

    async def fake_run_compile(
        _prompt: str,
        _options: object,
        log_dir: Path,
        project_dir: Path,
    ) -> str:
        (project_dir / "product-contract.json").write_text('{"stray": true}\n', encoding="utf-8")
        (project_dir / "spec.json").write_text('{"stray": true}\n', encoding="utf-8")
        (project_dir / "legitimate-config.json").write_text('{"keep": true}\n', encoding="utf-8")
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text("", encoding="utf-8")
        return json.dumps(asdict(_valid_spec()))

    monkeypatch.setattr("otto.spec_compile_flat.make_agent_options", fake_options)
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fake_run_compile)

    session_dir = tmp_path / "otto_logs" / "sessions" / "s1"
    await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=session_dir,
        intent="build an issue tracker",
        config={"spec_compile_no_cache": True},
    )

    assert not (tmp_path / "product-contract.json").exists()
    assert not (tmp_path / "spec.json").exists()
    assert (tmp_path / "legitimate-config.json").read_text(encoding="utf-8") == '{"keep": true}\n'
    assert (session_dir / "spec" / "spec.json").is_file()


@pytest.mark.asyncio
async def test_compile_flat_spec_missing_webapp_entry_route_is_hard_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_options(_project_dir: Path, _config: dict[str, object], *, agent_type: str | None = None):
        assert agent_type == "spec"
        return SimpleNamespace(
            max_turns=1,
            provider="claude",
            model="claude-sonnet-test",
        )

    async def fake_run_compile(
        _prompt: str,
        _options: object,
        log_dir: Path,
        _project_dir: Path,
    ) -> str:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text("", encoding="utf-8")
        payload = asdict(_valid_spec())
        payload["behavior_journeys"][0].pop("entry_route", None)
        return json.dumps(payload)

    monkeypatch.setattr("otto.spec_compile_flat.make_agent_options", fake_options)
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fake_run_compile)

    with pytest.raises(StructuredSpecValidationError, match="entry_route"):
        await compile_flat_spec(
            project_dir=tmp_path,
            session_dir=tmp_path / "otto_logs" / "sessions" / "s1",
            intent="build an issue tracker",
            config={"spec_compile_no_cache": True},
        )


@pytest.mark.asyncio
async def test_compile_flat_spec_repairs_inadequate_itracker_pass_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_options(_project_dir: Path, _config: dict[str, object], *, agent_type: str | None = None):
        assert agent_type == "spec"
        return SimpleNamespace(
            max_turns=1,
            provider="claude",
            model="claude-sonnet-test",
        )

    weak_payload = _itracker_comment_mention_payload(_weak_comment_mention_pass_model())
    repaired_payload = _itracker_comment_mention_payload(_strong_comment_mention_pass_model())
    repaired_payload["product_overview"]["one_liner"] = "This unrelated repair drift must be ignored."
    prompts: list[str] = []

    async def fake_run_compile(
        prompt: str,
        _options: object,
        log_dir: Path,
        _project_dir: Path,
    ) -> str:
        prompts.append(prompt)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text("", encoding="utf-8")
        return json.dumps(weak_payload if len(prompts) == 1 else repaired_payload)

    monkeypatch.setattr("otto.spec_compile_flat.make_agent_options", fake_options)
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fake_run_compile)

    session_dir = tmp_path / "otto_logs" / "sessions" / "s1"
    spec = await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=session_dir,
        intent="build an issue tracker with comment mentions and an inbox",
        config={"spec_compile_no_cache": True},
        max_retries=0,
    )

    assert len(prompts) == 2
    assert "comment_mention_inbox" in prompts[1]
    assert "state-changing action lacks a non-tautological post-action observable" in prompts[1]
    assert spec.product_overview["one_liner"] == weak_payload["product_overview"]["one_liner"]
    action_observable = spec.behavior_journeys[0]["pass_model"]["actions"][0]["success_observables"][0]
    assert action_observable["primary_action_id"] == "comment.mention"
    assert action_observable["kind"] == "persisted_data_visible"
    metrics = json.loads((session_dir / "compile_metrics.json").read_text(encoding="utf-8"))
    assert metrics["contract_repair_attempts"] == 1


@pytest.mark.asyncio
async def test_compile_flat_spec_exhausts_weak_itracker_pass_model_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_options(_project_dir: Path, _config: dict[str, object], *, agent_type: str | None = None):
        assert agent_type == "spec"
        return SimpleNamespace(
            max_turns=1,
            provider="claude",
            model="claude-sonnet-test",
        )

    weak_payload = _itracker_comment_mention_payload(_weak_comment_mention_pass_model())
    prompts: list[str] = []

    async def fake_run_compile(
        prompt: str,
        _options: object,
        log_dir: Path,
        _project_dir: Path,
    ) -> str:
        prompts.append(prompt)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text("", encoding="utf-8")
        return json.dumps(weak_payload)

    monkeypatch.setattr("otto.spec_compile_flat.make_agent_options", fake_options)
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fake_run_compile)

    session_dir = tmp_path / "otto_logs" / "sessions" / "s1"
    with pytest.raises(SpecContractRepairExhaustedError) as excinfo:
        await compile_flat_spec(
            project_dir=tmp_path,
            session_dir=session_dir,
            intent="build an issue tracker with comment mentions and an inbox",
            config={"spec_compile_no_cache": True},
            max_retries=0,
        )

    # 1 initial compile + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS repair attempts
    # (max_retries=0 + 1 + 5). The anti-false-pass INVARIANT this test guards —
    # a weak pass_model must exhaust and raise, never fall back, never write
    # spec.json — is unchanged below; only the bounded attempt count grew when
    # the brittle 2-cap was raised to 5 (05b204df9, run #9 evidence).
    assert len(prompts) == 1 + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS
    assert excinfo.value.code == "verification_contract_invalid"
    assert "comment_mention_inbox" in excinfo.value.path
    assert "state-changing action lacks a non-tautological post-action observable" in str(excinfo.value)
    assert not (session_dir / "spec" / "spec.json").exists()


@pytest.mark.asyncio
async def test_flat_spec_output_schema_avoids_ajv_strict_union_type_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options_seen: list[SimpleNamespace] = []

    def fake_options(_project_dir: Path, _config: dict[str, object], *, agent_type: str | None = None):
        assert agent_type == "spec"
        options = SimpleNamespace(
            max_turns=1,
            provider="claude",
            model="claude-sonnet-test",
        )
        options_seen.append(options)
        return options

    async def fake_run_compile(
        _prompt: str,
        _options: object,
        log_dir: Path,
        _project_dir: Path,
    ) -> str:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text("", encoding="utf-8")
        return json.dumps(asdict(_valid_spec()))

    monkeypatch.setattr("otto.spec_compile_flat.make_agent_options", fake_options)
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fake_run_compile)

    await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=tmp_path / "otto_logs" / "sessions" / "s1",
        intent="build an issue tracker",
        config={"spec_compile_no_cache": True},
    )

    schema = options_seen[0].output_format["schema"]
    assert _strict_union_type_paths(schema) == []


def test_intent_claims_over_cap_warns_without_strict_failure() -> None:
    spec = _valid_spec()
    claim_ids = [f"claim.issue_create_{idx}" for idx in range(INTENT_CLAIMS_MAX + 1)]
    spec.intent_claims = [
        {"id": claim_id, "text": f"Create issue claim {idx}", "source_line": idx}
        for idx, claim_id in enumerate(claim_ids, start=1)
    ]
    spec.core_entities[0]["fields"][0]["intent_claim_ids"] = claim_ids
    spec.core_entities[0]["primary_actions"][0]["intent_claim_ids"] = claim_ids

    warnings = validate_structured_spec(spec, strict=True)

    assert any("intent_claims has 31 entries" in warning for warning in warnings)


def test_validate_structured_spec_accepts_dict_from_disk() -> None:
    spec = _valid_spec()
    roundtripped = json.loads(json.dumps(asdict(spec)))

    warnings = validate_structured_spec(roundtripped, strict=True)

    assert warnings == []
    assert roundtripped["product_overview"]["top_level_pages"][0]["id"] == "team.backlog"


def test_product_overview_json_roundtrip_validates() -> None:
    spec = _valid_spec()
    roundtripped = json.loads(json.dumps(asdict(spec)))

    assert roundtripped["schema_version"] == SCHEMA_VERSION
    assert roundtripped["product_overview"]["primary_navigation"]["sidebar"] == ["team.backlog"]
    assert validate_structured_spec(roundtripped, strict=True) == []


def test_product_overview_missing_is_advisory() -> None:
    spec = _valid_spec()
    spec.product_overview = {}

    warnings = validate_structured_spec(spec, strict=True)

    assert any("product_overview is missing" in warning for warning in warnings)


def test_product_overview_top_level_pages_is_advisory() -> None:
    spec = _valid_spec()
    spec.product_overview["top_level_pages"] = []

    warnings = validate_structured_spec(spec, strict=True)

    assert any("top_level_pages" in warning for warning in warnings)


def test_phases_cross_reference_primary_actions() -> None:
    spec = _valid_spec()
    spec.product_overview["phases"][0]["covers_primary_action_ids"] = ["issue.archive"]

    warnings = validate_structured_spec(spec, strict=True)

    assert any("issue.archive" in warning for warning in warnings)


def test_sidebar_references_must_resolve_to_top_level_pages() -> None:
    spec = _valid_spec()
    spec.product_overview["primary_navigation"]["sidebar"] = ["team.archive"]

    warnings = validate_structured_spec(spec, strict=True)

    assert any("team.archive" in warning for warning in warnings)


def test_uncovered_intent_claim_warns() -> None:
    spec = _valid_spec()
    spec.intent_claims.append({"id": "claim.audit_log", "text": "Audit log exists", "source_line": 3})

    warnings = validate_structured_spec(spec, strict=True)

    assert any("claim.audit_log" in warning for warning in warnings)


def test_unreferenced_primary_action_warns() -> None:
    spec = _valid_spec()
    spec.core_entities[0]["primary_actions"].append({
        "id": "issue.delete",
        "verb": "delete",
        "success_observable": "Issue disappears",
        "error_observable": "Permission error is visible",
        "intent_claim_ids": ["claim.issue_create"],
    })

    warnings = validate_structured_spec(spec, strict=True)

    assert any("issue.delete" in warning for warning in warnings)


def test_webapp_root_cold_start_journey_gap_warns() -> None:
    spec = _valid_spec()
    spec.behavior_journeys[0]["start_state"] = "authenticated_seeded_workspace"
    spec.behavior_journeys[0]["entry_route"] = "/app"

    warnings = validate_structured_spec(spec, strict=True)

    assert any("entry_route '/'" in warning for warning in warnings)


def test_legacy_flat_spec_without_entry_route_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "intent": "legacy",
        "project_kind": "webapp",
        "behavior_journeys": [{"id": "legacy", "description": "User sees the app."}],
    }))

    with pytest.raises(StructuredSpecValidationError, match="entry_route"):
        load_flat_spec(path)


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

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: Any):
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

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: Any):
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

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: Any):
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


def test_structured_output_tool_input_requires_exact_claude_tool_name(tmp_path: Path) -> None:
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

    assert recovered is None

    messages.write_text(
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

    async def fake_run_agent_with_timeout(*_args: object, **kwargs: Any):
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
