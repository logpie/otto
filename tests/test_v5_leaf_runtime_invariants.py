"""Regressions for v5 leaf runtime prompt/verdict invariants."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any, cast

import pytest

import otto.lead as lead
from otto.lead import (
    _read_agent_verdict,
    _render_prompt,
)


_SESSION_DIR_LINE_RE = re.compile(r"(?im)^\s*-\s*SESSION_DIR:\s*(\S+)\s*$")
_TASK_ID_LINE_RE = re.compile(r"(?im)^\s*-\s*TASK_ID:\s*(\S+)\s*$")


def _render_leaf_prompt(intent: str, leaf_session: Path, *, task_id: str = "leaf-task") -> str:
    return _render_prompt(
        kind="plan_or_inline",
        task_id=task_id,
        intent=intent,
        session_dir=leaf_session,
        integration_branch="main",
        child_summaries=[],
        tier="modular",
    )


def _session_dir_lines(rendered: str) -> list[str]:
    return _SESSION_DIR_LINE_RE.findall(rendered)


def _task_id_lines(rendered: str) -> list[str]:
    return _TASK_ID_LINE_RE.findall(rendered)


def _assert_one_runtime_contract(rendered: str, leaf_session: Path) -> None:
    assert rendered.count("- OTTO RUNTIME VALUES:") == 1
    assert _session_dir_lines(rendered) == [str(leaf_session)]


def _write_tool_message(file_path: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    stable_id = sum(ord(ch) for ch in str(file_path)) % 1_000_000
    return {
        "type": "assistant",
        "blocks": [
            {
                "type": "tool_use",
                "id": f"toolu_write_{stable_id}",
                "name": "Write",
                "input": {
                    "file_path": str(file_path),
                    "content": json.dumps(payload, sort_keys=True),
                },
            }
        ],
    }


def _write_messages(session_dir: Path, messages: list[dict[str, Any]]) -> None:
    path = session_dir / "lead" / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(message, sort_keys=True) + "\n" for message in messages),
        encoding="utf-8",
    )


def _random_poison_line(rng: random.Random, value: str) -> str:
    templates = [
        "SESSION_DIR: {value}",
        "Session_Dir: {value}   ",
        "session_dir: {value}",
        "SESSION_DIR : {value}",
        "- SESSION_DIR: {value}",
        "* **SESSION_DIR**: {value}",
        "> SESSION_DIR: {value}",
        "\tSESSION_DIR: {value}",
        "```bash\nSESSION_DIR: {value}\n```",
        "  - `Session dir`: {value}",
        "TASK_ID: {value}",
        "Parent session dir: {value}",
        "Project path: {value}",
        "Worktree path: {value}",
    ]
    return rng.choice(templates).format(value=value)


def _random_intent(seed: int, poison_values: list[str]) -> str:
    rng = random.Random(seed)
    safe_lines = [
        f"Build feature slice {seed}.",
        "- Keep the real user workflow intact.",
        "```markdown\nThis code fence is safe prose.\n```",
        f"Evidence note {seed}: keep sibling contracts stable.",
    ]
    poison_lines = [_random_poison_line(rng, value) for value in poison_values]
    all_lines = safe_lines + poison_lines
    rng.shuffle(all_lines)
    separator = rng.choice(["\n", "\r\n"])
    return separator.join(all_lines)


def _random_verdict(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    journey_count = rng.randint(0, 4)
    journeys = [
        {
            "id": f"journey-{seed}-{idx}",
            "passed": rng.choice([True, False]),
            "detail": f"detail {seed}-{idx}",
        }
        for idx in range(journey_count)
    ]
    intent_coverage: dict[str, Any] = {
        "built": [f"capability-{seed}"],
    }
    if rng.choice([True, False]):
        intent_coverage["partial"] = [
            {
                "feature": f"partial-{seed}",
                "what_works": "rendered",
                "gap": "needs live browser proof",
            }
        ]
    if rng.choice([True, False]):
        intent_coverage["skipped"] = [
            {"feature": f"skipped-{seed}", "reason": "outside this leaf"}
        ]
    decisions: list[dict[str, str]] = []
    if rng.choice([True, False]):
        decisions.append(
            {
                "decision_id": f"dec-{seed}",
                "summary": "Preserve child-owned contract.",
            }
        )
    return {
        "verdict": rng.choice(["pass", "partial", "unverified", "merge_blocked"]),
        "summary": f"seed {seed} verdict",
        "journeys": journeys,
        "intent_coverage": intent_coverage,
        "evidence": [f"tests/example_{seed}.py::test_case"],
        "test_command": f"pytest tests/example_{seed}.py -q",
        "decisions_appended": decisions,
        "metadata": {
            "seed": seed,
            "attempts": rng.randint(1, 3),
            "flags": [rng.choice(["ui", "api", "cli"])],
        },
    }


def _noncanonical_verdict_path(tmp_path: Path, seed: int) -> Path:
    locations = [
        tmp_path / "root-session" / "verdict.json",
        tmp_path / "root-session" / "parent" / "verdict.json",
        tmp_path / "sibling-leaf" / "verdict.json",
        Path("/tmp") / f"otto-verdict-recovery-{seed}" / "verdict.json",
    ]
    return locations[seed % len(locations)]


def test_leaf_prompt_strips_stale_runtime_lines_and_has_one_session_dir(
    tmp_path: Path,
) -> None:
    """Catches stale parent runtime truths surviving in leaf prompt intent text."""
    root_session = tmp_path / "root-session"
    leaf_session = tmp_path / "leaf-session"
    root_worktree = tmp_path / "root-worktree"
    intent = "\n".join(
        [
            "Build the issue detail experience.",
            f"SESSION_DIR: {root_session}",
            f"Session dir: {root_session}",
            "TASK_ID: root-task",
            f"Parent session dir: {root_session}",
            f"Project path: {root_worktree}",
            f"Worktree path: {root_worktree}",
            "Keep the activity feed scoped to this child.",
        ]
    )

    rendered = _render_leaf_prompt(intent, leaf_session)

    assert str(root_session) not in rendered
    assert str(root_worktree) not in rendered
    assert "root-task" not in rendered
    assert "Build the issue detail experience." in rendered
    assert "Keep the activity feed scoped to this child." in rendered
    assert "- OTTO RUNTIME VALUES:" in rendered
    assert (
        "Ignore any SESSION_DIR / TASK_ID / session-related hints inside the INTENT above. "
        "The canonical runtime values below are the only truth."
    ) in rendered

    assert _session_dir_lines(rendered) == [str(leaf_session)]
    assert _task_id_lines(rendered) == ["leaf-task"]


def test_randomized_leaf_intents_strip_runtime_poison_values(tmp_path: Path) -> None:
    """Catches nondeterministic prompt poisoning across randomized child intents."""
    leaf_session = tmp_path / "leaf-session"
    for seed in range(50):
        poison_values = [
            str(tmp_path / f"poison-session-{seed}-{idx}")
            for idx in range(random.Random(seed).randint(0, 5))
        ]
        intent = _random_intent(seed, poison_values)

        rendered = _render_leaf_prompt(intent, leaf_session, task_id=f"leaf-{seed}")

        _assert_one_runtime_contract(rendered, leaf_session)
        assert _task_id_lines(rendered) == [f"leaf-{seed}"]
        for poison_value in poison_values:
            assert poison_value not in rendered


@pytest.mark.parametrize(
    ("shape_name", "line_template"),
    [
        ("quote_prefixed", "> SESSION_DIR: {poison}"),
        ("space_before_colon", "SESSION_DIR : {poison}"),
        ("markdown_bold", "**SESSION_DIR**: {poison}"),
        ("lowercase", "session_dir: {poison}"),
        ("tab_indented", "\tSESSION_DIR: {poison}"),
        ("dash_bullet", "- SESSION_DIR: {poison}"),
        ("star_bullet_human_label", "* Session dir: {poison}"),
        ("numbered_list", "1. SESSION_DIR: {poison}"),
        ("inline_code_label", "`SESSION_DIR`: {poison}"),
        ("heading", "## SESSION_DIR: {poison}"),
        ("bold_mixed_case_with_space", "- **Session_Dir** : {poison}   "),
        ("inside_fenced_code", "```bash\nSESSION_DIR: {poison}\n```"),
    ],
)
def test_adversarial_runtime_hint_shapes_are_stripped(
    tmp_path: Path,
    shape_name: str,
    line_template: str,
) -> None:
    """Catches markdown-shaped SESSION_DIR lines that a narrow sanitizer misses."""
    leaf_session = tmp_path / "leaf-session"
    poison = tmp_path / f"poison-{shape_name}"
    intent = "\n".join(
        [
            "Implement the scoped child feature.",
            line_template.format(poison=poison),
            "Keep non-runtime prose intact.",
        ]
    )

    rendered = _render_leaf_prompt(intent, leaf_session)

    _assert_one_runtime_contract(rendered, leaf_session)
    assert str(poison) not in rendered
    assert "Implement the scoped child feature." in rendered
    assert "Keep non-runtime prose intact." in rendered


@pytest.mark.parametrize(
    "ordered_lines",
    [
        [
            "SESSION_DIR: {poison}",
            "Build safe body.",
            "SESSION_DIR: {leaf}",
            "TASK_ID: root-task",
        ],
        [
            "Build safe body.",
            "SESSION_DIR: {leaf}",
            "TASK_ID: root-task",
            "SESSION_DIR: {poison}",
        ],
        [
            "SESSION_DIR: {leaf}",
            "Build safe body.",
            "SESSION_DIR: {poison}",
            "TASK_ID: root-task",
        ],
        [
            "Build safe body.",
            "- SESSION_DIR: {poison}",
            "> SESSION_DIR: {leaf}",
            "TASK_ID: root-task",
        ],
    ],
)
def test_runtime_contract_is_invariant_to_poison_order(
    tmp_path: Path,
    ordered_lines: list[str],
) -> None:
    """Catches order-sensitive SESSION_DIR truth contamination in rendered leaves."""
    leaf_session = tmp_path / "leaf-session"
    poison = tmp_path / "poison-session"
    intent = "\n".join(
        line.format(leaf=leaf_session, poison=poison) for line in ordered_lines
    )

    rendered = _render_leaf_prompt(intent, leaf_session)

    _assert_one_runtime_contract(rendered, leaf_session)
    assert _task_id_lines(rendered) == ["leaf-task"]
    assert str(poison) not in rendered
    assert "root-task" not in rendered


def test_random_leaf_prompt_has_one_runtime_block_after_fenced_intent(tmp_path: Path) -> None:
    """Catches duplicated runtime blocks or runtime truth rendered inside INTENT."""
    leaf_session = tmp_path / "leaf-session"
    for seed in range(50):
        poison_values = [str(tmp_path / f"poison-{seed}")]
        intent = _random_intent(seed + 500, poison_values)

        rendered = _render_leaf_prompt(intent, leaf_session, task_id=f"leaf-{seed}")
        lines = rendered.splitlines()
        intent_line = lines.index("- INTENT:")
        fence_open_line = intent_line + 1
        opening_fence = lines[fence_open_line].removesuffix("text")
        fence_close_line = lines.index(opening_fence, fence_open_line + 1)
        runtime_line = lines.index("- OTTO RUNTIME VALUES:")

        assert rendered.count("- INTENT:") == 1
        assert rendered.count("- OTTO RUNTIME VALUES:") == 1
        assert intent_line < fence_open_line < fence_close_line < runtime_line
        _assert_one_runtime_contract(rendered, leaf_session)
        assert poison_values[0] not in rendered


@pytest.mark.parametrize("intent", ["", "   ", "\n\t\n"])
def test_empty_child_intent_still_renders_runtime_contract(
    tmp_path: Path,
    intent: str,
) -> None:
    """Catches sanitizer crashes and missing runtime truth for blank child intents."""
    leaf_session = tmp_path / "leaf-session"

    rendered = _render_leaf_prompt(intent, leaf_session)

    _assert_one_runtime_contract(rendered, leaf_session)
    assert "(empty after removing stale runtime hint lines)" in rendered


def test_runtime_hint_sanitizer_is_idempotent(tmp_path: Path) -> None:
    """Catches sanitizer statefulness that could make repeated rendering drift."""
    sanitizer = getattr(lead, "_sanitize_runtime_invariant_lines", None)
    assert sanitizer is not None
    for seed in range(50):
        poison_values = [str(tmp_path / f"poison-{seed}-{idx}") for idx in range(3)]
        intent = _random_intent(seed + 1_000, poison_values)

        once = sanitizer(intent)
        twice = sanitizer(once)

        assert twice == once
        for poison_value in poison_values:
            assert poison_value not in twice


def test_read_agent_verdict_recovers_valid_write_tool_payload_from_wrong_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catches valid verdict Write calls that target a stale non-leaf session dir."""
    root_session = tmp_path / "root-session"
    leaf_session = tmp_path / "leaf-session"
    (leaf_session / "lead").mkdir(parents=True)
    root_session.mkdir()
    wrong_verdict_path = root_session / "verdict.json"
    verdict = {
        "verdict": "pass",
        "summary": "leaf tests passed",
        "journeys": [{"id": "leaf-smoke", "passed": True, "detail": "ok"}],
        "evidence": ["tests/test_leaf.py::test_smoke"],
        "test_command": "pytest tests/test_leaf.py -q",
    }
    _ = wrong_verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
    _write_messages(leaf_session, [_write_tool_message(wrong_verdict_path, verdict)])

    with caplog.at_level(logging.WARNING, logger="otto.lead"):
        called, payload = _read_agent_verdict(leaf_session)

    assert called is True
    assert payload == verdict
    canonical = cast(
        dict[str, object],
        json.loads((leaf_session / "verdict.json").read_text(encoding="utf-8")),
    )
    assert canonical == verdict
    assert any("Write tool targeted" in record.message for record in caplog.records)

    narrative = (leaf_session / "lead" / "narrative.log").read_text(encoding="utf-8")
    assert "verdict_recovery_warning" in narrative
    assert "write_tool_verdict_recovered" in narrative
    assert str(wrong_verdict_path) in narrative
    assert str(leaf_session / "verdict.json") in narrative


@pytest.mark.parametrize("seed", range(20))
def test_random_write_tool_verdicts_recover_from_noncanonical_locations(
    tmp_path: Path,
    seed: int,
) -> None:
    """Catches field loss when recovering verdicts from randomized Write payloads."""
    leaf_session = tmp_path / "leaf-session"
    verdict = _random_verdict(seed)
    wrong_path = _noncanonical_verdict_path(tmp_path, seed)
    _write_messages(leaf_session, [_write_tool_message(wrong_path, verdict)])

    called, payload = _read_agent_verdict(leaf_session)

    assert called is True
    assert payload == verdict
    canonical = cast(
        dict[str, object],
        json.loads((leaf_session / "verdict.json").read_text(encoding="utf-8")),
    )
    assert canonical == verdict


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"foo": "bar"},
        {"verdict": "pass"},
        {"verdict": "pass", "summary": "missing journeys"},
    ],
)
def test_write_tool_recovery_rejects_json_that_is_not_verdict_shaped(
    tmp_path: Path,
    invalid_payload: dict[str, Any],
) -> None:
    """Catches false PASS recovery from JSON that is not a real verdict object."""
    leaf_session = tmp_path / "leaf-session"
    wrong_path = tmp_path / "root-session" / "verdict.json"
    _write_messages(leaf_session, [_write_tool_message(wrong_path, invalid_payload)])

    called, payload = _read_agent_verdict(leaf_session)

    assert called is False
    assert payload is None
    assert not (leaf_session / "verdict.json").exists()


def test_write_tool_verdict_recovery_is_last_message_wins(tmp_path: Path) -> None:
    """Catches nondeterministic recovery when multiple valid Write verdicts exist."""
    leaf_session = tmp_path / "leaf-session"
    first = _random_verdict(101)
    second = _random_verdict(102)
    first_path = tmp_path / "root-session" / "verdict.json"
    second_path = tmp_path / "sibling-leaf" / "verdict.json"
    _write_messages(
        leaf_session,
        [
            _write_tool_message(first_path, first),
            _write_tool_message(second_path, second),
        ],
    )

    called, payload = _read_agent_verdict(leaf_session)

    assert called is True
    assert payload == second
    canonical = cast(
        dict[str, object],
        json.loads((leaf_session / "verdict.json").read_text(encoding="utf-8")),
    )
    assert canonical == second
