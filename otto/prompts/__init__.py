"""Otto prompts — externalized for easy iteration without code changes."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def _load(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


def build_prompt() -> str:
    """The v3 agentic build prompt."""
    return _load("build.md")


def certifier_prompt() -> str:
    """The certifier prompt (with {intent} and {evidence_dir} placeholders)."""
    return _load("certifier.md")
