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
) -> int:
    """Determine QA tier based on residual risk after verification.

    Tier 0: skip QA (all [must] items have tests, local change, first attempt)
    Tier 1: targeted QA (unmapped [must] items, cross-cutting changes)
    Tier 2: full QA with browser (visual/SPA, auth/crypto, retries)
    """
    from otto.tasks import spec_binding, spec_is_verifiable

    diff_files = diff_info.get("files", [])
    spec_test_mapping = spec_test_mapping or {}

    # Tier 2: high-risk domains
    HIGH_RISK_PATTERNS = ["auth", "crypto", "permission", "migration",
                          "payment", "security", "token", "session"]
    if any(pattern in f.lower() for f in diff_files for pattern in HIGH_RISK_PATTERNS):
        return 2

    # Tier 2: non-verifiable [must] items require browser/subjective QA.
    if any(spec_binding(item) == "must" and not spec_is_verifiable(item) for item in spec):
        return 2

    # Tier 2: visual/UI specs (need browser), SPA apps, or retries
    from otto.tasks import spec_text
    has_visual = any(
        spec_binding(item) == "should"
        and any(kw in spec_text(item).lower()
                for kw in ("ui", "layout", "style", "visual", "responsive"))
        for item in spec
    )
    is_spa = any(f.endswith((".jsx", ".tsx", ".vue", ".svelte")) for f in diff_files)
    if has_visual or is_spa or attempt > 0:
        return 2

    # Tier 1: unmapped [must] items or cross-cutting changes
    unmapped = [item for item in spec
                if spec_binding(item) == "must"
                and not spec_test_mapping.get(spec_text(item))]
    if unmapped or len(diff_files) > 5:
        return 1

    # Tier 0: every [must] item has a test, local change, first attempt
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
    result_icon = "\u2713 PASSED" if must_passed_count == must_total else "\u2717 FAILED"

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

    qa_prompt = f"""Test this implementation against the acceptance criteria and the original task prompt.

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

    # Configure MCP servers for browser testing (tier 2)
    qa_mcp_servers = {}
    if tier >= 2:
        user_claude_json = Path.home() / ".claude.json"
        if user_claude_json.exists():
            try:
                user_config = json.loads(user_claude_json.read_text())
                for name, srv in user_config.get("mcpServers", {}).items():
                    if name == "chrome-devtools":
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
                pass

    _qa_settings = config.get("qa_agent_settings", "project").split(",")
    qa_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_qa_settings,
        env=_subprocess_env(),
        # Keep CC's default prompt (Glob over find, etc.) + append QA instructions
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": QA_SYSTEM_PROMPT_V45},
    )
    if qa_mcp_servers:
        qa_opts.mcp_servers = qa_mcp_servers
    if config.get("model"):
        qa_opts.model = config["model"]

    qa_timeout = config.get("qa_timeout", 3600)
    report_lines: list[str] = []
    qa_cost = 0.0
    qa_actions: list[dict] = []
    _pending_tool_uses: dict[str, dict] = {}  # tool_use_id -> action dict

    from otto.display import build_agent_tool_event as _build_agent_tool_event

    try:
        async def _run_qa():
            nonlocal report_lines, qa_cost
            _result_msg = None
            async for message in query(prompt=qa_prompt, options=qa_opts):
                if isinstance(message, ResultMessage):
                    _result_msg = message
                elif hasattr(message, "session_id") and hasattr(message, "is_error"):
                    _result_msg = message
                elif AssistantMessage and isinstance(message, AssistantMessage):
                    for block in message.content:
                        if TextBlock and isinstance(block, TextBlock) and block.text:
                            report_lines.append(block.text)
                            if on_progress:
                                for line in block.text.splitlines():
                                    line_s = line.strip()
                                    if not line_s or len(line_s) < 10:
                                        continue
                                    # PASS/FAIL/verdict lines → structured findings
                                    has_verdict = any(m in line_s for m in
                                                      ["PASS", "FAIL", "must", "should",
                                                       "✅", "❌", "✓", "✗"])
                                    if has_verdict:
                                        try:
                                            on_progress("qa_finding", {"text": line_s[:200]})
                                        except Exception:
                                            pass
                                    else:
                                        # Reasoning narration → status line
                                        try:
                                            on_progress("qa_status", {"text": line_s[:120]})
                                        except Exception:
                                            pass
                        elif ToolUseBlock and isinstance(block, ToolUseBlock):
                            # Capture QA actions for proof artifacts
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
                                    _pending_tool_uses[tool_id] = action
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
                                    # Store path from input if available
                                    action["path"] = inp.get("path", "")
                                    action["detail"] = inp.get("description", "") or inp.get("detail", "")
                                qa_actions.append(action)
                                if tool_id:
                                    _pending_tool_uses[tool_id] = action
                            if on_progress:
                                try:
                                    event = _build_agent_tool_event(block)
                                    # Also capture browser/MCP tools
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
                            # Match result to pending tool use by tool_use_id
                            tid = getattr(block, "tool_use_id", None)
                            if tid and tid in _pending_tool_uses:
                                raw_content = _unwrap_tool_result_content(
                                    getattr(block, "content", "")
                                )
                                _pending_tool_uses[tid]["output"] = raw_content
                                _pending_tool_uses[tid]["is_error"] = bool(
                                    getattr(block, "is_error", False)
                                )
                # Handle UserMessage containing ToolResultBlocks
                elif UserMessage and isinstance(message, UserMessage):
                    for block in getattr(message, "content", []):
                        if ToolResultBlock and isinstance(block, ToolResultBlock):
                            tid = getattr(block, "tool_use_id", None)
                            if tid and tid in _pending_tool_uses:
                                raw_content = _unwrap_tool_result_content(
                                    getattr(block, "content", "")
                                )
                                _pending_tool_uses[tid]["output"] = raw_content
                                _pending_tool_uses[tid]["is_error"] = bool(
                                    getattr(block, "is_error", False)
                                )
            # Extract cost from the final result (after stream completes)
            if _result_msg:
                raw_cost = getattr(_result_msg, "total_cost_usd", None)
                if isinstance(raw_cost, (int, float)):
                    qa_cost = float(raw_cost)

        await asyncio.wait_for(_run_qa(), timeout=qa_timeout)
    except asyncio.TimeoutError:
        report_lines.append(f"\n[QA agent timed out after {qa_timeout}s]")
    except Exception as e:
        error_str = str(e)
        report_lines.append(f"\n[QA agent error: {error_str}]")
        # Check if QA already wrote a verdict before crashing
        if verdict_file.exists():
            try:
                partial = json.loads(verdict_file.read_text().strip())
                if "must_passed" in partial:
                    # QA completed verdict before crash — use it
                    return {
                        "must_passed": partial["must_passed"],
                        "verdict": partial,
                        "raw_report": "\n".join(report_lines),
                        "cost_usd": qa_cost,
                    }
            except (json.JSONDecodeError, OSError):
                pass
        # No verdict written — flag as infrastructure error for retry
        is_infra = any(kw in error_str.lower() for kw in
                       ("api_error", "internal server", "stream closed"))
        if is_infra:
            return {
                "must_passed": None,
                "verdict": None,
                "raw_report": "\n".join(report_lines),
                "cost_usd": qa_cost,
                "infrastructure_error": True,
            }

    raw_report = "\n".join(report_lines)

    # Try to read verdict from file first, then parse from report
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

    must_passed = verdict.get("must_passed", False)

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
    }
