from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "otto_as_user.py"
SCRIPTS_DIR = SCRIPT_PATH.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location("tests._otto_as_user_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
OTTO_AS_USER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OTTO_AS_USER
SPEC.loader.exec_module(OTTO_AS_USER)


def make_run_result(
    tmp_path: Path,
    *,
    duration_s: float = 5.0,
    output: str = "",
    details: dict[str, object] | None = None,
) -> object:
    return OTTO_AS_USER.RunResult(
        scenario_id="A1",
        returncode=1,
        started_at="2026-04-21T00:00:00Z",
        finished_at="2026-04-21T00:00:01Z",
        duration_s=duration_s,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(tmp_path),
        debug_log=str(tmp_path / "debug.log"),
        output=output,
        details=details or {},
    )


@pytest.mark.parametrize(
    ("narrative_text", "debug_text", "output", "duration_s", "details"),
    [
        ("Not logged in", "", "", 5.0, {}),
        ("", "Please run /login", "", 5.0, {}),
        ("subscription RATE LIMIT hit under load", "", "", 5.0, {}),
        ("request failed with HTTP 429 throttle window exceeded", "", "", 5.0, {}),
        (
            "Command failed with exit code 1\nCheck stderr output for details\n",
            "",
            "",
            1.2,
            {"summary": {"cost_usd": 0.0}},
        ),
    ],
)
def test_classify_failure_detects_infra_signatures(
    tmp_path: Path,
    narrative_text: str,
    debug_text: str,
    output: str,
    duration_s: float,
    details: dict[str, object],
) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(narrative_text, encoding="utf-8")
    debug.write_text(debug_text, encoding="utf-8")

    result = make_run_result(tmp_path, duration_s=duration_s, output=output, details=details)

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "INFRA"


def test_classify_failure_leaves_real_failures_as_fail(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(
        "Traceback (most recent call last):\nAssertionError: expected hello.py to exist\n",
        encoding="utf-8",
    )
    debug.write_text("pytest failed with rc=1\n", encoding="utf-8")

    result = make_run_result(
        tmp_path,
        duration_s=12.0,
        details={"summary": {"cost_usd": 1.25}},
    )

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "FAIL"
