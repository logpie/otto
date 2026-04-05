"""Story compiler — generates user stories from intent.

Compiles a product intent into testable user stories. Each story represents
a realistic user scenario that tests features working together end-to-end.

Stories are product-independent (no HTTP paths, field names, or status codes).
They describe WHAT to test from the user's perspective, not HOW. The journey
agent figures out HOW using the product manifest.

Stories are shareable across products via --stories flag for fair comparison.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

logger = logging.getLogger("otto.certifier.stories")


def _fix_json_newlines(s: str) -> str:
    """Fix literal newlines inside JSON string values.

    LLMs often produce JSON with real line breaks inside quoted strings
    instead of \\n escape sequences. This scans the string and replaces
    literal newlines (0x0a) that appear inside quoted values.
    """
    result = []
    in_string = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and in_string and i + 1 < len(s):
            # Escaped character — pass through both chars
            result.append(c)
            result.append(s[i + 1])
            i += 2
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
        elif c == "\n" and in_string:
            result.append("\\n")
        elif c == "\r" and in_string:
            result.append("\\r")
        elif c == "\t" and in_string:
            result.append("\\t")
        else:
            result.append(c)
        i += 1
    return "".join(result)


@dataclass
class StoryStep:
    """A single step in a user story."""

    action: str  # "create a task with title 'Buy groceries'"
    verify: str  # "task created, appears with correct title"
    verify_in_browser: str = ""  # "task appears in the task list on screen"
    entity: str = ""  # "task" — the domain object
    operation: str = ""  # "create" | "read" | "list" | "update" | "delete" | "auth"
    mode: str = "api"  # "api" | "browser" | "both"
    uses_output_from: int | None = None  # step index that produces data this step needs


@dataclass
class UserStory:
    """A user story to test end-to-end."""

    id: str  # "new-user-first-task"
    persona: str  # "new_user" | "returning_user" | "admin" | "visitor"
    title: str  # "New User Creates Their First Task"
    narrative: str  # user-perspective description of the whole flow
    steps: list[StoryStep]
    critical: bool = False  # must pass for certification
    tests_integration: list[str] = field(default_factory=list)  # ["auth", "task-crud"]
    break_strategies: list[str] = field(default_factory=list)  # ["double_submit", "long_input"]


@dataclass
class StorySet:
    """All user stories for a product intent."""

    intent: str
    stories: list[UserStory]
    compiled_at: str = ""
    cost_usd: float = 0.0


STORY_COMPILER_PROMPT = """\
You are a QA test designer. Given a product intent, design user stories that test
the product END-TO-END as real users would use it.

Intent: {intent}

Each story is a realistic scenario from a specific persona's perspective.
Stories test INTEGRATION — features working together — not individual endpoints.

PERSONAS:
- new_user: first-time user (register → first action → verify)
- returning_user: existing user (login → use features → verify state)
- admin: administrator (login → manage → verify admin capabilities)
- visitor: unauthenticated person (try to access → verify rejection)

REQUIRED STORIES (generate ALL of these):
1. **First Experience**: A new user registers, uses the core feature for the first time,
   and verifies it worked. This tests the primary value proposition.
2. **Feature Lifecycle**: Create something, modify it, verify changes, delete it.
   Tests the full CRUD cycle and state consistency.
3. **Data Isolation**: Two different users each create data. Verify each user only
   sees their own data. Tests multi-user security.
4. **Persistence**: Create data, log out, log back in, verify data is still there.
   Tests that data is durable, not just in-memory.
5. **Access Control**: An unauthenticated visitor tries to use protected features.
   Tests that auth is enforced.
6. **Search/Filter** (if applicable): Create multiple items with different attributes,
   then search/filter and verify correct results.
7. **Edge Cases**: Try common edge cases within realistic flows — empty inputs,
   very long strings, special characters in names.

For each story, also specify BREAK STRATEGIES — things to try after the happy path
to see if the product handles edge cases well:
- double_submit: submit the same form/request twice rapidly
- long_input: use very long strings (1000+ chars)
- empty_required: submit with empty required fields
- special_chars: use special characters (emoji, unicode, HTML tags)
- id_guessing: try to access another user's data by guessing IDs
- direct_url: navigate directly to protected URLs without auth
- back_after_submit: go back in browser after form submission
- concurrent_modify: modify the same resource from two sessions

RULES:
- Each step describes WHAT to do and WHAT to verify, in plain English
- Do NOT include HTTP methods, paths, field names, or status codes
- Do NOT assume any specific API convention
- Steps should be written as instructions to a human tester
- Mark which steps should be verified via browser (UI/UX check) vs API (data check)
- Include data dependencies: "use the task created in step 2"
- Generate 5-8 stories covering ALL features mentioned in the intent
- Stories must only describe actions through the product's user interface (API requests, CLI commands, browser interactions)
- Do NOT include operational steps like: restart server, kill process, edit source files, modify database directly, change environment variables, install packages
- For persistence testing, use "close the session and start a new one" NOT "restart the server"

STORY IDs:
Each story must have a stable "id" field — a short kebab-case slug describing the scenario's
INTENT (e.g. "first-todo-crud", "persist-across-sessions", "two-user-data-isolation",
"unauth-access-denied", "edge-case-inputs"). The id should be based on WHAT the story tests,
not the exact title wording. Two compilations of the same intent should produce the same ids
even if titles differ slightly.

Output JSON only:
{{
  "stories": [
    {{
      "id": "new-user-first-task",
      "persona": "new_user",
      "title": "New User Creates Their First Task",
      "narrative": "A new user registers, creates their first task, and verifies it appears in their list.",
      "critical": true,
      "tests_integration": ["auth", "task-crud", "task-list"],
      "break_strategies": ["double_submit", "long_input", "special_chars"],
      "steps": [
        {{
          "action": "register a new account with email and password",
          "verify": "registration succeeds, user is logged in or can log in",
          "verify_in_browser": "registration form submits successfully, redirected to main page",
          "entity": "user",
          "operation": "auth",
          "mode": "both"
        }},
        {{
          "action": "create a task with a specific title and description",
          "verify": "task is created and returned with an ID",
          "verify_in_browser": "task appears in the task list on the page",
          "entity": "task",
          "operation": "create",
          "mode": "both",
          "uses_output_from": 0
        }},
        {{
          "action": "list all tasks",
          "verify": "the created task appears in the list with correct title",
          "verify_in_browser": "task list shows the task with its title and details",
          "entity": "task",
          "operation": "list",
          "mode": "both",
          "uses_output_from": 1
        }}
      ]
    }}
  ]
}}
"""


async def compile_stories(
    intent: str,
    config: dict[str, Any] | None = None,
) -> StorySet:
    """Compile user stories from intent. One LLM call."""
    config = config or {}

    prompt = STORY_COMPILER_PROMPT.format(intent=intent)

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

    story_set = _parse_stories(raw_output, intent)
    story_set.cost_usd = cost_usd
    story_set.compiled_at = time.strftime("%Y-%m-%d %H:%M:%S")

    logger.info("Compiled %d stories ($%.3f)", len(story_set.stories), cost_usd)
    return story_set


def _slugify(text: str, max_len: int = 60) -> str:
    """Convert a title/narrative into a kebab-case slug for use as a fallback story ID."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug or "story"


def _parse_stories(raw: str, intent: str) -> StorySet:
    """Parse LLM output into StorySet."""
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
        raise ValueError(f"No JSON in story compiler output: {text[:300]}")

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # LLMs often produce literal newlines inside JSON string values.
        # Attempt repair (same fix as product_planner._fix_json_newlines).
        data = json.loads(_fix_json_newlines(json_str))
    stories = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(data.get("stories", [])):
        steps = []
        for s in item.get("steps", []):
            steps.append(
                StoryStep(
                    action=s.get("action", ""),
                    verify=s.get("verify", ""),
                    verify_in_browser=s.get("verify_in_browser", ""),
                    entity=s.get("entity", ""),
                    operation=s.get("operation", ""),
                    mode=s.get("mode", "api"),
                    uses_output_from=s.get("uses_output_from"),
                )
            )
        # Use LLM-provided id; fall back to slug from narrative (intent-based)
        # then title, then index.
        story_id = item.get("id", "").strip()
        if not story_id:
            source = item.get("narrative", "") or item.get("title", "")
            story_id = _slugify(source) if source else f"story-{idx}"
        # Deduplicate ids
        base_id = story_id
        counter = 2
        while story_id in seen_ids:
            story_id = f"{base_id}-{counter}"
            counter += 1
        seen_ids.add(story_id)
        stories.append(
            UserStory(
                id=story_id,
                persona=item.get("persona", "unknown"),
                title=item.get("title", ""),
                narrative=item.get("narrative", ""),
                steps=steps,
                critical=item.get("critical", False),
                tests_integration=item.get("tests_integration", []),
                break_strategies=item.get("break_strategies", []),
            )
        )

    return StorySet(intent=intent, stories=stories)


# --- Caching ---


def story_cache_path(project_dir: Path, intent: str) -> Path:
    digest = hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
    cache_dir = project_dir / "otto_logs" / "certifier"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"stories-{digest}.json"


def save_stories(story_set: StorySet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(story_set), indent=2, default=str))


def load_stories(path: Path) -> StorySet:
    data = json.loads(path.read_text())
    stories = []
    for item in data.get("stories", []):
        steps = [StoryStep(**s) for s in item.get("steps", [])]
        story = UserStory(
            id=item["id"],
            persona=item.get("persona", "unknown"),
            title=item.get("title", ""),
            narrative=item.get("narrative", ""),
            steps=steps,
            critical=item.get("critical", False),
            tests_integration=item.get("tests_integration", []),
            break_strategies=item.get("break_strategies", []),
        )
        stories.append(story)
    return StorySet(
        intent=data["intent"],
        stories=stories,
        compiled_at=data.get("compiled_at", ""),
        cost_usd=data.get("cost_usd", 0.0),
    )


def load_or_compile_stories(
    project_dir: Path,
    intent: str,
    config: dict[str, Any] | None = None,
) -> tuple[StorySet, str, Path, float]:
    """Return cached stories when available, otherwise compile."""
    import asyncio

    cache = story_cache_path(project_dir, intent)
    if cache.exists():
        return load_stories(cache), "cache", cache, 0.0

    started_at = time.monotonic()
    story_set = asyncio.run(compile_stories(intent, config=config))
    duration = round(time.monotonic() - started_at, 1)
    save_stories(story_set, cache)
    return story_set, "compiled", cache, duration
