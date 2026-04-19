"""Otto prompts — externalized for easy iteration without code changes."""

from __future__ import annotations

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
    "max_certify_rounds",
    "evidence_dir",
    "target",
})

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
    for key in _KNOWN_PLACEHOLDERS:
        placeholder = "{" + key + "}"
        value = vars.get(key, "")
        text = text.replace(placeholder, "" if value is None else str(value))
    return text


def build_prompt() -> str:
    """The v3 agentic build prompt (raw text — placeholders not substituted)."""
    return _load("build.md")


def improve_prompt() -> str:
    """Improve prompt — certify first, then fix. Agent drives the loop."""
    return _load("improve.md")


def code_prompt() -> str:
    """Code-only prompt (steps 1-6: explore, plan, build, test, review, commit)."""
    return _load("code.md")


def certifier_prompt(*, mode: str = "standard") -> str:
    """The certifier prompt (with placeholders, raw text).

    Modes:
      standard  — verify product works (quick, default)
      fast      — happy path smoke test
      thorough  — find what's broken (adversarial)
      hillclimb — suggest product improvements
      target    — measure metric against threshold
    """
    prompts = {
        "standard": "certifier.md",
        "fast": "certifier-fast.md",
        "thorough": "certifier-thorough.md",
        "hillclimb": "certifier-hillclimb.md",
        "target": "certifier-target.md",
    }
    return _load(prompts.get(mode, "certifier.md"))


def spec_prompt() -> str:
    """The spec agent prompt (light variant, raw text with placeholders)."""
    return _load("spec-light.md")
