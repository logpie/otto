"""Tests for the certifier story-subset interface.

Validates:
- `_format_stories_section` rendering
- `{stories_section}` placeholder support across all certifier prompts
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otto.certifier import (
    _format_stories_section,
    _generate_agentic_html_pow,
    _render_certifier_prompt,
    _render_pow_markdown,
)


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


# ---------- merge_context preamble for post-merge cert pruning ----------


def test_merge_context_preamble_renders_when_provided(tmp_path: Path):
    """When stories carry a merge_context, the rendered prompt includes the
    skip/flag instructions and the merge-diff file list. This is what
    lets the cert agent prune inline during post-merge verification."""
    out = _render_certifier_prompt(
        mode="standard",
        intent="bookmark manager",
        evidence_dir=tmp_path,
        stories=[
            {"name": "csv export works", "source_branch": "build/csv"},
            {"name": "settings page renders", "source_branch": "build/settings"},
        ],
        merge_context={
            "target": "main",
            "diff_files": ["app/csv.py", "app/utils.py"],
            "allow_skip": True,
        },
    )
    # Preamble appears
    assert "Merge Verification Context" in out
    assert "multi-branch merge into `main`" in out
    # Diff file list rendered
    assert "`app/csv.py`" in out
    assert "`app/utils.py`" in out
    # Verdict tokens documented in the prompt
    assert "SKIPPED" in out
    assert "FLAG_FOR_HUMAN" in out
    assert "When in doubt, test it." in out
    # Stories still rendered after the preamble
    assert "csv export works" in out
    assert "settings page renders" in out


def test_merge_context_with_full_verify_suppresses_skip_but_keeps_flag(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="standard",
        intent="bookmark manager",
        evidence_dir=tmp_path,
        stories=[{"name": "csv export works"}],
        merge_context={
            "target": "main",
            "diff_files": ["app/csv.py"],
            "allow_skip": False,
        },
    )
    assert "Merge Verification Context" in out
    assert "SKIPPED" not in out
    assert "FLAG_FOR_HUMAN" in out
    assert "Test every story below; do not skip on file overlap." in out


def test_merge_context_omitted_when_none(tmp_path: Path):
    """No merge_context (e.g. otto certify, otto build's certify phase) →
    no preamble, the prompt is the standard stories-only form."""
    out = _render_certifier_prompt(
        mode="standard",
        intent="test",
        evidence_dir=tmp_path,
        stories=[{"name": "story-a"}],
        merge_context=None,
    )
    assert "Merge Verification Context" not in out
    assert "story-a" in out


def test_merge_context_with_no_diff_files_renders_safely(tmp_path: Path):
    """Empty diff list shouldn't crash the renderer — degenerate but
    possible (e.g. clean merge that auto-resolved everything via gitattrs)."""
    out = _render_certifier_prompt(
        mode="standard",
        intent="test",
        evidence_dir=tmp_path,
        stories=[{"name": "story-a"}],
        merge_context={"target": "main", "diff_files": [], "allow_skip": True},
    )
    assert "Merge Verification Context" in out
    assert "no files in merge diff" in out


# ---------- marker parser: new SKIPPED / FLAG_FOR_HUMAN verdicts ----------


def test_parse_story_result_recognizes_skipped_verdict():
    """SKIPPED is the cert agent's signal that a story's feature wasn't
    touched by the merge diff. Must NOT count as `passed=True` (it wasn't
    tested) but also NOT trip the "has_failures" check."""
    from otto.markers import parse_certifier_markers
    text = (
        "STORY_RESULT: csv-export | SKIPPED | no overlap with merge diff\n"
        "STORY_RESULT: settings    | PASS    | renders correctly\n"
        "VERDICT: PASS\n"
    )
    parsed = parse_certifier_markers(text)
    by_id = {s["story_id"]: s for s in parsed.stories}
    assert by_id["csv-export"]["verdict"] == "SKIPPED"
    assert by_id["csv-export"]["passed"] is False
    assert by_id["settings"]["verdict"] == "PASS"
    assert by_id["settings"]["passed"] is True


def test_parse_story_result_recognizes_flag_for_human_verdict():
    """FLAG_FOR_HUMAN is for genuine cross-branch contradictions."""
    from otto.markers import parse_certifier_markers
    text = (
        "STORY_RESULT: dark-mode | FLAG_FOR_HUMAN | branch B deleted the settings page\n"
        "VERDICT: PASS\n"
    )
    parsed = parse_certifier_markers(text)
    s = parsed.stories[0]
    assert s["verdict"] == "FLAG_FOR_HUMAN"
    assert s["passed"] is False
    assert "deleted the settings page" in s["summary"]


def test_skipped_and_flagged_dont_count_as_failures():
    """The cert outcome should remain PASSED if all non-PASS verdicts are
    SKIPPED or FLAG_FOR_HUMAN (only an explicit FAIL flips the outcome)."""
    from otto.markers import parse_certifier_markers
    text = (
        "STORY_RESULT: a | PASS           | works\n"
        "STORY_RESULT: b | SKIPPED        | no overlap\n"
        "STORY_RESULT: c | FLAG_FOR_HUMAN | contradicted by branch X\n"
        "VERDICT: PASS\n"
    )
    parsed = parse_certifier_markers(text)
    has_failures = any(s.get("verdict", "FAIL") == "FAIL" for s in parsed.stories)
    assert not has_failures, "SKIPPED/FLAG must not trip has_failures"


def test_explicit_fail_still_counts_as_failure():
    """Sanity: FAIL stays FAIL — we didn't accidentally swallow real failures."""
    from otto.markers import parse_certifier_markers
    text = (
        "STORY_RESULT: a | PASS    | works\n"
        "STORY_RESULT: b | FAIL    | crashed on submit\n"
        "STORY_RESULT: c | SKIPPED | no overlap\n"
        "VERDICT: FAIL\n"
    )
    parsed = parse_certifier_markers(text)
    has_failures = any(s.get("verdict", "FAIL") == "FAIL" for s in parsed.stories)
    assert has_failures


def test_pow_rendering_distinguishes_all_story_verdicts(tmp_path: Path):
    story_results = [
        {"story_id": "story-pass", "summary": "pass summary", "verdict": "PASS", "passed": True},
        {"story_id": "story-fail", "summary": "fail summary", "verdict": "FAIL", "passed": False},
        {"story_id": "story-skip", "summary": "skip summary", "verdict": "SKIPPED", "passed": False},
        {
            "story_id": "story-flag",
            "summary": "flag summary",
            "verdict": "FLAG_FOR_HUMAN",
            "passed": False,
        },
    ]

    markdown = _render_pow_markdown(
        story_results,
        outcome="passed",
        duration=12.0,
        cost=0.34,
        stories_passed=1,
        stories_tested=4,
    )
    assert "✓ PASS" in markdown
    assert "✗ FAIL" in markdown
    assert "– SKIPPED" in markdown
    assert "⚠ FLAG_FOR_HUMAN" in markdown

    _generate_agentic_html_pow(
        tmp_path,
        story_results,
        "passed",
        12.0,
        0.34,
        1,
        4,
    )
    html = (tmp_path / "proof-of-work.html").read_text()
    assert "✓ PASS" in html
    assert "✗ FAIL" in html
    assert "– SKIPPED" in html
    assert "⚠ FLAG_FOR_HUMAN" in html


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
