"""Tests for the certifier story-subset interface.

Validates:
- `_format_stories_section` rendering
- `{stories_section}` placeholder support across all certifier prompts
"""

from __future__ import annotations

import json
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


def test_render_includes_evidence_contract_with_explicit_story(tmp_path: Path):
    out = _render_certifier_prompt(
        mode="standard",
        intent="Certify the PDF export web flow.",
        evidence_dir=tmp_path / "evidence",
        stories=[
            {
                "story_id": "pdf-export",
                "claim": "User can click Export PDF and download the generated file.",
                "surface": "DOM",
                "methodology": "live-ui-events",
            }
        ],
    )

    assert "Evidence Contract" in out
    assert str(tmp_path / "evidence") in out
    assert "`pdf-export`" in out
    assert "story-named screenshot/clip" in out
    assert "file validation" in out
    assert "$ " in out


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
    custom_project_server_pid = 250
    other_server_pid = 300
    alive = {project_server_pid, custom_project_server_pid}
    kill_calls: list[tuple[int, int]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"p100\np{project_server_pid}\np{custom_project_server_pid}\np{other_server_pid}\n",
                stderr="",
            )
        if args[:3] == ["ps", "-o", "command="]:
            pid = int(args[-1])
            command = {
                project_server_pid: f"{project_dir}/.venv/bin/python .venv/bin/flask --app app run --port 5199",
                custom_project_server_pid: "python3 -m fieldops.server --host 127.0.0.1 --port 5107",
                other_server_pid: "/tmp/other/.venv/bin/python -m http.server 8000",
            }[pid]
            return subprocess.CompletedProcess(args, 0, stdout=f"{command}\n", stderr="")
        if args[:2] == ["lsof", "-a"] and "-d" in args and "cwd" in args:
            pid = int(args[args.index("-p") + 1])
            cwd = {
                project_server_pid: project_dir,
                custom_project_server_pid: project_dir,
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
        if pid in {project_server_pid, custom_project_server_pid} and sig == signal.SIGTERM:
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
        },
        {
            "pid": custom_project_server_pid,
            "command": "python3 -m fieldops.server --host 127.0.0.1 --port 5107",
            "cwd": str(project_dir),
        }
    ]
    assert kill_calls == [
        (project_server_pid, signal.SIGTERM),
        (custom_project_server_pid, signal.SIGTERM),
    ]


def test_certifier_cleanup_removes_only_new_untracked_runtime_files(tmp_path: Path):
    project_dir = tmp_path
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_dir, check=True)
    (project_dir / "app.py").write_text("print('ok')\n")
    subprocess.run(["git", "add", "app.py"], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, check=True, capture_output=True, text=True)

    (project_dir / "preexisting.log").write_text("keep\n")
    (project_dir / "runtime.db").write_text("remove\n")
    (project_dir / "runtime-dir").mkdir()
    (project_dir / "runtime-dir" / "state.json").write_text("{}\n")

    cleaned = certifier_module._cleanup_certifier_untracked_delta(
        project_dir,
        {"preexisting.log"},
    )

    assert set(cleaned) == {"runtime.db", "runtime-dir/"}
    assert (project_dir / "preexisting.log").exists()
    assert not (project_dir / "runtime.db").exists()
    assert not (project_dir / "runtime-dir").exists()


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
    assert f"`{tmp_path}`" in out
    assert "Do not shorten it, reconstruct it" in out
    assert "otto_logs/sessions/certify/evidence" in out
    assert "at least one `.webm` browser" in out


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
    assert "agent-browser --session merge-visual record start" in out
    assert "HTML inspection alone is" in out
    assert "not browser proof" in out
    assert "list the files" in out
    assert "{evidence_dir}" not in out
    assert "otto_logs/sessions/certify/evidence" in out
    assert "At least one `.webm` browser recording is required" in out
    assert str(tmp_path / "recording.webm") in out


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


def test_parse_certifier_markers_accepts_markdown_decorated_final_markers():
    """Provider reports can decorate markers even when the semantic verdict is clear."""
    from otto.markers import parse_certifier_markers

    text = (
        "✦ STORIES_TESTED: 1\n"
        "✦ STORIES_PASSED: 1\n"
        "✦ STORY_RESULT: ESCALATION_NOTE_FOOTER | PASS | visual proof captured\n"
        "COVERAGE_OBSERVED:\n"
        "- screenshot and curl evidence were captured\n"
        "COVERAGE_GAPS:\n"
        "- none\n"
        "**VERDICT: PASS**\n"
        "**DIAGNOSIS**: Escalation footer is visible after merge.\n"
    )

    parsed = parse_certifier_markers(text, certifier_mode="standard")

    assert parsed.verdict_seen is True
    assert parsed.verdict_pass is True
    assert parsed.stories_tested == 1
    assert parsed.stories_passed == 1
    assert parsed.stories[0]["story_id"] == "ESCALATION_NOTE_FOOTER"
    assert parsed.diagnosis == "Escalation footer is visible after merge."


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


def test_pow_demo_evidence_accepts_walkthrough_video_plus_story_screenshot(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "page@abc123.webm").write_bytes(b"video")
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
    assert demo["demo_status"] == "strong"
    assert demo["primary_demo"]["name"] == "page@abc123.webm"
    assert demo["counts"]["generic_recordings"] == 1
    assert demo["counts"]["story_screenshots"] == 1
    assert demo["counts"]["story_videos"] == 0
    assert report["agent_outcome"] == "passed"
    assert report["outcome"] == "passed"
    assert report["verdict_label"] == "PASS"
    assert report["evidence_gate"]["status"] == "complete"
    assert report["proof_quality"] == "complete"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_demo_evidence_assigns_story_number_screenshot_by_meaning(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "story-3-status-filter.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "audit-timeline-feature",
                "summary": "Audit timeline displays events and filters",
                "claim": "Audit timeline shows 3 status changes and a technician filter.",
                "observed_result": "Audit page loaded.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Browser page displayed timeline events.",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "dispatch-status-filter",
                "summary": "Status filter limits Kanban columns",
                "claim": "Status filter limits the dispatch board columns.",
                "observed_result": "Only blocked work orders were visible.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Browser page showed the filtered dispatch board.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        2,
        2,
        evidence_dir=evidence_dir,
        intent="Certify the integrated web app after merging dispatch and audit branches.",
    )

    demo = report["demo_evidence"]
    dispatch_story = next(story for story in demo["stories"] if story["id"] == "dispatch-status-filter")
    audit_story = next(story for story in demo["stories"] if story["id"] == "audit-timeline-feature")
    assert dispatch_story["visual_items"][0]["name"] == "story-3-status-filter.png"
    assert audit_story["visual_items"] == []
    assert demo["demo_status"] == "partial"
    assert report["agent_outcome"] == "passed"
    assert report["outcome"] == "passed"
    assert report["proof_quality"] == "partial"
    assert report["evidence_gate"]["status"] == "partial"
    assert report["evidence_gate"]["blocks_pass"] is False
    assert report["evidence_gate"]["would_block_audit_pass"] is True


def test_pow_demo_evidence_matches_descriptive_visual_filenames(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "ui-incident-detail-full.png").write_bytes(b"image")
    (evidence_dir / "invalid-incident-handling.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "Incident detail page renders all operator action UI elements",
                "summary": "Incident detail page renders all operator action UI elements",
                "claim": "Incident detail page renders all operator action UI elements.",
                "observed_result": "Incident detail page displayed the operator forms and checklist.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Browser screenshot shows the incident detail UI.",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "Invalid incident IDs return proper 404 error pages",
                "summary": "Invalid incident IDs return proper 404 error pages",
                "claim": "Invalid incident IDs return proper 404 error pages.",
                "observed_result": "Invalid incident page displayed a 404 error.",
                "surface": "HTML page",
                "methodology": "live-ui-events",
                "evidence": "Browser screenshot shows invalid incident handling.",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "Complete test suite passes with full coverage of all features",
                "summary": "Complete test suite passes with full coverage",
                "claim": "76 tests pass covering API, dashboard, incident detail, operator actions.",
                "observed_result": "Full test coverage with 100% pass rate.",
                "surface": "pytest",
                "methodology": "source-level",
                "evidence": "pytest reported 76/76 tests passed.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        3,
        3,
        evidence_dir=evidence_dir,
        intent="Build an incident command center web app with operator workflows.",
    )

    by_id = {story["id"]: story for story in report["demo_evidence"]["stories"]}
    assert by_id["Incident detail page renders all operator action UI elements"]["visual_items"][0]["name"] == (
        "ui-incident-detail-full.png"
    )
    assert by_id["Invalid incident IDs return proper 404 error pages"]["visual_items"][0]["name"] == (
        "invalid-incident-handling.png"
    )
    assert report["demo_evidence"]["demo_status"] == "strong"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_demo_evidence_matches_multisurface_story_id_screenshots(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "app-startup.png").write_bytes(b"image")
    (evidence_dir / "incident-detail-display.png").write_bytes(b"image")
    (evidence_dir / "incident-detail-labels.png").write_bytes(b"image")
    (evidence_dir / "incident-list-display.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "app-startup",
                "summary": "Flask app startup, health endpoint, and incident list all functional",
                "claim": "App startup working, Flask initialized, health endpoint functional, incident list populated.",
                "observed_result": "App startup with health endpoint functional confirmed via screenshot.",
                "surface": "HTTP;DOM",
                "methodology": "live-ui-events;http-request",
                "evidence": "App startup with health endpoint functional confirmed via screenshot (app-startup.png).",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "incident-detail-display",
                "summary": "Incident detail shows complete incident info with comments and audit trail",
                "claim": "Detail page shows incident with comments and audit trail.",
                "observed_result": "Detail page shows incident with comments and complete audit trail.",
                "surface": "DOM;HTTP",
                "methodology": "live-ui-events;http-request",
                "evidence": "Detail page shows incident with comments and audit trail (incident-detail-display.png).",
                "failure_evidence": "incident-detail-display.png",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "incident-detail-labels",
                "summary": "Incident detail correctly displays labels with colors",
                "claim": "Labels displayed on incident detail.",
                "observed_result": "Labels displayed on incident detail with colors.",
                "surface": "DOM;HTTP",
                "methodology": "live-ui-events;http-request",
                "evidence": "Labels displayed on incident detail with colors (incident-detail-labels.png).",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "incident-list-display",
                "summary": "Incident list correctly displays all seeded data with proper formatting",
                "claim": "Incident list displays seeded data correctly.",
                "observed_result": "Incident list displays all seeded data correctly.",
                "surface": "DOM;HTTP",
                "methodology": "live-ui-events;http-request",
                "evidence": "Incident list displays all seeded data correctly (incident-list-display.png).",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        4,
        4,
        evidence_dir=evidence_dir,
        intent="Build an incident operations web app.",
    )

    by_id = {story["id"]: story for story in report["demo_evidence"]["stories"]}
    assert by_id["app-startup"]["visual_items"][0]["name"] == "app-startup.png"
    assert by_id["incident-detail-display"]["visual_items"][0]["name"] == "incident-detail-display.png"
    assert by_id["incident-detail-display"]["visual_items"][0]["caption"] == ""
    assert by_id["incident-detail-labels"]["visual_items"][0]["name"] == "incident-detail-labels.png"
    assert by_id["incident-list-display"]["visual_items"][0]["name"] == "incident-list-display.png"
    assert all(story["proof_level"] == "story screenshot" for story in by_id.values())
    assert report["demo_evidence"]["demo_status"] == "strong"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_demo_evidence_groups_story_id_prefix_screenshots(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "operator-identity-and-audit.webm").write_bytes(b"video")
    (evidence_dir / "operator-identity-01-incident-list.png").write_bytes(b"image")
    (evidence_dir / "operator-identity-02-operator-set.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "operator-identity-and-audit",
                "summary": "Operator identity and audit attribution",
                "claim": "Operator selection persists and actions are attributed in the audit log.",
                "observed_result": "Operator selector, role badges, and audit events were visible.",
                "surface": "screenshot;HTTP;source-level;video",
                "methodology": "live-ui-events",
                "evidence": "Browser workflow recorded with matching screenshots.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        evidence_dir=evidence_dir,
        intent="Build an operator identity web workflow.",
    )

    story = report["demo_evidence"]["stories"][0]
    visual_names = {item["name"] for item in story["visual_items"]}
    assert "operator-identity-and-audit.webm" in visual_names
    assert "operator-identity-01-incident-list.png" in visual_names
    assert "operator-identity-02-operator-set.png" in visual_names
    assert report["visual_evidence"]["unassigned"] == []
    assert report["demo_evidence"]["counts"]["story_screenshots"] == 2
    assert report["demo_evidence"]["demo_status"] == "strong"


def test_pow_demo_evidence_can_attach_one_visual_to_multiple_ui_stories(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "search-results.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "search-posts",
                "summary": "Search finds posts by content",
                "claim": "Search finds posts by content.",
                "observed_steps": ['entered "japan"', "results loaded"],
                "observed_result": "Post results loaded with matching content.",
                "surface": "DOM / screenshot",
                "methodology": "live-ui-events",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "search-users",
                "summary": "Search finds users",
                "claim": "Search finds users.",
                "observed_steps": ['entered "alice"', "results loaded"],
                "observed_result": "User results loaded with matching profile details.",
                "surface": "DOM / screenshot",
                "methodology": "live-ui-events",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "api-search",
                "summary": "JSON API search works",
                "claim": "JSON API search returns matching data.",
                "observed_result": "curl returned JSON results.",
                "surface": "HTTP",
                "methodology": "http-request",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        3,
        3,
        evidence_dir=evidence_dir,
        intent="Certify the Microfeed search web UI and API.",
    )

    by_id = {story["id"]: story for story in report["demo_evidence"]["stories"]}
    assert by_id["search-posts"]["visual_items"][0]["name"] == "search-results.png"
    assert by_id["search-users"]["visual_items"][0]["name"] == "search-results.png"
    assert by_id["api-search"]["visual_items"] == []
    assert report["demo_evidence"]["demo_status"] == "strong"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_demo_evidence_matches_drilldown_visual_from_descriptive_filename(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "incident-detail-from-analytics.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "story-013-drill-down-to-details",
                "summary": "Analytics dashboard provides functional drill-down to incident details",
                "claim": "Analytics dashboard provides functional drill-down to incident details.",
                "observed_result": "Clicking an incident link opened the incident detail page.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Analytics page contains clickable incident links that navigate to detail pages.",
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
        intent="Certify the analytics web dashboard.",
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["visual_items"][0]["name"] == "incident-detail-from-analytics.png"
    assert story["proof_level"] == "story screenshot"
    assert report["demo_evidence"]["demo_status"] == "strong"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_demo_evidence_does_not_credit_broad_screenshot_as_story_specific(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "02-dispatch-board.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "dispatch-kanban-columns",
                "summary": "Kanban board renders status columns",
                "claim": "Dispatch board shows kanban columns.",
                "observed_result": "Dispatch page loaded.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Browser page displayed a dispatch board.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        evidence_dir=evidence_dir,
        intent="Certify the integrated web app after merging dispatch branches.",
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["visual_items"] == []
    assert story["proof_level"] == "text evidence"
    assert report["demo_evidence"]["demo_status"] == "missing"
    assert report["outcome"] == "passed"
    assert report["proof_quality"] == "missing"
    assert report["evidence_gate"]["blocks_pass"] is False
    assert report["evidence_gate"]["would_block_audit_pass"] is True


def test_pow_demo_evidence_recovers_misplaced_referenced_visual_artifact(tmp_path: Path):
    evidence_dir = tmp_path / "run" / "certify" / "evidence"
    legacy_dir = tmp_path / "otto_logs" / "sessions" / "certify" / "evidence"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "dashboard-main.png").write_bytes(b"image")
    (tmp_path / "messages.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "blocks": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {
                            "command": (
                                "page.screenshot({ path: '"
                                + str(legacy_dir / "dashboard-main.png")
                                + "' });"
                            )
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "dashboard-view",
                "summary": "Dashboard displays dispatch metrics",
                "claim": "Dashboard displays dispatch metrics.",
                "observed_result": "Dashboard metrics loaded.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "Visual evidence: dashboard-main.png shows dashboard metrics.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        evidence_dir=evidence_dir,
        intent="Certify the dashboard web UI.",
    )

    assert (evidence_dir / "dashboard-main.png").exists()
    story = report["demo_evidence"]["stories"][0]
    assert story["visual_items"][0]["name"] == "dashboard-main.png"
    assert story["proof_level"] == "story screenshot"
    assert report["demo_evidence"]["demo_status"] == "partial"
    assert report["outcome"] == "passed"
    assert report["proof_quality"] == "partial"


def test_pow_demo_evidence_does_not_treat_cli_words_as_ui(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "cli-report-basic",
                "summary": "CLI report displays all required metrics",
                "claim": "CLI report displays all required metrics.",
                "observed_result": "Report displays status breakdown and technician summary.",
                "surface": "CLI",
                "methodology": "cli-execution",
                "evidence": "python3 -m fieldops.report printed the required totals.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent="Certify the integrated web app with CLI reporting.",
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["needs_visual"] is False
    assert story["proof_level"] == "text evidence"
    assert report["outcome"] == "passed"


def test_file_validation_detection_ignores_generic_file_and_audit_export_words():
    assert not certifier_module._story_is_file_or_download(
        {
            "story_id": "audit-timeline",
            "claim": "Audit timeline displays events with complete information.",
            "summary": "Audit timeline page fully functional",
            "evidence": "Metric card shows Exports: 2.",
        }
    )
    assert not certifier_module._story_is_file_or_download(
        {
            "story_id": "notes-persistence",
            "claim": "Notes form saves and persists to append-only file.",
            "summary": "Notes persistence fully operational.",
        }
    )
    assert certifier_module._story_is_file_or_download(
        {
            "story_id": "csv-export",
            "claim": "CSV export endpoint with headers and filters.",
            "summary": "CSV export endpoint fully functional.",
        }
    )


def test_http_api_and_csv_stories_do_not_require_browser_visuals():
    assert not certifier_module._story_is_web_ui(
        {
            "story_id": "api-responses",
            "claim": "JSON API endpoints return valid structured data.",
            "observed_steps": ["requested /api/work-orders", "parsed JSON"],
            "observed_result": "HTTP 200 with valid JSON and required fields.",
            "surface": "HTTP / screenshot",
            "methodology": "http-request",
        }
    )
    assert not certifier_module._story_is_web_ui(
        {
            "story_id": "csv-export",
            "claim": "CSV export endpoint returns valid CSV data.",
            "observed_steps": ["requested /api/work-orders.csv", "counted rows"],
            "observed_result": "HTTP 200 with text/csv and all 12 columns.",
            "surface": "HTTP / screenshot",
            "methodology": "http-request",
        }
    )
    assert certifier_module._story_is_web_ui(
        {
            "story_id": "navigation-responsive",
            "claim": "Navigation menu works across all pages with responsive design support.",
            "observed_steps": ["tested page title updates on each nav action"],
            "observed_result": "All navigation links functional.",
            "surface": "HTTP / DOM / screenshot",
            "methodology": "live-ui-events",
        }
    )


def test_pow_demo_evidence_does_not_require_video_for_http_file_story(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "pdf-export-ui-flow.webm").write_bytes(b"story-video")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "pdf-export-ui-flow",
                "summary": "PDF export can be downloaded from the dashboard",
                "claim": "User can click the dashboard export control and download a PDF.",
                "observed_steps": ["opened dashboard", "clicked Export PDF"],
                "observed_result": "browser downloaded a PDF",
                "surface": "DOM / screenshot / video",
                "methodology": "live-ui-events",
                "evidence": "Browser recording showed the export flow and file validation confirmed application/pdf.",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "pdf-export-alias",
                "summary": "/expenses.pdf aliases /tickets.pdf",
                "claim": "/expenses.pdf alias works identically to /tickets.pdf.",
                "observed_steps": ["requested /expenses.pdf with curl"],
                "observed_result": "200 OK, application/pdf, correct Content-Disposition",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": "HTTP GET /expenses.pdf => 200, application/pdf, content-type and bytes verified.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        2,
        2,
        evidence_dir=evidence_dir,
        intent="Certify the existing PDF export feature as a user-visible product flow.",
    )

    demo = report["demo_evidence"]
    alias_story = next(story for story in demo["stories"] if story["id"] == "pdf-export-alias")
    ui_story = next(story for story in demo["stories"] if story["id"] == "pdf-export-ui-flow")
    assert ui_story["needs_visual"] is True
    assert alias_story["needs_visual"] is False
    assert alias_story["needs_file_validation"] is True
    assert alias_story["proof_level"] == "file validation"
    assert demo["demo_status"] == "strong"
    assert report["verdict_label"] == "PASS"


def test_pow_file_validation_satisfies_browser_download_header_story(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "story-014-csv-content-disposition",
                "summary": "CSV export configured for proper browser download behavior",
                "claim": "CSV export configured for proper browser download behavior.",
                "observed_steps": ["requested the CSV export endpoint with curl"],
                "observed_result": "HTTP 200 with text/csv and attachment content disposition.",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": (
                    "$ curl -i http://localhost:5000/api/analytics/export.csv\n"
                    "HTTP/1.1 200 OK\n"
                    "Content-Type: text/csv\n"
                    "Content-Disposition: attachment; filename=analytics.csv"
                ),
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
        intent="Certify the analytics dashboard and CSV download flow.",
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["needs_file_validation"] is True
    assert story["has_file_validation"] is True
    assert story["needs_visual"] is False
    assert story["proof_level"] == "file validation"
    assert report["demo_evidence"]["demo_status"] == "strong"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_file_validation_accepts_observed_curl_export_details(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "timeline.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "timeline",
                "summary": "Timeline renders in the browser",
                "claim": "Timeline renders in the browser.",
                "observed_steps": ["opened the home timeline"],
                "observed_result": "Timeline rendered with seeded posts.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "timeline.png",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "csv-export",
                "summary": "CSV export working",
                "claim": "Timeline CSV export returns a downloadable file.",
                "observed_steps": [
                    "Called /api/export/timeline.csv?as=alice",
                    "verified headers",
                ],
                "observed_result": "CSV file with proper headers returned.",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": "",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        2,
        2,
        evidence_dir=evidence_dir,
        intent="Build a web app with timeline CSV export.",
    )

    story = next(story for story in report["demo_evidence"]["stories"] if story["id"] == "csv-export")
    assert story["needs_file_validation"] is True
    assert story["has_file_validation"] is True
    assert story["proof_level"] == "file validation"
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_file_validation_uses_story_context_with_curl_rows(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "csv-filtering-combined",
                "summary": "Combined filters working correctly",
                "claim": "CSV export supports combining status and severity filters.",
                "observed_steps": [
                    "requested /api/incidents/export?status=open&severity=high",
                    "verified returned rows matched both filters",
                ],
                "observed_result": "2 rows returned and every row had status=open and severity=high.",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": (
                    "$ curl -s 'http://127.0.0.1:8900/api/incidents/export?status=open&severity=high' | wc -l\n"
                    "2\n"
                    "row verified: status=open and severity=high"
                ),
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "csv-deterministic-ordering",
                "summary": "Row ordering deterministic and stable",
                "claim": "CSV export rows are returned in deterministic order across repeated requests.",
                "observed_steps": [
                    "made three consecutive requests to /api/incidents/export",
                    "compared the final row across all responses",
                ],
                "observed_result": "Every request returned the same final row.",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": (
                    "$ curl -s http://127.0.0.1:8900/api/incidents/export | tail -1\n"
                    "5,Nightly batch job timeout,resolved,medium,Alice Chen\n"
                    "$ curl -s http://127.0.0.1:8900/api/incidents/export | tail -1\n"
                    "5,Nightly batch job timeout,resolved,medium,Alice Chen"
                ),
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        2,
        2,
        intent="Add CSV export endpoints with filtering and deterministic ordering.",
    )

    by_id = {story["id"]: story for story in report["demo_evidence"]["stories"]}
    assert by_id["csv-filtering-combined"]["has_file_validation"] is True
    assert by_id["csv-deterministic-ordering"]["has_file_validation"] is True
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_generic_recording_does_not_cover_unvisualized_ui_story(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    (evidence_dir / "dashboard.png").write_bytes(b"png")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "dashboard",
                "summary": "Dashboard loads",
                "claim": "Dashboard loads in the browser.",
                "observed_steps": ["opened dashboard"],
                "observed_result": "Dashboard rendered.",
                "surface": "DOM / screenshot",
                "methodology": "live-ui-events",
                "evidence": "dashboard.png",
                "verdict": "PASS",
                "passed": True,
            },
            {
                "story_id": "navigation-responsive",
                "summary": "Navigation works across pages",
                "claim": "Navigation links update page titles across the app.",
                "observed_steps": ["clicked nav links"],
                "observed_result": "Page titles updated.",
                "surface": "DOM / screenshot",
                "methodology": "live-ui-events",
                "evidence": "Generic walkthrough only.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        2,
        2,
        evidence_dir=evidence_dir,
        intent="Certify the web app navigation and dashboard.",
    )

    nav = next(story for story in report["demo_evidence"]["stories"] if story["id"] == "navigation-responsive")
    assert nav["needs_visual"] is True
    assert nav["proof_level"] == "generic walkthrough only"
    assert report["demo_evidence"]["demo_status"] == "partial"
    assert report["outcome"] == "passed"
    assert report["proof_quality"] == "partial"
    assert report["evidence_gate"]["status"] == "partial"
    assert report["evidence_gate"]["blocks_pass"] is False
    assert report["evidence_gate"]["would_block_audit_pass"] is True


def test_pow_demo_evidence_accepts_walkthrough_with_story_text_evidence(tmp_path: Path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "recording.webm").write_bytes(b"video")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "web-status-change",
                "summary": "Status form updates incident status",
                "claim": "Status form updates incident status and persists the change.",
                "observed_steps": ["selected status", "submitted form", "verified detail page"],
                "observed_result": "Incident changed to resolved and audit row was created.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": (
                    "POST /incidents/4/status returned 303; subsequent GET /incidents/4 "
                    "showed status resolved and audit row status_changed for Alice Chen."
                ),
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        evidence_dir=evidence_dir,
        intent="Certify the web app workflow.",
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["proof_level"] == "walkthrough + text evidence"
    assert report["demo_evidence"]["demo_status"] == "strong"
    assert report["outcome"] == "passed"


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


def test_pow_required_demo_missing_marks_proof_missing_without_failing_product(tmp_path: Path):
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
    assert report["outcome"] == "passed"
    assert report["verdict_label"] == "PASS with warnings"
    assert report["proof_quality"] == "missing"
    assert report["demo_evidence"]["demo_required"] is True
    assert report["demo_evidence"]["demo_status"] == "missing"
    assert report["evidence_gate"]["blocks_pass"] is False
    assert report["evidence_gate"]["would_block_audit_pass"] is True
    assert report["evidence_gate"]["missing_requirements"]
    assert report["round_history"][-1]["product_passed"] is True
    assert "PASS" in (tmp_path / "proof-of-work.md").read_text()
    html = (tmp_path / "proof-of-work.html").read_text()
    assert "PASS with warnings" in html
    assert "Missing" in html


def test_pow_evidence_spec_records_story_requirements(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "json-notifications",
                "summary": "JSON API returns notifications",
                "claim": "GET /api/notifications returns notification payloads.",
                "observed_steps": ["curl /api/notifications"],
                "observed_result": "HTTP 200 JSON response",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": "$ curl -s http://localhost:5000/api/notifications\n[]",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent="Certify a JSON API.",
    )

    spec = report["evidence_spec"]
    assert spec["app_kind"] == "api"
    assert spec["global_video_required"] is False
    story = spec["stories"][0]
    assert story["id"] == "json-notifications"
    assert story["requires_command"] is True
    assert story["requires_visual"] is False


def test_pow_does_not_require_file_validation_for_pytest_story(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "story-10-pytest-test-suite",
                "summary": "All pytest tests pass including CSV export tests",
                "claim": "All pytest tests pass including CSV export tests",
                "observed_steps": ["uv run pytest -q"],
                "observed_result": "82 passed",
                "surface": "CLI",
                "methodology": "cli-execution",
                "evidence": "$ uv run pytest -q\n82 passed",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent="Certify the merged app and tests.",
    )

    demo = report["demo_evidence"]
    assert demo["demo_status"] == "not_applicable"
    story = report["evidence_spec"]["stories"][0]
    assert story["requires_command"] is True
    assert story["requires_file_validation"] is False


def test_pow_does_not_require_file_validation_for_non_file_audit_export_story(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "audit-events",
                "summary": "All workflow actions create proper audit events with actor attribution",
                "claim": "All workflow actions create audit events with action and detail",
                "observed_steps": ["reviewed audit event list", "examined audit export"],
                "observed_result": "Audit events include action, detail, actor name, and role",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": "comment_added, status_changed, and assignee_changed audit rows include actor details",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent="Verify workflow actions and audit logs.",
    )

    demo = report["demo_evidence"]
    assert demo["demo_status"] == "not_applicable"
    story = report["evidence_spec"]["stories"][0]
    assert story["requires_file_validation"] is False


def test_pow_merge_verification_does_not_require_fresh_ui_demo_video(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "web-status-change",
                "summary": "Status form updates incident status",
                "claim": "Status form updates incident status and persists the change.",
                "observed_steps": ["submitted status form"],
                "observed_result": "Incident status changed and audit row was recorded.",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "POST /incidents/4/status returned 303; GET /incidents/4 showed status resolved.",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent=(
            "Verify the merged branch integration for this Otto landing operation.\n\n"
            "Merged branches:\n- improve/status-form\n\nStory union to verify:\n"
            "1. web-status-change: Status form updates incident status."
        ),
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["needs_visual"] is False
    assert report["demo_evidence"]["demo_status"] == "not_applicable"
    assert report["outcome"] == "passed"


def test_pow_file_validation_accepts_csv_row_order_evidence(tmp_path: Path):
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "story-8-deterministic-row-order",
                "summary": "Row ordering deterministic DESC",
                "claim": "DESC ordering by created_at",
                "observed_steps": ["inspected created_at timestamps in CSV"],
                "observed_result": "Timestamps descending from newest to oldest",
                "surface": "HTTP",
                "methodology": "http-request",
                "evidence": (
                    "Verified: All CSV exports sorted by created_at in descending order. "
                    "Latest incident appears first and oldest incident appears last."
                ),
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        12.0,
        0.0,
        1,
        1,
        intent="Verify CSV export ordering.",
    )

    story = report["demo_evidence"]["stories"][0]
    assert story["needs_file_validation"] is True
    assert story["has_file_validation"] is True
    assert report["evidence_gate"]["blocks_pass"] is False


def test_pow_round_history_labels_proof_repair_separately(tmp_path: Path):
    (tmp_path / "recording.webm").write_bytes(b"video")
    (tmp_path / "dashboard-create.png").write_bytes(b"image")
    report = write_test_pow_report(
        tmp_path,
        [
            {
                "story_id": "dashboard-create",
                "summary": "Dashboard create flow works",
                "claim": "User can create a dashboard item.",
                "observed_steps": ["clicked create"],
                "observed_result": "item appeared",
                "surface": "DOM",
                "methodology": "live-ui-events",
                "evidence": "dashboard-create.png",
                "verdict": "PASS",
                "passed": True,
            },
        ],
        "passed",
        20.0,
        0.0,
        1,
        1,
        evidence_dir=tmp_path,
        round_history=[
            {
                "round": 1,
                "verdict": "failed",
                "stories_tested": 1,
                "diagnosis": "Required demo proof gate failed: no browser video walkthrough was recorded",
                "phase": "proof_gate",
                "product_passed": True,
            },
            {
                "round": 2,
                "verdict": "passed",
                "stories_tested": 1,
                "phase": "proof_repair",
                "phase_attempt": 1,
                "product_passed": True,
            },
        ],
        intent="Build a web dashboard.",
    )

    first, second = report["round_history"]
    assert first["phase_label"] == "Proof check"
    assert second["phase_label"] == "Proof repair 1"
    html = (tmp_path / "proof-of-work.html").read_text()
    assert "Proof check" in html
    assert "Proof repair 1" in html


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
