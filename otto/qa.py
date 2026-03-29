"""Otto QA — adversarial QA agent, verdict parsing, risk-based tiering."""

import asyncio
import json
import re
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
    query,
)
from otto.observability import append_text_log
from otto.theme import console

# UserMessage may not exist in all SDK versions — used for tool-result capture
try:
    from claude_agent_sdk.types import UserMessage  # noqa: F401
except (ImportError, AttributeError):
    UserMessage = None  # type: ignore[assignment,misc]


QA_SYSTEM_PROMPT_V45 = """\
You are an adversarial QA tester. Test the implementation against
the acceptance criteria and the original task prompt.

Testing order — test in the order listed:
1. Verifiable [must] items FIRST — use code inspection, scripts, curl, unit checks.
   If ANY [must] item fails, write the verdict immediately. Do not proceed to
   browser testing or [should] items.
2. Non-verifiable [must ◈] items — these are subjective (visual, UX, wording).
   For visual/layout/styling items: start a dev server, navigate to the page,
   and verify in the browser. Code inspection alone cannot confirm appearance.
   For non-visual subjective items: use your best judgment with evidence.
3. [should] items — note observations, do not block merge.

Items marked ◈ cannot be verified by code alone. Visual items MUST use browser.
Run the full existing test suite once for broad regression coverage.

Then for each [must] item you verify, record at least one targeted proof
tied to that spec_id. Prefer a deterministic targeted command when available
(single test file/test case, curl, node -e script). If no targeted command
exists, record the best direct proof available and explain why.
A single targeted command may satisfy multiple [must] items if it directly
verifies them. If a blocking [must] fails, you may stop after recording
proof for the checked items.

When taking browser screenshots, ALWAYS save to a file path:
  take_screenshot(filePath="<proof_dir>/screenshot-<name>.png")
Do NOT take screenshots without filePath — inline screenshots break the message pipe.

Also check:
- Does the implementation contradict the ORIGINAL task prompt?
- Does it break existing functionality?

For each [must] item, include spec_id (matching the criterion number) and
a "proof" array — short strings describing what you did to verify.
Only cite proof that directly verifies THAT criterion, not exploration.

Write your verdict to the output file as JSON:
{
  "must_passed": true/false,
  "must_items": [
    {"spec_id": 1, "criterion": "...", "status": "pass/fail", "evidence": "...", "proof": ["ran jest: 5 passed", "curl /api returns 200"]}
  ],
  "should_notes": [
    {"criterion": "...", "observation": "...", "screenshot": "path or null"}
  ],
  "regressions": [],
  "prompt_intent": "Implementation matches/diverges from original prompt because...",
  "extras": ["Agent added contributing factor explanations — improves UX"]
}

Kill any servers you started (by PID, not pkill)."""


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
    """Determine QA tier based on residual risk after verification.

    Tier 0: skip QA (all [must] items have tests, local change, first attempt)
    Tier 1: targeted QA (unmapped [must] items, cross-cutting changes)
    Tier 2: full QA with browser (visual/SPA, auth/crypto, retries)
    """
    from otto.tasks import spec_binding, spec_is_verifiable

    diff_files = [str(path) for path in (diff_info.get("files") or [])]
    spec_test_mapping = spec_test_mapping or {}
    log_lines = [
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] QA tier decision for {task.get('key', 'unknown')}",
        f"attempt: {attempt}",
        f"changed files: {len(diff_files)}",
    ]

    # Tier 2: high-risk domains
    HIGH_RISK_PATTERNS = ["auth", "crypto", "permission", "migration",
                          "payment", "security", "token", "session"]
    matched_risk_patterns = sorted({
        pattern
        for f in diff_files
        for pattern in HIGH_RISK_PATTERNS
        if pattern in f.lower()
    })
    log_lines.append(
        "risk patterns: "
        + (", ".join(matched_risk_patterns) if matched_risk_patterns else "none matched")
    )
    if matched_risk_patterns:
        log_lines.append("visual specs: not evaluated (tier 2 already required)")
        log_lines.append("spa detection: not evaluated (tier 2 already required)")
        log_lines.append("test mapping: not evaluated (tier 2 already required)")
        log_lines.append("final tier: 2")
        if log_dir:
            append_text_log(log_dir / "qa-tier.log", log_lines + [""])
        return 2

    # Tier 2: non-verifiable [must] items require browser/subjective QA.
    non_verifiable_must = any(
        spec_binding(item) == "must" and not spec_is_verifiable(item)
        for item in spec
    )
    log_lines.append(f"non-verifiable [must]: {'yes' if non_verifiable_must else 'no'}")
    if non_verifiable_must:
        log_lines.append("visual specs: required by non-verifiable [must]")
        log_lines.append("spa detection: not evaluated (tier 2 already required)")
        log_lines.append("test mapping: not evaluated (tier 2 already required)")
        log_lines.append("final tier: 2")
        if log_dir:
            append_text_log(log_dir / "qa-tier.log", log_lines + [""])
        return 2

    # Tier 2: visual/UI specs (need browser), SPA apps, or retries
    from otto.tasks import spec_text
    has_visual = any(
        spec_binding(item) == "should"
        and any(kw in spec_text(item).lower()
                for kw in ("ui", "layout", "style", "visual", "responsive"))
        for item in spec
    )
    # Only count SOURCE files as SPA indicators, not test files
    is_spa = any(
        f.endswith((".jsx", ".tsx", ".vue", ".svelte"))
        and not any(seg in f.lower() for seg in ("test", "__tests__", "spec", ".test.", ".spec."))
        for f in diff_files
    )
    log_lines.append(f"visual specs detected: {'yes' if has_visual else 'no'}")
    log_lines.append(f"spa detection: {'yes' if is_spa else 'no'}")
    log_lines.append(f"retry escalation: {'yes' if attempt > 0 else 'no'}")
    if has_visual or is_spa or attempt > 0:
        log_lines.append("test mapping: not evaluated (tier 2 already required)")
        log_lines.append("final tier: 2")
        if log_dir:
            append_text_log(log_dir / "qa-tier.log", log_lines + [""])
        return 2

    # Tier 1: unmapped [must] items or cross-cutting changes
    unmapped = [item for item in spec
                if spec_binding(item) == "must"
                and not spec_test_mapping.get(spec_text(item))]
    log_lines.append(
        "unmapped [must] items: "
        + (
            ", ".join(spec_text(item)[:80] for item in unmapped[:3])
            if unmapped else
            "none"
        )
    )
    log_lines.append(f"cross-cutting diff: {'yes' if len(diff_files) > 5 else 'no'}")
    if unmapped or len(diff_files) > 5:
        log_lines.append("final tier: 1")
        if log_dir:
            append_text_log(log_dir / "qa-tier.log", log_lines + [""])
        return 1

    # Tier 0: every [must] item has a test, local change, first attempt
    log_lines.append("final tier: 0")
    if log_dir:
        append_text_log(log_dir / "qa-tier.log", log_lines + [""])
    return 0


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


def _write_proof_artifacts(
    log_dir: Path,
    verdict: dict,
    qa_actions: list[dict],
    task: dict,
    original_prompt: str,
    cost_usd: float,
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
    r.append(f"**{result_icon}** — {must_passed_count}/{must_total} must items — QA ${cost_usd:.2f}")
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

        task_must_items = [
            item for item in must_items
            if item.get("task_key") == task_key
        ]
        task_integration_findings = [
            item for item in integration_findings
            if task_key in (item.get("tasks_involved") or [])
        ]
        task_passed = (
            all(item.get("status") == "pass" for item in task_must_items)
            and not any(item.get("status") == "fail" for item in task_integration_findings)
            and not regressions
            and bool(test_suite_passed)
        )
        task_verdict = {
            **batch_verdict,
            "must_passed": task_passed,
            "must_items": task_must_items,
            "integration_findings": task_integration_findings,
        }
        _write_proof_artifacts(
            task_log_dir,
            task_verdict,
            qa_actions,
            task,
            task.get("prompt", ""),
            per_task_cost,
        )

    return batch_count, batch_coverage


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


def _build_qa_mcp_servers(enable_browser: bool) -> dict[str, dict]:
    """Load browser MCP configuration when QA may need browser verification."""
    if not enable_browser:
        return {}

    qa_mcp_servers = {}
    user_claude_json = Path.home() / ".claude.json"
    if not user_claude_json.exists():
        return qa_mcp_servers

    try:
        user_config = json.loads(user_claude_json.read_text())
        for name, srv in user_config.get("mcpServers", {}).items():
            if name != "chrome-devtools":
                continue
            srv = dict(srv)
            args = list(srv.get("args", []))
            if "--headless" not in args:
                args.append("--headless")
            if not any(a.startswith("--viewport") for a in args):
                args.extend(["--viewport", "1280x720"])
            if not any(a.startswith("--userDataDir") for a in args):
                otto_chrome_profile = str(Path.home() / ".cache" / "otto" / "chrome-profile")
                args.extend(["--userDataDir", otto_chrome_profile])
            srv["args"] = args
            qa_mcp_servers[name] = srv
    except Exception:
        return {}

    return qa_mcp_servers


async def _run_qa_prompt(
    *,
    qa_prompt: str,
    config: dict[str, Any],
    project_dir: Path,
    verdict_file: Path,
    on_progress: Any = None,
    enable_browser: bool = False,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute a QA prompt and return parsed verdict plus captured actions."""
    qa_mcp_servers = _build_qa_mcp_servers(enable_browser)

    _qa_settings = config.get("qa_agent_settings", "project").split(",")
    qa_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_qa_settings,
        env=_subprocess_env(),
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    if qa_mcp_servers:
        qa_opts.mcp_servers = qa_mcp_servers
    if config.get("model"):
        qa_opts.model = config["model"]

    qa_timeout = config.get("qa_timeout", 3600)
    report_lines: list[str] = []
    qa_cost = 0.0
    qa_actions: list[dict] = []
    pending_tool_uses: dict[str, dict] = {}

    from otto.display import build_agent_tool_event as _build_agent_tool_event

    try:
        async def _run_query():
            nonlocal qa_cost
            result_msg = None
            async for message in query(prompt=qa_prompt, options=qa_opts):
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
                            if block.name == "Bash":
                                action = {
                                    "type": "bash",
                                    "command": inp.get("command", ""),
                                    "output": "",
                                    "is_error": False,
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
                                }
                                if mcp_action == "take_screenshot":
                                    action["path"] = inp.get("path", "")
                                    action["detail"] = inp.get("description", "") or inp.get("detail", "")
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
                    qa_cost = float(raw_cost)

        await asyncio.wait_for(_run_query(), timeout=qa_timeout)
    except asyncio.TimeoutError:
        report_lines.append(f"\n[QA agent timed out after {qa_timeout}s]")
    except Exception as exc:
        error_str = str(exc)
        report_lines.append(f"\n[QA agent error: {error_str}]")
        if verdict_file.exists():
            try:
                partial = json.loads(verdict_file.read_text().strip())
                if "must_passed" in partial:
                    return {
                        "must_passed": partial["must_passed"],
                        "verdict": partial,
                        "raw_report": "\n".join(report_lines),
                        "cost_usd": qa_cost,
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
                "cost_usd": qa_cost,
                "qa_actions": qa_actions,
                "infrastructure_error": True,
            }

    raw_report = "\n".join(report_lines)
    verdict = None
    if verdict_file.exists():
        try:
            verdict_text = verdict_file.read_text().strip()
            if verdict_text:
                verdict = json.loads(verdict_text)
        except (json.JSONDecodeError, OSError):
            pass
        finally:
            verdict_file.unlink(missing_ok=True)
    else:
        verdict_file.unlink(missing_ok=True)

    if not verdict or "must_passed" not in verdict:
        verdict = _parse_qa_verdict_json(raw_report)

    # Write QA agent log for debugging (what tools were called, what the agent said)
    if log_dir:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_lines = []
            for action in qa_actions:
                atype = action.get("type", "unknown")
                if atype == "bash":
                    cmd = action.get("command", "")[:120]
                    output = action.get("output", "")[:200]
                    log_lines.append(f"● Bash  {cmd}")
                    if output:
                        log_lines.append(f"  → {output}")
                elif atype == "browser":
                    log_lines.append(f"● Browser:{action.get('action', '')}  {action.get('detail', '')[:80]}")
                else:
                    log_lines.append(f"● {atype}  {action.get('detail', '')[:80]}")
            if report_lines:
                log_lines.append("")
                log_lines.extend(report_lines[-10:])  # last 10 lines of agent text
            log_lines.append(f"\nCost: ${qa_cost:.2f}")
            (log_dir / "qa-agent.log").write_text("\n".join(log_lines))
        except Exception:
            pass

    return {
        "must_passed": verdict.get("must_passed", False),
        "verdict": verdict,
        "raw_report": raw_report,
        "cost_usd": qa_cost,
        "qa_actions": qa_actions,
    }


async def run_qa_agent_v45(
    task: dict[str, Any],
    spec: list,
    config: dict[str, Any],
    project_dir: Path,
    original_prompt: str,
    diff: str,
    tier: int = 1,
    focus_items: list | None = None,
    prev_failed: list[str] | None = None,
    on_progress: Any = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """v4.5 QA agent — structured JSON verdict, risk-based tiering.

    Returns {must_passed, verdict, raw_report, cost_usd, proof_count, proof_coverage}.
    """
    from otto.tasks import spec_text, spec_binding, spec_is_verifiable

    # Sort specs: verifiable [must] first, then non-verifiable [must], then [should].
    # QA tests in this order and fails fast on [must] failures.
    def _spec_sort_key(item):
        b = spec_binding(item)
        v = spec_is_verifiable(item)
        if b == "must" and v:
            return 0  # verifiable must — test first
        elif b == "must":
            return 1  # non-verifiable must — test second
        else:
            return 2  # should — test last
    sorted_spec = sorted(spec, key=_spec_sort_key)

    # Build spec section with binding levels and verifiability
    spec_lines = []
    for i, item in enumerate(sorted_spec):
        text = spec_text(item)
        binding = spec_binding(item)
        marker = "" if spec_is_verifiable(item) else " \u25c8"
        spec_lines.append(f"  {i+1}. [{binding}{marker}] {text}")
    spec_section = "\n".join(spec_lines)

    # Build focus section for targeted QA
    focus_section = ""
    if prev_failed:
        focus_section += "\n\nPRIORITY — These items failed in the previous QA round. Verify they are fixed FIRST:\n"
        focus_section += "\n".join(f"  - {c}" for c in prev_failed)
        focus_section += "\nThen verify remaining items haven't regressed."
    elif focus_items:
        focus_texts = [spec_text(item) for item in focus_items]
        focus_section = "\n\nFocus your testing on these items that lack test coverage:\n"
        focus_section += "\n".join(f"  - {t}" for t in focus_texts)

    # Create a temp file for the verdict
    with tempfile.NamedTemporaryFile(suffix=".json", prefix="otto_qa_", delete=False) as tf:
        verdict_file = Path(tf.name)

    qa_prompt = f"""{QA_SYSTEM_PROMPT_V45}

Test this implementation against the acceptance criteria and the original task prompt.

You are working in {project_dir}. All project files are in this directory. Do not search outside it.

ORIGINAL TASK PROMPT:
{original_prompt}

ACCEPTANCE CRITERIA:
{spec_section}
{focus_section}

DIFF:
{diff}

Write your JSON verdict to: {verdict_file}
Save any browser screenshots to: {log_dir / "qa-proofs" if log_dir else "/tmp"}/screenshot-<name>.png
"""

    # Ensure qa-proofs dir exists before QA so agent can save screenshots there
    if log_dir:
        (log_dir / "qa-proofs").mkdir(parents=True, exist_ok=True)
    # _run_qa_prompt keeps CC defaults via system_prompt={"type": "preset", "preset": "claude_code"}.
    qa_result = await _run_qa_prompt(
        qa_prompt=qa_prompt,
        config=config,
        project_dir=project_dir,
        verdict_file=verdict_file,
        on_progress=on_progress,
        enable_browser=tier >= 2,
        log_dir=log_dir,
    )
    must_passed = qa_result.get("must_passed", False)
    verdict = qa_result.get("verdict", {})
    raw_report = qa_result.get("raw_report", "")
    qa_cost = qa_result.get("cost_usd", 0.0)
    qa_actions = qa_result.get("qa_actions", [])

    # Write proof artifacts if log_dir is provided
    proof_count = 0
    proof_coverage = ""
    if log_dir:
        try:
            proof_count, proof_coverage = _write_proof_artifacts(
                log_dir, verdict, qa_actions, task,
                original_prompt, qa_cost,
            )
        except Exception:
            pass

    # Emit summary for display
    if on_progress:
        try:
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
        "must_passed": must_passed,
        "verdict": verdict,
        "raw_report": raw_report,
        "cost_usd": qa_cost,
        "proof_count": proof_count,
        "proof_coverage": proof_coverage,
        "infrastructure_error": qa_result.get("infrastructure_error", False),
    }


async def run_batch_qa_agent(
    tasks_with_specs: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
    *,
    diff: str,
    on_progress: Any = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Run exhaustive QA over a merged batch."""
    return await _run_batch_qa_agent(
        tasks_with_specs,
        config,
        project_dir,
        diff=diff,
        on_progress=on_progress,
        log_dir=log_dir,
    )


async def run_targeted_batch_qa_agent(
    tasks_with_specs: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
    *,
    diff: str,
    retried_task_keys: set[str],
    on_progress: Any = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Run a second-round batch QA focused on retried tasks."""
    return await _run_batch_qa_agent(
        tasks_with_specs,
        config,
        project_dir,
        diff=diff,
        retried_task_keys=retried_task_keys,
        on_progress=on_progress,
        log_dir=log_dir,
    )


def _build_batch_qa_prompt(
    tasks_with_specs: list[dict[str, Any]],
    project_dir: Path,
    verdict_file: Path,
    screenshot_dir: Path,
    diff: str,
    *,
    retried_task_keys: set[str] | None = None,
) -> str:
    task_list = "\n".join(
        f"- #{task.get('id', '?')} {task.get('prompt', '')} (task_key: {task.get('key', 'unknown')})"
        for task in tasks_with_specs
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

    return f"""You are an adversarial QA tester. Test the integrated result of multiple tasks
against the acceptance criteria and original task prompts.

Testing methodology — use code inspection, scripts, curl, and unit checks:
1. Verifiable [must] items FIRST — run targeted commands to verify each item.
   Prefer deterministic targeted commands (single test, curl, node -e script).
   Do NOT rely on code reading alone — run the code and verify actual behavior.
   For API endpoints: start the server and make actual HTTP requests.
   For data isolation: test with multiple users/accounts to verify boundaries.
   For auth: test both authorized and unauthorized access paths.
2. Non-verifiable [must ◈] items — start a dev server, navigate, verify in browser.
3. [should] items — note observations, do not block.

Verify ALL [must] items exhaustively. Do not stop at the first failure.
Every verdict item must include the owning task_key for attribution.
Return exactly one `must_items` entry for every [must] spec listed below. Do not omit any task/spec pair.
For each [must] item, record targeted proof (command + output), not just code inspection.

Generate and run cross-task integration tests for interactions between these tasks.
Run the full existing test suite as a regression check.{retry_focus}

You are working in {project_dir}. All project files are in this directory. Do not search outside it.

BATCH TASKS:
{task_list}

ACCEPTANCE CRITERIA:
{format_batch_spec(tasks_with_specs)}

MERGED DIFF:
{diff}

Write your JSON verdict to: {verdict_file}
Save any browser screenshots to: {screenshot_dir}/screenshot-<name>.png

Use this JSON structure:
{{
  "must_passed": true,
  "must_items": [
    {{"task_key": "abc123", "spec_id": 1, "criterion": "...", "status": "pass/fail", "evidence": "...", "proof": ["..."]}}
  ],
  "integration_findings": [
    {{"description": "...", "status": "pass/fail", "test": "...", "tasks_involved": ["abc123", "def456"]}}
  ],
  "regressions": [],
  "test_suite_passed": true
}}"""


def _finalize_batch_qa_result(
    qa_result: dict[str, Any],
    tasks_with_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    verdict = qa_result.get("verdict", {}) or {}
    if not isinstance(verdict, dict):
        verdict = {}
    integration_findings = verdict.get("integration_findings", []) or []
    integration_failed = any(item.get("status") == "fail" for item in integration_findings)
    regressions = verdict.get("regressions", []) or []
    test_suite_passed = verdict.get("test_suite_passed", True)
    infrastructure_error = bool(qa_result.get("infrastructure_error", False))
    expected_pairs = _expected_batch_must_matrix(tasks_with_specs or [])
    actual_pairs: set[tuple[str, int]] = set()
    for item in verdict.get("must_items", []) or []:
        task_key = str(item.get("task_key", "") or "").strip()
        try:
            spec_id = int(item.get("spec_id"))
        except (TypeError, ValueError):
            continue
        if task_key:
            actual_pairs.add((task_key, spec_id))
    missing_pairs = sorted(expected_pairs - actual_pairs)
    if missing_pairs:
        verdict["coverage_error"] = {
            "expected_count": len(expected_pairs),
            "actual_count": len(actual_pairs),
            "missing": [
                {"task_key": task_key, "spec_id": spec_id}
                for task_key, spec_id in missing_pairs
            ],
        }
    overall_passed = (
        bool(qa_result.get("must_passed"))
        and not infrastructure_error
        and not missing_pairs
        and not integration_failed
        and not regressions
        and bool(test_suite_passed)
    )

    failed_task_keys = {
        item.get("task_key")
        for item in verdict.get("must_items", []) or []
        if item.get("status") == "fail" and item.get("task_key")
    }
    for item in integration_findings:
        if item.get("status") == "fail":
            failed_task_keys.update(
                key for key in (item.get("tasks_involved") or []) if key
            )
    failed_task_keys.update(task_key for task_key, _spec_id in missing_pairs)

    return {
        "must_passed": overall_passed,
        "verdict": verdict,
        "raw_report": qa_result.get("raw_report", ""),
        "cost_usd": qa_result.get("cost_usd", 0.0),
        "failed_task_keys": sorted(failed_task_keys),
        "test_suite_passed": bool(test_suite_passed),
        "infrastructure_error": infrastructure_error,
    }


async def _run_batch_qa_agent(
    tasks_with_specs: list[dict[str, Any]],
    config: dict[str, Any],
    project_dir: Path,
    *,
    diff: str,
    retried_task_keys: set[str] | None = None,
    on_progress: Any = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Run exhaustive QA over a merged batch."""
    # Log tier decision for batch QA (always full/tier 2 — combined specs + integration)
    if log_dir:
        task_count = len(tasks_with_specs)
        spec_count = sum(len(t.get("spec", [])) for t in tasks_with_specs)
        is_retry = bool(retried_task_keys)
        append_text_log(log_dir / "qa-tier.log", [
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Batch QA tier decision",
            f"mode: batch (always full/tier 2)",
            f"tasks: {task_count}",
            f"total specs: {spec_count}",
            f"retry round: {'yes — focused on ' + ', '.join(sorted(retried_task_keys)) if is_retry else 'no (initial)'}",
            f"browser: enabled",
            f"reason: batch QA verifies integrated codebase with combined specs + cross-task integration tests",
            "",
        ])
    with tempfile.NamedTemporaryFile(suffix=".json", prefix="otto_batch_qa_", delete=False) as tf:
        verdict_file = Path(tf.name)

    screenshot_dir = log_dir / "qa-proofs" if log_dir else Path("/tmp")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    qa_prompt = _build_batch_qa_prompt(
        tasks_with_specs,
        project_dir,
        verdict_file,
        screenshot_dir,
        diff,
        retried_task_keys=retried_task_keys,
    )

    qa_result = await _run_qa_prompt(
        qa_prompt=qa_prompt,
        config=config,
        project_dir=project_dir,
        verdict_file=verdict_file,
        on_progress=on_progress,
        enable_browser=True,
        log_dir=log_dir,
    )
    final_result = _finalize_batch_qa_result(qa_result, tasks_with_specs)

    # Write per-task qa-agent.log references so each task's logs are self-contained
    if log_dir:
        batch_qa_log = log_dir / "qa-agent.log"
        for task in tasks_with_specs:
            task_key = task.get("key", "")
            if not task_key:
                continue
            task_log_dir = project_dir / "otto_logs" / task_key
            task_log_dir.mkdir(parents=True, exist_ok=True)
            try:
                append_text_log(task_log_dir / "qa-agent.log", [
                    f"[batch QA — see {log_dir.name}/qa-agent.log for full details]",
                    "",
                ])
            except Exception:
                pass

    proof_count = 0
    proof_coverage = ""
    if log_dir:
        try:
            batch_verdict = dict(final_result.get("verdict", {}) or {})
            batch_verdict["must_passed"] = final_result.get("must_passed", False)
            proof_count, proof_coverage = _write_batch_proof_artifacts(
                log_dir,
                batch_verdict,
                qa_result.get("qa_actions", []) or [],
                tasks_with_specs,
                float(final_result.get("cost_usd", 0.0) or 0.0),
            )
        except Exception:
            pass

    return {
        **final_result,
        "proof_count": proof_count,
        "proof_coverage": proof_coverage,
    }
