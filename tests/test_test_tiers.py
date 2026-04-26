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
    assert {
        "tests/test_web_mission_control.py",
        "tests/test_web_events_history.py",
        "tests/test_web_landing.py",
        "tests/test_web_project_launcher.py",
        "tests/test_web_queue_actions.py",
        "tests/test_web_review_packet.py",
        "tests/test_web_watcher_controls.py",
        "tests/test_mission_control_adapters.py",
        "tests/test_mission_control_model.py",
        "tests/test_mission_control_actions.py",
        "tests/test_mission_control_polish.py",
        "tests/test_web_bundle_freshness.py",
        "tests/test_web_cache_headers.py",
    }.issubset(commands[1])


def test_fast_tier_excludes_slow_and_integration() -> None:
    [argv] = _argvs("fast")

    assert "not browser and not slow and not integration and not heavy" in argv


def test_browser_smoke_builds_bundle_and_targets_browser_smoke_marker() -> None:
    [(env, argv)] = commands_for_tier("browser-smoke", [])

    assert env is None
    assert "browser and smoke" in argv
    assert "playwright" in argv


def test_prepush_verifies_committed_web_bundle() -> None:
    commands = _argvs("prepush")

    assert ["npm", "run", "web:verify"] in commands


def test_extra_pytest_args_are_forwarded() -> None:
    [argv] = _argvs("smoke", "-x", "-vv")

    assert argv[-2:] == ["-x", "-vv"]
