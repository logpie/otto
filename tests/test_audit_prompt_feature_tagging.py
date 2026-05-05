"""Phase A2.3 — Audit prompt MUST embed the Feature-tagging contract.

The audit agent emits `audit/attempt-NN/walkthrough.jsonl` and per-Feature
verdicts derived from the tagged walkthrough actions (research §A2 + §4).
The prompt is the contract surface — it must teach the agent:

* every walkthrough action carries `feature_ids[]`
* untagged actions outside `action_kind: "exploration"` are rejected
* ≥90% non-exploration tagging coverage is required
* per-project-kind worked examples (webapp, api, library, cli)

These tests pin the contract markers in the rendered prompt so future
edits don't silently drop the requirement.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otto.audit import (
    AuditAgentInput,
    AuditAgentOutput,
    AuditVerdict,
    _audit_prompt,
    default_audit_agent,
)
from otto.spec_compile import Group, Spec, StructureDecisions


def _make_input(tmp_path: Path, *, kind: str = "webapp") -> AuditAgentInput:
    spec = Spec(
        intent="ship something",
        project_kind=kind,
        structure=StructureDecisions(payload={}),
        groups=[
            Group(id="s1", name="Group One", dependencies=[], owned_paths=[],
                  feature_ids=[], checks=[]),
        ],
    )
    return AuditAgentInput(
        spec=spec,
        project_dir=tmp_path,
        integrated_worktree=tmp_path,
        build_summary={},
        merge_summary={},
        cross_slice_evidence=[],
        walkthrough_artifacts=[],
    )


def test_prompt_contains_feature_tagging_contract_markers(tmp_path: Path) -> None:
    """Render the audit prompt and verify it carries the contract."""
    prompt = _audit_prompt(_make_input(tmp_path))

    # NON-NEGOTIABLE contract phrases (research §A2 + §4).
    assert "feature_ids" in prompt, (
        "audit prompt must teach the agent to populate feature_ids[]"
    )
    assert "action_kind" in prompt, (
        "audit prompt must teach the agent the action_kind discriminator"
    )
    assert "exploration" in prompt, (
        "audit prompt must whitelist `exploration` for untagged actions"
    )
    # ≥90% threshold — research §A2 hard rule. The contract markdown
    # spells it `≥90%`; accept either Unicode or ASCII fallback.
    assert ("≥90%" in prompt) or (">=90%" in prompt), (
        "audit prompt must state the ≥90% non-exploration coverage rule"
    )
    # Walkthrough artifact filename — agent must know where to write.
    assert "walkthrough.jsonl" in prompt
    # Feature verdicts artifact — agent must produce per-Feature output.
    assert "feature-verdicts.json" in prompt


def test_prompt_contains_per_project_kind_examples(tmp_path: Path) -> None:
    """The contract embeds worked JSONL examples for each project_kind.

    Without these the agent has no template to imitate; the contract
    becomes abstract advice. These markers MUST appear regardless of the
    spec's actual project_kind because the agent should learn the shape
    for ANY product, not just the current one.
    """
    prompt = _audit_prompt(_make_input(tmp_path, kind="library"))

    # Section headers from audit-feature-tagging.md.
    for kind in ("webapp", "api", "library", "cli"):
        assert kind in prompt, f"missing {kind!r} example in audit prompt"

    # action_kind enum members the agent must choose from.
    for action in (
        "browser_navigation",
        "api_request",
        "cli_invoke",
        "import_check",
        "type_check",
    ):
        assert action in prompt, f"missing {action!r} action_kind in prompt"


def test_prompt_uses_feature_audits_wire_format(tmp_path: Path) -> None:
    """A0.4 cutover: the prompt MUST ask the agent for `feature_audits`
    (the canonical wire key). The legacy `capability_verdicts` mention
    was removed once back-compat was dropped.
    """
    prompt = _audit_prompt(_make_input(tmp_path))
    # Canonical key requested in the JSON schema block.
    assert "feature_audits:" in prompt
    assert "feature_id" in prompt
    # Legacy key must be entirely gone from the prompt.
    assert "capability_verdicts" not in prompt
    assert "Per-capability" not in prompt


def test_prompt_contract_block_unconditional_on_walkthrough_artifacts(
    tmp_path: Path,
) -> None:
    """The contract must render even when no walkthrough artifacts have
    been pre-generated — the agent itself produces walkthrough.jsonl, so
    the contract is upstream of any artifact list.
    """
    inp = _make_input(tmp_path)
    assert inp.walkthrough_artifacts == []
    prompt = _audit_prompt(inp)
    assert "Walkthrough Feature-tagging contract" in prompt
    assert "feature_ids" in prompt


def test_default_audit_agent_renders_feature_tagging_prompt(tmp_path: Path) -> None:
    """Stub-agent integration: spy on the prompt the default agent would
    send. We don't actually call the LLM — we patch the runner to
    capture its prompt argument and assert the contract is present.
    """
    inp = _make_input(tmp_path)

    captured: dict[str, str] = {}

    async def fake_runner(prompt, options, *, log_dir, phase_name,
                          phase_label, timeout, project_dir):
        captured["prompt"] = prompt
        # Return a minimal valid audit-agent JSON envelope so the parser
        # doesn't crash.
        body = (
            "```json\n"
            "{\"verdict\": \"passed\", \"narrative\": \"stub\", "
            "\"group_verdicts\": [], \"feature_audits\": [], "
            "\"quality_score\": 3, \"quality_findings\": "
            "[\"a\", \"b\"]}\n"
            "```"
        )
        return body, 0.0, "session-stub", {}

    import otto.agent as agent_mod

    original = agent_mod.run_agent_with_timeout
    agent_mod.run_agent_with_timeout = fake_runner  # type: ignore[assignment]
    try:
        out = asyncio.run(default_audit_agent(inp))
    finally:
        agent_mod.run_agent_with_timeout = original  # type: ignore[assignment]

    assert isinstance(out, AuditAgentOutput)
    assert out.verdict == AuditVerdict.PASSED

    prompt = captured.get("prompt", "")
    assert prompt, "default_audit_agent did not invoke run_agent_with_timeout"
    # Contract markers reach the agent through the default pipeline.
    assert "feature_ids" in prompt
    assert "action_kind" in prompt
    assert ("≥90%" in prompt) or (">=90%" in prompt)
    assert "walkthrough.jsonl" in prompt
    # Per-kind examples reach the agent too.
    for kind in ("webapp", "api", "library", "cli"):
        assert kind in prompt


def test_default_audit_agent_uses_input_config_for_provider_options(
    tmp_path: Path, monkeypatch
) -> None:
    """CLI/runtime provider overrides must reach spawned audit agents."""
    inp = _make_input(tmp_path, kind="library")
    inp.config = {"provider": "codex", "_cli_overrides": {"provider": "codex"}}

    captured: dict[str, object] = {}

    def fake_make_options(_project_dir, config, *, agent_type, **_kwargs):
        from otto.agent import AgentOptions

        captured["config"] = dict(config)
        captured["agent_type"] = agent_type
        return AgentOptions()

    async def fake_runner(prompt, options, *, log_dir, phase_name,
                          phase_label, timeout, project_dir):
        body = (
            "```json\n"
            "{\"verdict\": \"passed\", \"narrative\": \"stub\", "
            "\"group_verdicts\": [], \"feature_audits\": [], "
            "\"quality_score\": 3, \"quality_findings\": []}\n"
            "```"
        )
        return body, 0.0, "session-stub", {}

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_runner)

    out = asyncio.run(default_audit_agent(inp))

    assert out.verdict == AuditVerdict.PASSED
    assert captured["agent_type"] == "certifier"
    assert captured["config"] == {
        "provider": "codex",
        "_cli_overrides": {"provider": "codex"},
    }
