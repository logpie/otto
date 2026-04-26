"""Build the Mission Control SPA bundle once per pytest session.

Why a helper, not inline conftest code:
- Browser tests must run against the *real* built bundle (Phase 3B of
  plan-mc-audit.md). A stale or skipped build silently produces false-pass
  visual/regression tests.
- The build is expensive (~10s typecheck + bundle); we cache success per
  session so individual tests pay no cost after the first.
- ``OTTO_BROWSER_SKIP_BUILD=1`` short-circuits for fast iteration: the
  developer asserts the bundle they have on disk is good enough.

The fixture is exposed via ``conftest.py`` as ``build_bundle``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
STATIC_ASSETS_DIR: Final[Path] = REPO_ROOT / "otto" / "web" / "static" / "assets"


class BundleBuildError(RuntimeError):
    """Raised when ``npm run web:verify`` fails."""


def ensure_bundle_built() -> Path:
    """Build the SPA bundle once per process; return the static assets dir.

    The function is idempotent within a single Python process: subsequent
    calls re-verify the assets dir exists and short-circuit. Cross-process
    isolation is the test runner's job — pytest's session scope handles it.
    """

    if os.environ.get("OTTO_BROWSER_SKIP_BUILD") == "1":
        _verify_assets_present(reason="OTTO_BROWSER_SKIP_BUILD=1 was set but assets are missing")
        return STATIC_ASSETS_DIR

    if getattr(ensure_bundle_built, "_done", False):
        _verify_assets_present(reason="bundle was previously built but assets disappeared")
        return STATIC_ASSETS_DIR

    _run_npm("web:verify")
    _verify_assets_present(reason="npm run web:build succeeded but produced no assets")
    setattr(ensure_bundle_built, "_done", True)
    return STATIC_ASSETS_DIR


def _run_npm(script: str) -> None:
    proc = subprocess.run(
        ["npm", "run", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = (
            f"npm run {script} failed (cwd={REPO_ROOT}, exit={proc.returncode}).\n"
            + f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
        raise BundleBuildError(msg)


def _verify_assets_present(*, reason: str) -> None:
    if not STATIC_ASSETS_DIR.is_dir():
        raise BundleBuildError(f"{reason}: {STATIC_ASSETS_DIR} does not exist.")
    js_files = list(STATIC_ASSETS_DIR.glob("*.js"))
    css_files = list(STATIC_ASSETS_DIR.glob("*.css"))
    if not js_files or not css_files:
        msg = (
            f"{reason}: expected JS+CSS in {STATIC_ASSETS_DIR}, "
            + f"found js={[p.name for p in js_files]} css={[p.name for p in css_files]}."
        )
        raise BundleBuildError(msg)
