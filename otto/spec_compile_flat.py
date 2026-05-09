"""Flat spec compilation for v5.

Replaces v4's group/contract synthesis. Only emits:
    - the user's intent (verbatim)
    - project_kind (detected)
    - behavior_journeys[]: list of user-language journey descriptions

NO groups, NO owned_paths, NO shared_contracts, NO frozen ownership of any kind.
The Lead at runtime decides decomposition; integration audit at every merge node
provides scope accountability.

Behavior journeys MUST be written in user-language. A lint pass rejects
implementation-language tokens (CSS selectors, getByRole, data-testid, etc.)
and re-prompts the compiler on rejection. After 2 retries, the compile is
written with a `lint_warnings` list so the user can see the issue but the run
continues (best-effort).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.agent import make_agent_options
from otto.observability import save_rendered_prompt, sha256_text, update_input_provenance
from otto.paths import session_intent

logger = logging.getLogger("otto.spec_compile_flat")

SCHEMA_VERSION = 1


@dataclass
class FlatSpec:
    """The minimal spec v5 produces. Intent + journeys, nothing else."""

    schema_version: int = SCHEMA_VERSION
    intent: str = ""
    intent_hash: str = ""
    project_kind: str = "webapp"
    behavior_journeys: list[dict[str, Any]] = field(default_factory=list)
    lint_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Behavior journey lint
# ---------------------------------------------------------------------------

# Patterns that indicate implementation-language leakage in journey text.
# A behavior journey is supposed to read like a user manual ("the user clicks
# 'Save'"), not like a Playwright test ("page.getByRole('button', ...)").
_IMPL_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bclass\s*=\s*['\"][^'\"]+['\"]", "CSS class selector"),
    (r"\bid\s*=\s*['\"][^'\"]+['\"]", "DOM id selector"),
    (r"\bdata-testid\b", "data-testid attribute"),
    (r"\bgetByRole\b", "Playwright getByRole"),
    (r"\bgetByText\b", "Playwright getByText"),
    (r"\bquerySelector\b", "DOM querySelector"),
    (r"page\.\w+\(", "Playwright page.*() API"),
    (r"\.locator\(", "Playwright locator"),
    (r"\bbody\s*>\s*", "CSS descendant selector"),
    (r"#[a-zA-Z][\w-]*\s*\{", "CSS id rule"),
    (r"\.[a-zA-Z][\w-]*\s*\{", "CSS class rule"),
)


def lint_journey(text: str) -> list[str]:
    """Return a list of human-readable warnings for one journey description.

    Empty list = clean (user-language only).
    """
    issues: list[str] = []
    for pattern, label in _IMPL_LEAK_PATTERNS:
        if re.search(pattern, text):
            issues.append(f"contains {label}")
    return issues


def lint_spec(spec: FlatSpec) -> list[str]:
    """Lint every behavior journey. Returns aggregated warnings."""
    warnings: list[str] = []
    for j in spec.behavior_journeys:
        jid = j.get("id") or "<unnamed>"
        desc = j.get("description") or ""
        per_journey = lint_journey(desc)
        for issue in per_journey:
            warnings.append(f"journey {jid!r}: {issue}")
    return warnings


# ---------------------------------------------------------------------------
# Compile entrypoint
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """You are a product spec compiler. Your job: read the user's intent and emit a JSON object describing the product as a flat list of user-visible behavior journeys.

INTENT:
{intent}

OUTPUT a single JSON object with this exact shape (no prose, no fences, just JSON):
{{
  "project_kind": "webapp" | "cli" | "api" | "library" | "service",
  "behavior_journeys": [
    {{
      "id": "snake_case_short_id",
      "description": "User-language steps describing what happens. Like a manual entry."
    }}
  ]
}}

RULES (HARD — do NOT violate):
1. Behavior journeys MUST be in user-language. They describe what a USER does and SEES.
   GOOD: "User clicks 'Add Transaction', enters $50 with category 'Food', saves. The new transaction appears in the list."
   BAD:  "Click element with class .add-btn. Verify .txn-list has data-testid='row'."
   NEVER use: CSS selectors, getByRole, getByText, querySelector, .locator(), data-testid, DOM ids.

2. Aim for 5-12 journeys. Each journey covers one coherent user-visible behavior.

3. Cover the full intent. If the intent says CSV export, there is a CSV export journey. If it says responsive UI, there is a journey that asserts mobile usability. If it says empty states, there is a journey for the fresh-app case.

4. Include integration journeys: at least one journey should cross multiple features (e.g., "import CSV then filter the imported transactions").

5. Each id is snake_case, unique, ≤32 chars.
"""


_PROMPT_RETRY_SUFFIX = """

YOUR PREVIOUS OUTPUT WAS REJECTED FOR LINT VIOLATIONS:
{warnings}

Re-emit the JSON. Re-write each flagged journey in user-language. No DOM selectors anywhere.
"""


async def compile_flat_spec(
    project_dir: Path,
    session_dir: Path,
    intent: str,
    config: dict[str, Any],
    *,
    project_kind_hint: str | None = None,
    max_retries: int = 2,
) -> FlatSpec:
    """Compile a flat spec for the user's intent.

    Best-effort: on lint rejection, re-prompt up to ``max_retries`` times.
    After exhaustion, accept the latest output but populate ``lint_warnings``
    so the user can see the issue. Run continues; the lint is advisory.
    """
    intent = (intent or "").strip()
    if not intent:
        raise ValueError("compile_flat_spec: intent is empty")

    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)

    # Persist intent verbatim per philosophy invariant.
    intent_path = session_intent(project_dir, session_dir.name)
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(intent, encoding="utf-8")

    intent_h = sha256_text(intent)

    options = make_agent_options(project_dir, config, agent_type="spec")
    # Force structured output via the SDK's output_format machinery.
    setattr(
        options,
        "output_format",
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "project_kind": {"type": "string"},
                    "behavior_journeys": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["id", "description"],
                        },
                    },
                },
                "required": ["project_kind", "behavior_journeys"],
            },
        },
    )

    # Single-turn compile with retry-on-lint-failure.
    last_warnings: list[str] = []
    parsed: dict[str, Any] | None = None
    preview = FlatSpec(intent=intent, intent_hash=intent_h)
    prompt_text = _PROMPT_TEMPLATE.format(intent=intent)
    prompt_entry: dict[str, str] = {"template": "compile-spec-flat", "rendered_sha256": "", "rendered_path": ""}
    spec: FlatSpec = preview
    accepted = False
    for attempt in range(1, max_retries + 2):  # initial + max_retries
        if attempt == 1:
            prompt_text = _PROMPT_TEMPLATE.format(intent=intent)
        else:
            prompt_text = _PROMPT_TEMPLATE.format(intent=intent) + _PROMPT_RETRY_SUFFIX.format(
                warnings="\n".join(f"  - {w}" for w in last_warnings)
            )

        prompt_subdir = spec_dir / ("compile-agent" if attempt == 1 else f"compile-agent-retry-{attempt:02d}")
        prompt_subdir.mkdir(parents=True, exist_ok=True)
        prompt_entry = save_rendered_prompt(
            prompts_dir=session_dir / "prompts",
            template="compile-spec-flat",
            rendered_text=prompt_text,
        )

        result_text = await _run_compile(prompt_text, options, prompt_subdir, project_dir)

        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError as exc:
            logger.warning("compile_flat_spec attempt %d: invalid JSON (%s); retrying", attempt, exc)
            last_warnings = [f"output was not valid JSON: {exc}"]
            continue

        # Build a FlatSpec preview to lint.
        if not isinstance(parsed, dict):
            last_warnings = [f"output was not a JSON object (got {type(parsed).__name__})"]
            continue
        journeys_raw = parsed.get("behavior_journeys")
        if not isinstance(journeys_raw, list):
            last_warnings = ["output 'behavior_journeys' is not a list"]
            continue

        preview = FlatSpec(
            intent=intent,
            intent_hash=intent_h,
            project_kind=str(parsed.get("project_kind") or project_kind_hint or "webapp"),
            behavior_journeys=[
                {"id": str(j.get("id") or ""), "description": str(j.get("description") or "")}
                for j in journeys_raw
                if isinstance(j, dict)
            ],
        )
        warnings = lint_spec(preview)
        if not warnings:
            spec = preview
            accepted = True
            break
        last_warnings = warnings
        logger.warning(
            "compile_flat_spec attempt %d: %d lint warnings; retrying",
            attempt,
            len(warnings),
        )
    if not accepted:
        # All attempts had lint warnings. Best-effort: accept anyway but record warnings.
        spec = preview
        spec.lint_warnings = last_warnings
        logger.warning(
            "compile_flat_spec: lint warnings persist after %d attempts; accepting anyway (best-effort)",
            max_retries + 1,
        )

    # Persist spec.json.
    spec_path = spec_dir / "spec.json"
    spec_path.write_text(_serialize_spec(spec) + "\n", encoding="utf-8")

    # Update input provenance.
    update_input_provenance(
        session_dir=session_dir,
        intent={
            "fallback_reason": "",
            "resolved_text": intent,
            "sha256": intent_h,
            "source": "cli-argument",
        },
        spec={"source": "compile-agent-flat", "path": str(spec_path), "sha256": ""},
        prompts=[prompt_entry],
    )

    return spec


async def _run_compile(prompt: str, options: Any, log_dir: Path, project_dir: Path) -> str:
    """Run one compile attempt. Returns the LLM's text output."""
    from otto.agent import run_agent_with_timeout

    text, _cost, _session_id, _breakdown = await run_agent_with_timeout(
        prompt,
        options,
        log_dir=log_dir,
        phase_name="SPEC_COMPILE_FLAT",
        phase_label="compile-flat",
        timeout=int(options.max_turns or 60) * 30,  # generous; small prompt
        project_dir=project_dir,
    )
    return _extract_first_json_object(text or "")


def _extract_first_json_object(text: str) -> str:
    """Pull the first balanced JSON object out of free-form LLM output.

    Handles: bare object, ```json fenced block, JSON followed by explanation
    prose, JSON preceded by a header.
    """
    text = text.strip()
    # First, strip code fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    # Find the first '{' and walk to the matching '}'.
    start = text.find("{")
    if start < 0:
        return text  # no object; return as-is for json.loads to fail with clear error
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]  # unterminated; let parser report


def _serialize_spec(spec: FlatSpec) -> str:
    return json.dumps(
        {
            "schema_version": spec.schema_version,
            "intent": spec.intent,
            "intent_hash": spec.intent_hash,
            "project_kind": spec.project_kind,
            "behavior_journeys": list(spec.behavior_journeys),
            "lint_warnings": list(spec.lint_warnings),
        },
        indent=2,
    )


def load_flat_spec(spec_path: Path) -> FlatSpec:
    """Load a previously-compiled flat spec from disk."""
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    return FlatSpec(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        intent=str(data.get("intent", "")),
        intent_hash=str(data.get("intent_hash", "")),
        project_kind=str(data.get("project_kind", "webapp")),
        behavior_journeys=list(data.get("behavior_journeys", [])),
        lint_warnings=list(data.get("lint_warnings", [])),
    )
