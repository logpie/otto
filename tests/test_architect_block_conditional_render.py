"""Architect block in lead.md is conditionally stripped at render time.

Audit F-1 follow-up: lead.md has a ~245-line architect-only section. For
feature children (non-root, task_role != 'foundation'), that block is
~50% of the prompt budget spent on dead instructions. Stripping it at
render time keeps the source file intact (so file-grep tests still pass)
while sharpening the rendered prompt for feature Leads.
"""

from __future__ import annotations

from otto.lead import _strip_architect_block
from pathlib import Path


def _read_lead_template() -> str:
    return Path("otto/prompts/lead.md").read_text(encoding="utf-8")


def test_lead_md_has_architect_block_markers() -> None:
    template = _read_lead_template()
    assert "<!-- LEAD_ARCHITECT_BLOCK_START -->" in template
    assert "<!-- LEAD_ARCHITECT_BLOCK_END -->" in template
    assert template.index("<!-- LEAD_ARCHITECT_BLOCK_START -->") < template.index(
        "<!-- LEAD_ARCHITECT_BLOCK_END -->"
    )


def test_strip_removes_architect_block_content() -> None:
    template = _read_lead_template()
    stripped = _strip_architect_block(template)
    # The content between the markers (the architect-only section) is gone:
    assert "If you are the Architect / Foundation Lead" not in stripped
    # The Hard Rules section (BEFORE the architect block) still survives:
    assert "Hard Rules — read these first" in stripped
    # The Build Inline section (AFTER the architect block) still survives:
    assert "Build Inline (every Lead — feature or otherwise)" in stripped
    # A placeholder comment replaces the block so the markdown reads cleanly:
    assert "Architect / Foundation guidance omitted" in stripped


def test_strip_significantly_shrinks_prompt() -> None:
    template = _read_lead_template()
    stripped = _strip_architect_block(template)
    # Feature Lead prompts should be substantially smaller — the architect
    # block alone is ~10K chars; the strip should remove at least 8K to be
    # meaningful (gives a margin for the placeholder + minor restructure).
    delta = len(template) - len(stripped)
    assert delta >= 8000, f"strip removed only {delta} chars; expected >= 8000"


def test_strip_is_idempotent_when_markers_absent() -> None:
    """Falls back to identity on a template missing markers — degrades
    gracefully rather than producing a broken prompt."""
    template = "No markers here.\nJust prose.\n"
    assert _strip_architect_block(template) == template


def test_strip_preserves_hard_rules_intact() -> None:
    """The Hard Rules block (top of lead.md) survives stripping — those
    are the load-bearing invariants feature children MUST still read."""
    template = _read_lead_template()
    stripped = _strip_architect_block(template)
    # Specific Hard Rules clauses that bind on every Lead:
    assert "Write the verdict file" in stripped
    assert "Never `git add -A`" in stripped
    assert "feature_owned_paths" in stripped  # the stub anti-pattern rule
    assert "child_intent must include" in stripped.replace(
        "Every `submit_subtask(intent=...)` MUST tell the child its stack, its\n   `owned_paths` (or extension glob), what foundation contracts it imports,\n   and which paths are forbidden to it.", "child_intent must include"
    )  # rubric exists, content paraphrased
