from __future__ import annotations

from scripts.test_tiers import commands_for_tier


def _argvs(tier: str, *extra: str) -> list[list[str]]:
    return [argv for _env, argv in commands_for_tier(tier, extra)]


def test_smoke_tier_uses_smoke_marker_and_disables_playwright() -> None:
    [argv] = _argvs("smoke")

    assert "-m" in argv
    assert "smoke and not browser" in argv
    assert "-p" in argv
    assert "no:playwright" in argv


def test_web_tier_typechecks_before_pytest() -> None:
    commands = _argvs("web")

    assert commands[0] == ["npm", "run", "web:typecheck"]
    assert "tests/test_web_mission_control.py" in commands[1]


def test_fast_tier_excludes_slow_and_integration() -> None:
    [argv] = _argvs("fast")

    assert "not browser and not slow and not integration and not heavy" in argv


def test_browser_smoke_sets_skip_build_and_targets_browser_smoke_marker() -> None:
    [(env, argv)] = commands_for_tier("browser-smoke", [])

    assert env == {"OTTO_BROWSER_SKIP_BUILD": "1"}
    assert "browser and smoke" in argv
    assert "playwright" in argv


def test_extra_pytest_args_are_forwarded() -> None:
    [argv] = _argvs("smoke", "-x", "-vv")

    assert argv[-2:] == ["-x", "-vv"]
