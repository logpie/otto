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
Design user stories that test this product end-to-end as real users would use it.

Intent: {intent}

REQUIRED STORIES (generate ALL that apply):
1. **First Experience**: New user registers, uses core feature, verifies it worked.
2. **Feature Lifecycle**: Create → modify → verify changes → delete. Full CRUD.
3. **Data Isolation**: Two users create data. Each only sees their own.
4. **Persistence**: Create data, end session, start new session, verify data persists.
5. **Access Control**: Unauthenticated request to protected features → rejected.
6. **Search/Filter** (if applicable): Create items with different attributes, filter, verify.
7. **Edge Cases**: Empty inputs, long strings, special characters in realistic flows.

RULES:
- Steps describe WHAT to do and verify, in plain English. No HTTP methods or paths.
- Include data dependencies between steps: "use the item created in step 2"
- 5-8 stories, 2-4 steps each. Keep stories focused — one integration concern each.
- Do NOT include operational steps (restart server, edit files, modify database).
- For persistence: "end the session and start a new one", NOT "restart the server".
- break_strategies: optional adversarial tests per story (double_submit, long_input, special_chars, id_guessing). Can be empty.

STORY IDs: stable kebab-case slug based on WHAT the story tests (e.g. "first-experience",
"crud-lifecycle", "data-isolation", "persistence", "access-control"). Same intent should
produce the same ids across compilations.

Output JSON only:
{{
  "stories": [
    {{
      "id": "first-experience",
      "persona": "new_user",
      "title": "New User First Experience",
      "narrative": "A new user registers and uses the core feature for the first time.",
      "critical": true,
      "tests_integration": ["auth", "core-feature"],
      "break_strategies": [],
      "steps": [
        {{
          "action": "register a new account with email and password",
          "verify": "registration succeeds, user can access the product",
          "entity": "user",
          "operation": "auth",
          "mode": "api"
        }},
        {{
          "action": "use the product's main feature",
          "verify": "feature works correctly, data is created/returned",
          "entity": "",
          "operation": "create",
          "mode": "api",
          "uses_output_from": 0
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
