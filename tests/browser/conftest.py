"""Pytest fixtures for the Mission Control browser test suite.

Fixture catalog (one-line each — full docs on each function below):

- ``build_bundle``        — session-scoped: ensures the SPA bundle is built once
- ``mc_backend``          — function-scoped: spins up FastAPI on an atomic free port
- ``monkeypatch_watcher_subprocess`` — replaces ``subprocess.Popen`` in the
  watcher-launch path with a fake; tests asserting watcher behavior must
  request this fixture explicitly so real ``otto queue run`` never spawns
- ``frozen_clock``        — pins wall clock to ``2026-04-25T12:00:00Z`` UTC
- ``disable_animations``  — function-scoped helper: ``apply(page)`` injects CSS
- ``mc_page``             — convenience: ``mc_backend`` + ``page`` navigated and hydrated
- ``pages_two``           — two contexts/pages on the same backend (two-tab tests)
- ``viewport_desktop`` / ``viewport_mba`` / ``viewport_iphone`` — device profiles
- ``console_must_be_clean`` / ``network_must_be_clean`` — assertion helpers
  with allowlist hooks
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from ._helpers.build_bundle import ensure_bundle_built
from ._helpers.server import MCBackend, start_backend


# ---------------------------------------------------------------------------
# Build-bundle (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def build_bundle() -> Path:
    """Run ``npm run web:typecheck && npm run web:build`` once per session.

    Returns the static-assets directory. On failure, raises ``BundleBuildError``
    with the full npm output — every browser test in the session fails with
    the same actionable error.

    Skip with ``OTTO_BROWSER_SKIP_BUILD=1`` for fast iteration. The fixture
    still verifies that ``otto/web/static/assets/`` contains JS+CSS.
    """

    return ensure_bundle_built()


# ---------------------------------------------------------------------------
# Backend (function-scoped, isolated per test)
# ---------------------------------------------------------------------------


@pytest.fixture
def mc_backend(tmp_path: Path, build_bundle: Path) -> Iterator[MCBackend]:
    """Start a Mission Control backend bound to a freshly-initialised git project.

    The project lives under ``tmp_path / "project"`` and is initialised as a
    git repo (otto refuses non-git project dirs). Managed-projects root is
    isolated to ``tmp_path / "managed-projects"`` so ``/api/projects`` cannot
    leak the developer's ``~/otto-projects``.

    Tear-down stops uvicorn and asserts no orphan watcher subprocess survived.
    """

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    _git_init(project_dir)

    projects_root = tmp_path / "managed-projects"
    projects_root.mkdir(parents=True)

    backend = start_backend(project_dir, projects_root=projects_root)
    try:
        yield backend
    finally:
        backend.stop()
        _assert_no_orphan_watcher(project_dir)


def _git_init(project_dir: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.email", "browser@example.com"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Browser Test"], cwd=project_dir, check=True)
    (project_dir / "README.md").write_text("# browser-test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=project_dir, check=True)


def _assert_no_orphan_watcher(project_dir: Path) -> None:
    """Sanity check: the watcher supervisor metadata should not show a live PID.

    We don't kill anything here — that's the test's responsibility — but a
    stale ``running`` state after teardown indicates a leak.
    """

    from otto.mission_control.supervisor import read_supervisor

    metadata, _err = read_supervisor(project_dir)
    if metadata is None:
        return
    pid = metadata.get("watcher_pid")
    if not isinstance(pid, int):
        return
    if metadata.get("stop_requested_at"):
        return  # stop was requested; metadata is allowed to linger
    if _pid_alive(pid):  # pragma: no cover — only fires on actual leak
        raise AssertionError(
            f"orphan watcher pid={pid} survived test teardown for project {project_dir}"
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Watcher monkeypatch
# ---------------------------------------------------------------------------


class _FakeWatcherProc:
    """Stand-in for ``subprocess.Popen`` in the watcher-launch path.

    Records argv + cwd, claims to be running with a fake pid, exits cleanly
    when ``terminate()`` is called. Tests can introspect via the registry on
    ``monkeypatch_watcher_subprocess``.
    """

    _next_pid = 99000

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.argv = list(argv)
        self.cwd = kwargs.get("cwd")
        self.returncode: int | None = None
        self._stopped = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self._stopped = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def monkeypatch_watcher_subprocess(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[_FakeWatcherProc]]:
    """Replace ``subprocess.Popen`` in ``otto.mission_control.service`` with a fake.

    Watcher tests must use this fixture so real ``otto queue run`` subprocesses
    never spawn during the suite. The returned dict has key ``spawned``: a list
    of ``_FakeWatcherProc`` instances appended in order, useful for assertions:

        def test_watcher_start(mc_backend, monkeypatch_watcher_subprocess):
            ...
            assert len(monkeypatch_watcher_subprocess["spawned"]) == 1
            assert "queue" in monkeypatch_watcher_subprocess["spawned"][0].argv

    Real subprocess.Popen elsewhere (e.g. ``serializers.py`` calling git) is
    untouched — we patch only the service module's binding.
    """

    spawned: list[_FakeWatcherProc] = []
    registry = {"spawned": spawned}

    def fake_popen(argv: list[str], **kwargs: Any) -> _FakeWatcherProc:
        proc = _FakeWatcherProc(argv, **kwargs)
        spawned.append(proc)
        return proc

    # Patch the binding inside the watcher module rather than subprocess globally:
    # otto.mission_control.service imports `subprocess` at module scope and uses
    # `subprocess.Popen`. We patch the attribute on that module's `subprocess`.
    import otto.mission_control.service as svc

    monkeypatch.setattr(svc.subprocess, "Popen", fake_popen)
    return registry


# ---------------------------------------------------------------------------
# Frozen clock
# ---------------------------------------------------------------------------

DEFAULT_FROZEN_TIMESTAMP = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[datetime]:
    """Pin wall clock to ``2026-04-25T12:00:00Z`` UTC for deterministic tests.

    Uses ``freezegun`` (added in dev deps); returns the frozen datetime so
    tests can assert against it directly.

    Note: this only affects Python-side time. JavaScript ``Date.now()`` in the
    SPA is *not* frozen by this fixture — for visual-regression tests that
    care about rendered timestamps, mask the dynamic regions or stub
    ``window.Date`` via ``page.add_init_script``.
    """

    from freezegun import freeze_time

    freezer = freeze_time(DEFAULT_FROZEN_TIMESTAMP)
    freezer.start()
    monkeypatch.setenv("TZ", "UTC")
    try:
        yield DEFAULT_FROZEN_TIMESTAMP
    finally:
        freezer.stop()


# ---------------------------------------------------------------------------
# Animation disable
# ---------------------------------------------------------------------------

_DISABLE_ANIMATIONS_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important;
  animation-delay: 0s !important;
  transition-duration: 0s !important;
  transition-delay: 0s !important;
  scroll-behavior: auto !important;
}
"""


@pytest.fixture
def disable_animations() -> Callable[[Any], None]:
    """Return a callable that injects an animation-killing stylesheet onto a page.

    Usage::

        def test_x(mc_backend, page, disable_animations):
            page.goto(mc_backend.url)
            disable_animations(page)
            ...
    """

    def _apply(page: Any) -> None:
        page.add_style_tag(content=_DISABLE_ANIMATIONS_CSS)

    return _apply


# ---------------------------------------------------------------------------
# Convenience pages
# ---------------------------------------------------------------------------


@pytest.fixture
def mc_page(mc_backend: MCBackend, page: Any, disable_animations: Callable[[Any], None]) -> Any:
    """Navigate ``page`` to the backend, wait for hydration, return the page.

    Hydration check: we wait for ``#root`` to have at least one child element,
    which is what React mounts into per ``otto/web/static/index.html``. Tests
    that need a stricter readiness signal should add their own waits.
    """

    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#root')?.children.length > 0", timeout=10_000)
    disable_animations(page)
    return page


@pytest.fixture
def pages_two(mc_backend: MCBackend, browser: Any) -> Iterator[tuple[Any, Any]]:
    """Yield two independent browser pages on the same backend (two-tab tests)."""

    ctx_a = browser.new_context()
    ctx_b = browser.new_context()
    page_a = ctx_a.new_page()
    page_b = ctx_b.new_page()
    try:
        yield page_a, page_b
    finally:
        ctx_a.close()
        ctx_b.close()


# ---------------------------------------------------------------------------
# Viewports / device profiles
# ---------------------------------------------------------------------------


@pytest.fixture
def viewport_desktop() -> dict[str, Any]:
    """1920×1080 desktop (Mac mini) viewport, no device emulation."""

    return {"viewport": {"width": 1920, "height": 1080}, "device_scale_factor": 2}


@pytest.fixture
def viewport_mba() -> dict[str, Any]:
    """1440×900 MacBook Air viewport."""

    return {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 2}


@pytest.fixture
def viewport_iphone(playwright: Any) -> dict[str, Any]:
    """iPhone 14 device profile (webkit) for mobile-layout tests."""

    return dict(playwright.devices["iPhone 14"])


# ---------------------------------------------------------------------------
# Console + network noise
# ---------------------------------------------------------------------------


@pytest.fixture
def console_must_be_clean() -> Callable[..., Callable[[], None]]:
    """Wire a page so unexpected console errors/warnings fail the test.

    Returns an attach helper. Usage::

        def test_x(mc_backend, page, console_must_be_clean):
            assert_clean = console_must_be_clean(page, allow=[r"^DevTools.*"])
            page.goto(mc_backend.url)
            ...
            assert_clean()
    """

    def _attach(page: Any, *, allow: list[str] | None = None) -> Callable[[], None]:
        allow_patterns = [re.compile(p) for p in (allow or [])]
        captured: list[str] = []

        def _on_console(msg: Any) -> None:
            if msg.type not in ("error", "warning"):
                return
            text = msg.text
            if any(p.search(text) for p in allow_patterns):
                return
            captured.append(f"[{msg.type}] {text}")

        page.on("console", _on_console)

        def _assert() -> None:
            if captured:
                joined = "\n".join(captured)
                raise AssertionError(f"unexpected console messages:\n{joined}")

        return _assert

    return _attach


@pytest.fixture
def network_must_be_clean() -> Callable[..., Callable[[], None]]:
    """Wire a page so any 4xx/5xx response (outside the allowlist) fails the test.

    Usage mirrors ``console_must_be_clean``.
    """

    def _attach(page: Any, *, allow: list[str] | None = None) -> Callable[[], None]:
        allow_patterns = [re.compile(p) for p in (allow or [])]
        captured: list[str] = []

        def _on_response(response: Any) -> None:
            if response.status < 400:
                return
            url = response.url
            if any(p.search(url) for p in allow_patterns):
                return
            captured.append(f"{response.status} {url}")

        page.on("response", _on_response)

        def _assert() -> None:
            if captured:
                joined = "\n".join(captured)
                raise AssertionError(f"unexpected failing responses:\n{joined}")

        return _assert

    return _attach
