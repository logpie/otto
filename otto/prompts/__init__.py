"""Otto prompts — externalized for easy iteration without code changes."""

from __future__ import annotations

import re
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent

# Placeholders that otto ships in prompt files. Every key in this set must be
# accepted by render_prompt() with a sensible empty default so call sites can
# pass only what they know.
_KNOWN_PLACEHOLDERS = frozenset({
    "intent",
    "spec_section",
    "prior_spec_section",
    "spec_path",
    "focus_section",
    "stories_section",   # Phase 4.0 — must-verify story subset for merge cert
    "max_certify_rounds",
    "evidence_dir",
    "target",
    "strict_mode",
    "project_context",
    "project_intent_section",
    "merge_section",
})

_PLACEHOLDER_PATTERN = re.compile(
    r"\{(" + "|".join(map(re.escape, sorted(_KNOWN_PLACEHOLDERS))) + r")\}"
)

def _load(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


def render_prompt(name: str, **vars: object) -> str:
    """Load a prompt file and substitute `{placeholder}` tokens.

    Missing placeholders render as empty string (not KeyError). Unknown-to-otto
    keys in `vars` are allowed but unused. Replacement is limited to Otto's
    known placeholder names so literal `{...}` sequences elsewhere in prompt
    files survive unchanged.
    """
    text = _load(name)
    return _PLACEHOLDER_PATTERN.sub(
        lambda match: "" if vars.get(match.group(1), "") is None else str(vars.get(match.group(1), "")),
        text,
    )
