"""A0.3 — Slice → Group vocabulary lint for otto/prompts/*.md.

Free-form prose must use "group" terminology. The literal token "slice"
(case-insensitive) is only allowed inside contexts owned by separate
wire-format cutovers:

* Fenced JSON code blocks (the spec wire-format examples that still emit
  the legacy ``slices`` key under dual-write back-compat).
* The ``<spec_json>...</spec_json>`` envelope marker mentioned by name.

Any other appearance of "slice" / "Slice" / "slices" / "Slices" is a
regression of the prompt-prose rename and fails this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).parent.parent / "otto" / "prompts"

# Match the literal word "slice" (or "slices"/"slice's"/"sliced" etc.) on a
# whole-word boundary, case-insensitive.
SLICE_WORD = re.compile(r"\bslice", re.IGNORECASE)

# Allowed substrings on a line keep the rename non-disruptive.
ALLOWED_LINE_SUBSTRINGS = (
    "<spec_json>",  # references to the envelope marker by name
    "</spec_json>",
)


def _strip_fenced_code_blocks(text: str) -> list[tuple[int, str]]:
    """Yield (line_number, line) pairs OUTSIDE fenced ``` blocks.

    The compile-spec.md prompt embeds a literal spec example wrapped in
    ```json ... ``` plus other ```python / ```json snippets. Those are wire
    format / illustrative examples and may legitimately contain "slice".
    """
    in_fence = False
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((idx, line))
    return out


def _line_is_allowed(line: str) -> bool:
    for token in ALLOWED_LINE_SUBSTRINGS:
        if token in line:
            return True
    return False


@pytest.mark.parametrize(
    "prompt_path",
    sorted(PROMPTS_DIR.glob("*.md")),
    ids=lambda p: p.name,
)
def test_prose_uses_group_not_slice(prompt_path: Path) -> None:
    text = prompt_path.read_text()
    offenders: list[str] = []
    for lineno, line in _strip_fenced_code_blocks(text):
        if not SLICE_WORD.search(line):
            continue
        if _line_is_allowed(line):
            continue
        offenders.append(f"{prompt_path.name}:{lineno}: {line.rstrip()}")
    assert not offenders, (
        "Prompt prose must use 'group' vocabulary (A0.3). "
        "Found legacy 'slice' tokens outside fenced code blocks:\n"
        + "\n".join(offenders)
    )


def test_compile_spec_json_example_still_uses_slices_key() -> None:
    """Wire-format JSON example MUST keep emitting ``slices`` under dual-write.

    The Spec dataclass dual-writes both ``groups`` and ``slices`` for one
    cycle (see ``otto/spec_compile.py``); the prompt example mirrors the
    canonical wire payload, so the legacy key must still appear inside the
    fenced JSON block. If/when the dual-write cycle ends, this test will
    flip alongside the wire cutover.
    """
    text = (PROMPTS_DIR / "compile-spec.md").read_text()
    # The legacy key shows up as `"slices":` inside the example JSON block.
    assert '"slices":' in text, (
        "compile-spec.md no longer emits the legacy `slices` JSON key in its "
        "spec example — that's the separate wire-format cutover, not part "
        "of the prompt-prose rename."
    )


def test_prompt_files_referenced_by_module_render_clean() -> None:
    """Every prompt must still render via otto.prompts.render_prompt.

    Catches the case where a rename accidentally introduces an unknown
    ``{placeholder}`` token or breaks Markdown structure.
    """
    from otto.prompts import render_prompt  # local import: keeps test list short

    for prompt_path in sorted(PROMPTS_DIR.glob("*.md")):
        rendered = render_prompt(prompt_path.name)
        assert isinstance(rendered, str)
        assert len(rendered) > 0, f"{prompt_path.name} rendered empty"
