"""Journey compiler — generates user journeys from intent.

Like the intent compiler generates testable claims, this generates
testable user journeys. Each journey is a sequence of steps where
each step depends on the previous step's output.

The journeys are machine-executable — the Tier 2 runner can execute
them without LLM interpretation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

logger = logging.getLogger("otto.certifier.journey_compiler")


@dataclass
class JourneySpec:
    """A user journey to test end-to-end."""
    name: str
    description: str               # user-perspective story
    persona: str                   # "new_customer" | "returning_customer" | "admin" | "unauthenticated"
    steps: list[dict[str, Any]]    # machine-executable steps
    critical: bool = False         # must pass for certification


@dataclass
class JourneyMatrix:
    """All user journeys for a product."""
    intent: str
    journeys: list[JourneySpec]
    compiled_at: str = ""
    cost_usd: float = 0.0


JOURNEY_COMPILER_PROMPT = """\
You are a QA test designer. Given a product intent, generate comprehensive
user journeys that test the product end-to-end.

Intent: {intent}

Each journey is a sequence of executable steps where each step can depend on
previous outputs. Think like a real user of THIS product, not a generic store
template. Derive journeys from the ACTUAL intent.

Generate journeys for EVERY persona that would use this product:
- Unauthenticated visitors (public browsing or public read-only actions — NO
  registration needed)
- New users (registration + first meaningful action)
- Returning users (login, repeat actions)
- Admin users (management, configuration)
- Edge cases (empty states, errors, boundary conditions)

IMPORTANT: Unauthenticated journeys must NOT start with registration or login.
They test what a visitor can do WITHOUT an account. These journeys should
usually use fresh_session and read-only actions unless the intent explicitly
supports anonymous writes.

For each journey, provide:
- name: short descriptive name
- description: user-perspective story (1-2 sentences)
- persona: "new_customer" | "returning_customer" | "admin" | "unauthenticated"
- critical: true if this journey tests a core product promise
- steps: array of executable steps

Step types:
  register: {{action: "register", email: "...", password: "...", name: "..."}}
  login: {{action: "login", email: "...", password: "..."}}
  login_admin: {{action: "login_admin"}}
  get: {{action: "get", path: "/api/...", expect_status: 200, save_as: "variable_name"}}
  post: {{action: "post", path: "/api/...", body: {{...}}, expect_status: [200,201], save_as: "..."}}
  put: {{action: "put", path: "/api/...", body: {{...}}, expect_status: 200}}
  delete: {{action: "delete", path: "/api/...", expect_status: [200,204]}}
  verify_contains: {{action: "verify", source: "variable_name", contains: "expected_text"}}
  verify_count: {{action: "verify_count", source: "variable_name", min: 1}}
  fresh_session: {{action: "fresh_session"}}  // start new unauthenticated session

Variables: steps can reference previous outputs via {{{{var_name}}}}

RULES:
- Use generic reasoning: primary entity, core user action, CRUD flow,
  persistence, permissions, and recovery/error states.
- Generate 5-10 journeys covering ALL major product features
- Each journey should test a REALISTIC user flow, not just one endpoint
- Critical journeys must cover: the primary user value proposition
- Include at least one error/edge case journey
- Include at least one "state persistence" journey (data survives across sessions)
- Steps must be specific: include real field names, realistic test data
- Include candidate_paths when route shape may vary across implementations
- Keep auth journeys when the intent implies accounts, admin roles, or protected
  functionality
- Do not invent commerce flows like catalog, cart, checkout, or orders unless
  the intent actually describes them

Output JSON only:
{{
  "journeys": [
    {{
      "name": "New User Creates And Manages A Task",
      "description": "A new user signs up, creates a task, confirms it appears in their list, updates it, and deletes it",
      "persona": "new_customer",
      "critical": true,
      "steps": [
        {{"action": "register", "email": "newuser@test.com", "password": "Test123!", "name": "Test User"}},
        {{"action": "login", "email": "newuser@test.com", "password": "Test123!"}},
        {{"action": "post", "path": "/api/tasks", "candidate_paths": ["/api/tasks", "/api/task"], "body": {{"title": "Pay rent", "description": "Due Friday", "status": "open"}}, "expect_status": [200, 201], "save_as": "created_task"}},
        {{"action": "get", "path": "/api/tasks", "candidate_paths": ["/api/tasks", "/tasks"], "expect_status": 200, "save_as": "tasks"}},
        {{"action": "verify_contains", "source": "tasks", "contains": "Pay rent"}},
        {{"action": "put", "path": "/api/tasks/{{{{last_created_id}}}}", "candidate_paths": ["/api/tasks/{{{{last_created_id}}}}", "/api/task/{{{{last_created_id}}}}"], "body": {{"title": "Pay rent", "description": "Paid", "status": "done"}}, "expect_status": [200, 204]}},
        {{"action": "get", "path": "/api/tasks", "candidate_paths": ["/api/tasks", "/tasks"], "expect_status": 200, "save_as": "updated_tasks"}},
        {{"action": "verify_contains", "source": "updated_tasks", "contains": "done"}},
        {{"action": "delete", "path": "/api/tasks/{{{{last_created_id}}}}", "candidate_paths": ["/api/tasks/{{{{last_created_id}}}}", "/api/task/{{{{last_created_id}}}}"], "expect_status": [200, 204]}}
      ]
    }}
  ]
}}
"""


async def compile_journeys(
    intent: str,
    config: dict[str, Any] | None = None,
) -> JourneyMatrix:
    """Compile user journeys from intent. One LLM call."""
    config = config or {}

    prompt = JOURNEY_COMPILER_PROMPT.format(intent=intent)

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

    matrix = _parse_journeys(raw_output, intent)
    matrix.cost_usd = cost_usd
    matrix.compiled_at = time.strftime("%Y-%m-%d %H:%M:%S")

    logger.info("Compiled %d journeys ($%.3f)", len(matrix.journeys), cost_usd)
    return matrix


def _parse_journeys(raw: str, intent: str) -> JourneyMatrix:
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
            json_str = text[start:end + 1]
    if json_str is None:
        raise ValueError(f"No JSON in journey compiler output: {text[:300]}")

    data = json.loads(json_str)
    journeys = []
    for item in data.get("journeys", []):
        journeys.append(JourneySpec(
            name=item.get("name", ""),
            description=item.get("description", ""),
            persona=item.get("persona", "unknown"),
            steps=item.get("steps", []),
            critical=item.get("critical", False),
        ))

    return JourneyMatrix(intent=intent, journeys=journeys)


def save_journey_matrix(matrix: JourneyMatrix, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(matrix), indent=2, default=str))


def load_journey_matrix(path: Path) -> JourneyMatrix:
    data = json.loads(path.read_text())
    journeys = [JourneySpec(**j) for j in data.get("journeys", [])]
    return JourneyMatrix(
        intent=data["intent"],
        journeys=journeys,
        compiled_at=data.get("compiled_at", ""),
        cost_usd=data.get("cost_usd", 0.0),
    )
