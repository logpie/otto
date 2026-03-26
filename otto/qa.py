"""Otto QA — adversarial QA agent, verdict parsing, risk-based tiering."""

import asyncio
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import (
        AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
        UserMessage,
    )
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    AssistantMessage = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]
    UserMessage = None  # type: ignore[assignment,misc]
    ToolResultBlock = None  # type: ignore[assignment,misc]

from otto.theme import console
from otto.verify import _subprocess_env


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
Always run existing tests before writing the verdict.

When taking browser screenshots, ALWAYS save to a file path:
  take_screenshot(filePath="<proof_dir>/screenshot-<name>.png")
Do NOT take screenshots without filePath — inline screenshots break the message pipe.

Also check:
- Does the implementation contradict the ORIGINAL task prompt?
- Does it break existing functionality?

Write your verdict to the output file as JSON:
{
  "must_passed": true/false,
  "must_items": [
    {"criterion": "...", "status": "pass/fail", "evidence": "..."}
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

    Returns {must_passed, verdict, raw_report, cost_usd}.
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
    # Track QA verification actions for proof artifacts
    qa_actions: list[dict[str, str]] = []
    pending_tool_actions: dict[str, int] = {}

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
                elif UserMessage and isinstance(message, UserMessage):
                    # UserMessage contains ToolResultBlock — extract Bash output
                    for block in message.content:
                        if not (ToolResultBlock and isinstance(block, ToolResultBlock)):
                            continue
                        tool_use_id = getattr(block, "tool_use_id", "")
                        action_idx = pending_tool_actions.pop(tool_use_id, None)
                        if action_idx is None or not (0 <= action_idx < len(qa_actions)):
                            continue
                        action = qa_actions[action_idx]
                        text = _extract_tool_result_text(getattr(block, "content", None))
                        if action["type"] == "bash":
                            action["output"] = text[:2000]
                        elif action["type"] == "browser":
                            action["output"] = text[:4000]
                        # Screenshot data is too large for the message pipe
                        # (>1MB base64 crashes the SDK). QA prompt instructs
                        # the agent to save screenshots via filePath instead.
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
                            inp = block.input or {}
                            action_idx: int | None = None
                            # Collect Bash commands for proof scripts
                            if block.name == "Bash":
                                cmd = inp.get("command", "")
                                if cmd:
                                    qa_actions.append({
                                        "type": "bash",
                                        "command": cmd,
                                        "output": "",
                                    })
                                    action_idx = len(qa_actions) - 1
                            # Collect Browser/MCP actions for proof report
                            elif block.name.startswith("mcp__"):
                                action = block.name.split("__")[-1]
                                detail = ""
                                if action == "take_screenshot":
                                    detail = (
                                        inp.get("selector")
                                        or inp.get("url")
                                        or inp.get("filePath", "")
                                    )[:120]
                                elif "url" in inp:
                                    detail = inp["url"][:120]
                                elif "selector" in inp:
                                    detail = inp["selector"][:120]
                                qa_actions.append({
                                    "type": "browser",
                                    "action": action,
                                    "detail": detail,
                                    "path": str(inp.get("filePath", ""))[:400],
                                    "input": json.dumps(inp, sort_keys=True, default=str)[:4000],
                                    "output": "",
                                })
                                action_idx = len(qa_actions) - 1

                            block_id = getattr(block, "id", "")
                            if action_idx is not None and block_id:
                                pending_tool_actions[block_id] = action_idx

                            if on_progress:
                                try:
                                    event = _build_agent_tool_event(block)
                                    # Also capture browser/MCP tools
                                    if not event and block.name.startswith("mcp__"):
                                        action = block.name.split("__")[-1]
                                        detail = ""
                                        if "url" in inp:
                                            detail = inp["url"][:60]
                                        elif "selector" in inp:
                                            detail = inp["selector"][:60]
                                        event = {"name": f"Browser:{action}", "detail": detail}
                                    if event:
                                        on_progress("agent_tool", event)
                                except Exception:
                                    pass
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

    # Write proof artifacts if log_dir provided
    proof_count = 0
    if log_dir:
        try:
            proof_count = _write_proof_artifacts(
                log_dir, verdict, qa_actions, task, original_prompt, qa_cost,
            )
        except Exception:
            pass  # proof writing is best-effort

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
            })
        except Exception:
            pass

    return {
        "must_passed": must_passed,
        "verdict": verdict,
        "raw_report": raw_report,
        "cost_usd": qa_cost,
        "proof_count": proof_count,
    }


_REPLAY_SKIP_SUBSTRINGS = (
    "kill ", "pkill ", "rm -rf", "git push",
    "npm run dev", "npm start", "next dev",
    "npx next dev", "npx next start",
    "python -m http", "python3 -m http",
    "node server", "serve ", "flask run",
    "uvicorn", "gunicorn", "nohup",
)


def _is_non_replayable(cmd: str) -> bool:
    """Check if a command is non-replayable (server start, destructive, background)."""
    lower = cmd.lower()
    if any(skip in lower for skip in _REPLAY_SKIP_SUBSTRINGS):
        return True
    if re.search(r"(?<![>&])\s*&\s*$", cmd):
        return True
    return False


_EXPLORATION_COMMAND_PREFIXES = (
    "find ", "grep ", "rg ", "git diff", "git log", "git show", "git status",
    "git rev-parse", "wc ", "ls ", "pwd", "cd ", "cat ", "sed ", "head ",
    "tail ", "sort ", "uniq ", "stat ", "file ", "tree ", "basename ",
    "dirname ", "realpath ", "which ", "locate ", "readlink ",
)

_VERIFICATION_COMMAND_PATTERNS = (
    r"(^|[;&( ])(?:uv run )?pytest(?:\s|$)",
    r"(^|[;&( ])python(?:3)? -m pytest(?:\s|$)",
    r"(^|[;&( ])(?:npx )?jest(?:\s|$)",
    r"(^|[;&( ])(?:npx )?vitest(?:\s|$)",
    r"(^|[;&( ])(?:bun x )?playwright test(?:\s|$)",
    r"(^|[;&( ])cypress run(?:\s|$)",
    r"(^|[;&( ])(?:npm|pnpm|yarn|bun) (?:run )?(?:test|lint|typecheck|check|build)\b",
    r"(^|[;&( ])tsc(?:\s|$)",
    r"(^|[;&( ])mypy(?:\s|$)",
    r"(^|[;&( ])ruff(?:\s|$)",
    r"(^|[;&( ])eslint(?:\s|$)",
    r"(^|[;&( ])cargo (?:test|check|build)\b",
    r"(^|[;&( ])go (?:test|build)\b",
    r"(^|[;&( ])(?:mvn|gradle|./gradlew) (?:test|check|build)\b",
    r"(^|[;&( ])dotnet (?:test|build)\b",
    r"(^|[;&( ])curl(?:\s|$)",
    r"(^|[;&( ])http(?:\s|$)",
    r"(^|[;&( ])node -e(?:\s|$)",
    r"(^|[;&( ])python(?:3)? -c(?:\s|$)",
    r"(^|[;&( ])python(?:3)?\s+-\s+<<",
    r"(^|[;&( ])node\s+<<",
    r"(^|[;&( ])(?:test|\[|true|false)(?:\s|$)",
)

_BROWSER_TRACE_ACTIONS = {
    "navigate", "navigate_page", "click", "fill", "fill_form", "type",
    "press_key", "hover", "select", "wait_for", "wait_for_load_state",
    "go_back", "go_forward", "reload", "new_page", "select_page",
    "close_page", "list_pages", "resize_page", "emulate",
}


def _is_verification_command(cmd: str) -> bool:
    """Keep only replayable commands that generate hard verification evidence."""
    stripped = cmd.strip()
    if not stripped or _is_non_replayable(stripped):
        return False

    lower = stripped.lower()
    if re.search(r"(^|[ \t])(--version|--help)([ \t]|$)", lower):
        return False
    if any(lower.startswith(prefix) for prefix in _EXPLORATION_COMMAND_PREFIXES):
        return False

    if any(re.search(pattern, lower) for pattern in _VERIFICATION_COMMAND_PATTERNS):
        return True

    return bool(re.search(
        r"(^|[;&( ])(?:\./)?[\w./-]*(?:test|spec|check|verify|assert|lint|typecheck|build)[\w./-]*(?:\s|$)",
        lower,
    ))


def _trim_evidence_output(text: str, max_lines: int = 12, max_chars: int = 1600) -> str:
    """Return a compact but reproducible output excerpt."""
    if not text:
        return ""

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    excerpt = "\n".join(lines[-max_lines:])
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[-max_chars:]


def _extract_tool_result_text(raw_content: Any) -> str:
    """Unwrap Claude SDK tool result payloads into plain text."""
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        parts: list[str] = []
        for item in raw_content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined
    return str(raw_content)


def _normalize_report_prompt(original_prompt: str) -> str:
    """Collapse multiline shell prompts into a single readable line."""
    return re.sub(r"\s+", " ", original_prompt).strip()


def _load_browser_input(action: dict[str, str]) -> dict[str, Any]:
    raw = action.get("input", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _browser_assertion_input(action: dict[str, str]) -> str:
    data = _load_browser_input(action)
    for key in ("function", "expression", "script"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if data:
        return json.dumps(data, indent=2, sort_keys=True)
    return action.get("detail", "").strip()


def _is_browser_assertion(action: dict[str, str]) -> bool:
    action_name = action.get("action", "")
    if action_name == "take_screenshot" or action_name in _BROWSER_TRACE_ACTIONS:
        return False
    return bool(action.get("output", "").strip())


def _looks_like_path(value: str) -> bool:
    return value.endswith((".png", ".jpg", ".jpeg"))


def _resolve_screenshot_file(
    action: dict[str, str],
    available: set[str],
) -> str | None:
    for candidate in (action.get("path", ""), action.get("detail", "")):
        if not candidate:
            continue
        name = Path(candidate).name
        if name in available and _looks_like_path(name):
            return name
    return None


def _screenshot_caption(action: dict[str, str], filename: str) -> str:
    detail = action.get("detail", "").strip()
    if detail and Path(detail).name != filename:
        return detail
    stem = Path(filename).stem
    stem = stem.removeprefix("screenshot-").replace("-", " ").replace("_", " ").strip()
    return stem or "Browser screenshot"


def _write_proof_artifacts(
    log_dir: Path,
    verdict: dict[str, Any],
    qa_actions: list[dict[str, str]],
    task: dict[str, Any],
    original_prompt: str,
    cost_usd: float,
) -> int:
    """Write proof artifacts to log_dir/qa-proofs/.

    Returns the number of proof files written.
    """
    proofs_dir = log_dir / "qa-proofs"
    if proofs_dir.exists():
        # Clean stale proof files but preserve screenshots saved by QA agent
        for old in proofs_dir.iterdir():
            if old.suffix not in (".png", ".jpg", ".jpeg"):
                old.unlink(missing_ok=True)
    proofs_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    must_items = verdict.get("must_items", [])

    # Write per-must-item proof files
    for i, item in enumerate(must_items):
        proof_path = proofs_dir / f"must-{i + 1}.md"
        proof_path.write_text(
            f"# {item.get('criterion', 'Unknown criterion')}\n"
            f"Status: {item.get('status', 'unknown')}\n"
            f"Evidence: {item.get('evidence', 'none')}\n"
        )
        count += 1

    # Filter out QA-internal and keep only replayable verification commands
    _QA_INTERNAL_PATTERNS = ("otto_qa_", "/var/folders/", "/tmp/otto_")
    _QA_INFRA_PREFIXES = (
        "sleep ", "lsof ", "echo ", "cat >", "cat ", "kill ", "pkill ",
        "mkdir ", "ls ", "pwd", "cd ", "curl -s -o /dev/null",
    )
    verification_commands = [
        a for a in qa_actions
        if a["type"] == "bash" and a.get("command")
        and not any(p in a["command"] for p in _QA_INTERNAL_PATTERNS)
        and not a["command"].strip().startswith(_QA_INFRA_PREFIXES)
        and _is_verification_command(a["command"])
    ]
    if verification_commands:
        lines = [
            "#!/bin/bash",
            "# Re-run all QA verification commands",
            "# Generated by otto QA proof-of-work system",
            "set -e",
            "",
        ]
        for cmd_info in verification_commands:
            cmd = cmd_info["command"]
            if _is_non_replayable(cmd):
                lines.append(f"# Skipped (non-replayable): {cmd[:60]}")
                continue
            label = cmd.replace("\n", " ")[:60].replace('"', '\\"')
            lines.append(f'echo "Testing: {label}"')
            lines.append(cmd)
            lines.append("")
        regression_script = proofs_dir / "regression-check.sh"
        regression_script.write_text("\n".join(lines))
        regression_script.chmod(0o755)
        count += 1

    # Write proof-report.md — human-readable, reproducible evidence only
    must_passed = verdict.get("must_passed", False)
    status_icon = "\u2713 passed" if must_passed else "\u2717 failed"
    passed_count = sum(1 for m in must_items if m.get("status") == "pass")
    normalized_prompt = _normalize_report_prompt(original_prompt)

    report_lines = [
        f"# Proof Report",
        f"**Task:** {normalized_prompt}",
        f"**Result:** {task.get('key', '?')} | {status_icon} | {passed_count}/{len(must_items)} must | QA ${cost_usd:.2f}",
        "",
    ]

    report_lines.append("## Must Verdict")
    for item in must_items:
        criterion = item.get("criterion", "")
        status = item.get("status", "unknown")
        icon = "\u2713" if status == "pass" else "\u2717"
        report_lines.append(f"- {icon} {criterion}")
    if not must_items:
        report_lines.append("- No [must] items recorded")
    report_lines.append("")

    replayable = [c for c in verification_commands
                  if not _is_non_replayable(c["command"])]
    if replayable:
        report_lines.append("## Verification Commands")
        for idx, cmd_info in enumerate(replayable, start=1):
            cmd = cmd_info["command"]
            output = _trim_evidence_output(cmd_info.get("output", ""))
            report_lines.append(f"### VC{idx}")
            report_lines.append("```bash")
            report_lines.append(cmd)
            report_lines.append("```")
            if output:
                report_lines.append("Observed output:")
                report_lines.append("```text")
                report_lines.append(output)
                report_lines.append("```")
            else:
                report_lines.append("Observed output: not captured")
            report_lines.append("")

    browser_assertions = [
        action for action in qa_actions
        if action.get("type") == "browser" and _is_browser_assertion(action)
    ]
    if browser_assertions:
        report_lines.append("## Browser Assertions")
        for idx, action in enumerate(browser_assertions, start=1):
            action_name = action.get("action", "browser_action")
            assertion_input = _browser_assertion_input(action)
            output = _trim_evidence_output(action.get("output", ""), max_lines=20, max_chars=2400)
            report_lines.append(f"### BA{idx} `{action_name}`")
            if assertion_input:
                report_lines.append("Input:")
                report_lines.append("```text")
                report_lines.append(assertion_input)
                report_lines.append("```")
            if output:
                report_lines.append("Observed output:")
                report_lines.append("```text")
                report_lines.append(output)
                report_lines.append("```")
            else:
                report_lines.append("Observed output: not captured")
            report_lines.append("")

    # Collect screenshots saved by QA agent to qa-proofs/ and map by filePath
    browser_actions = [a for a in qa_actions if a["type"] == "browser"]
    screenshot_refs = sorted(
        f.name for f in proofs_dir.glob("screenshot-*.png") if f.is_file()
    )
    count += len(screenshot_refs)
    available_refs = set(screenshot_refs)
    matched_refs: set[str] = set()
    screenshot_lines: list[str] = []
    ss_counter = 1
    for action in browser_actions:
        if action.get("action") != "take_screenshot":
            continue
        ref = _resolve_screenshot_file(action, available_refs)
        if not ref:
            continue
        matched_refs.add(ref)
        caption = _screenshot_caption(action, ref)
        screenshot_lines.append(f"- SS{ss_counter} [{ref}]({ref})")
        screenshot_lines.append(f"  Description: {caption}")
        ss_counter += 1

    for ref in screenshot_refs:
        if ref in matched_refs:
            continue
        screenshot_lines.append(f"- SS{ss_counter} [{ref}]({ref})")
        screenshot_lines.append("  Description: QA browser screenshot")
        ss_counter += 1

    if screenshot_lines:
        report_lines.append("## Screenshots")
        report_lines.extend(screenshot_lines)
        report_lines.append("")

    proof_report = proofs_dir / "proof-report.md"
    proof_report.write_text("\n".join(report_lines))
    count += 1

    return count
