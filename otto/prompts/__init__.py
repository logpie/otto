"""Otto prompts — externalized for easy iteration without code changes."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def _load(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


def build_prompt() -> str:
    """The v3 agentic build prompt (includes certification steps 7-9)."""
    return _load("build.md")


def code_prompt() -> str:
    """Code-only prompt (steps 1-6: explore, plan, build, test, review, commit).

    Used by system-driven modes where certification is handled externally.
    No certification knowledge — the agent just builds/fixes code.
    """
    return _load("code.md")


def certifier_prompt(*, mode: str = "standard") -> str:
    """The certifier prompt (with {intent} and {evidence_dir} placeholders).

    Modes:
      standard  — verify product works (quick, default)
      thorough  — find what's broken (adversarial, for otto improve)
      hillclimb — suggest product improvements (for otto improve --mode quality)
    """
    prompts = {
        "standard": "certifier.md",
        "thorough": "certifier-thorough.md",
        "hillclimb": "certifier-hillclimb.md",
    }
    return _load(prompts.get(mode, "certifier.md"))
