"""Tier 2: Agentic browser-based product evaluation.

An LLM agent that actually USES the product like a real person:
- Navigates pages in a real browser
- Fills forms, clicks buttons
- Completes multi-step user journeys
- Takes screenshots as evidence
- Reports what works and what doesn't

This catches what Tier 1 (API probes) misses:
- JavaScript errors that break UI
- Forms that don't submit
- Navigation that dead-ends
- Visual rendering issues
- Multi-step flows that fail midway

The agent receives:
1. The requirement matrix (what to test)
2. The adapter output (how to auth, what routes exist)
3. The app URL
4. Browser tools (chrome-devtools MCP)

It does NOT see the source code — it tests as a user, not a developer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query
from otto.certifier.adapter import TestConfig
from otto.certifier.intent_compiler import Claim, RequirementMatrix
from otto.observability import append_text_log

logger = logging.getLogger("otto.certifier.tier2")


@dataclass
class JourneyStep:
    action: str         # "navigate", "click", "fill", "verify", "screenshot"
    detail: str         # what was done
    passed: bool
    screenshot: str = ""  # path to screenshot file
    error: str = ""


@dataclass
class JourneyResult:
    name: str
    description: str
    passed: bool
    steps: list[JourneyStep]
    duration_s: float = 0.0


@dataclass
class Tier2Result:
    product_dir: str
    intent: str
    base_url: str
    journeys_tested: int
    journeys_passed: int
    journeys_failed: int
    journeys: list[JourneyResult]
    cost_usd: float = 0.0
    duration_s: float = 0.0

    def score(self) -> str:
        total = self.journeys_tested
        if total == 0:
            return "n/a"
        return f"{self.journeys_passed}/{total} ({round(self.journeys_passed / total * 100)}%)"


TIER2_SYSTEM_PROMPT = """\
You are a product QA agent testing a web application through a REAL BROWSER.
Your job is to complete user journeys by actually using the UI.

CRITICAL RULES:
- You MUST use browser tools (navigate_page, click, fill, take_screenshot)
- You MUST NOT use curl, fetch, requests, or any HTTP client
- You MUST NOT use Bash to make API calls
- You are testing as a REAL USER — navigate pages, click buttons, fill forms
- Take a screenshot BEFORE and AFTER every major action
- Save screenshots to the current directory with descriptive names

For each journey:
1. navigate_page to the app URL
2. Take a screenshot showing the initial state
3. Interact with the UI step by step (click links, fill forms, submit)
4. Take a screenshot at each key moment
5. Record what you see — not what the API returns

If a form doesn't submit, that's a FAILURE.
If a page shows an error, take a screenshot and record the error.
If navigation leads to a dead end, that's a FAILURE.
If you can't find a button or link, that's a UX FAILURE.

You are NOT a developer. You cannot read source code or make API calls.
You can only see what a real user would see in the browser.

After testing all journeys, output a JSON report:
```json
{{
  "journeys": [
    {{
      "name": "journey name",
      "passed": true/false,
      "steps": [
        {{
          "action": "what you did (e.g., 'clicked Add to Cart button')",
          "detail": "what happened (e.g., 'cart badge updated to show 1 item')",
          "passed": true/false,
          "screenshot": "filename.png",
          "error": "error if failed (e.g., 'button did nothing, no feedback')"
        }}
      ]
    }}
  ]
}}
```
"""


def _tier2_log(project_dir: Path, *lines: str) -> None:
    append_text_log(project_dir / "certifier-reports" / "tier2.log", lines)


async def run_tier2(
    matrix: RequirementMatrix,
    test_config: TestConfig,
    base_url: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
) -> Tier2Result:
    """Run Tier 2 agentic browser evaluation."""
    config = config or {}
    start_time = time.monotonic()

    _tier2_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Tier 2 evaluation started",
        f"base_url: {base_url}",
        f"claims: {len(matrix.claims)}",
    )

    # Build user journeys from critical claims
    journeys = _build_journeys(matrix)

    # Build the prompt
    prompt = _build_tier2_prompt(journeys, test_config, base_url)

    # Chrome DevTools MCP configuration
    mcp_servers: dict[str, Any] = {}
    chrome_config = config.get("chrome_mcp")
    if chrome_config:
        mcp_servers["chrome-devtools"] = chrome_config
    else:
        # Default chrome-devtools config
        mcp_servers["chrome-devtools"] = {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest"],
        }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=["project"],
        env=_subprocess_env(),
        system_prompt=TIER2_SYSTEM_PROMPT,
        mcp_servers=mcp_servers,
    )

    model = config.get("qa_model") or config.get("planner_model")
    if model:
        options.model = str(model)

    raw_output, cost, _result = await run_agent_query(prompt, options)
    cost_usd = float(cost or 0.0)
    duration_s = round(time.monotonic() - start_time, 1)

    _tier2_log(
        project_dir,
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Tier 2 completed "
        f"(${cost_usd:.3f}, {duration_s:.1f}s)",
    )

    result = _parse_tier2_output(raw_output, matrix, project_dir)
    result.cost_usd = cost_usd
    result.duration_s = duration_s
    result.base_url = base_url
    result.product_dir = str(project_dir)
    result.intent = matrix.intent

    # Save report
    report_dir = project_dir / "certifier-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    _save_tier2_report(result, report_dir / "tier2-report.json")
    _save_tier2_markdown(result, report_dir / "tier2-report.md")

    return result


def _build_journeys(matrix: RequirementMatrix) -> list[dict[str, Any]]:
    """Convert critical claims into user journeys for the agent."""
    journeys = []

    # Group related claims into multi-step journeys
    auth_claims = [c for c in matrix.claims if c.category == "feature" and "auth" in c.id]
    cart_claims = [c for c in matrix.claims if "cart" in c.id]
    checkout_claims = [c for c in matrix.claims if "checkout" in c.id]
    admin_claims = [c for c in matrix.claims if "admin" in c.id]
    catalog_claims = [c for c in matrix.claims if "catalog" in c.id]

    # Journey 1: New user registration + shopping
    if auth_claims or cart_claims:
        journeys.append({
            "name": "New User Shopping Journey",
            "description": "Register a new account, browse products, add to cart, view cart",
            "steps": [
                "Navigate to the homepage",
                "Find and click the registration/signup link",
                "Fill in the registration form with: name='Test Shopper', email='tier2-test@eval.com', password='Secure123!'",
                "Submit the registration form",
                "Take a screenshot of the result",
                "Browse the product catalog",
                "Click on a product to view its details",
                "Take a screenshot of the product detail page",
                "Add the product to the cart",
                "Navigate to the cart page",
                "Verify the cart shows the added product with correct price",
                "Take a screenshot of the cart",
            ],
        })

    # Journey 2: Checkout flow
    if checkout_claims:
        journeys.append({
            "name": "Checkout Flow",
            "description": "From cart, proceed to checkout, verify payment integration",
            "steps": [
                "Navigate to the cart page (login if needed)",
                "Click proceed to checkout or checkout button",
                "Take a screenshot of the checkout page",
                "Fill in shipping/billing details if required",
                "Verify Stripe payment form or redirect appears",
                "Take a screenshot of the payment step",
            ],
        })

    # Journey 3: Admin panel
    if admin_claims:
        journeys.append({
            "name": "Admin Management",
            "description": "Login as admin, manage products and view orders",
            "steps": [
                "Navigate to the admin panel or admin login",
                "Login with admin credentials",
                "Take a screenshot of the admin dashboard",
                "Navigate to the product management section",
                "Create a new product with name='Tier 2 Test Product', price=49.99",
                "Take a screenshot showing the new product",
                "Navigate to the orders section",
                "Verify orders are displayed",
                "Take a screenshot of the orders list",
            ],
        })

    # Journey 4: Error handling
    journeys.append({
        "name": "Error Handling",
        "description": "Test edge cases and error states",
        "steps": [
            "Try to access a page that doesn't exist (e.g., /nonexistent)",
            "Take a screenshot of the 404 page",
            "Try to login with wrong credentials",
            "Verify an error message is shown (not a crash)",
            "Take a screenshot of the error state",
            "Try to access an admin page without admin privileges",
            "Verify access is denied gracefully",
        ],
    })

    return journeys


def _build_tier2_prompt(
    journeys: list[dict[str, Any]],
    test_config: TestConfig,
    base_url: str,
) -> str:
    """Build the prompt for the Tier 2 agent."""
    parts = [f"Test the application at: {base_url}\n"]

    # Credentials
    if test_config.seeded_users:
        parts.append("Available credentials:")
        for user in test_config.seeded_users:
            parts.append(f"  - {user.email} / {user.password} ({user.role})")
        parts.append("")

    # Journeys
    parts.append("Complete these user journeys:\n")
    for i, journey in enumerate(journeys, 1):
        parts.append(f"## Journey {i}: {journey['name']}")
        parts.append(f"{journey['description']}\n")
        for j, step in enumerate(journey["steps"], 1):
            parts.append(f"{j}. {step}")
        parts.append("")

    parts.append(
        "After completing all journeys, output your findings as a JSON block "
        "with the structure described in your instructions."
    )

    return "\n".join(parts)


def _parse_tier2_output(
    raw: str,
    matrix: RequirementMatrix,
    project_dir: Path,
) -> Tier2Result:
    """Parse the agent's journey results."""
    text = raw.strip()

    # Extract JSON
    data = None
    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            json_part = parts[-1].split("```")[0].strip()
            try:
                data = json.loads(json_part)
            except json.JSONDecodeError:
                pass

    if data is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

    journeys: list[JourneyResult] = []
    if data and "journeys" in data:
        for j in data["journeys"]:
            steps = []
            for s in j.get("steps", []):
                steps.append(JourneyStep(
                    action=s.get("action", ""),
                    detail=s.get("detail", ""),
                    passed=s.get("passed", False),
                    screenshot=s.get("screenshot", ""),
                    error=s.get("error", ""),
                ))
            journeys.append(JourneyResult(
                name=j.get("name", ""),
                description=j.get("description", ""),
                passed=j.get("passed", False),
                steps=steps,
            ))

    passed = sum(1 for j in journeys if j.passed)
    failed = sum(1 for j in journeys if not j.passed)

    return Tier2Result(
        product_dir=str(project_dir),
        intent=matrix.intent,
        base_url="",
        journeys_tested=len(journeys),
        journeys_passed=passed,
        journeys_failed=failed,
        journeys=journeys,
    )


def _save_tier2_report(result: Tier2Result, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "product_dir": result.product_dir,
        "intent": result.intent,
        "base_url": result.base_url,
        "score": result.score(),
        "journeys_tested": result.journeys_tested,
        "journeys_passed": result.journeys_passed,
        "journeys_failed": result.journeys_failed,
        "cost_usd": result.cost_usd,
        "duration_s": result.duration_s,
        "journeys": [
            {
                "name": j.name,
                "description": j.description,
                "passed": j.passed,
                "steps": [
                    {
                        "action": s.action,
                        "detail": s.detail,
                        "passed": s.passed,
                        "screenshot": s.screenshot,
                        "error": s.error,
                    }
                    for s in j.steps
                ],
            }
            for j in result.journeys
        ],
    }
    path.write_text(json.dumps(data, indent=2))


def _save_tier2_markdown(result: Tier2Result, path: Path) -> None:
    lines = [
        "# Tier 2 Certification Report — User Journey Testing",
        "",
        f"> **Product:** `{result.product_dir}`",
        f"> **Score:** {result.score()}",
        f"> **Cost:** ${result.cost_usd:.2f}",
        f"> **Duration:** {result.duration_s:.0f}s",
        "",
        "## What This Report Measures",
        "",
        "This is Tier 2 (agentic) certification. An AI agent used the product",
        "through a real browser, completing multi-step user journeys.",
        "",
        "**What it verifies:**",
        "- UI renders and is interactive",
        "- Forms submit and produce results",
        "- Multi-step flows complete end-to-end",
        "- Error states are handled gracefully",
        "- Visual evidence via screenshots",
        "",
    ]

    for journey in result.journeys:
        icon = "✓" if journey.passed else "✗"
        lines.append(f"## {icon} {journey.name}")
        lines.append(f"_{journey.description}_")
        lines.append("")

        for step in journey.steps:
            step_icon = "✓" if step.passed else "✗"
            lines.append(f"- {step_icon} **{step.action}**: {step.detail}")
            if step.screenshot:
                lines.append(f"  ![screenshot]({step.screenshot})")
            if step.error:
                lines.append(f"  > Error: {step.error}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
