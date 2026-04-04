"""Intent compiler — turns natural language intent into testable claims.

This is the most important step. Without explicit claims, the evaluator
is "grading vibes." The compiler decomposes an intent into a requirement
matrix where each claim is testable, prioritized, and has a suggested
test approach.

The compiler runs ONCE before any testing. It doesn't see the product
code — only the original intent. This ensures evaluation criteria are
defined independently of the implementation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

logger = logging.getLogger("otto.certifier")

TestStep = dict[str, Any]


@dataclass
class Claim:
    """A single testable claim derived from the intent."""

    id: str                    # e.g., "task-create"
    description: str           # "Users can register with email and password"
    priority: str              # "critical" | "important" | "nice"
    category: str              # "feature" | "ux" | "data" | "error-handling" | "security"
    test_approach: str         # "api" | "browser" | "cli" | "code-review"
    test_steps: list[TestStep]  # machine-executable steps for the baseline runner
    hard_fail: bool = False    # if True, failing this claim fails the entire certification


@dataclass
class RequirementMatrix:
    """The full set of testable claims for a product."""

    intent: str
    claims: list[Claim]
    non_goals: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    product_type_hint: str = ""  # "web" | "cli" | "api" | "desktop"
    compiled_at: str = ""
    cost_usd: float = 0.0

    def critical_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.priority == "critical"]

    def by_category(self, cat: str) -> list[Claim]:
        return [c for c in self.claims if c.category == cat]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


INTENT_COMPILER_PROMPT = """\
You are a requirements analyst. Given a product intent (what someone asked
to be built), produce a requirement matrix of TESTABLE CLAIMS.

Intent: {intent}
{schema_hint_block}

For each claim:
- id: short kebab-case identifier derived from the actual feature (e.g.,
  "auth-register", "task-create", "note-share", "admin-manage-users")
- description: one sentence describing the observable behavior
- priority: "critical" (product is broken without it), "important" (significant
  gap), or "nice" (polish/enhancement)
- category: "feature", "ux", "data", "error-handling", or "security"
- test_approach: how to verify — "api" (HTTP requests), "browser" (navigate
  and interact), "cli" (run commands), "code-review" (inspect source)
- test_steps: 1-5 MACHINE-EXECUTABLE JSON objects only. Never natural language strings.
- hard_fail: true if this claim MUST pass for certification (critical features)

Also identify:
- non_goals: things the intent does NOT ask for (prevent over-testing)
- ambiguities: parts of the intent that are unclear (flag, don't assume)
- product_type_hint: "web", "cli", "api", "desktop", or "unknown"

RULES:
- Derive claims from the ACTUAL intent, not from a generic product template.
- Only include claims the intent actually asks for. Don't invent features.
- Each claim must be independently testable with concrete structured steps.
- Critical claims should cover: can a user do the core thing the product promises?
- Mark registration, login, admin access, core CRUD flows, and the primary user
  flow as critical when the intent calls for them.
- Mark error handling, edge cases, UI polish as important or nice.
- Every test step must be a dict with an "action" key.
- Use generic language and generic reasoning: primary entities, core user
  actions, CRUD flows, access control, persistence, and error handling.
- If a claim operates on a primary entity, include candidate paths that match
  plausible route shapes for that entity.
- For create/update actions, include all plausibly required fields based on the
  actual intent, not on a store-specific schema.
- For access-control claims, prefer protected or admin-only endpoints and views.
- Allowed actions:
  - "http": may include "method", "path", "candidate_paths", "body",
    "body_variants", "expect_status", "expect_body_contains", "expect_json_keys"
  - "navigate": may include "path", "candidate_paths", "expect_status",
    "expect_body_contains"
  - "cli": may include "command", "expect_exit_code", "expect_stdout_contains"
  - "check_exists": may include "path", "candidate_paths", "target",
    "existence_statuses"
- Prefer concrete request bodies with realistic fixtures.
- Prefer candidate_paths when route shape may vary across implementations.
- 10-25 claims for a typical product. Don't over-decompose.

Example framing for a task manager:
- Good claim IDs: "auth-register", "task-create", "task-list", "task-update",
  "task-delete", "task-persistence", "admin-manage-users"
- Bad claim IDs: "catalog-list", "cart-add-item", "checkout-complete" unless
  the intent is actually an e-commerce product.

Output JSON only:
{{
  "product_type_hint": "web",
  "non_goals": ["calendar sync", "mobile app"],
  "ambiguities": ["unclear whether collaborators can edit shared tasks"],
  "claims": [
    {{
      "id": "task-create",
      "description": "Users can create a task with the required fields",
      "priority": "critical",
      "category": "feature",
      "test_approach": "api",
      "test_steps": [
        {{
          "action": "http",
          "method": "POST",
          "path": "/api/tasks",
          "candidate_paths": [
            "/api/tasks",
            "/api/task",
            "/tasks"
          ],
          "body": {{
            "title": "Pay rent",
            "description": "Due on Friday",
            "status": "open"
          }},
          "expect_status": [200, 201],
          "expect_body_contains": ["Pay rent"],
          "expect_json_keys": ["id", "title"]
        }},
        {{
          "action": "http",
          "method": "GET",
          "path": "/api/tasks",
          "candidate_paths": ["/api/tasks", "/tasks"],
          "expect_status": [200],
          "expect_body_contains": ["Pay rent"]
        }}
      ],
      "hard_fail": true
    }}
  ]
}}
"""


async def compile_intent(
    intent: str,
    config: dict[str, Any] | None = None,
    schema_hint: str = "",
) -> RequirementMatrix:
    """Compile a natural language intent into a testable requirement matrix.

    This does NOT see the product code. Only the intent.
    """

    config = config or {}

    schema_hint_block = ""
    if schema_hint.strip():
        schema_hint_block = (
            "\nThe application uses these data models and field names:\n"
            f"{schema_hint.strip()}\n"
            "Use these exact field names in test step bodies, not guesses.\n"
        )

    prompt = INTENT_COMPILER_PROMPT.format(
        intent=intent,
        schema_hint_block=schema_hint_block,
    )

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=".",
        setting_sources=["project"],
        env=_subprocess_env(),
        effort="medium",
        max_turns=1,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )
    model = config.get("planner_model")
    if model:
        options.model = str(model)

    started_at = time.monotonic()
    raw_output, cost, _result = await run_agent_query(prompt, options)
    cost_usd = float(cost or 0.0)
    duration_s = round(time.monotonic() - started_at, 1)

    matrix = _parse_matrix(raw_output, intent)
    matrix.cost_usd = cost_usd
    matrix.compiled_at = time.strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        "Intent compiled: %d claims (%d critical), $%.3f, %.1fs",
        len(matrix.claims), len(matrix.critical_claims()), cost_usd, duration_s,
    )

    return matrix


def _parse_matrix(raw: str, intent: str) -> RequirementMatrix:
    """Parse the LLM output into a RequirementMatrix."""

    text = raw.strip()

    json_str = None
    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            json_str = parts[-1].split("```")[0].strip()
    if json_str is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            json_str = text[start : end + 1]
    if json_str is None:
        raise ValueError(f"No JSON in intent compiler output: {text[:300]}")

    data = json.loads(json_str)

    claims = []
    for item in data.get("claims", []):
        claims.append(
            Claim(
                id=item.get("id", ""),
                description=item.get("description", ""),
                priority=item.get("priority", "important"),
                category=item.get("category", "feature"),
                test_approach=item.get("test_approach", "api"),
                test_steps=_normalize_claim_steps(item),
                hard_fail=item.get("hard_fail", False),
            )
        )

    return RequirementMatrix(
        intent=intent,
        claims=claims,
        non_goals=data.get("non_goals", []),
        ambiguities=data.get("ambiguities", []),
        product_type_hint=data.get("product_type_hint", "unknown"),
    )


def save_matrix(matrix: RequirementMatrix, path: Path) -> None:
    """Save requirement matrix to JSON for reuse."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(matrix.to_dict(), indent=2))


def load_matrix(path: Path) -> RequirementMatrix:
    """Load a saved requirement matrix."""

    data = json.loads(path.read_text())
    claims = [
        Claim(
            id=item.get("id", ""),
            description=item.get("description", ""),
            priority=item.get("priority", "important"),
            category=item.get("category", "feature"),
            test_approach=item.get("test_approach", "api"),
            test_steps=_normalize_claim_steps(item),
            hard_fail=item.get("hard_fail", False),
        )
        for item in data.get("claims", [])
    ]
    return RequirementMatrix(
        intent=data["intent"],
        claims=claims,
        non_goals=data.get("non_goals", []),
        ambiguities=data.get("ambiguities", []),
        product_type_hint=data.get("product_type_hint", "unknown"),
        compiled_at=data.get("compiled_at", ""),
        cost_usd=data.get("cost_usd", 0.0),
    )


def _normalize_claim_steps(claim: dict[str, Any]) -> list[TestStep]:
    """Ensure every claim exposes structured, machine-executable steps."""

    raw_steps = claim.get("test_steps", [])
    if all(isinstance(step, dict) for step in raw_steps):
        return [_normalize_structured_step(step) for step in raw_steps]
    return [_generic_fallback_step(claim, raw_steps)]


def _normalize_structured_step(step: dict[str, Any]) -> TestStep:
    normalized = dict(step)
    normalized["action"] = str(normalized.get("action", "check_exists"))
    if normalized["action"] in {"http", "navigate"}:
        normalized.setdefault(
            "expect_status",
            [200] if normalized["action"] == "navigate" else [200, 201],
        )
    if normalized["action"] == "cli":
        normalized.setdefault("expect_exit_code", 0)
    if normalized["action"] == "check_exists":
        normalized.setdefault("target", "http")
    return normalized


def _generic_fallback_step(claim: dict[str, Any], raw_steps: list[Any]) -> TestStep:
    candidate_paths = _claim_candidate_paths(claim)
    step: TestStep = {
        "action": "check_exists",
        "target": "http" if candidate_paths else "note",
        "existence_statuses": [200, 201, 204, 302, 401, 403],
        "reason": (
            f"Missing structured test_steps for claim {claim.get('id') or claim.get('description') or 'unknown'}"
        ),
    }
    if candidate_paths:
        step["candidate_paths"] = candidate_paths
        step["path"] = candidate_paths[0]
    if raw_steps:
        step["raw_steps"] = raw_steps
    return step


def _claim_candidate_paths(claim: dict[str, Any]) -> list[str]:
    candidate_paths = claim.get("candidate_paths")
    if isinstance(candidate_paths, list) and candidate_paths:
        return [str(path) for path in candidate_paths]
    if claim.get("path"):
        return [str(claim["path"])]
    return []
