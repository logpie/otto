"""Otto product QA — journey-based verification of the whole product.

After all tasks pass individually, product QA tests user journeys from
product-spec.md. It verifies that features work TOGETHER as a product,
not just individually. This is the verification step of the i2p outer loop.

Product QA tests what per-task QA cannot:
- Features working together (cross-feature flows)
- Data flowing across surfaces (API → UI → extension)
- State consistency over time (create → modify → delete → verify)

The QA agent runs real commands (curl, browser automation, file checks)
and collects evidence. It does NOT judge by reading code. Evidence-based
verification only.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
from otto.observability import append_text_log

logger = logging.getLogger("otto.product_qa")


def _qa_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "otto_logs" / "product-qa.log", lines)


PRODUCT_QA_SYSTEM_PROMPT = """\
You are a product QA agent. Your job is to verify that a product works
as a whole by testing user journeys end-to-end.

You will receive:
1. A product spec with user journeys to test
2. Access to the full codebase

For each user journey:
1. Start any required servers/services
2. Execute the journey steps using real commands (curl, browser, CLI)
3. Verify each step produces the expected result
4. Collect evidence (command output, HTTP responses, file contents)
5. Report pass/fail with evidence

RULES:
- Do NOT judge by reading code. Run real commands and observe real behavior.
- If a server needs to start, start it and wait for it to be ready.
- Test the HAPPY PATH first, then edge cases if the journey specifies them.
- If a step fails, capture the error and move to the next journey.
  Don't stop at the first failure.
- Evidence must be concrete: actual command + actual output.

After testing all journeys, output ONLY a JSON block with your results:

```json
{
  "product_passed": true/false,
  "journeys": [
    {
      "name": "journey name from spec",
      "passed": true/false,
      "steps": [
        {"action": "what you did", "expected": "what should happen", "actual": "what happened", "passed": true/false}
      ],
      "error": "overall error description if failed, null if passed",
      "evidence": "key evidence (truncated command output)"
    }
  ]
}
```
"""


async def run_product_qa(
    product_spec_path: Path,
    project_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Run product QA — test user journeys from product-spec.md.

    Returns a dict with product_passed (bool) and journeys (list).
    """
    if not product_spec_path.exists():
        return {"product_passed": True, "journeys": [], "skipped": True,
                "reason": "no product-spec.md"}

    product_spec = product_spec_path.read_text()

    _qa_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] product QA started",
        f"spec: {product_spec_path}",
    )

    prompt = f"""\
Test this product by executing the user journeys defined in the product spec.

Product spec:
{product_spec}

The codebase is in the current directory. Explore it to understand how to
start the application and run the journeys.
"""

    # Full agent session with tools — needs to run commands, read files, etc.
    # Include chrome-devtools MCP if available (for web app testing)
    mcp_servers = {}
    chrome_config = config.get("chrome_mcp")
    if chrome_config:
        mcp_servers["chrome-devtools"] = chrome_config

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=_qa_settings(config),
        env=_subprocess_env(),
        system_prompt=PRODUCT_QA_SYSTEM_PROMPT,
    )
    if mcp_servers:
        options.mcp_servers = mcp_servers

    model = _qa_model(config)
    if model:
        options.model = model

    started_at = time.monotonic()
    raw_output, cost, _result = await run_agent_query(prompt, options)
    cost_usd = float(cost or 0.0)
    duration_s = round(time.monotonic() - started_at, 1)

    _qa_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] product QA completed "
        f"(${cost_usd:.3f}, {duration_s:.1f}s)",
    )

    # Parse the JSON result
    result = _parse_qa_output(raw_output)
    result["cost_usd"] = cost_usd
    result["duration_s"] = duration_s

    # Persist verdict
    verdict_path = project_dir / "otto_logs" / "product-qa-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        verdict_path.write_text(json.dumps(result, indent=2))
    except OSError:
        pass

    _qa_log(
        project_dir,
        f"product_passed: {result.get('product_passed')}",
        f"journeys: {len(result.get('journeys', []))} tested",
        "",
    )

    return result


def _parse_qa_output(raw: str) -> dict[str, Any]:
    """Parse product QA agent output into structured result."""
    text = raw.strip()

    # Extract JSON from markdown fences
    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            json_part = parts[-1].split("```")[0].strip()
            try:
                return json.loads(json_part)
            except json.JSONDecodeError:
                pass

    # Try bare JSON
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Fallback — couldn't parse
    return {
        "product_passed": False,
        "journeys": [],
        "parse_error": f"Could not parse QA output: {text[:200]}",
    }


def _qa_settings(config: dict[str, Any]) -> list[str]:
    settings = config.get("qa_agent_settings")
    if settings in (None, ""):
        settings = config.get("planner_agent_settings", "project") or "project"
    return str(settings).split(",")


def _qa_model(config: dict[str, Any]) -> str | None:
    model = config.get("qa_model")
    if model in (None, ""):
        model = config.get("planner_model")
    return str(model) if model else None
