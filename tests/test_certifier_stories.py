"""Tests for the certifier story-subset interface.

Validates:
- `_format_stories_section` rendering
- `{stories_section}` placeholder support across all certifier prompts
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otto.certifier import _format_stories_section, _render_certifier_prompt


# ---------- _format_stories_section ----------


def test_format_stories_section_empty_returns_empty():
    assert _format_stories_section(None) == ""
    assert _format_stories_section([]) == ""


def test_format_stories_section_renders_constraint_block():
    out = _format_stories_section([
        {"name": "csv export works", "description": "user can download CSV", "source_branch": "build/csv"},
    ])
    assert "Stories to Verify (REQUIRED)" in out
    assert "Run ONLY these stories" in out
    assert "csv export works" in out
    assert "user can download CSV" in out
    assert "build/csv" in out


def test_format_stories_section_handles_missing_fields():
    """Stories without source_branch or description still render."""
    out = _format_stories_section([
        {"name": "minimal story"},
    ])
    assert "Stories to Verify (REQUIRED)" in out
    assert "1. **minimal story**" in out


def test_format_stories_section_falls_back_to_summary_or_id():
    """When name is absent, fall back to summary or story_id."""
    out = _format_stories_section([
        {"summary": "from summary"},
        {"story_id": "from-id"},
    ])
    assert "from summary" in out
    assert "from-id" in out


def test_format_stories_section_numbers_stories():
    out = _format_stories_section([
        {"name": "first"},
        {"name": "second"},
    ])
    assert "1. **first**" in out
    assert "2. **second**" in out


# ---------- _render_certifier_prompt ----------


def test_render_includes_stories_section_when_provided(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="standard",
        intent="test",
        evidence_dir=tmp_path,
        stories=[{"name": "csv export"}],
    )
    assert "Stories to Verify" in out
    assert "csv export" in out


def test_render_omits_stories_section_when_none(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="standard",
        intent="test",
        evidence_dir=tmp_path,
        stories=None,
    )
    assert "Stories to Verify" not in out


def test_render_omits_stories_section_when_empty_list(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="standard",
        intent="test",
        evidence_dir=tmp_path,
        stories=[],
    )
    assert "Stories to Verify" not in out


@pytest.mark.parametrize("mode", ["standard", "fast", "thorough", "hillclimb", "target"])
def test_all_certifier_modes_accept_stories(tmp_path: Path, mode: str):
    """Every certifier mode supports the stories parameter; no rendering crash."""
    out = _render_certifier_prompt(
        mode=mode,
        intent="test product",
        evidence_dir=tmp_path,
        stories=[{"name": "story-a"}],
        target="latency < 100ms" if mode == "target" else None,
        focus="auth flow" if mode == "hillclimb" else None,
    )
    assert "story-a" in out, f"mode={mode}: stories_section not rendered"


# ---------- prompt placeholder support ----------


@pytest.mark.parametrize("prompt_file", [
    "certifier.md",
    "certifier-fast.md",
    "certifier-thorough.md",
    "certifier-hillclimb.md",
    "certifier-target.md",
])
def test_all_certifier_prompts_have_stories_placeholder(prompt_file: str):
    """Every certifier prompt has {stories_section} so subset cert works in all modes."""
    from otto.prompts import _PROMPTS_DIR
    content = (_PROMPTS_DIR / prompt_file).read_text()
    assert "{stories_section}" in content, \
        f"{prompt_file} missing {{stories_section}} placeholder"
