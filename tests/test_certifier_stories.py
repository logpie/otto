"""Tests for the certifier story-subset interface.

Validates:
- `_format_stories_section` rendering
- `{stories_section}` placeholder support across all certifier prompts
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import otto.certifier as certifier_module
import pytest

from otto.certifier import (
    _format_stories_section,
    _render_certifier_prompt,
    _render_pow_markdown,
    _write_certifier_verification_plan,
)
from tests._helpers import write_test_pow_report


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


@pytest.mark.parametrize("mode", ["standard", "fast", "thorough", "hillclimb", "target"])
def test_all_certifier_modes_are_read_only(tmp_path: Path, mode: str):
    """Split-mode certifiers must evaluate only; implementation belongs to fix/improve."""
    out = _render_certifier_prompt(
        mode=mode,
        intent="test product",
        evidence_dir=tmp_path,
        target="latency < 100ms" if mode == "target" else None,
    )
    assert "Read-only boundary" in out
    assert "Do NOT edit" in out
    assert "Otto's" in out and ("fix phase" in out or "improver phase" in out)
    assert "Repository hygiene" in out
    assert "git status --short" in out
    assert "__pycache__" in out
    assert "Never delete tracked or pre-existing user files" in out


@pytest.mark.parametrize("mode", ["standard", "fast", "thorough", "hillclimb", "target"])
def test_all_certifier_modes_require_server_cleanup(tmp_path: Path, mode: str):
    out = _render_certifier_prompt(
        mode=mode,
        intent="test web product",
        evidence_dir=tmp_path,
        target="latency < 100ms" if mode == "target" else None,
        focus="review product usability" if mode == "hillclimb" else None,
    )
    assert "App/server process lifecycle" in out
    assert "you own cleanup" in out
    assert "verify the port is closed" in out
    assert "Never kill pre-existing user" in out and "processes" in out


def test_certifier_cleanup_terminates_new_project_dev_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_server_pid = 200
    other_server_pid = 300
    alive = {project_server_pid}
    kill_calls: list[tuple[int, int]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"p100\np{project_server_pid}\np{other_server_pid}\n",
                stderr="",
            )
        if args[:3] == ["ps", "-o", "command="]:
            pid = int(args[-1])
            command = {
                project_server_pid: f"{project_dir}/.venv/bin/python .venv/bin/flask --app app run --port 5199",
                other_server_pid: "/tmp/other/.venv/bin/python -m http.server 8000",
            }[pid]
            return subprocess.CompletedProcess(args, 0, stdout=f"{command}\n", stderr="")
        if args[:2] == ["lsof", "-a"] and "-d" in args and "cwd" in args:
            pid = int(args[args.index("-p") + 1])
            cwd = {
                project_server_pid: project_dir,
                other_server_pid: Path("/tmp/other"),
            }[pid]
            return subprocess.CompletedProcess(args, 0, stdout=f"p{pid}\nn{cwd}\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        kill_calls.append((pid, sig))
        if pid == project_server_pid and sig == signal.SIGTERM:
            alive.discard(pid)
            return
        raise AssertionError(f"unexpected kill: pid={pid} sig={sig}")

    monkeypatch.setattr(certifier_module.subprocess, "run", fake_run)
    monkeypatch.setattr(certifier_module.os, "kill", fake_kill)

    cleaned = certifier_module._cleanup_certifier_background_servers(project_dir, {100})

    assert cleaned == [
        {
            "pid": project_server_pid,
            "command": f"{project_dir}/.venv/bin/python .venv/bin/flask --app app run --port 5199",
            "cwd": str(project_dir),
        }
    ]
    assert kill_calls == [(project_server_pid, signal.SIGTERM)]


@pytest.mark.parametrize("mode", ["standard", "thorough"])
def test_bug_certifier_modes_require_reproducible_failures(tmp_path: Path, mode: str):
    """Bug certifiers should not turn hypothetical or coverage-only gaps into fake bugs."""
    out = _render_certifier_prompt(
        mode=mode,
        intent="test product",
        evidence_dir=tmp_path,
    )
    assert "reproducible" in out
    assert "WARN" in out
    assert "missing regression tests" in out or "weak coverage" in out
    assert "already" in out and "PASS" in out


@pytest.mark.parametrize("mode", ["fast", "standard", "thorough"])
def test_certifier_scopes_test_only_intents_to_test_coverage(tmp_path: Path, mode: str):
    """Adding tests should not cause the certifier to re-certify the feature matrix."""
    out = _render_certifier_prompt(
        mode=mode,
        intent="Add a PDF export smoke test.",
        evidence_dir=tmp_path,
    )
    assert "test-only work" in out
    assert "Do NOT re-certify the referenced" in out
    assert "relevant test command" in out
    assert (
        "full feature matrix" in out
        or "full product bug hunt" in out
        or "full product matrix" in out
    )


@pytest.mark.parametrize("mode", ["standard", "thorough"])
def test_product_certification_prompt_does_not_downgrade_existing_feature_to_test_only(tmp_path: Path, mode: str):
    out = _render_certifier_prompt(
        mode=mode,
        intent="Certify the existing PDF export feature as a user-visible product flow.",
        evidence_dir=tmp_path,
    )
    assert "If the operator explicitly asks to certify an existing feature" in out
    assert "Do NOT" in out and "downgrade it to test-only work" in out


@pytest.mark.parametrize("mode", ["standard", "thorough"])
def test_certifier_prompt_uses_documented_agent_browser_recording_workflow(tmp_path: Path, mode: str):
    out = _render_certifier_prompt(
        mode=mode,
        intent="Certify a web app download flow.",
        evidence_dir=tmp_path,
    )
    assert "agent-browser --session visual open http://localhost:PORT" in out
    assert "agent-browser --session visual record start" in out
    assert "recording.webm http://localhost:PORT" not in out
    assert "recording.webm" in out
    assert "contextual" in out and "walkthrough evidence" in out
    assert "story-mapped video" in out and "proof" in out


def test_hillclimb_defaults_to_agent_browser_for_web_products(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="hillclimb",
        intent="test web product",
        evidence_dir=tmp_path,
    )
    assert "default to `agent-browser`" in out
    assert "Use scripted Playwright only when" in out
    assert "1-3 highest-impact improvements" in out


def test_hillclimb_keeps_scoped_improvement_stable_across_rounds(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="hillclimb",
        intent="test web product",
        evidence_dir=tmp_path,
        focus="choose one small high-impact improvement",
    )
    assert "Keep scope stable across rounds" in out
    assert "emit one primary" in out
    assert "Do not introduce a new `FAIL` in a later round" in out
    assert "reported as `WARN`, not blockers" in out
    assert "story IDs stable between rounds" in out


def test_improve_prompt_discourages_test_only_or_speculative_churn():
    from otto.prompts import render_prompt

    out = render_prompt(
        "improve.md",
        session_dir="/tmp/session",
        max_certify_rounds="2",
    )
    assert "Fix the product issue the certifier actually proved" in out
    assert "only to satisfy a narrow test" in out
    assert "already works" in out
    assert "clear user value" in out


# ---------- merge_context preamble for post-merge cert pruning ----------


def test_merge_context_renders_dedicated_merge_prompt_when_provided(tmp_path: Path):
    """Merge cert uses the dedicated prompt and keeps scope in the plan."""
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
    assert "You are certifying an integrated merge before it lands" in out
    assert "Merge Verification Plan" in out
    assert "`app/csv.py`" in out
    assert "`app/utils.py`" in out
    assert "SKIPPED" in out
    assert "FLAG_FOR_HUMAN" in out
    assert "A prior task's proof-of-work can justify `SKIPPED`" in out
    assert "Merge Verification Context" not in out
    # Stories still rendered after the preamble
    assert "csv export works" in out
    assert "settings page renders" in out


def test_merge_context_uses_merge_specific_certifier_prompt(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="standard",
        intent="bookmark manager",
        evidence_dir=tmp_path,
        stories=[{"name": "csv export works", "source_branch": "build/csv"}],
        merge_context={
            "target": "main",
            "diff_files": ["app/csv.py"],
            "allow_skip": True,
            "plan_text": "## Merge Verification Plan\n\n- Risk level: `clean_disjoint`\n",
        },
    )

    assert "You are certifying an integrated merge before it lands" in out
    assert "Risk level: `clean_disjoint`" in out
    assert "A prior task's proof-of-work can justify `SKIPPED`" in out
    assert "If any story is `FLAG_FOR_HUMAN`, the final `VERDICT` must be `FAIL`" in out


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
    assert "Merge Verification Plan" in out
    assert "SKIPPED" not in out
    assert "FLAG_FOR_HUMAN" in out
    assert "Skipping is disabled for this merge" in out


def test_write_certifier_verification_plan_records_story_results(tmp_path: Path):
    plan = _write_certifier_verification_plan(
        report_dir=tmp_path,
        mode="thorough",
        target=None,
        story_results=[
            {
                "story_id": "pdf-export",
                "summary": "PDF export works",
                "verdict": "PASS",
                "observed_result": "Downloaded a PDF.",
                "surface": "HTTP",
            }
        ],
        explicit_stories=None,
    )

    assert (tmp_path / "verification-plan.json").exists()
    assert plan["scope"] == "certify"
    assert plan["policy"] == "full"
    assert plan["checks"][0]["id"] == "pdf-export"
    assert plan["checks"][0]["status"] == "pass"


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
    assert "Merge Verification Plan" in out
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
    by_id = {story["story_id"]: story for story in parsed.stories}
    assert by_id["b"]["verdict"] == "FAIL"
    assert by_id["b"]["passed"] is False
    has_failures = any(s["verdict"] == "FAIL" for s in parsed.stories)
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

    write_test_pow_report(
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


def test_pow_demo_evidence_marks_story_specific_web_video_strong(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "save-filter.webm").write_bytes(b"video")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "save-filter",
                "summary": "saved filter appears after clicking Save view",
                "claim": "User can save a dashboard filter from the browser UI.",
                "observed_steps": ["opened dashboard", "clicked Save view"],
                "observed_result": "saved filter appeared",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Browser UI showed the saved view.",
                "verdict": "PASS",
                "passed": True,
            }
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        evidence_dir=evidence_dir,
    )

    demo = report["demo_evidence"]
    assert demo["app_kind"] == "web"
    assert demo["demo_required"] is True
    assert demo["demo_status"] == "strong"
    assert demo["primary_demo"]["name"] == "save-filter.webm"
    assert demo["stories"][0]["proof_level"] == "story video"
    html = (tmp_path / "proof-of-work.html").read_text()
    assert "Demo Proof" in html
    assert "save-filter.webm" in html


def test_pow_demo_evidence_marks_generic_recording_plus_screenshot_partial(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "pdf-export-ui-link.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "pdf-export-ui-link",
                "summary": "PDF export button downloads from the dashboard",
                "claim": "User can click the dashboard export control and download a PDF.",
                "observed_steps": ["opened dashboard", "clicked Export PDF"],
                "observed_result": "browser requested a PDF and saved a file",
                "surface": "DOM / screenshot",
                "methodology": "live-ui-events",
                "evidence": "Browser UI showed the export control and file validation confirmed application/pdf.",
                "verdict": "PASS",
                "passed": True,
            }
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        evidence_dir=evidence_dir,
        intent="Certify the existing PDF export feature as a user-visible product flow.",
    )

    demo = report["demo_evidence"]
    assert demo["demo_required"] is True
    assert demo["demo_status"] == "partial"
    assert "generic walkthrough" in demo["demo_reason"]
    assert demo["counts"]["generic_recordings"] == 1
    assert demo["counts"]["story_screenshots"] == 1
    assert demo["counts"]["story_videos"] == 0
    assert report["outcome"] == "passed"
    assert report["verdict_label"] == "PASS with warnings"
    assert report["evidence_gate"]["status"] == "warn"


def test_pow_demo_evidence_marks_fast_mode_video_not_required(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "pdf-smoke-test-added",
                "summary": "PDF smoke test was added and passes",
                "claim": "A smoke test covers PDF export.",
                "observed_result": "pytest passed",
                "surface": "source-level",
                "methodology": "source-review",
                "evidence": "pytest tests/test_pdf.py passed",
                "verdict": "PASS",
                "passed": True,
            }
        ],
        "passed",
        8.0,
        0.0,
        1,
        1,
        certifier_mode="fast",
    )

    demo = report["demo_evidence"]
    assert demo["demo_required"] is False
    assert demo["demo_status"] == "not_applicable"
    assert "Fast certification" in demo["demo_reason"]


def test_pow_required_demo_missing_blocks_passing_report(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "pdf-export-download",
                "summary": "PDF export can be downloaded from the dashboard",
                "claim": "User can click the dashboard export control and download a PDF.",
                "observed_steps": ["reviewed pytest coverage only"],
                "observed_result": "pytest passed",
                "surface": "source-level",
                "methodology": "source-review",
                "evidence": "tests/test_pdf_export.py passed",
                "verdict": "PASS",
                "passed": True,
            }
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent="Certify the existing PDF export feature as a user-visible product flow.",
    )

    assert report["agent_outcome"] == "passed"
    assert report["outcome"] == "failed"
    assert report["verdict_label"] == "FAIL"
    assert report["demo_evidence"]["demo_required"] is True
    assert report["demo_evidence"]["demo_status"] == "missing"
    assert report["evidence_gate"]["blocks_pass"] is True
    assert "Required demo proof gate failed" in report["diagnosis"]
    assert "FAIL" in (tmp_path / "proof-of-work.md").read_text()


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
