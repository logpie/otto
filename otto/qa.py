"""Otto QA — adversarial QA agent, verdict parsing, risk-based tiering."""

import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import (
        AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
    )
except ImportError:
    from otto._agent_stub import ClaudeAgentOptions, query, ResultMessage
    AssistantMessage = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    ToolUseBlock = None  # type: ignore[assignment,misc]
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
"""

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
                            if on_progress:
                                try:
                                    event = _build_agent_tool_event(block)
                                    # Also capture browser/MCP tools
                                    if not event and block.name.startswith("mcp__"):
                                        action = block.name.split("__")[-1]
                                        detail = ""
                                        inp = block.input or {}
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
            })
        except Exception:
            pass

    return {
        "must_passed": must_passed,
        "verdict": verdict,
        "raw_report": raw_report,
        "cost_usd": qa_cost,
    }
