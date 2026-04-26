#!/usr/bin/env python3
"""Run Otto's documented verification tiers.

The default pytest suite is intentionally broad and now takes several minutes.
This helper makes the faster tiers explicit so local development can use the
smallest gate that matches the change, while release/merge work can still run
the full suite.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence


PYTEST = [sys.executable, "-m", "pytest"]


def pytest_cmd(*args: str) -> list[str]:
    return [*PYTEST, *args]


def commands_for_tier(tier: str, pytest_args: Sequence[str]) -> list[tuple[dict[str, str] | None, list[str]]]:
    extra = list(pytest_args)
    if tier == "smoke":
        return [(None, pytest_cmd("-q", "-m", "smoke and not browser", "-p", "no:playwright", *extra))]
    if tier == "fast":
        return [(None, pytest_cmd("-q", "-m", "not browser and not slow and not integration and not heavy", "--maxfail=10", *extra))]
    if tier == "default":
        return [(None, pytest_cmd("-q", "--maxfail=10", *extra))]
    if tier == "full":
        return [(None, pytest_cmd("-q", "-m", "not browser", "-p", "no:playwright", "--maxfail=10", *extra))]
    if tier == "integration":
        return [(None, pytest_cmd("-q", "-m", "integration and not browser", "--maxfail=10", *extra))]
    if tier == "slow":
        return [(None, pytest_cmd("-q", "-m", "slow and not browser", "--maxfail=10", *extra))]
    if tier == "web":
        return [
            (None, ["npm", "run", "web:typecheck"]),
            (None, pytest_cmd(
                "-q",
                "tests/test_web_mission_control.py",
                "tests/test_web_project_launcher.py",
                "tests/test_web_review_packet.py",
                "tests/test_mission_control_model.py",
                "tests/test_mission_control_actions.py",
                "tests/test_web_bundle_freshness.py",
                "tests/test_web_cache_headers.py",
                *extra,
            )),
        ]
    if tier == "browser-smoke":
        return [
            ({"OTTO_BROWSER_SKIP_BUILD": "1"}, pytest_cmd(
                "-q",
                "-m",
                "browser and smoke",
                "-p",
                "playwright",
                *extra,
            ))
        ]
    if tier == "browser":
        return [
            ({"OTTO_BROWSER_SKIP_BUILD": "1"}, pytest_cmd(
                "-q",
                "-m",
                "browser",
                "-p",
                "playwright",
                *extra,
            ))
        ]
    if tier == "prepush":
        return [
            (None, ["uv", "run", "ruff", "check", "otto", "scripts", "tests"]),
            (None, ["npm", "run", "web:typecheck"]),
            (None, pytest_cmd("-q", "--maxfail=10", *extra)),
        ]
    raise ValueError(f"unknown tier: {tier}")


def run_command(env_delta: dict[str, str] | None, argv: list[str]) -> int:
    env = os.environ.copy()
    if env_delta:
        env.update(env_delta)
    print("+ " + " ".join(argv), flush=True)
    return subprocess.run(argv, env=env).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tier",
        choices=["smoke", "fast", "default", "full", "integration", "slow", "web", "browser-smoke", "browser", "prepush"],
        help=(
            "smoke=smallest confidence gate, fast=non-browser minus slow/integration/heavy, "
            "default=current non-browser suite, full=explicit non-browser full suite, "
            "integration=integration-only, slow=slow-only, web=TS + web backend tests, "
            "browser-smoke=Playwright smoke, browser=full browser suite, prepush=lint + TS + default"
        ),
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="extra args passed to pytest tiers")
    args = parser.parse_args(argv)
    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]

    for env_delta, command in commands_for_tier(args.tier, pytest_args):
        code = run_command(env_delta, command)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
