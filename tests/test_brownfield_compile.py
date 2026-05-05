"""Tests for `compile_spec(brownfield=True)` (A6.3).

Stubs `run_agent_with_timeout` to avoid LLM cost. Verifies that:
* the brownfield flag selects the brownfield prompt template
* the project preamble is interpolated into the rendered prompt
* the greenfield path is unchanged when brownfield=False
* `base_spec=...` (reserved for A6.4) emits a warning and is ignored
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from otto.spec_compile import (
    Feature,
    Group,
    Guardrail,
    Spec,
    compile_spec,
    spec_to_dict,
)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@otto.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Otto Tester"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        check=True,
    )


def _git_add_commit(path: Path, message: str = "snapshot") -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", message], check=True)


def _seed_python_project(project_dir: Path) -> None:
    _git_init(project_dir)
    (project_dir / "README.md").write_text(
        "# Sample CLI\n\nA tiny linter.\n"
    )
    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "sample-cli"\nversion = "0.1.0"\n'
    )
    (project_dir / "linter.py").write_text(
        "def main():\n    print('hello')\n"
    )
    _git_add_commit(project_dir)


def _agent_returns_spec(spec_dict: dict[str, object]) -> object:
    """Build an awaitable that mimics run_agent_with_timeout's signature
    and returns a tuple containing a fixed spec JSON."""
    body = json.dumps(spec_dict)
    text = f"<spec_json>{body}</spec_json>\nSPEC_PATH: anything\n"

    async def _stub(*_args: object, **_kwargs: object):
        return text, 0.0, "stub-session", {}

    return _stub


def _capturing_agent(captured: dict[str, object], spec_dict: dict[str, object]) -> object:
    """Wraps _agent_returns_spec but stores the rendered prompt in
    `captured` so tests can assert on its content."""
    body = json.dumps(spec_dict)
    text = f"<spec_json>{body}</spec_json>\nSPEC_PATH: anything\n"

    async def _stub(prompt, *_args: object, **_kwargs: object):
        captured["prompt"] = prompt
        return text, 0.0, "stub-session", {}

    return _stub


def _minimal_spec_dict() -> dict[str, object]:
    spec = Spec(
        intent="document this CLI tool",
        project_kind="cli",
        groups=[Group(id="lint", name="Lint")],
        features=[
            Feature(
                id="lint-main",
                name="Lint subcommand",
                description="Runs the lint pass.",
                evidence_kinds=["CLIProbe"],
                group_id="lint",
            ),
        ],
    )
    return spec_to_dict(spec)


def _minimal_config() -> dict[str, object]:
    return {
        "_intent_source": "cli-argument",
        "_intent_fallback_reason": "",
        "_spec_source": "compile-agent",
    }


# ---------------------------------------------------------------------------
# Brownfield branch reaches the brownfield prompt + preamble
# ---------------------------------------------------------------------------


def test_brownfield_compile_uses_brownfield_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "otto.agent.run_agent_with_timeout",
        _capturing_agent(captured, _minimal_spec_dict()),
    )
    # Avoid touching the real make_agent_options machinery
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )
    monkeypatch.setattr("otto.config.get_spec_timeout", lambda _c: 30)
    monkeypatch.setattr(
        "otto.observability.save_rendered_prompt",
        lambda *_a, **_kw: {"sha256": "x", "path": "x"},
    )
    monkeypatch.setattr(
        "otto.observability.update_input_provenance",
        lambda *_a, **_kw: None,
    )

    spec = asyncio.run(
        compile_spec(
            "document this CLI tool",
            project,
            run_dir,
            _minimal_config(),
            project_kind="cli",
            brownfield=True,
        )
    )
    assert spec.project_kind == "cli"
    assert any(f.id == "lint-main" for f in spec.features)

    rendered = captured["prompt"]
    assert isinstance(rendered, str)
    # Brownfield-mode marker (from prompt body)
    assert "brownfield mode" in rendered
    # Preamble file names interpolated
    assert "linter.py" in rendered
    assert "pyproject.toml" in rendered
    # Anti-derivation guidance present
    assert "scope hint" in rendered.lower() or "scope hint" in rendered


def test_greenfield_compile_unchanged(tmp_path: Path, monkeypatch) -> None:
    """compile_spec(brownfield=False) does NOT render the preamble or
    the brownfield-mode template — greenfield path is untouched."""
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "otto.agent.run_agent_with_timeout",
        _capturing_agent(captured, _minimal_spec_dict()),
    )
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )
    monkeypatch.setattr("otto.config.get_spec_timeout", lambda _c: 30)
    monkeypatch.setattr(
        "otto.observability.save_rendered_prompt",
        lambda *_a, **_kw: {"sha256": "x", "path": "x"},
    )
    monkeypatch.setattr(
        "otto.observability.update_input_provenance",
        lambda *_a, **_kw: None,
    )

    asyncio.run(
        compile_spec(
            "build me a tiny CLI",
            project,
            run_dir,
            _minimal_config(),
            project_kind="cli",
            brownfield=False,  # explicit
        )
    )
    rendered = captured["prompt"]
    assert isinstance(rendered, str)
    # Brownfield-mode marker absent
    assert "brownfield mode" not in rendered
    # File preamble NOT inlined (linter.py path comes from preamble; should not appear)
    assert "linter.py" not in rendered


# ---------------------------------------------------------------------------
# A6.4 — Additive-mode reconciliation
# ---------------------------------------------------------------------------


def _stub_compile_internals(monkeypatch, captured: dict[str, object],
                             spec_dict: dict[str, object]) -> None:
    """Stub everything needed to run compile_spec without LLM cost."""
    monkeypatch.setattr(
        "otto.agent.run_agent_with_timeout",
        _capturing_agent(captured, spec_dict),
    )
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )
    monkeypatch.setattr("otto.config.get_spec_timeout", lambda _c: 30)
    monkeypatch.setattr(
        "otto.observability.save_rendered_prompt",
        lambda *_a, **_kw: {"sha256": "x", "path": "x"},
    )
    monkeypatch.setattr(
        "otto.observability.update_input_provenance",
        lambda *_a, **_kw: None,
    )


def test_reconcile_carries_forward_unchanged_groups(
    tmp_path: Path, monkeypatch
) -> None:
    """Base group ids the agent doesn't re-emit are preserved."""
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    base = Spec(
        intent="document this CLI tool",
        project_kind="cli",
        groups=[
            Group(id="lint", name="Lint", owned_paths=["lint/**"]),
            Group(id="format", name="Format", owned_paths=["fmt/**"]),
        ],
    )
    base.intent_hash = "deadbeef"

    # Agent emits only the "lint" group (with new info)
    new_dict = spec_to_dict(
        Spec(
            intent="document this CLI tool",
            project_kind="cli",
            groups=[Group(id="lint", name="Lint (refined)")],
        )
    )
    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, new_dict)

    spec = asyncio.run(
        compile_spec(
            "x", project, run_dir, _minimal_config(),
            project_kind="cli", brownfield=True, base_spec=base,
        )
    )
    group_ids = [g.id for g in spec.groups]
    assert "lint" in group_ids and "format" in group_ids
    # Agent's view wins on title for re-emitted group
    lint = next(g for g in spec.groups if g.id == "lint")
    assert lint.name == "Lint (refined)"
    # Base group untouched
    fmt = next(g for g in spec.groups if g.id == "format")
    assert fmt.owned_paths == ["fmt/**"]
    # intent + intent_hash from base
    assert spec.intent == "document this CLI tool"
    assert spec.intent_hash == "deadbeef"


def test_reconcile_preserves_feature_audit_state(
    tmp_path: Path, monkeypatch
) -> None:
    """Per-Feature verdict + coverage state from base survive on
    matching ids (the agent doesn't author these)."""
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    base_feature = Feature(
        id="lint-main",
        name="Lint subcommand (old name)",
        description="old desc",
        evidence_kinds=["CLIProbe"],
        group_id="lint",
        verdict="passed",
        evidence_completeness="proxy_only",
        coverage_confidence="medium",
        multi_actor_required=True,
        audit_pre_merge=True,
    )
    base = Spec(
        intent="x",
        project_kind="cli",
        groups=[Group(id="lint", name="Lint")],
        features=[base_feature],
    )
    base.intent_hash = "h"

    # Agent emits an updated NAME and DESCRIPTION but no audit state.
    new_dict = spec_to_dict(
        Spec(
            intent="x",
            project_kind="cli",
            groups=[Group(id="lint", name="Lint")],
            features=[
                Feature(
                    id="lint-main",
                    name="Lint subcommand (renamed)",
                    description="fresh description",
                    evidence_kinds=["CLIProbe", "RepoTestCheck"],
                    group_id="lint",
                ),
            ],
        )
    )
    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, new_dict)

    spec = asyncio.run(
        compile_spec(
            "x", project, run_dir, _minimal_config(),
            project_kind="cli", brownfield=True, base_spec=base,
        )
    )
    feat = next(f for f in spec.features if f.id == "lint-main")
    # Author fields from agent
    assert feat.name == "Lint subcommand (renamed)"
    assert feat.description == "fresh description"
    assert feat.evidence_kinds == ["CLIProbe", "RepoTestCheck"]
    # Audit/coverage state preserved from base
    assert feat.verdict == "passed"
    assert feat.evidence_completeness == "proxy_only"
    assert feat.coverage_confidence == "medium"
    assert feat.multi_actor_required is True
    assert feat.audit_pre_merge is True


def test_reconcile_appends_new_features(tmp_path: Path, monkeypatch) -> None:
    """Agent-emitted Feature ids not in base are appended."""
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    base = Spec(
        intent="x", project_kind="cli",
        groups=[Group(id="lint", name="Lint")],
        features=[
            Feature(id="lint-main", name="Lint", group_id="lint"),
        ],
    )
    base.intent_hash = "h"

    new_dict = spec_to_dict(
        Spec(
            intent="x", project_kind="cli",
            groups=[Group(id="lint", name="Lint")],
            features=[
                Feature(id="lint-main", name="Lint", group_id="lint"),
                Feature(id="format-main", name="Format", group_id="lint"),
            ],
        )
    )
    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, new_dict)

    spec = asyncio.run(
        compile_spec(
            "x", project, run_dir, _minimal_config(),
            project_kind="cli", brownfield=True, base_spec=base,
        )
    )
    feature_ids = sorted(f.id for f in spec.features)
    assert feature_ids == ["format-main", "lint-main"]


def test_reconcile_warns_on_conflicting_group_title(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    base = Spec(
        intent="x", project_kind="cli",
        groups=[Group(id="lint", name="Lint")],
    )
    base.intent_hash = "h"

    new_dict = spec_to_dict(
        Spec(
            intent="x", project_kind="cli",
            groups=[Group(id="lint", name="Lint completely renamed")],
        )
    )
    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, new_dict)

    with caplog.at_level(logging.WARNING, logger="otto.spec_compile"):
        spec = asyncio.run(
            compile_spec(
                "x", project, run_dir, _minimal_config(),
                project_kind="cli", brownfield=True, base_spec=base,
            )
        )
    # Warning surfaced (visible to operators)
    assert any(
        "title changed" in r.message and "lint" in r.message
        for r in caplog.records
    )
    # Agent's title wins
    assert spec.groups[0].name == "Lint completely renamed"


def test_reconcile_dedupes_guardrails_by_text(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    _seed_python_project(project)
    run_dir = tmp_path / "session" / "spec"

    base = Spec(
        intent="x", project_kind="cli",
        guardrails=[
            Guardrail(id="g1", text="No video upload"),
            Guardrail(id="g2", text="No external CDN"),
        ],
    )
    base.intent_hash = "h"

    new_dict = spec_to_dict(
        Spec(
            intent="x", project_kind="cli",
            guardrails=[
                Guardrail(id="new1", text="No video upload"),  # dup-by-text
                Guardrail(id="new2", text="No telemetry"),     # new
            ],
        )
    )
    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, new_dict)

    spec = asyncio.run(
        compile_spec(
            "x", project, run_dir, _minimal_config(),
            project_kind="cli", brownfield=True, base_spec=base,
        )
    )
    texts = [g.text for g in spec.guardrails]
    # Three unique texts; "No video upload" appears once
    assert sorted(set(texts)) == sorted(texts)
    assert sorted(texts) == sorted([
        "No video upload",
        "No external CDN",
        "No telemetry",
    ])


# ---------------------------------------------------------------------------
# Determinism: same input → same persisted spec.json
# ---------------------------------------------------------------------------


def test_brownfield_compile_is_deterministic(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    _seed_python_project(project)

    monkeypatch.setattr(
        "otto.agent.run_agent_with_timeout",
        _agent_returns_spec(_minimal_spec_dict()),
    )
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )
    monkeypatch.setattr("otto.config.get_spec_timeout", lambda _c: 30)
    monkeypatch.setattr(
        "otto.observability.save_rendered_prompt",
        lambda *_a, **_kw: {"sha256": "x", "path": "x"},
    )
    monkeypatch.setattr(
        "otto.observability.update_input_provenance",
        lambda *_a, **_kw: None,
    )

    run_dir_a = tmp_path / "sess-a" / "spec"
    run_dir_b = tmp_path / "sess-b" / "spec"
    asyncio.run(
        compile_spec(
            "x", project, run_dir_a, _minimal_config(),
            project_kind="cli", brownfield=True,
        )
    )
    asyncio.run(
        compile_spec(
            "x", project, run_dir_b, _minimal_config(),
            project_kind="cli", brownfield=True,
        )
    )
    a = (run_dir_a / "spec.json").read_text()
    b = (run_dir_b / "spec.json").read_text()
    assert a == b
