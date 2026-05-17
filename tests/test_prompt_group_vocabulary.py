"""A0.3 — Slice → Group vocabulary lint for otto/prompts/*.md.

Free-form prose must use "group" terminology. The literal token "slice"
(case-insensitive) is only allowed inside contexts owned by separate
wire-format cutovers:

* Fenced JSON code blocks (the spec wire-format examples that still emit
  the legacy ``slices`` key under dual-write back-compat).
* The ``<spec_json>...</spec_json>`` envelope marker mentioned by name.

Any other appearance of "slice" / "Slice" / "groups" / "Slices" is a
regression of the prompt-prose rename and fails this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).parent.parent / "otto" / "prompts"

# Match the literal word "slice" (or "groups"/"slice's"/"sliced" etc.) on a
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


def test_compile_spec_json_example_uses_groups_key() -> None:
    """Wire-format JSON example uses canonical ``groups`` key (post-A1 cutover).

    Dual-emit of legacy ``slices`` was dropped in A1; the prompt
    example mirrors the canonical wire payload.
    """
    text = (PROMPTS_DIR / "compile-spec.md").read_text()
    assert '"groups":' in text
    assert '"slices":' not in text


def test_compile_spec_warns_against_monolithic_shared_store_contention() -> None:
    text = (PROMPTS_DIR / "compile-spec.md").read_text()
    assert "Shared stores/data contracts need the same treatment" in text
    assert "all need to add methods to one store file" in text
    assert "mark the contested file shared" in text


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


def test_runtime_agent_prompt_policy_snippets_are_persistent_files() -> None:
    """Large reusable agent instructions live in otto/prompts, not Python literals."""
    expected = {
        "build.py": (
            "build-merge-repair.md",
            "build-layer2-regression-requirement.md",
            "build-agent-static-policy.md",
            "build-final-instruction.md",
        ),
        "audit.py": ("audit-final-task.md",),
        "spec_compile.py": (
            "compile-spec-brownfield-baseline-guidance.md",
            "compile-spec-brownfield-target-guidance.md",
            "compile-spec-structured-output.md",
        ),
    }

    for source_name, prompt_names in expected.items():
        source = (PROMPTS_DIR.parent / source_name).read_text(encoding="utf-8")
        for prompt_name in prompt_names:
            assert (PROMPTS_DIR / prompt_name).exists()
            assert prompt_name in source


def test_build_agent_policy_requires_playwright_base_url_for_relative_routes() -> None:
    text = (PROMPTS_DIR / "build-agent-static-policy.md").read_text(encoding="utf-8")
    assert "Default BrowserJourney tool policy: use `agent-browser`" in text
    assert 'Do not write `"latest"`, `"^latest"`, `"*"`, or invented package versions' in text
    assert "npm_config_cache=/tmp/otto-npm-cache npm install" in text
    assert "Only choose repo-native Playwright" in text
    assert "use.baseURL" in text
    assert "OTTO_BROWSER_PORT" in text
    assert "OTTO_BROWSER_BASE_URL" in text
    assert "not proof\nthat a product server is already running" in text
    assert "wait until\n`OTTO_BROWSER_BASE_URL` accepts connections" in text
    assert "agent-browser --session" in text
    assert "below 32 ASCII slug characters" in text
    assert "AGENT_BROWSER_SOCKET_DIR" in text
    assert "Long session names derived from product titles" in text
    assert "/Users/*/.agents/**" in text
    assert "Do not invoke Codex skills, read `SKILL.md`, or open user-level" in text
    assert "do not import\nPlaywright or launch Chromium directly" in text
    assert "specific missing `agent-browser` capability" in text
    assert "keep it as a stable dispatcher" in text
    assert "tests/browser_journeys/<feature>.py" in text
    assert "do not edit\n`tests/run_browser_journey.py` just to add your feature journey" in text
    assert "Semantic `find` supports" in text
    assert "agent-browser find label Type select expense" in text
    assert "implement server boot, `agent-browser` session/socket\nsetup" in text
    assert "`unknown journey`" in text
    assert "do not use a broad global\nsemantic locator such as `find label Search" in text
    assert "`find label To ...`" in text
    assert "decode the CLI output robustly" in text
    assert "'str' object has no attribute 'get'" in text
    assert "zero matching artifacts is a runner/evidence bug" in text
    assert "page.goto(\"/transactions\")" in text
    assert "invalid URL" in text
    assert "screenshot is blank or\nnear-blank" in text
    assert "Group-level BrowserJourney modules must stay stage-local" in text
    audit_text = (PROMPTS_DIR / "audit-final-task.md").read_text(encoding="utf-8")
    assert "OTTO_BROWSER_SESSION=audit-main" in audit_text
    assert "AGENT_BROWSER_SOCKET_DIR=/tmp/otto-ab/audit-main" in audit_text


def test_compile_spec_browser_journeys_are_agent_browser_first() -> None:
    text = (PROMPTS_DIR / "compile-spec.md").read_text(encoding="utf-8")
    assert "`agent-browser --session <unique-id>`" in text
    assert "default browser runner is a project-owned" in text
    assert "stable shared dispatcher owned by the foundation/shared-runner group" in text
    assert "tests/browser_journeys/transactions.py" in text
    assert "Do not\nmake every feature group edit `tests/run_browser_journey.py`" in text
    assert "Group checks must be stage-local" in text
    assert "foundation/shell check require buttons" in text
    assert "Do not also put critical contract `paths` in `shared_scaffold`" in text
    assert '"any group may extend" surfaces' in text
    assert "include that path in the owner's `owned_paths`" in text
    assert "Do not emit routine feature checks" in text
    assert "Use repo-native Playwright only when" in text
    assert "Playwright only when" in text
    assert '"command": ["pytest", "tests/browser/test_shell.py"]' not in text
    assert "`npm run test:browser -- tests/browser/<feature>.spec.ts`" in text
    assert "typically a Playwright" not in text


def test_lead_prompt_contains_v6_dag_quality_rules() -> None:
    text = (PROMPTS_DIR / "lead.md").read_text(encoding="utf-8")
    assert "3-5 build leaves" in text
    assert "Avoid recursive decomposition" in text
    assert "Do not create a\nseparate integration child" in text
    assert "Every\nextra session pays setup" in text
