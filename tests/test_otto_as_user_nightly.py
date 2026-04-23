from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "otto_as_user_nightly.py"
SCRIPTS_DIR = SCRIPT_PATH.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location("tests._otto_as_user_nightly_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
OTTO_AS_USER_NIGHTLY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OTTO_AS_USER_NIGHTLY
SPEC.loader.exec_module(OTTO_AS_USER_NIGHTLY)


def test_n9_is_registered() -> None:
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIOS
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS
    assert OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"].description == "mission control workflow"


def test_n9_step_plan_mentions_cancelled_background_build() -> None:
    steps = OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS["N9"].step_plan
    assert any("background, will be cancelled" in step for step in steps)
    assert any("harness appends cancel envelope" in step for step in steps)
    assert any(step == "otto merge --all --cleanup-on-success" for step in steps)
