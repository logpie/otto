"""lead.md + lead-architect.md compose conditionally by Lead role.

Audit F-1 follow-up. lead.md (~225 lines) is the universal Lead prompt —
Hard Rules, Decide, Build Inline, Verdict, Tools — that every Lead reads.
lead-architect.md (~250 lines) is appended ONLY for the root Lead and
foundation children, who actually design the scaffold and author
CHARTER's feature_owned_paths / foundation_contracts.

The split keeps the file structure aligned with audience: a reader of
lead.md sees what every Lead sees, no runtime stripping, no markers.
"""

from __future__ import annotations

from otto.lead import _compose_lead_template
from pathlib import Path


def _read(name: str) -> str:
    return Path("otto/prompts") / name


def test_lead_md_is_core_only() -> None:
    """lead.md should NOT contain architect-only content. That lives in
    lead-architect.md and is appended only when needed."""
    text = (Path("otto/prompts") / "lead.md").read_text(encoding="utf-8")
    # Architect-specific phrases must not be in the core:
    assert "If you are the Architect / Foundation Lead" not in text
    assert "feature_owned_paths" in text  # the Hard Rule mention IS here
    # But the deep architect-block content must NOT be here:
    assert "Foundation Contracts" not in text  # CHARTER block heading
    assert "registration_isolation" not in text  # architect-only contract field


def test_lead_architect_md_exists_and_is_architect_only() -> None:
    arch = (Path("otto/prompts") / "lead-architect.md").read_text(encoding="utf-8")
    assert "If you are the Architect / Foundation Lead" in arch
    assert "Foundation Contracts" in arch
    assert "registration_isolation" in arch
    assert "Information Architecture Contract" in arch


def test_compose_feature_lead_excludes_architect() -> None:
    """Feature children get just lead.md."""
    composed = _compose_lead_template(include_architect=False)
    assert "If you are the Architect / Foundation Lead" not in composed
    # But Hard Rules still survive:
    assert "Hard Rules" in composed
    assert "Write the verdict file" in composed
    assert "Never `git add -A`" in composed
    # And the stub anti-pattern Hard Rule is preserved:
    assert "Foundation does NOT seed feature-owned files" in composed


def test_compose_architect_lead_includes_both() -> None:
    """Root + foundation Leads get lead.md + lead-architect.md concatenated."""
    composed = _compose_lead_template(include_architect=True)
    # Core content:
    assert "Hard Rules" in composed
    assert "Build Inline" in composed
    # Architect content:
    assert "If you are the Architect / Foundation Lead" in composed
    assert "Foundation Contracts" in composed
    assert "registration_isolation" in composed


def test_feature_lead_prompt_is_substantially_smaller() -> None:
    """Feature children's prompt should be well under half the architect's,
    so instruction-following actually holds on the rules they need."""
    core = _compose_lead_template(include_architect=False)
    full = _compose_lead_template(include_architect=True)
    # The split should give feature children at least 50% reduction:
    assert len(core) < len(full) * 0.5, (
        f"feature prompt is {len(core)} chars, architect is {len(full)}; "
        f"feature should be < {int(len(full) * 0.5)}"
    )
