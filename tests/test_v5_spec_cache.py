from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from otto.spec_compile_flat import compile_flat_spec, compile_message_metrics_from_jsonl


def _payload() -> dict[str, Any]:
    observable = {
        "kind": "persisted_data_visible",
        "primary_action_id": "issue.create",
        "description": "The created issue title appears in the backlog after submit.",
        "text": "Fix login",
    }
    return {
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
        "intent_claims": [
            {"id": "claim.issue_create", "text": "Members can create issues", "source_line": 1}
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
                        "success_observable": "Issue appears in the backlog",
                        "error_observable": "Inline error explains the missing title",
                        "intent_claim_ids": ["claim.issue_create"],
                    }
                ],
            }
        ],
        "cold_start_states": [{"id": "unauthenticated", "name": "Unauthenticated"}],
        "permissions": [{"id": "member", "name": "Member", "gates": ["issue.create"]}],
        "quality_constraints": [],
        "behavior_journeys": [
            {
                "id": "create_issue_from_home",
                "role": "illustrative",
                "description": "A visitor reaches /, signs in, creates an issue, and sees it in the backlog.",
                "covers_primary_actions": ["issue.create"],
                "start_state": "unauthenticated",
                "entry_route": "/",
                "verification_level": "ui",
                "pass_model": {
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
                },
            }
        ],
    }


def _patch_compile_agent(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_options(_project_dir: Path, config: dict[str, Any], *, agent_type: str | None = None):
        return SimpleNamespace(
            max_turns=1,
            provider=config.get("provider", "claude"),
            model=config.get("model", "claude-sonnet-test"),
        )

    async def fake_run_compile(
        _prompt: str,
        _options: Any,
        log_dir: Path,
        _project_dir: Path,
    ) -> str:
        calls.append(str(log_dir))
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "type": "assistant",
                    "ts": "2026-05-14T10:00:01Z",
                    "blocks": [{"type": "text", "text": "{"}],
                    "usage": {"output_tokens": 2, "total_tokens": 2},
                }),
                json.dumps({
                    "type": "phase_end",
                    "phase": "spec",
                    "duration_s": 1.5,
                    "usage": {"input_tokens": 20, "output_tokens": 7, "total_tokens": 27},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        return json.dumps(_payload())

    monkeypatch.setattr("otto.spec_compile_flat.make_agent_options", fake_options)
    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fake_run_compile)


@pytest.mark.asyncio
async def test_spec_compile_cache_reuses_identical_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_compile_agent(monkeypatch, calls)

    first = await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=tmp_path / "otto_logs" / "sessions" / "s1",
        intent="build an issue tracker",
        config={},
    )
    second_session = tmp_path / "otto_logs" / "sessions" / "s2"
    second = await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=second_session,
        intent="build an issue tracker",
        config={},
    )

    assert len(calls) == 1
    assert second.intent_hash == first.intent_hash
    metrics = json.loads((second_session / "compile_metrics.json").read_text(encoding="utf-8"))
    assert metrics["cache_hit"] is True
    assert metrics["total_tokens"] == 0


@pytest.mark.asyncio
async def test_spec_compile_cache_misses_when_model_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_compile_agent(monkeypatch, calls)

    await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=tmp_path / "otto_logs" / "sessions" / "s1",
        intent="build an issue tracker",
        config={"model": "claude-model-a"},
    )
    await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=tmp_path / "otto_logs" / "sessions" / "s2",
        intent="build an issue tracker",
        config={"model": "claude-model-b"},
    )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_spec_compile_corrupt_cache_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_compile_agent(monkeypatch, calls)

    await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=tmp_path / "otto_logs" / "sessions" / "s1",
        intent="build an issue tracker",
        config={},
    )
    cache_specs = list((tmp_path / "otto_logs" / "cross-sessions" / "spec-cache").glob("*/spec.json"))
    assert len(cache_specs) == 1
    cache_specs[0].write_text("{not json", encoding="utf-8")

    await compile_flat_spec(
        project_dir=tmp_path,
        session_dir=tmp_path / "otto_logs" / "sessions" / "s2",
        intent="build an issue tracker",
        config={},
    )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_spec_compile_no_cache_invokes_provider_each_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    _patch_compile_agent(monkeypatch, calls)

    for idx in range(2):
        await compile_flat_spec(
            project_dir=tmp_path,
            session_dir=tmp_path / "otto_logs" / "sessions" / f"s{idx}",
            intent="build an issue tracker",
            config={"spec_compile_no_cache": True},
        )

    assert len(calls) == 2


def test_compile_message_metrics_extracts_first_token_and_usage(tmp_path: Path) -> None:
    messages = tmp_path / "messages.jsonl"
    messages.write_text(
        "\n".join([
            json.dumps({"type": "phase_start", "phase": "spec", "ts": "2026-05-14T10:00:00Z"}),
            json.dumps({
                "type": "assistant",
                "ts": "2026-05-14T10:00:03Z",
                "blocks": [{"type": "text", "text": "hello"}],
                "usage": {"output_tokens": 3, "total_tokens": 3},
            }),
            json.dumps({
                "type": "phase_end",
                "phase": "spec",
                "duration_s": 4.0,
                "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    metrics = compile_message_metrics_from_jsonl(messages)

    assert metrics == {
        "first_token_ts": "2026-05-14T10:00:03Z",
        "total_tokens": 16,
        "output_tokens": 5,
    }
