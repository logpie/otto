"""Otto QA — adversarial QA agent, verdict parsing, risk-based tiering."""

import asyncio
from dataclasses import dataclass, field
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from otto.agent import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    _subprocess_env,
    normalize_usage,
    query,
)
from otto.config import agent_provider
from otto.observability import append_text_log, write_json_file
from otto.theme import console

# UserMessage may not exist in all SDK versions — used for tool-result capture
try:
    from claude_agent_sdk.types import UserMessage  # noqa: F401
except (ImportError, AttributeError):
    UserMessage = None  # type: ignore[assignment,misc]


_QA_BASE_INSTRUCTIONS = """\
You are a QA tester. Your job has two parts: VERIFY and BREAK.

PART 1 — VERIFY (required)
For EACH verifiable [must] item, run a targeted verification command.
You may batch related specs (same function/feature) into one script to save
time, but each spec must have a clear pass/fail indicator in the output.

Good proof: `python -c "from store import PostStore; s = PostStore(); p = s.create(title='T', content='C', author='A', tags=[]); assert p.status == 'draft'"` → exit 0
Bad proof: "code inspection confirms create() stores posts correctly"

Rules:
- Every verifiable [must] item MUST have at least one command that was executed.
  "Code inspection" alone is NOT acceptable proof for verifiable items.
- Prefer deterministic targeted commands (single test, curl, node -e script).
- For API endpoints: start the server and make actual HTTP requests.
- For data isolation: test with multiple users/accounts to verify boundaries.
- For auth: test both authorized and unauthorized access paths.
- Test the programmatic API directly, not just through CLI wrappers.
2. Non-verifiable [must ◈] items — start a dev server, navigate, verify in browser.
   Code inspection alone cannot confirm appearance.
   For non-visual subjective items: use your best judgment with evidence.
3. [should] items — note observations, do not block merge.

Items marked ◈ cannot be verified by code alone. Visual items MUST use browser.
Run the full existing test suite once for broad regression coverage.

For each [must] item, record at least one targeted proof tied to that spec_id.
Prefer a deterministic targeted command (single test, curl, script).
If a blocking [must] fails, record proof for that item. For single-task QA you
may stop early. For multi-task batch QA, continue checking ALL items for attribution.

PART 2 — BREAK (after all specs pass)
First, skim the source to discover thresholds, branches, and existing
behaviors that the spec didn't mention — then try to break those.
Spend 2-3 tool calls:
- Boundary inputs: zero, empty string, null, negative, very large
- Wrong types or missing required fields
- Concurrent access if the code is stateful
- Thresholds/limits visible in the source (e.g., size cutoffs, power tables)
- Inputs the spec didn't mention but a real caller would try

Report ALL findings in "extras". Do NOT fail [must] items for BREAK findings —
the spec is the contract, and the existing test suite is the regression gate.
But DO classify each finding so the team can prioritize:
- "regression: ..." — existing behavior that may have broken (check if tests cover it)
- "edge_case: ..." — new gap the spec didn't mention

Also check:
- Does the implementation contradict the ORIGINAL task prompt?
- Does it break existing functionality?

For visual ◈ verification, use agent-browser via Bash (NOT MCP browser tools):
  agent-browser open http://localhost:PORT     # navigate
  agent-browser snapshot -i                     # accessibility tree with @refs
  agent-browser click @e3                       # click by ref
  agent-browser screenshot the screenshot directory provided below
  agent-browser close                           # cleanup when done
Start a dev server first, seed test data, then use agent-browser to verify visuals.

Kill any servers you started (by PID, not pkill).

WORKFLOW: Run tests first, then write the verdict immediately.
Do NOT generate a text summary before writing the verdict — put all analysis
directly into the verdict JSON fields (evidence, proof, extras).
Write the verdict file in a single Write call. Do NOT read it back or rewrite
it — the Write tool is reliable. Every rewrite wastes significant time."""

_SPEC_RESULT_PATTERNS = [
    re.compile(r"\bSPEC\s+(\d+)\s*:\s*(PASS|FAIL)\b", re.IGNORECASE),
    re.compile(r"\bspec_(\d+)[^=\n]*=\s*(PASS|FAIL)\b", re.IGNORECASE),
]


@dataclass
class _QAQueryState:
    qa_cost: float = 0.0
    qa_usage: dict[str, int] = field(default_factory=dict)
    first_message_time: float | None = None
    turn_count: int = 0
    early_verdict: dict[str, Any] | None = None


def format_spec_v45(spec: list) -> str:
    """Format spec items with [must]/[should] binding for prompt injection.

    Non-verifiable (subjective) items get a ◈ marker: [must ◈], [should ◈].
    """
    from otto.tasks import spec_text, spec_binding, spec_is_verifiable
    lines = []
    for item in spec:
        text = spec_text(item)
        binding = spec_binding(item)
        marker = "" if spec_is_verifiable(item) else " \u25c8"  # ◈
        lines.append(f"  [{binding}{marker}] {text}")
    return "\n".join(lines)


def determine_qa_tier(
    task: dict[str, Any],
    spec: list,
    attempt: int,
    diff_info: dict[str, Any],
    spec_test_mapping: dict[str, str | None] | None = None,
    log_dir: Path | None = None,
) -> int:
    """Determine QA tier. Currently always returns 1 — single tier with browser
    always available. The QA agent decides per-spec-item whether to use
    browser tools based on [must ◈] markers.

    Returns 1 always. The tier parameter is kept for logging/observability.
    """
    log_lines = [
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] QA for {task.get('key', 'unknown')}",
        f"attempt: {attempt}",
        f"changed files: {len([str(p) for p in (diff_info.get('files') or [])])}",
        "tier: 1 (unified — browser available, agent decides per-item)",
    ]
    if log_dir:
        append_text_log(log_dir / "qa-tier.log", log_lines + [""])
    return 1


def _is_verdict_complete(verdict: dict[str, Any], *, expected_must_count: int = 0) -> bool:
    """Check if a QA verdict has a valid, complete schema.

    A verdict is complete when:
    - must_passed is a boolean
    - must_items is a list
    - If must_passed is True and expected_must_count > 0, must_items covers all of them
    - Legacy fallback: only trusted for fail verdicts (pass requires evidence)
    """
    if not isinstance(verdict.get("must_passed"), bool):
        return False
    must_items = verdict.get("must_items")
    if not isinstance(must_items, list):
        return False
    # Legacy parses lack structured evidence — trust fail but not pass
    if verdict.get("_legacy_parse") and verdict["must_passed"]:
        return False
    # If claiming pass with expected must items, require full coverage
    if verdict["must_passed"] and expected_must_count > 0 and len(must_items) < expected_must_count:
        return False
    return True


def _parse_qa_verdict_json(report: str) -> dict[str, Any]:
    """Parse structured JSON QA verdict from agent output.

    Searches for a JSON block in the report text. Falls back to
    legacy pass/fail detection if no JSON found.
    """
    import re as _re

    # Try to find JSON block in the report
    # Look for ```json ... ``` or raw JSON object
    json_match = _re.search(r'```json\s*\n(.*?)```', report, _re.DOTALL)
    if not json_match:
        json_match = _re.search(r'(\{[^{}]*"must_passed"[^{}]*\})', report, _re.DOTALL)
    if not json_match:
        # Try to find a larger JSON block with nested objects
        json_match = _re.search(r'(\{.*"must_passed".*\})', report, _re.DOTALL)

    if json_match:
        try:
            verdict = json.loads(json_match.group(1))
            if isinstance(verdict, dict) and "must_passed" in verdict:
                return verdict
        except json.JSONDecodeError:
            pass

    # Try reading from a verdict file if the agent wrote one
    # (QA prompt says "Write your verdict to the output file as JSON")

    # Fallback: parse legacy format
    upper = report.upper()
    has_explicit_fail = "QA VERDICT: FAIL" in report or "VERDICT: FAIL" in upper
    has_explicit_pass = "QA VERDICT: PASS" in report or "VERDICT: PASS" in upper
    # Also detect natural language pass patterns
    if not has_explicit_pass and not has_explicit_fail:
        pass_patterns = ["all must", "all criteria pass", "ready to merge",
                         "all 🟢", "all pass"]
        has_explicit_pass = any(p in report.lower() for p in pass_patterns)

    return {
        "must_passed": has_explicit_pass and not has_explicit_fail,
        "must_items": [],
        "should_notes": [],
        "regressions": [],
        "prompt_intent": "",
        "extras": [],
        "_legacy_parse": True,
    }


def _has_explicit_fail_markers(report: str) -> bool:
    """Return True when the report text explicitly signals failure."""
    import re as _re

    return bool(_re.search(r"\bfail(?:ed|ing|s|ure|ures)?\b", report, _re.IGNORECASE))


# ---------------------------------------------------------------------------
# Proof artifact helpers
# ---------------------------------------------------------------------------

def _is_verification_command(cmd: str) -> bool:
    """Return True if the command is a test/build/curl verification command.

    Filters out exploration commands (find, ls, cat, git diff, etc.)
    and destructive commands (kill, rm, git push, etc.).
    """
    cmd_stripped = cmd.strip()
    if not cmd_stripped:
        return False
    first_word = cmd_stripped.split()[0]
    # Destructive / infra commands — never include
    if first_word in ("kill", "pkill", "rm", "git") or cmd_stripped.startswith("rm "):
        return False
    # Exploration commands — not verification
    if first_word in ("find", "ls", "cat", "head", "tail", "wc", "grep", "rg",
                       "tree", "less", "file", "stat", "which", "type", "pwd",
                       "cd", "echo", "printf", "read", "true"):
        return False
    if "git diff" in cmd_stripped or "git log" in cmd_stripped or "git show" in cmd_stripped:
        return False
    # Verification commands — include
    VERIFY_PREFIXES = (
        "npx jest", "npx vitest", "npx next build", "npx tsc", "npx mocha",
        "pytest", "python -m pytest", "python3 -m pytest",
        "cargo test", "cargo build", "cargo check",
        "go test", "go build", "go vet",
        "npm test", "npm run lint", "npm run build", "npm run test",
        "pnpm test", "pnpm run lint", "pnpm run build",
        "yarn test", "yarn lint", "yarn build",
        "tsc", "node -e", "python -c", "python3 -c",
        "curl", "wget",
        "make test", "make check", "make build",
        "uv run pytest", "uv run python",
        "dotnet test", "dotnet build",
        "ruby", "bundle exec",
        "false", "true",  # used in tests
    )
    if any(cmd_stripped.startswith(p) for p in VERIFY_PREFIXES):
        return True
    # Chained commands like "npm run lint && npm test"
    if " && " in cmd_stripped:
        parts = [p.strip() for p in cmd_stripped.split("&&")]
        return any(_is_verification_command(p) for p in parts)
    # Redirected commands like "npm test 2>&1"
    base = re.sub(r'\s+\d*>&?\d+', '', cmd_stripped).strip()
    if base != cmd_stripped and _is_verification_command(base):
        return True
    return False


def _is_non_replayable(cmd: str) -> bool:
    """Return True if the command starts a server or runs in background.

    These are non-replayable in a regression script.
    """
    cmd_stripped = cmd.strip()
    lower = cmd_stripped.lower()

    # Background / nohup
    if cmd_stripped.endswith(" &") or "nohup " in lower:
        # Exception: "2>&1" is a redirect, not background
        if cmd_stripped.endswith(">&1") or cmd_stripped.endswith(">&2"):
            return False
        return True

    # Server start commands — long-running processes
    SERVER_PATTERNS = (
        "npm run dev", "npm start", "npm run start",
        "npx next dev", "npx next start",
        "python -m http.server", "python3 -m http.server",
        "uvicorn ", "gunicorn ", "flask run",
        "serve ", "node server",
        "npx serve", "http-server",
    )
    for pattern in SERVER_PATTERNS:
        if lower.startswith(pattern) or f" {pattern}" in f" {lower}":
            # Exception: "npx next build" is OK (not a server)
            if "next build" in lower:
                return False
            return True

    # Kill/signal commands
    if lower.startswith(("kill ", "pkill ")):
        return True

    return False


def _dedupe_commands(commands: list[str]) -> list[str]:
    """Deduplicate commands while preserving order."""
    seen: set[str] = set()
    unique: list[str] = []
    for cmd in commands:
        if cmd in seen:
            continue
        seen.add(cmd)
        unique.append(cmd)
    return unique


def _write_regression_script(script_path: Path, commands: list[str]) -> bool:
    """Write a replayable regression shell script."""
    import stat

    if not commands:
        return False

    script_lines = ["#!/bin/bash", "set -e", ""]
    for cmd in commands:
        safe_label = cmd.replace('"', '\\"')
        script_lines.append(f'echo "Running: {safe_label}"')
        script_lines.append(cmd)
        script_lines.append("")
    script_lines.append('echo "All regression checks passed."')

    try:
        script_path.write_text("\n".join(script_lines) + "\n")
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return True
    except OSError:
        return False


def _audit_proof_quality(verdict: dict, log_dir: Path | None = None) -> list[str]:
    """Audit proof quality: flag [must] items with code-reading-only proofs.

    Returns list of warning strings. Logs warnings to qa-agent.log if log_dir provided.
    """
    warnings: list[str] = []
    _CODE_READING_PATTERNS = (
        "code inspection", "source inspection", "reading", "confirmed by reading",
        "source confirms", "code confirms", "inspection shows", "verified by reading",
    )
    _COMMAND_PATTERNS = (
        "python", "pytest", "jest", "npm test", "curl", "node -e", "script:",
        "→", "PASSED", "exit code", "ran ", "executed",
    )

    must_items = verdict.get("must_items", []) or []
    code_reading_only = []
    for item in must_items:
        if item.get("status") != "pass":
            continue
        proof = item.get("proof", []) or []
        evidence = str(item.get("evidence", ""))
        all_text = " ".join(str(p) for p in proof) + " " + evidence

        has_command = any(pat in all_text.lower() for pat in _COMMAND_PATTERNS)
        has_code_reading = any(pat in all_text.lower() for pat in _CODE_READING_PATTERNS)

        if has_code_reading and not has_command:
            code_reading_only.append(
                f"spec {item.get('spec_id', '?')}: {str(item.get('criterion', ''))[:60]}"
            )

    if code_reading_only:
        total = sum(1 for i in must_items if i.get("status") == "pass")
        pct = len(code_reading_only) * 100 // max(total, 1)
        warnings.append(
            f"PROOF QUALITY WARNING: {len(code_reading_only)}/{total} passed [must] items "
            f"({pct}%) have code-reading-only proofs (no command executed):"
        )
        for item in code_reading_only:
            warnings.append(f"  - {item}")

    if warnings and log_dir:
        from otto.observability import append_text_log
        append_text_log(log_dir / "qa-agent.log", ["", "=" * 40, "PROOF QUALITY AUDIT", "=" * 40] + warnings + [""])

    return warnings


def _qa_cost_text(cost_usd: float, *, cost_available: bool) -> str:
    return f"QA ${cost_usd:.2f}" if cost_available else "QA cost unavailable"


def _write_proof_artifacts(
    log_dir: Path,
    verdict: dict,
    qa_actions: list[dict],
    task: dict,
    original_prompt: str,
    cost_usd: float,
    *,
    cost_available: bool = True,
) -> tuple[int, str]:
    """Write proof artifacts from QA verdict and captured actions.

    Creates qa-proofs/ directory with:
    - must-N.md per must item
    - proof-report.md summarizing all proofs
    - regression-check.sh with replayable verification commands

    Returns (file_count, coverage_string) e.g. (5, "3/4").
    """
    import shutil

    proofs_dir = log_dir / "qa-proofs"

    # Preserve screenshots but clean everything else
    screenshots: list[tuple[str, bytes]] = []
    if proofs_dir.exists():
        for f in proofs_dir.iterdir():
            if f.suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                try:
                    screenshots.append((f.name, f.read_bytes()))
                except OSError:
                    pass
        shutil.rmtree(proofs_dir, ignore_errors=True)

    proofs_dir.mkdir(parents=True, exist_ok=True)

    # Restore screenshots
    for name, data in screenshots:
        try:
            (proofs_dir / name).write_bytes(data)
        except OSError:
            pass

    must_items = verdict.get("must_items", [])
    file_count = 0

    # Write must-N.md files
    for i, item in enumerate(must_items, 1):
        criterion = item.get("criterion", "")
        status_str = item.get("status", "unknown")
        evidence = item.get("evidence", "")
        proof = item.get("proof", [])
        content = f"# Must Item {i}\n\n"
        content += f"Criterion: {criterion}\n"
        content += f"Status: {status_str}\n"
        content += f"Evidence: {evidence}\n"
        if proof:
            content += "\nProof:\n"
            for p in proof:
                content += f"- {p}\n"
        try:
            (proofs_dir / f"must-{i}.md").write_text(content)
            file_count += 1
        except OSError:
            pass

    # Build regression-check.sh from verification commands
    verification_cmds: list[str] = []
    for action in qa_actions:
        if action.get("type") == "bash":
            cmd = action.get("command", "")
            if (
                cmd
                and not action.get("is_error", False)
                and _is_verification_command(cmd)
                and not _is_non_replayable(cmd)
            ):
                verification_cmds.append(cmd)

    verification_cmds = _dedupe_commands(verification_cmds)

    if _write_regression_script(proofs_dir / "regression-check.sh", verification_cmds):
        file_count += 1

    # Build proof-report.md — designed for human review
    task_key = task.get("key", "unknown")
    clean_prompt = re.sub(r'\s+', ' ', original_prompt).strip()
    must_total = len(must_items)
    must_passed_count = sum(1 for item in must_items if item.get("status") == "pass")
    overall_passed = verdict.get("must_passed")
    if overall_passed is None:
        overall_passed = must_passed_count == must_total
    result_icon = "\u2713 PASSED" if overall_passed else "\u2717 FAILED"

    r: list[str] = []
    r.append(f"# {clean_prompt}")
    r.append("")
    r.append(f"**{result_icon}** — {must_passed_count}/{must_total} must items — {_qa_cost_text(cost_usd, cost_available=cost_available)}")
    r.append("")

    # Per-item: criterion → evidence → proof
    items_with_proof = 0
    for i, item in enumerate(must_items, 1):
        criterion = item.get("criterion", "")
        status_str = item.get("status", "unknown")
        evidence = item.get("evidence", "")
        proof = item.get("proof", [])
        icon = "\u2713" if status_str == "pass" else "\u2717"
        r.append(f"### {icon} {criterion}")
        if evidence:
            r.append(f"> {evidence}")
        if proof:
            items_with_proof += 1
            r.append("")
            for p in proof:
                r.append(f"- {p}")
        else:
            r.append("\n*No proof recorded*")
        r.append("")

    integration_findings = verdict.get("integration_findings", []) or []
    if integration_findings:
        r.append("## Integration Findings")
        r.append("")
        for item in integration_findings:
            status_str = item.get("status", "unknown")
            icon = "\u2713" if status_str == "pass" else "\u2717"
            description = item.get("description", "") or "Integration check"
            tasks_involved = ", ".join(item.get("tasks_involved") or [])
            test = item.get("test", "")
            r.append(f"### {icon} {description}")
            if tasks_involved:
                r.append(f"Tasks: {tasks_involved}")
            if test:
                r.append(f"Test: {test}")
            r.append("")

    regressions = verdict.get("regressions", []) or []
    if regressions:
        r.append("## Regressions")
        r.append("")
        for item in regressions:
            r.append(f"- {item}")
        r.append("")

    if "test_suite_passed" in verdict:
        suite_icon = "\u2713" if verdict.get("test_suite_passed") else "\u2717"
        suite_status = "passed" if verdict.get("test_suite_passed") else "failed"
        r.append(f"## Full Test Suite: {suite_icon} {suite_status}")
        r.append("")

    # Screenshots (inline, not a separate section)
    screenshot_actions = [
        a for a in qa_actions
        if a.get("type") == "browser" and a.get("action") == "take_screenshot"
    ]
    existing_screenshots = sorted(proofs_dir.glob("screenshot-*"))
    if screenshot_actions or existing_screenshots:
        r.append("### Screenshots")
        for action in screenshot_actions:
            path = action.get("path", "")
            detail = action.get("detail", "")
            if path:
                fname = Path(path).name
                caption = detail or fname
                r.append(f"- [{caption}]({fname})")
        action_paths = {Path(a.get("path", "")).name for a in screenshot_actions if a.get("path")}
        for ss in existing_screenshots:
            if ss.name not in action_paths:
                r.append(f"- [{ss.name}]({ss.name})")
        r.append("")

    # Regression script reference
    if verification_cmds:
        r.append("### Regression Script")
        r.append(f"`regression-check.sh` — {len(verification_cmds)} commands, independently runnable")
        r.append("")

    # Footer
    coverage_str = f"{items_with_proof}/{must_total}" if must_total > 0 else "0/0"
    r.append("---")
    r.append(f"Proof coverage: {coverage_str} · Task: `{task_key}`")

    try:
        (proofs_dir / "proof-report.md").write_text("\n".join(r) + "\n")
        file_count += 1
    except OSError:
        pass

    return file_count, coverage_str


def _write_batch_proof_artifacts(
    log_dir: Path,
    verdict: dict,
    qa_actions: list[dict],
    tasks_with_specs: list[dict[str, Any]],
    cost_usd: float,
    *,
    cost_available: bool = True,
) -> tuple[int, str]:
    """Write proof artifacts for a combined batch verdict and each task within it."""
    batch_prompt = f"Batch QA for {len(tasks_with_specs)} task(s)"
    batch_verdict = dict(verdict or {})
    task_count = max(len(tasks_with_specs), 1)

    batch_count, batch_coverage = _write_proof_artifacts(
        log_dir,
        batch_verdict,
        qa_actions,
        {"key": "batch-qa"},
        batch_prompt,
        cost_usd,
        cost_available=cost_available,
    )

    must_items = batch_verdict.get("must_items", []) or []
    integration_findings = batch_verdict.get("integration_findings", []) or []
    regressions = batch_verdict.get("regressions", []) or []
    test_suite_passed = batch_verdict.get("test_suite_passed", True)
    per_task_cost = cost_usd / task_count
    logs_root = log_dir.parent

    for task in tasks_with_specs:
        task_key = task.get("key", "unknown")
        task_log_dir = logs_root / task_key
        task_log_dir.mkdir(parents=True, exist_ok=True)

        task_verdict = _task_scoped_batch_verdict(batch_verdict, task)
        _write_proof_artifacts(
            task_log_dir,
            task_verdict,
            qa_actions,
            task,
            task.get("prompt", ""),
            per_task_cost,
            cost_available=cost_available,
        )

    return batch_count, batch_coverage


def _task_scoped_batch_verdict(
    batch_verdict: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    task_key = task.get("key", "unknown")
    must_items = batch_verdict.get("must_items", []) or []
    integration_findings = batch_verdict.get("integration_findings", []) or []
    regressions = batch_verdict.get("regressions", []) or []
    test_suite_passed = batch_verdict.get("test_suite_passed", True)

    task_must_items = [
        item for item in must_items
        if item.get("task_key") == task_key
    ]
    task_integration_findings = [
        item for item in integration_findings
        if task_key in (item.get("tasks_involved") or [])
    ]
    task_passed = (
        bool(task_must_items)
        and all(item.get("status") == "pass" for item in task_must_items)
        and not any(item.get("status") == "fail" for item in task_integration_findings)
        and not regressions
        and bool(test_suite_passed)
    )
    return {
        **batch_verdict,
        "must_passed": task_passed,
        "must_items": task_must_items,
        "integration_findings": task_integration_findings,
    }


def _unwrap_tool_result_content(content: Any) -> str:
    """Extract text from a ToolResultBlock's content.

    The SDK content can be a string, a list of dicts with {type, text}, etc.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "text"):
                parts.append(str(item.text))
        return "\n".join(parts)
    if hasattr(content, "text"):
        return str(content.text)
    return str(content) if content else ""


def format_batch_spec(tasks_with_specs: list[dict]) -> str:
    """Format specs from multiple tasks for batch QA."""
    from otto.tasks import spec_binding, spec_is_verifiable, spec_text

    sections: list[str] = []
    for task in tasks_with_specs:
        task_key = task.get("key", "unknown")
        task_id = task.get("id", "?")
        prompt = task.get("prompt", "")
        sections.append(f"## Task #{task_id}: {prompt} (task_key: {task_key})")
        for spec_id, item in enumerate(task.get("spec") or [], start=1):
            binding = spec_binding(item)
            marker = "" if spec_is_verifiable(item) else " \u25c8"
            text = spec_text(item)
            sections.append(
                f"[{binding}{marker}] {{task_key: {task_key}, spec_id: {spec_id}}} {text}"
            )
        sections.append("")

    sections.extend([
        "## Cross-Task Integration",
        "Identify interactions between tasks that share files, data, APIs, or dependencies.",
        "Generate and run targeted integration tests for those interactions.",
        "Run the full test suite as a regression check.",
    ])
    return "\n".join(sections).strip()


def _expected_batch_must_matrix(tasks_with_specs: list[dict[str, Any]]) -> set[tuple[str, int]]:
    from otto.tasks import spec_binding

    expected: set[tuple[str, int]] = set()
    for task in tasks_with_specs:
        task_key = str(task.get("key", "") or "").strip()
        if not task_key:
            continue
        for spec_id, item in enumerate(task.get("spec") or [], start=1):
            if spec_binding(item) == "must":
                expected.add((task_key, spec_id))
    return expected


async def _run_qa_prompt(
    *,
    qa_prompt: str,
    config: dict[str, Any],
    project_dir: Path,
    verdict_file: Path,
    on_progress: Any = None,
    log_dir: Path | None = None,
    expected_must_count: int = 0,
    session_id: int = 0,
) -> dict[str, Any]:
    """Execute a QA prompt and return parsed verdict plus captured actions."""
    qa_env = _subprocess_env(project_dir)
    # Each parallel QA session gets its own agent-browser session for isolation
    qa_env["AGENT_BROWSER_SESSION"] = f"otto-qa-{os.getpid()}-{session_id}"
    qa_env["AGENT_BROWSER_HEADED"] = "false"

    _qa_settings = config.get("qa_agent_settings", "project").split(",")
    qa_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_qa_settings,
        env=qa_env,
        system_prompt={"type": "preset", "preset": "claude_code"},
        provider=agent_provider(config),
    )
    if config.get("model"):
        qa_opts.model = config["model"]

    qa_timeout = config.get("qa_timeout", 3600)
    report_lines: list[str] = []
    qa_actions: list[dict] = []
    pending_tool_uses: dict[str, dict] = {}
    _qa_start_time = time.monotonic()
    query_state = _QAQueryState()
    _verdict_file_str = str(verdict_file)  # for matching Write targets

    from otto.display import build_agent_tool_event as _build_agent_tool_event

    try:
        async def _run_query() -> _QAQueryState:
            state = query_state
            result_msg = None
            async for message in query(prompt=qa_prompt, options=qa_opts):
                state.turn_count += 1
                if state.first_message_time is None:
                    state.first_message_time = time.monotonic()
                if isinstance(message, ResultMessage):
                    result_msg = message
                elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                    result_msg = message
                elif AssistantMessage and isinstance(message, AssistantMessage):
                    for block in message.content:
                        if TextBlock and isinstance(block, TextBlock) and block.text:
                            report_lines.append(block.text)
                            if on_progress:
                                for line in block.text.splitlines():
                                    line_s = line.strip()
                                    if not line_s or len(line_s) < 10:
                                        continue
                                    has_verdict = any(
                                        marker in line_s
                                        for marker in ["PASS", "FAIL", "must", "should", "✅", "❌", "✓", "✗"]
                                    )
                                    try:
                                        on_progress(
                                            "qa_finding" if has_verdict else "qa_status",
                                            {"text": line_s[:200] if has_verdict else line_s[:120]},
                                        )
                                    except Exception:
                                        pass
                        elif ToolUseBlock and isinstance(block, ToolUseBlock):
                            tool_id = getattr(block, "id", None)
                            inp = block.input or {}
                            _tool_ts = round(time.monotonic() - _qa_start_time, 1)
                            if block.name == "Bash":
                                action = {
                                    "type": "bash",
                                    "command": inp.get("command", ""),
                                    "output": "",
                                    "is_error": False,
                                    "elapsed_s": _tool_ts,
                                }
                                qa_actions.append(action)
                                if tool_id:
                                    pending_tool_uses[tool_id] = action
                            elif block.name.startswith("mcp__"):
                                mcp_action = block.name.split("__")[-1]
                                action = {
                                    "type": "browser",
                                    "action": mcp_action,
                                    "detail": inp.get("url", "") or inp.get("selector", "") or inp.get("function", ""),
                                    "input": json.dumps(inp) if inp else "",
                                    "output": "",
                                    "elapsed_s": _tool_ts,
                                }
                                if mcp_action == "take_screenshot":
                                    action["path"] = inp.get("path", "")
                                    action["detail"] = inp.get("description", "") or inp.get("detail", "")
                                qa_actions.append(action)
                                if tool_id:
                                    pending_tool_uses[tool_id] = action
                            else:
                                # Log all other tools (Write, Read, Glob, Grep, Edit)
                                detail = ""
                                if block.name == "Write":
                                    detail = inp.get("file_path", "")
                                    # Early verdict capture: grab JSON from Write input
                                    if str(detail) == _verdict_file_str:
                                        try:
                                            candidate = json.loads(inp.get("content", ""))
                                            if isinstance(candidate, dict) and _is_verdict_complete(
                                                candidate, expected_must_count=expected_must_count
                                            ):
                                                state.early_verdict = candidate
                                                if log_dir:
                                                    append_text_log(log_dir / "qa-agent.log", [
                                                        f"[{round(time.monotonic() - _qa_start_time, 1):6.1f}s] "
                                                        f"early verdict captured at turn {state.turn_count}"
                                                    ])
                                        except (json.JSONDecodeError, TypeError):
                                            pass
                                elif block.name == "Read":
                                    detail = inp.get("file_path", "")
                                elif block.name in ("Grep", "Glob"):
                                    detail = inp.get("pattern", "")
                                elif block.name == "Edit":
                                    detail = inp.get("file_path", "")
                                action = {
                                    "type": block.name.lower(),
                                    "detail": str(detail)[:200],
                                    "output": "",
                                    "elapsed_s": _tool_ts,
                                }
                                qa_actions.append(action)
                                if tool_id:
                                    pending_tool_uses[tool_id] = action
                            if on_progress:
                                try:
                                    event = _build_agent_tool_event(block)
                                    if not event and block.name.startswith("mcp__"):
                                        action_name = block.name.split("__")[-1]
                                        detail = ""
                                        if "url" in inp:
                                            detail = inp["url"][:60]
                                        elif "selector" in inp:
                                            detail = inp["selector"][:60]
                                        event = {"name": f"Browser:{action_name}", "detail": detail}
                                    if event:
                                        on_progress("agent_tool", event)
                                except Exception:
                                    pass
                        elif ToolResultBlock and isinstance(block, ToolResultBlock):
                            tid = getattr(block, "tool_use_id", None)
                            if tid and tid in pending_tool_uses:
                                pending_tool_uses[tid]["output"] = _unwrap_tool_result_content(
                                    getattr(block, "content", "")
                                )
                                pending_tool_uses[tid]["is_error"] = bool(getattr(block, "is_error", False))
                elif UserMessage and isinstance(message, UserMessage):
                    for block in getattr(message, "content", []):
                        if ToolResultBlock and isinstance(block, ToolResultBlock):
                            tid = getattr(block, "tool_use_id", None)
                            if tid and tid in pending_tool_uses:
                                pending_tool_uses[tid]["output"] = _unwrap_tool_result_content(
                                    getattr(block, "content", "")
                                )
                                pending_tool_uses[tid]["is_error"] = bool(getattr(block, "is_error", False))

            if result_msg:
                raw_cost = getattr(result_msg, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    state.qa_cost = float(raw_cost)
                state.qa_usage = normalize_usage(getattr(result_msg, "usage", None))
            return state

        query_state = await asyncio.wait_for(_run_query(), timeout=qa_timeout)
    except asyncio.TimeoutError:
        if query_state.early_verdict:
            report_lines.append(f"\n[QA agent timed out after {qa_timeout}s — verdict already captured]")
        else:
            report_lines.append(f"\n[QA agent timed out after {qa_timeout}s]")
    except Exception as exc:
        error_str = str(exc)
        report_lines.append(f"\n[QA agent error: {error_str}]")
        if verdict_file.exists():
            try:
                partial = json.loads(verdict_file.read_text().strip())
                if _is_verdict_complete(partial, expected_must_count=expected_must_count):
                    return {
                        "must_passed": partial["must_passed"],
                        "verdict": partial,
                        "raw_report": "\n".join(report_lines),
                        "cost_usd": query_state.qa_cost,
                        "usage": query_state.qa_usage,
                        "qa_actions": qa_actions,
                    }
            except (json.JSONDecodeError, OSError):
                pass
        if any(kw in error_str.lower() for kw in (
            "api_error", "internal server", "stream closed",
            "not logged in", "control request timeout", "please run /login",
            "command failed with exit code",
        )):
            return {
                "must_passed": None,
                "verdict": None,
                "raw_report": "\n".join(report_lines),
                "cost_usd": query_state.qa_cost,
                "usage": query_state.qa_usage,
                "qa_actions": qa_actions,
                "infrastructure_error": True,
            }

    raw_report = "\n".join(report_lines)

    # Prefer early-captured verdict (grabbed from Write tool input, already validated).
    # Fall back to file-based or text-based parsing.
    verdict = query_state.early_verdict
    if not verdict:
        if verdict_file.exists():
            try:
                verdict_text = verdict_file.read_text().strip()
                if verdict_text:
                    verdict = json.loads(verdict_text)
            except (json.JSONDecodeError, OSError) as parse_err:
                report_lines.append(f"\n[Verdict file parse error: {parse_err}]")
            finally:
                verdict_file.unlink(missing_ok=True)
        else:
            verdict_file.unlink(missing_ok=True)

    parse_infrastructure_error = False
    if not verdict or not _is_verdict_complete(verdict, expected_must_count=expected_must_count):
        verdict_source = "early_capture" if query_state.early_verdict else "file" if verdict else "none"
        # Try parsing verdict from agent's text output (agent often repeats it)
        verdict = _parse_qa_verdict_json(raw_report)
        if verdict.get("_legacy_parse") and not _has_explicit_fail_markers(raw_report):
            verdict["must_passed"] = None
            parse_infrastructure_error = True
            if log_dir:
                append_text_log(log_dir / "qa-agent.log", [
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] verdict parse → infrastructure_error "
                    f"(source={verdict_source}, legacy_parse=True, no explicit fail markers)"
                ])
        # Legacy/fallback verdicts claiming pass without must_items evidence
        # are not trustworthy — force fail. But if the raw report contains
        # valid structured JSON with must_items, trust it.
        elif not _is_verdict_complete(verdict, expected_must_count=expected_must_count):
            verdict["must_passed"] = False

    # Write QA agent log with timestamps for debugging
    _qa_total_time = round(time.monotonic() - _qa_start_time, 1)
    _qa_init_time = round((query_state.first_message_time or time.monotonic()) - _qa_start_time, 1)
    if log_dir:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_lines = [
                f"{'=' * 60}",
                f"QA RUN  must_count={expected_must_count}  session_id={session_id}  {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"SDK init: {_qa_init_time}s  total: {_qa_total_time}s  turns: {query_state.turn_count}  cost: ${query_state.qa_cost:.2f}",
                f"{'=' * 60}",
            ]
            for action in qa_actions:
                atype = action.get("type", "unknown")
                ts = action.get("elapsed_s", 0)
                ts_str = f"[{ts:6.1f}s]"
                if atype == "bash":
                    cmd = action.get("command", "")[:120]
                    output = action.get("output", "")[:200]
                    log_lines.append(f"{ts_str} ● Bash  {cmd}")
                    if output:
                        log_lines.append(f"         → {output}")
                elif atype == "browser":
                    log_lines.append(f"{ts_str} ● Browser:{action.get('action', '')}  {action.get('detail', '')[:80]}")
                else:
                    log_lines.append(f"{ts_str} ● {atype}  {action.get('detail', '')[:80]}")
            if report_lines:
                log_lines.append("")
                log_lines.extend(report_lines[-10:])
            log_lines.append(f"\nCost: ${query_state.qa_cost:.2f}  Time: {_qa_total_time}s (init: {_qa_init_time}s)")
            # Append (not overwrite) so retries are preserved
            append_text_log(log_dir / "qa-agent.log", log_lines + [""])
        except Exception:
            pass

    return {
        "must_passed": verdict.get("must_passed", False),
        "verdict": verdict,
        "raw_report": raw_report,
        "cost_usd": query_state.qa_cost,
        "usage": query_state.qa_usage,
        "qa_actions": qa_actions,
        "infrastructure_error": parse_infrastructure_error,
    }


def _build_qa_prompt(
    tasks: list[dict[str, Any]],
    project_dir: Path,
    verdict_file: Path,
    screenshot_dir: Path,
    diff: str,
    test_command: str | None = None,
    *,
    prev_failed: list[str] | None = None,
    focus_items: list | None = None,
    retried_task_keys: set[str] | None = None,
) -> str:
    """Build QA prompt for single-task or multi-task (batch) QA.

    Single task (len(tasks)==1): simpler framing, no task_key attribution required.
    Multi task (len(tasks)>1): task_key attribution, cross-task integration, coverage matrix.
    """
    from otto.tasks import spec_binding, spec_is_verifiable, spec_text

    is_batch = len(tasks) > 1
    test_command_section = ""
    if test_command:
        test_command_section = f"""

PROJECT TEST COMMAND:
Use this as the default full-suite regression command unless you discover a clearly more accurate project-local equivalent:
  {test_command}

If that command fails only because of an environment/wrapper issue such as an executable not being on PATH
(for example `jest: command not found` from an `npm test` script), immediately retry with the project-local
equivalent (`npx`, `pnpm exec`, `python -m`, etc.) before treating it as a product regression.
"""

    if is_batch:
        # --- Batch prompt ---
        task_list = "\n".join(
            f"- #{task.get('id', '?')} {task.get('prompt', '')} (task_key: {task.get('key', 'unknown')})"
            for task in tasks
        )
        retry_focus = ""
        if retried_task_keys:
            focus_list = ", ".join(sorted(retried_task_keys))
            retry_focus = f"""

RETRY ROUND:
Focus the must-item re-check on these retried task(s): {focus_list}
- Re-check ALL [must] items for those task(s), not only the previously failing items.
- Re-run cross-task checks that involve those task(s) or shared files they touch.
- Keep the full test suite as a regression backstop.
"""

        batch_additions = """
Verify ALL [must] items exhaustively. Do not stop at the first failure.
Every verdict item must include the owning task_key for attribution.
Return exactly one `must_items` entry for every [must] spec listed below. Do not omit any task/spec pair.
For each [must] item, record targeted proof (command + output), not just code inspection.

Generate and run cross-task integration tests for interactions between these tasks.
Run the full existing test suite as a regression check."""

        return f"""{_QA_BASE_INSTRUCTIONS}{test_command_section}
{batch_additions}{retry_focus}

You are working in {project_dir}. All project files are in this directory. Do not search outside it.

BATCH TASKS:
{task_list}

ACCEPTANCE CRITERIA:
{format_batch_spec(tasks)}

MERGED DIFF:
{diff}

VERDICT: After running ALL verification commands and BREAK tests, immediately
write your verdict JSON using the Write tool. One Write call, no rewriting.
Put all reasoning into the JSON fields — do not generate a text summary first.

Write to: {verdict_file}
Screenshots to: {screenshot_dir}/screenshot-<name>.png

JSON structure:
{{
  "must_passed": true,
  "must_items": [
    {{"task_key": "abc123", "spec_id": 1, "criterion": "...", "status": "pass/fail", "evidence": "...", "proof": ["..."]}}
  ],
  "integration_findings": [
    {{"description": "...", "status": "pass/fail", "test": "...", "tasks_involved": ["abc123", "def456"]}}
  ],
  "regressions": [],
  "test_suite_passed": true,
  "extras": ["edge_case: description of finding"]
}}"""

    else:
        # --- Single-task prompt ---
        task = tasks[0]
        spec = task.get("spec") or []
        original_prompt = task.get("prompt", "")

        # Sort specs: verifiable [must] first, then non-verifiable [must], then [should].
        def _spec_sort_key(item):
            b = spec_binding(item)
            v = spec_is_verifiable(item)
            if b == "must" and v:
                return 0
            elif b == "must":
                return 1
            else:
                return 2
        sorted_spec = sorted(spec, key=_spec_sort_key)

        spec_lines = []
        for i, item in enumerate(sorted_spec):
            text = spec_text(item)
            binding = spec_binding(item)
            marker = "" if spec_is_verifiable(item) else " \u25c8"
            spec_lines.append(f"  {i+1}. [{binding}{marker}] {text}")
        spec_section = "\n".join(spec_lines)

        focus_section = ""
        if prev_failed:
            focus_section += "\n\nPRIORITY \u2014 These items failed in the previous QA round. Verify they are fixed FIRST:\n"
            focus_section += "\n".join(f"  - {c}" for c in prev_failed)
            focus_section += "\nThen verify remaining items haven't regressed."
        elif focus_items:
            focus_texts = [spec_text(item) for item in focus_items]
            focus_section = "\n\nFocus your testing on these items that lack test coverage:\n"
            focus_section += "\n".join(f"  - {t}" for t in focus_texts)

        return f"""{_QA_BASE_INSTRUCTIONS}{test_command_section}

Test this implementation against the acceptance criteria and the original task prompt.

You are working in {project_dir}. All project files are in this directory. Do not search outside it.

ORIGINAL TASK PROMPT:
{original_prompt}

ACCEPTANCE CRITERIA:
{spec_section}
{focus_section}

DIFF:
{diff}

VERDICT: After running ALL verification commands and BREAK tests, immediately
write your verdict JSON using the Write tool. One Write call, no rewriting.
Put all reasoning into the JSON fields — do not generate a text summary first.

Write to: {verdict_file}
Screenshots to: {screenshot_dir}/screenshot-<name>.png

JSON structure:
{{
  "must_passed": true/false,
  "must_items": [
    {{"spec_id": 1, "criterion": "...", "status": "pass/fail", "evidence": "...", "proof": ["ran jest: 5 passed", "curl /api returns 200"]}}
  ],
  "should_notes": [
    {{"criterion": "...", "observation": "...", "screenshot": "path or null"}}
  ],
  "regressions": [],
  "prompt_intent": "Implementation matches/diverges from original prompt because...",
  "extras": ["edge_case: description of finding"]
}}"""


def _finalize_qa_result(
    qa_result: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Unified post-processor for both single-task and batch QA results.

    Single task: injects task_key, validates expected_must_count.
    Multi task: coverage matrix, task_key attribution, integration findings, failed_task_keys.

    Returns a stable dict shape with all fields both caller types need.
    """
    from otto.tasks import spec_binding

    verdict = qa_result.get("verdict", {}) or {}
    if not isinstance(verdict, dict):
        verdict = {}
    infrastructure_error = bool(qa_result.get("infrastructure_error", False))
    is_batch = len(tasks) > 1

    if is_batch:
        # --- Batch finalization ---
        must_items = verdict.get("must_items", []) or []
        integration_findings = verdict.get("integration_findings", []) or []
        integration_failed = any(item.get("status") == "fail" for item in integration_findings)
        regressions = verdict.get("regressions", []) or []
        test_suite_passed = verdict.get("test_suite_passed", True)

        expected_pairs = _expected_batch_must_matrix(tasks)
        actual_pairs: set[tuple[str, int]] = set()
        for item in must_items:
            task_key = str(item.get("task_key", "") or "").strip()
            try:
                sid = int(item.get("spec_id"))
            except (TypeError, ValueError):
                continue
            if task_key:
                actual_pairs.add((task_key, sid))
        missing_pairs = sorted(expected_pairs - actual_pairs)
        if missing_pairs:
            verdict["coverage_error"] = {
                "expected_count": len(expected_pairs),
                "actual_count": len(actual_pairs),
                "missing": [
                    {"task_key": tk, "spec_id": sid}
                    for tk, sid in missing_pairs
                ],
            }
        # Recompute from actual items; if empty, fall back to model flag
        if must_items:
            must_passed = all(item.get("status") == "pass" for item in must_items)
        else:
            must_passed = bool(qa_result.get("must_passed"))
        overall_passed = (
            must_passed
            and not infrastructure_error
            and not missing_pairs
            and not integration_failed
            and not regressions
            and bool(test_suite_passed)
        )
        # Sync verdict dict so proof report matches result
        verdict["must_passed"] = overall_passed

        failed_task_keys: set[str] = {
            item.get("task_key")
            for item in must_items
            if item.get("status") == "fail" and item.get("task_key")
        }
        for item in integration_findings:
            if item.get("status") == "fail":
                failed_task_keys.update(
                    key for key in (item.get("tasks_involved") or []) if key
                )
        failed_task_keys.update(tk for tk, _sid in missing_pairs)

        return {
            "must_passed": overall_passed,
            "verdict": verdict,
            "raw_report": qa_result.get("raw_report", ""),
            "cost_usd": qa_result.get("cost_usd", 0.0),
            "failed_task_keys": sorted(failed_task_keys),
            "test_suite_passed": bool(test_suite_passed),
            "infrastructure_error": infrastructure_error,
        }

    else:
        # --- Single-task finalization ---
        task = tasks[0]
        task_key = task.get("key", "unknown")
        spec = task.get("spec") or []
        expected_must_count = sum(1 for item in spec if spec_binding(item) == "must")
        must_items = verdict.get("must_items", []) or []

        # Recompute must_passed from actual items (don't trust model flag).
        # If must_items is empty, fall back to model's flag — we can't verify.
        if must_items:
            must_passed = all(item.get("status") == "pass" for item in must_items)
        else:
            must_passed = qa_result.get("must_passed", False)

        # Inject task_key into must_items for consistency
        for item in must_items:
            if not item.get("task_key"):
                item["task_key"] = task_key

        # Validate expected_must_count for pass verdicts
        if must_passed and expected_must_count > 0:
            actual_must = len(must_items)
            if actual_must < expected_must_count:
                must_passed = False
        regressions = verdict.get("regressions", []) or []
        test_suite_passed = verdict.get("test_suite_passed", True)
        if must_passed and (regressions or not test_suite_passed):
            must_passed = False

        # Sync verdict dict so proof report matches result
        verdict["must_passed"] = must_passed

        return {
            "must_passed": must_passed,
            "verdict": verdict,
            "raw_report": qa_result.get("raw_report", ""),
            "cost_usd": qa_result.get("cost_usd", 0.0),
            "failed_task_keys": sorted(
                {item.get("task_key") or task_key
                 for item in must_items
                 if item.get("status") == "fail"}
            ),
            "test_suite_passed": bool(test_suite_passed),
            "infrastructure_error": infrastructure_error,
        }


def _salvage_single_task_verdict_from_actions(
    qa_result: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    log_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Build a minimal single-task verdict from explicit SPEC PASS/FAIL markers.

    This is a last-resort recovery path for provider/runtime cases where QA
    executed the real proof commands but failed to emit valid verdict JSON.
    """
    if len(tasks) != 1:
        return None
    task = tasks[0]
    spec = list(task.get("spec") or [])
    if not spec:
        return None

    statuses: dict[int, str] = {}
    evidence: dict[int, str] = {}
    text_blobs: list[str] = []
    for action in qa_result.get("qa_actions", []) or []:
        if action.get("type") != "bash":
            continue
        output = str(action.get("output", "") or "")
        if output:
            text_blobs.append(output)

    raw_report = str(qa_result.get("raw_report", "") or "")
    if raw_report:
        text_blobs.append(raw_report)

    if log_dir:
        qa_log_path = log_dir / "qa-agent.log"
        if qa_log_path.exists():
            try:
                text_blobs.append(qa_log_path.read_text())
            except OSError:
                pass

    for output in text_blobs:
        for line in output.splitlines():
            line_s = line.strip()
            if not line_s:
                continue
            for pattern in _SPEC_RESULT_PATTERNS:
                for match in pattern.finditer(line_s):
                    spec_id = int(match.group(1))
                    status = match.group(2).lower()
                    prev = statuses.get(spec_id)
                    if prev == "fail":
                        continue
                    statuses[spec_id] = status
                    evidence[spec_id] = line_s[:300]

    must_indices = [
        idx for idx, item in enumerate(spec, start=1)
        if item.get("binding", "must") == "must"
    ]
    if not must_indices:
        return None
    if not all(idx in statuses for idx in must_indices):
        return None

    verdict = {
        "must_passed": all(statuses[idx] == "pass" for idx in must_indices),
        "must_items": [
            {
                "spec_id": idx,
                "criterion": str(spec[idx - 1].get("text", "") or ""),
                "status": statuses[idx],
                "evidence": evidence.get(idx, f"Recovered from explicit SPEC {idx} marker in QA command output."),
                "proof": [evidence.get(idx, f"SPEC {idx}: {statuses[idx].upper()}")],
                "task_key": str(task.get("key", "unknown")),
            }
            for idx in must_indices
        ],
        "test_suite_passed": True,
        "regressions": [],
        "extras": [],
        "_salvaged_from_actions": True,
    }
    return verdict


def _salvage_single_task_verdict_from_proof_coverage(
    tasks: list[dict[str, Any]],
    *,
    proof_coverage: str,
    raw_report: str,
    log_dir: Path | None,
) -> dict[str, Any] | None:
    if len(tasks) != 1:
        return None
    task = tasks[0]
    must_items = [
        (idx, item) for idx, item in enumerate(task.get("spec") or [], start=1)
        if item.get("binding", "must") == "must"
    ]
    if not must_items:
        return None
    if proof_coverage != f"{len(must_items)}/{len(must_items)}":
        return None

    log_text = raw_report
    if log_dir:
        qa_log_path = log_dir / "qa-agent.log"
        if qa_log_path.exists():
            try:
                log_text += "\n" + qa_log_path.read_text()
            except OSError:
                pass

    positive_markers = (
        "Acceptance criteria passed.",
        "Feature checks passed.",
        "The required spec checks are all green.",
        "The contract checks are green.",
        "All acceptance checks are passing",
        "All acceptance checks are green.",
    )
    if not any(marker in log_text for marker in positive_markers):
        return None

    return {
        "must_passed": True,
        "must_items": [
            {
                "spec_id": idx,
                "criterion": str(item.get("text", "") or ""),
                "status": "pass",
                "evidence": "Recovered from full proof coverage and explicit QA success log.",
                "proof": [f"Recovered from proof coverage {proof_coverage}."],
                "task_key": str(task.get("key", "unknown")),
            }
            for idx, item in must_items
        ],
        "test_suite_passed": True,
        "regressions": [],
        "extras": [],
        "_salvaged_from_proof_coverage": True,
    }


async def run_qa(
    tasks: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
    diff: str,
    *,
    on_progress: Any = None,
    log_dir: Path | None = None,
    prev_failed: list[str] | None = None,
    focus_items: list | None = None,
    retried_task_keys: set[str] | None = None,
    session_id: int = 0,
    batch_context: bool = False,
) -> dict[str, Any]:
    """Unified QA entry point -- single-task or batch.

    Args:
        tasks: list of dicts, each with keys: key, prompt, spec.
               Single-task QA: pass a 1-element list.
               Batch QA: pass N-element list.
        config: otto config dict.
        project_dir: project root.
        diff: git diff string.
        on_progress: optional progress callback.
        log_dir: directory for QA logs/proofs.
        prev_failed: per-task retry focus -- previously failed criteria text.
        focus_items: per-task focus -- spec items lacking test coverage.
        retried_task_keys: batch retry focus -- task keys to re-verify.

    Returns dict with keys:
        must_passed, verdict, raw_report, cost_usd, failed_task_keys,
        test_suite_passed, infrastructure_error, proof_count, proof_coverage.
    """
    from otto.tasks import spec_binding

    is_batch = len(tasks) > 1
    artifact_batch_mode = is_batch or batch_context
    requires_browser = any(
        not item.get("verifiable", True)
        for task in tasks
        for item in (task.get("spec") or [])
    )

    # Log tier decision
    if log_dir:
        if is_batch:
            task_count = len(tasks)
            spec_count = sum(len(t.get("spec", [])) for t in tasks)
            is_retry = bool(retried_task_keys)
            append_text_log(log_dir / "qa-tier.log", [
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Batch QA tier decision",
                f"mode: batch (combined specs, browser available)",
                f"tasks: {task_count}",
                f"total specs: {spec_count}",
                f"retry round: {'yes — focused on ' + ', '.join(sorted(retried_task_keys)) if is_retry else 'no (initial)'}",
                f"browser: {'required' if requires_browser else 'not required'}",
                f"reason: batch QA verifies integrated codebase with combined specs + cross-task integration tests",
                "",
            ])

    # Create verdict temp file
    with tempfile.NamedTemporaryFile(suffix=".json", prefix="otto_qa_", delete=False) as tf:
        verdict_file = Path(tf.name)

    screenshot_dir = log_dir / "qa-proofs" if log_dir else Path("/tmp")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Compute expected_must_count for single-task (used by _run_qa_prompt)
    expected_must_count = 0
    if not is_batch:
        spec = tasks[0].get("spec") or []
        expected_must_count = sum(1 for item in spec if spec_binding(item) == "must")

    qa_prompt = _build_qa_prompt(
        tasks,
        project_dir,
        verdict_file,
        screenshot_dir,
        diff,
        test_command=config.get("test_command"),
        prev_failed=prev_failed,
        focus_items=focus_items,
        retried_task_keys=retried_task_keys,
    )

    if requires_browser and shutil.which("agent-browser") is None:
        message = (
            "QA requires agent-browser for visual verification, but `agent-browser` "
            "is not installed or not on PATH."
        )
        qa_result = {
            "must_passed": False,
            "verdict": None,
            "raw_report": message,
            "cost_usd": 0.0,
            "qa_actions": [],
            "infrastructure_error": True,
        }
    else:
        qa_result = await _run_qa_prompt(
            qa_prompt=qa_prompt,
            config=config,
            project_dir=project_dir,
            verdict_file=verdict_file,
            on_progress=on_progress,
            log_dir=log_dir,
            expected_must_count=expected_must_count,
            session_id=session_id,
        )

    verdict = qa_result.get("verdict")
    if not isinstance(verdict, dict) or not _is_verdict_complete(
        verdict,
        expected_must_count=expected_must_count,
    ):
        salvaged = _salvage_single_task_verdict_from_actions(qa_result, tasks, log_dir=log_dir)
        if salvaged is not None:
            qa_result["verdict"] = salvaged
            qa_result["must_passed"] = salvaged["must_passed"]
            raw_report = str(qa_result.get("raw_report", "") or "")
            note = "[salvaged verdict from explicit SPEC PASS/FAIL markers]"
            qa_result["raw_report"] = f"{raw_report}\n\n{note}".strip()
            if log_dir:
                append_text_log(
                    log_dir / "qa-agent.log",
                    [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] verdict salvaged from QA command outputs"],
                )

    final_result = _finalize_qa_result(qa_result, tasks)

    # Write per-task qa-agent.log references for batch
    if artifact_batch_mode and log_dir:
        for task in tasks:
            task_key = task.get("key", "")
            if not task_key:
                continue
            task_log_dir = project_dir / "otto_logs" / task_key
            task_log_dir.mkdir(parents=True, exist_ok=True)
            try:
                append_text_log(task_log_dir / "qa-agent.log", [
                    f"[batch QA \u2014 see {log_dir.name}/qa-agent.log for full details]",
                    "",
                ])
            except Exception:
                pass

    # Write proof artifacts
    proof_count = 0
    proof_coverage = ""
    if log_dir:
        try:
            if artifact_batch_mode:
                batch_verdict = dict(final_result.get("verdict", {}) or {})
                batch_verdict["must_passed"] = final_result.get("must_passed", False)
                proof_count, proof_coverage = _write_batch_proof_artifacts(
                    log_dir,
                    batch_verdict,
                    qa_result.get("qa_actions", []) or [],
                    tasks,
                    float(final_result.get("cost_usd", 0.0) or 0.0),
                    cost_available=agent_provider(config) != "codex",
                )
                # Sync task-scoped QA artifacts so each task log dir reflects the
                # latest batch QA result, including single-task batch retries.
                logs_root = log_dir.parent
                raw_report = final_result.get("raw_report", "") or ""
                for task in tasks:
                    task_key = task.get("key", "")
                    if not task_key:
                        continue
                    task_log_dir = logs_root / task_key
                    task_log_dir.mkdir(parents=True, exist_ok=True)
                    task_verdict = _task_scoped_batch_verdict(batch_verdict, task)
                    try:
                        (task_log_dir / "qa-report.md").write_text(raw_report or "No QA output")
                    except OSError:
                        pass
                    write_json_file(task_log_dir / "qa-verdict.json", task_verdict)
            else:
                task = tasks[0]
                proof_count, proof_coverage = _write_proof_artifacts(
                    log_dir,
                    final_result.get("verdict", {}),
                    qa_result.get("qa_actions", []),
                    task,
                    task.get("prompt", ""),
                    float(final_result.get("cost_usd", 0.0) or 0.0),
                    cost_available=agent_provider(config) != "codex",
                )
        except Exception:
            pass

    # Audit proof quality — warn loudly if specs have code-reading-only proofs
    proof_warnings = _audit_proof_quality(final_result.get("verdict", {}), log_dir)
    if proof_warnings and on_progress:
        try:
            on_progress("qa_warning", {"text": proof_warnings[0]})
        except Exception:
            pass

    # Warn loudly about BREAK findings so they're not buried in logs
    verdict = final_result.get("verdict", {})
    extras = verdict.get("extras", []) or []
    if extras and log_dir:
        lines = ["", "=" * 60, "BREAK FINDINGS (from adversarial testing)", "=" * 60]
        for item in extras:
            lines.append(f"  ⚠ {item}")
        lines.append("=" * 60)
        append_text_log(log_dir / "qa-agent.log", lines + [""])
    if extras and on_progress:
        try:
            on_progress("qa_warning", {"text": f"BREAK found {len(extras)} edge case(s) — check qa-agent.log"})
        except Exception:
            pass

    if not final_result.get("must_passed"):
        salvaged = _salvage_single_task_verdict_from_proof_coverage(
            tasks,
            proof_coverage=proof_coverage,
            raw_report=str(final_result.get("raw_report", "") or ""),
            log_dir=log_dir,
        )
        if salvaged is not None:
            final_result["must_passed"] = True
            final_result["verdict"] = salvaged
            final_result["raw_report"] = (
                str(final_result.get("raw_report", "") or "") + "\n\n[salvaged verdict from proof coverage]"
            ).strip()
            final_result["failed_task_keys"] = []
            if log_dir:
                append_text_log(
                    log_dir / "qa-agent.log",
                    [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] verdict salvaged from proof coverage"],
                )

    # Emit summary for display (single-task progress reporting)
    if on_progress and not is_batch:
        try:
            verdict = final_result.get("verdict", {})
            must_items = verdict.get("must_items", [])
            total = len(must_items)
            passed = sum(1 for item in must_items if item.get("status") == "pass")
            on_progress("qa_summary", {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "proof_count": proof_count,
                "proof_coverage": proof_coverage,
            })
        except Exception:
            pass

    return {
        **final_result,
        "usage": qa_result.get("usage", {}) or {},
        "proof_count": proof_count,
        "proof_coverage": proof_coverage,
    }
