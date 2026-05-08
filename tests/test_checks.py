"""Tests for otto/checks.py — Check executors.

Coverage per Check kind:
- RepoTestCheck: happy (exit 0), failure (exit non-zero), timeout
- PytestCheck: happy (passing test), failure (failing test)
- BrowserJourney: happy (subprocess + glob collects artifacts), failure
- ApiProbe: happy (200 + body match), status mismatch, body mismatch,
  connection error
- StateInvariant: happy (true expression), failure (false), syntax error,
  filesystem helpers, http_get with base_url
- run_checks dispatch + raw_log_dir
- Unsupported kind
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import struct
import sys
import threading
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from otto.checks import _pytest_base_command, run_check, run_checks
from otto.spec_compile import (
    ApiProbe,
    BrowserJourney,
    CLIProbe,
    ImportCheck,
    PytestCheck,
    RepoTestCheck,
    StateInvariant,
    TypeCheck,
)


def _write_png(path: Path, width: int, height: int, rows: list[list[tuple[int, int, int]]]) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in rows:
        raw.append(0)
        for red, green, blue in row:
            raw.extend([red, green, blue])
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk("IHDR".encode(), struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk("IDAT".encode(), zlib.compress(bytes(raw)))
        + chunk("IEND".encode(), b"")
    )
    path.write_bytes(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _tiny_http_server(handler_cls: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# RepoTestCheck
# ---------------------------------------------------------------------------


def test_repo_test_happy_path(tmp_path: Path) -> None:
    check = RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "exit=0" in evidence.detail
    assert "ok" in evidence.raw["stdout"]
    assert evidence.duration_s >= 0


def test_repo_test_expands_path_globs_without_shell(tmp_path: Path) -> None:
    feature_dir = tmp_path / "src" / "features" / "search"
    feature_dir.mkdir(parents=True)
    (feature_dir / "FeedSearch.test.tsx").write_text("// one\n", encoding="utf-8")
    (feature_dir / "Search.test.tsx").write_text("// two\n", encoding="utf-8")

    code = (
        "import sys; "
        "expected=['src/features/search/FeedSearch.test.tsx',"
        "'src/features/search/Search.test.tsx']; "
        "print('\\n'.join(sys.argv[1:])); "
        "raise SystemExit(0 if sys.argv[1:]==expected else 9)"
    )
    check = RepoTestCheck(
        command=("python", "-c", code, "src/features/search/*.test.tsx"),
        timeout_s=10,
    )

    evidence = run_check(check, project_dir=tmp_path)

    assert evidence.passed is True
    assert "src/features/search/FeedSearch.test.tsx" in evidence.raw["stdout"]
    assert "src/features/search/*.test.tsx" not in evidence.raw["resolved_command"]


def test_repo_test_nonzero_exit_fails(tmp_path: Path) -> None:
    check = RepoTestCheck(command=("python", "-c", "import sys; sys.exit(7)"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "exit=7" in evidence.detail
    assert evidence.raw["exit_code"] == 7


def test_repo_test_empty_command_is_informational(tmp_path: Path) -> None:
    """v2.1 (F-class generalization): malformed check payload (empty
    command) → informational PASS, not slice-blocking. Audit's contract
    gate verifies the integrated product. See docs/intent-to-product-v2-plan.md.
    """
    check = RepoTestCheck(command=(), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "command is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


def test_repo_test_timeout_marked_as_failure(tmp_path: Path) -> None:
    check = RepoTestCheck(command=("python", "-c", "import time; time.sleep(5)"), timeout_s=1)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "timed out" in evidence.detail
    assert evidence.raw.get("timeout") is True


def test_repo_test_timeout_kills_descendants_holding_output_pipe(tmp_path: Path) -> None:
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', "
        "\"import sys, time; print('grandchild-started', flush=True); time.sleep(30)\"])\n"
        "print('parent-exited', flush=True)\n"
    )
    check = RepoTestCheck(command=(sys.executable, "-c", script), timeout_s=1)

    evidence = run_check(check, project_dir=tmp_path)

    assert evidence.passed is False
    assert "timed out" in evidence.detail
    assert evidence.raw.get("timeout") is True
    assert evidence.duration_s < 5


def test_repo_test_writes_raw_log_when_path_given(tmp_path: Path) -> None:
    log_path = tmp_path / "evidence" / "repo.log"
    check = RepoTestCheck(command=("python", "-c", "print('hello-raw')"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, raw_log_path=log_path)
    assert evidence.passed is True
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello-raw" in contents
    assert "exit_code=0" in contents


def test_repo_test_npm_run_bootstraps_locked_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_npm = bin_dir / "npm"
    fake_npm.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> npm.log\n"
        "if [ \"$1\" = \"ci\" ]; then mkdir -p node_modules; exit 0; fi\n"
        "if [ \"$1\" = \"run\" ] && [ \"$2\" = \"build\" ]; then\n"
        "  test -d node_modules || exit 127\n"
        "  echo built\n"
        "  exit 0\n"
        "fi\n"
        "exit 9\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    (tmp_path / "package.json").write_text('{"scripts":{"build":"vite build"}}\n', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")

    check = RepoTestCheck(command=("npm", "run", "build"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is True
    assert evidence.raw["bootstrap_command"] == [
        "npm",
        "ci",
        "--prefer-offline",
        "--no-audit",
        "--no-fund",
    ]
    assert evidence.raw["exit_code"] == 0
    assert (tmp_path / "npm.log").read_text(encoding="utf-8").splitlines() == [
        "ci --prefer-offline --no-audit --no-fund",
        "run build",
    ]


# ---------------------------------------------------------------------------
# PytestCheck
# ---------------------------------------------------------------------------


def test_pytest_check_passing_test(tmp_path: Path) -> None:
    test_file = tmp_path / "test_pass.py"
    test_file.write_text("def test_one(): assert 1 + 1 == 2\n", encoding="utf-8")
    check = PytestCheck(selector=str(test_file), timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is True
    assert evidence.raw["exit_code"] == 0


def test_pytest_check_failing_test(tmp_path: Path) -> None:
    test_file = tmp_path / "test_fail.py"
    test_file.write_text("def test_bad(): assert 1 == 2\n", encoding="utf-8")
    check = PytestCheck(selector=str(test_file), timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is False
    # pytest exits 1 on failures
    assert evidence.raw["exit_code"] != 0
    assert check.selector in evidence.raw["selector"]


def test_pytest_check_empty_selector_is_informational(tmp_path: Path) -> None:
    """v2.1: empty selector → informational PASS. See test_repo_test variant."""
    check = PytestCheck(selector="", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "selector is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


def test_pytest_check_imports_top_level_module_without_conftest(tmp_path: Path) -> None:
    """Regression: project_dir on PYTHONPATH so `from app import …` works.

    Reproduces the Microfeed bench failure where build agents emitted
    ``tests/test_models.py: from app import create_app`` and pytest's
    rootdir-without-conftest path fell back to import-mode that doesn't
    add the project root to sys.path.
    """
    (tmp_path / "app.py").write_text("def create_app(): return 'ok'\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_app.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from app import create_app\n"
        "def test_app(): assert create_app() == 'ok'\n",
        encoding="utf-8",
    )
    check = PytestCheck(selector="tests/test_app.py", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is True, evidence.raw
    assert evidence.raw["exit_code"] == 0


def test_pytest_check_uses_project_venv_pytest_before_uv(tmp_path: Path) -> None:
    venv_pytest = tmp_path / ".venv" / "bin" / "pytest"
    venv_pytest.parent.mkdir(parents=True)
    venv_pytest.write_text(
        "#!/bin/sh\nprintf 'project-venv-pytest %s\\n' \"$*\"\nexit 0\n",
        encoding="utf-8",
    )
    venv_pytest.chmod(0o755)

    check = PytestCheck(selector="tests/test_app.py", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is True, evidence.raw
    assert evidence.raw["command"][0] == str(venv_pytest)
    assert "project-venv-pytest" in evidence.raw["stdout"]


def test_pytest_check_uses_parent_project_venv_for_queue_worktree(tmp_path: Path) -> None:
    project = tmp_path / "project"
    worktree = project / ".worktrees" / "queued-task"
    venv_pytest = project / ".venv" / "bin" / "pytest"
    worktree.mkdir(parents=True)
    venv_pytest.parent.mkdir(parents=True)
    venv_pytest.write_text(
        "#!/bin/sh\nprintf 'parent-project-pytest %s\\n' \"$*\"\nexit 0\n",
        encoding="utf-8",
    )
    venv_pytest.chmod(0o755)

    check = PytestCheck(selector="tests/test_app.py", timeout_s=30)
    evidence = run_check(check, project_dir=worktree, cwd=worktree)

    assert evidence.passed is True, evidence.raw
    assert evidence.raw["command"][0] == str(venv_pytest)
    assert "parent-project-pytest" in evidence.raw["stdout"]


def test_pytest_command_prefers_path_pytest_over_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pytest = fake_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pytest.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    assert _pytest_base_command(tmp_path, tmp_path) == [str(fake_pytest)]


def test_pytest_command_skips_current_otto_venv_on_user_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    otto_bin = tmp_path / "otto-venv" / "bin"
    user_bin = tmp_path / "user-bin"
    otto_bin.mkdir(parents=True)
    user_bin.mkdir()
    (otto_bin / "pytest").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (user_bin / "pytest").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (otto_bin / "pytest").chmod(0o755)
    (user_bin / "pytest").chmod(0o755)
    monkeypatch.setattr("otto.checks.sys.executable", str(otto_bin / "python"))
    monkeypatch.setenv("VIRTUAL_ENV", str(otto_bin.parent))
    monkeypatch.setenv("PATH", f"{otto_bin}{os.pathsep}{user_bin}")

    assert _pytest_base_command(tmp_path, tmp_path) == [str(user_bin / "pytest")]


def test_repo_test_check_passes_pythonpath_to_subprocess(tmp_path: Path) -> None:
    """RepoTestCheck.command sees project_dir on PYTHONPATH."""
    (tmp_path / "mymod.py").write_text("VALUE = 42\n", encoding="utf-8")
    check = RepoTestCheck(
        command=("python", "-c", "import mymod; assert mymod.VALUE == 42"),
        timeout_s=10,
    )
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is True, evidence.raw


def test_repo_test_check_resolves_bare_python_away_from_otto_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    otto_bin = tmp_path / "otto-venv" / "bin"
    user_bin = tmp_path / "user-bin"
    otto_bin.mkdir(parents=True)
    user_bin.mkdir()
    (otto_bin / "python").write_text("#!/bin/sh\necho otto-python >&2\nexit 17\n", encoding="utf-8")
    (user_bin / "python3").write_text("#!/bin/sh\necho user-python3\nexit 0\n", encoding="utf-8")
    (otto_bin / "python").chmod(0o755)
    (user_bin / "python3").chmod(0o755)
    monkeypatch.setattr("otto.checks.sys.executable", str(otto_bin / "python"))
    monkeypatch.setenv("VIRTUAL_ENV", str(otto_bin.parent))
    monkeypatch.setenv("PATH", f"{otto_bin}{os.pathsep}{user_bin}")

    check = RepoTestCheck(command=("python", "-m", "pytest", "tests/test_app.py"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is True, evidence.raw
    assert evidence.raw["resolved_command"][0] == str(user_bin / "python3")
    assert "user-python3" in evidence.raw["stdout"]
    assert "otto-python" not in evidence.raw["stderr"]


def test_repo_test_check_prefers_project_venv_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_bin = tmp_path / ".venv" / "bin"
    user_bin = tmp_path / "user-bin"
    project_bin.mkdir(parents=True)
    user_bin.mkdir()
    (project_bin / "python").write_text("#!/bin/sh\necho project-python\nexit 0\n", encoding="utf-8")
    (user_bin / "python3").write_text("#!/bin/sh\necho user-python3\nexit 0\n", encoding="utf-8")
    (project_bin / "python").chmod(0o755)
    (user_bin / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(user_bin))

    check = RepoTestCheck(command=("python", "-m", "pytest", "tests/test_app.py"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is True, evidence.raw
    assert evidence.raw["resolved_command"][0] == str(project_bin / "python")
    assert "project-python" in evidence.raw["stdout"]


def test_repo_test_check_prefers_parent_project_venv_python_for_queue_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    worktree = project / ".worktrees" / "queued-task"
    project_bin = project / ".venv" / "bin"
    user_bin = tmp_path / "user-bin"
    project_bin.mkdir(parents=True)
    worktree.mkdir(parents=True)
    user_bin.mkdir()
    (project_bin / "python").write_text("#!/bin/sh\necho parent-project-python\nexit 0\n", encoding="utf-8")
    (user_bin / "python3").write_text("#!/bin/sh\necho user-python3\nexit 0\n", encoding="utf-8")
    (project_bin / "python").chmod(0o755)
    (user_bin / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(user_bin))

    check = RepoTestCheck(command=("python", "-m", "pytest", "tests/test_app.py"), timeout_s=10)
    evidence = run_check(check, project_dir=worktree, cwd=worktree)

    assert evidence.passed is True, evidence.raw
    assert evidence.raw["resolved_command"][0] == str(project_bin / "python")
    assert "parent-project-python" in evidence.raw["stdout"]


# ---------------------------------------------------------------------------
# BrowserJourney
# ---------------------------------------------------------------------------


def test_browser_journey_subprocess_and_globs_collect_artifacts(tmp_path: Path) -> None:
    # Write a script that creates two PNG-like artifacts then exits 0.
    script = tmp_path / "fake_browser.py"
    script.write_text(
        "from pathlib import Path\n"
        "evidence = Path('evidence/journey'); evidence.mkdir(parents=True, exist_ok=True)\n"
        "(evidence / 'step-1.png').write_bytes(b'fake-png-1')\n"
        "(evidence / 'step-2.png').write_bytes(b'fake-png-2')\n",
        encoding="utf-8",
    )
    check = BrowserJourney(
        command=("python", str(script)),
        evidence_globs=("evidence/journey/*.png",),
        timeout_s=15,
    )
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is True
    assert len(evidence.artifacts) == 2
    assert all(p.suffix == ".png" for p in evidence.artifacts)
    # Sorted by filename
    assert evidence.artifacts[0].name == "step-1.png"
    assert evidence.artifacts[1].name == "step-2.png"
    assert evidence.raw["resolved_command"][0].endswith(("python", "python3"))
    assert evidence.raw["browser_env"]["OTTO_BROWSER_BASE_URL"].startswith("http://127.0.0.1:")
    assert evidence.raw["browser_env"]["OTTO_BROWSER_SESSION"].startswith("ab")
    assert "/ab/" in evidence.raw["browser_env"]["AGENT_BROWSER_SOCKET_DIR"]
    assert len(evidence.raw["browser_env"]["AGENT_BROWSER_SOCKET_DIR"]) <= 18


def test_browser_journey_bootstraps_locked_node_project_for_python_wrapper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_npm = bin_dir / "npm"
    fake_npm.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$*\" >> npm.log\n"
        "if [ \"$1\" = \"ci\" ]; then mkdir -p node_modules/.bin; exit 0; fi\n"
        "exit 9\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    (tmp_path / "package.json").write_text(
        '{"scripts":{"dev":"vite --host 127.0.0.1"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    script = tmp_path / "tests" / "browser" / "journey.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "from pathlib import Path\n"
        "assert Path('node_modules/.bin').exists()\n"
        "out = Path('otto_artifacts/browser/check.png')\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_bytes(b'png')\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("python3", "tests/browser/journey.py"),
            evidence_globs=("otto_artifacts/browser/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is True
    assert evidence.raw["bootstrap_command"] == [
        "npm",
        "ci",
        "--prefer-offline",
        "--no-audit",
        "--no-fund",
    ]
    assert (tmp_path / "npm.log").read_text(encoding="utf-8").splitlines() == [
        "ci --prefer-offline --no-audit --no-fund",
    ]


def test_browser_journey_subprocess_failure_keeps_partial_artifacts(tmp_path: Path) -> None:
    script = tmp_path / "fake_browser.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('evidence').mkdir(exist_ok=True)\n"
        "Path('evidence/partial.png').write_bytes(b'p')\n"
        "import sys; sys.exit(2)\n",
        encoding="utf-8",
    )
    check = BrowserJourney(
        command=("python", str(script)),
        evidence_globs=("evidence/*.png",),
        timeout_s=15,
    )
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is False
    # Even on failure, artifacts that exist are collected.
    assert len(evidence.artifacts) == 1
    assert evidence.artifacts[0].name == "partial.png"


def test_browser_journey_failure_reports_blank_screenshot_diagnostic(tmp_path: Path) -> None:
    blank = tmp_path / "evidence" / "blank.png"
    script = tmp_path / "fake_browser.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path('evidence').mkdir(exist_ok=True)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    blank.parent.mkdir(parents=True)
    _write_png(blank, 8, 8, [[(245, 247, 244)] * 8 for _ in range(8)])

    evidence = run_check(
        BrowserJourney(
            command=("python", str(script)),
            evidence_globs=("evidence/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert evidence.detail == "exit=2 artifacts=1 blank screenshot evidence"
    assert evidence.raw["artifact_diagnostics"][0]["appears_blank"] is True
    assert "runtime render" in evidence.raw["artifact_diagnostics"][0]["diagnostic"]


def test_browser_journey_failure_reports_visible_screenshot_variance(tmp_path: Path) -> None:
    screenshot = tmp_path / "evidence" / "visible.png"
    script = tmp_path / "fake_browser.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path('evidence').mkdir(exist_ok=True)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    screenshot.parent.mkdir(parents=True)
    rows = [
        [(255, 255, 255), (0, 0, 0), (255, 0, 0), (0, 128, 0)] * 2
        for _ in range(8)
    ]
    _write_png(screenshot, 8, 8, rows)

    evidence = run_check(
        BrowserJourney(
            command=("python", str(script)),
            evidence_globs=("evidence/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert evidence.detail == "exit=2 artifacts=1"
    assert evidence.raw["artifact_diagnostics"][0]["appears_blank"] is False


def test_browser_journey_collects_printed_artifacts_when_glob_misses(tmp_path: Path) -> None:
    script = tmp_path / "fake_browser.py"
    screenshot = tmp_path / "tests" / "evidence" / "initial.png"
    script.write_text(
        "from pathlib import Path\n"
        f"p = Path({str(screenshot)!r})\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_bytes(b'png')\n"
        "print(f'Screenshot saved: {p}')\n",
        encoding="utf-8",
    )
    check = BrowserJourney(
        command=("python", str(script)),
        evidence_globs=("otto_artifacts/browser/*.png",),
        timeout_s=15,
    )

    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is True
    assert evidence.artifacts == [screenshot]
    assert evidence.detail == "exit=0 artifacts=1"


def test_browser_journey_fails_when_declared_evidence_missing(tmp_path: Path) -> None:
    script = tmp_path / "fake_browser.py"
    script.write_text("print('done without screenshots')\n", encoding="utf-8")
    check = BrowserJourney(
        command=("python", str(script)),
        evidence_globs=("evidence/*.png",),
        timeout_s=15,
    )

    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is False
    assert evidence.artifacts == []
    assert evidence.detail == "exit=0 artifacts=0 missing declared evidence"
    assert evidence.raw["missing_declared_evidence"] is True


def test_browser_journey_preflights_playwright_relative_routes_without_base_url(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test"}}),
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({ webServer: { command: 'npm run dev', url: 'http://127.0.0.1:4173' } });\n",
        encoding="utf-8",
    )
    browser_dir = tmp_path / "tests" / "browser"
    browser_dir.mkdir(parents=True)
    (browser_dir / "transactions.spec.ts").write_text(
        "import { test } from '@playwright/test';\n"
        "test('journey', async ({ page }) => { await page.goto('/transactions'); });\n",
        encoding="utf-8",
    )

    check = BrowserJourney(
        command=("npm", "run", "browser", "--", "transactions"),
        evidence_globs=("test-results/**/*.png",),
        timeout_s=15,
    )
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is False
    assert evidence.detail == "browser journey preflight failed artifacts=0"
    assert "baseURL" in evidence.raw["preflight_error"]
    assert "transactions.spec.ts" in evidence.raw["preflight_error"]


def test_browser_journey_preflights_playwright_hardcoded_port_without_otto_env(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test tests/browser/main.spec.ts"}}),
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({ webServer: { command: 'npm run dev -- --port 4173', "
        "url: 'http://127.0.0.1:4173' }, use: { baseURL: 'http://127.0.0.1:4173' } });\n",
        encoding="utf-8",
    )
    browser_dir = tmp_path / "tests" / "browser"
    browser_dir.mkdir(parents=True)
    (browser_dir / "main.spec.ts").write_text(
        "import { test } from '@playwright/test';\n"
        "test('journey', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("npm", "run", "browser"),
            evidence_globs=("test-results/**/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert evidence.detail == "browser journey preflight failed artifacts=0"
    assert "hard-codes loopback port" in evidence.raw["preflight_error"]
    assert "OTTO_BROWSER_BASE_URL" in evidence.raw["preflight_error"]


def test_browser_journey_preflights_duplicate_generated_playwright_config(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test tests/browser/main.spec.ts"}}),
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "const port = process.env.OTTO_BROWSER_PORT || '4173';\n"
        "const baseURL = process.env.OTTO_BROWSER_BASE_URL || `http://127.0.0.1:${port}`;\n"
        "export default defineConfig({ webServer: { command: `npm run dev -- --port ${port}`, url: baseURL }, use: { baseURL } });\n",
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.js").write_text(
        "export default { testIgnore: ['otto_logs/**'], testMatch: ['missing/**/*.spec.ts'] };\n",
        encoding="utf-8",
    )
    browser_dir = tmp_path / "tests" / "browser"
    browser_dir.mkdir(parents=True)
    (browser_dir / "main.spec.ts").write_text(
        "import { test } from '@playwright/test';\n"
        "test('journey', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("npm", "run", "browser"),
            evidence_globs=("test-results/**/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert "stale generated JS config" in evidence.raw["preflight_error"]
    assert "noEmit" in evidence.raw["preflight_error"]


def test_browser_journey_preflights_bare_runtime_test_ignore(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test tests/browser/main.spec.ts"}}),
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "const port = process.env.OTTO_BROWSER_PORT || '4173';\n"
        "const baseURL = process.env.OTTO_BROWSER_BASE_URL || `http://127.0.0.1:${port}`;\n"
        "export default defineConfig({ testIgnore: ['otto_logs/**'], webServer: { command: `npm run dev -- --port ${port}`, url: baseURL }, use: { baseURL } });\n",
        encoding="utf-8",
    )
    browser_dir = tmp_path / "tests" / "browser"
    browser_dir.mkdir(parents=True)
    (browser_dir / "main.spec.ts").write_text(
        "import { test } from '@playwright/test';\n"
        "test('journey', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("npm", "run", "browser"),
            evidence_globs=("test-results/**/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert "bare runtime ignore" in evidence.raw["preflight_error"]
    assert "No tests found" in evidence.raw["preflight_error"]


def test_browser_journey_preflights_agent_browser_without_unique_session(
    tmp_path: Path,
) -> None:
    check = BrowserJourney(
        command=("agent-browser", "open", "http://127.0.0.1:3000"),
        evidence_globs=("*.png",),
        timeout_s=15,
    )

    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is False
    assert evidence.detail == "browser journey preflight failed artifacts=0"
    assert "without a unique `--session`" in evidence.raw["preflight_error"]
    assert "AGENT_BROWSER_SOCKET_DIR" in evidence.raw["browser_env"]


def test_browser_journey_preflights_self_recursive_npm_browser_script(tmp_path: Path) -> None:
    journey = tmp_path / "tests" / "browser" / "test_transactions.py"
    journey.parent.mkdir(parents=True)
    journey.write_text(
        "import subprocess\n"
        "subprocess.run(['npm', 'run', 'browser', '--', "
        "'tests/browser/test_transactions.py'], check=True)\n",
        encoding="utf-8",
    )
    check = BrowserJourney(
        command=("python3", "tests/browser/test_transactions.py"),
        evidence_globs=("otto_artifacts/browser/transactions/*.png",),
        timeout_s=15,
    )

    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)

    assert evidence.passed is False
    assert evidence.detail == "browser journey preflight failed artifacts=0"
    assert "recursively runs the same journey" in evidence.raw["preflight_error"]


def test_browser_journey_preflights_agent_browser_without_server_boot(tmp_path: Path) -> None:
    journey = tmp_path / "tests" / "browser" / "full_workflow.py"
    journey.parent.mkdir(parents=True)
    journey.write_text(
        "import os, subprocess\n"
        "base_url = os.environ['OTTO_BROWSER_BASE_URL']\n"
        "subprocess.run(['agent-browser', '--session', 'ab12345', 'open', base_url], check=True)\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("python3", "tests/browser/full_workflow.py"),
            evidence_globs=("otto_artifacts/browser/full-workflow/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert evidence.detail == "browser journey preflight failed artifacts=0"
    assert "does not appear to start or delegate product-server boot" in evidence.raw["preflight_error"]
    assert "does not imply a server is already listening" in evidence.raw["preflight_error"]


def test_browser_journey_allows_agent_browser_with_server_boot(tmp_path: Path) -> None:
    journey = tmp_path / "tests" / "browser" / "full_workflow.py"
    journey.parent.mkdir(parents=True)
    journey.write_text(
        "import os, subprocess\n"
        "from pathlib import Path\n"
        "base_url = os.environ['OTTO_BROWSER_BASE_URL']\n"
        "if False:\n"
        "    server = subprocess.Popen(['npm', 'run', 'dev', '--', '--port', os.environ['OTTO_BROWSER_PORT']])\n"
        "    subprocess.run(['agent-browser', '--session', 'ab12345', 'open', base_url], check=True)\n"
        "out = Path('otto_artifacts/browser/full-workflow/ok.png')\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_bytes(b'png')\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("python3", "tests/browser/full_workflow.py"),
            evidence_globs=("otto_artifacts/browser/full-workflow/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.raw.get("preflight_error") is None
    assert "does not appear to start" not in evidence.detail


def test_browser_journey_preflights_overbroad_playwright_suite(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test"}}),
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "const port = process.env.OTTO_BROWSER_PORT || '4173';\n"
        "const baseURL = process.env.OTTO_BROWSER_BASE_URL || `http://127.0.0.1:${port}`;\n"
        "export default defineConfig({ webServer: { command: `npm run dev -- --port ${port}`, url: baseURL }, use: { baseURL } });\n",
        encoding="utf-8",
    )
    browser_dir = tmp_path / "tests" / "browser"
    browser_dir.mkdir(parents=True)
    for name in ("main.spec.ts", "settings.spec.ts"):
        (browser_dir / name).write_text(
            "import { test } from '@playwright/test';\n"
            "test('journey', async ({ page }) => { await page.goto('/'); });\n",
            encoding="utf-8",
        )

    evidence = run_check(
        BrowserJourney(
            command=("npm", "run", "browser"),
            evidence_globs=("test-results/**/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert "full browser suite" in evidence.raw["preflight_error"]


def test_browser_journey_preflights_python_wrapper_bypassing_browser_script(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test tests/browser/main.spec.ts"}}),
        encoding="utf-8",
    )
    (tmp_path / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "const port = process.env.OTTO_BROWSER_PORT || '4173';\n"
        "const baseURL = process.env.OTTO_BROWSER_BASE_URL || `http://127.0.0.1:${port}`;\n"
        "export default defineConfig({ webServer: { command: `npm run dev -- --port ${port}`, url: baseURL }, use: { baseURL } });\n",
        encoding="utf-8",
    )
    browser_dir = tmp_path / "tests" / "browser"
    browser_dir.mkdir(parents=True)
    (browser_dir / "main.spec.ts").write_text(
        "import { test } from '@playwright/test';\n"
        "test('journey', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )
    (browser_dir / "test_main.py").write_text(
        "import subprocess\n"
        "subprocess.run(['npx', 'playwright', 'test', 'tests/browser/main.spec.ts'], check=True)\n",
        encoding="utf-8",
    )

    evidence = run_check(
        BrowserJourney(
            command=("python3", "tests/browser/test_main.py"),
            evidence_globs=("test-results/**/*.png",),
            timeout_s=15,
        ),
        project_dir=tmp_path,
        cwd=tmp_path,
    )

    assert evidence.passed is False
    assert "bypasses it with a direct" in evidence.raw["preflight_error"]
    assert "tests/browser/test_main.py" in evidence.raw["preflight_error"]


def test_browser_journey_empty_command_is_informational(tmp_path: Path) -> None:
    """v2.1: empty browser command → informational PASS."""
    check = BrowserJourney(command=(), evidence_globs=(), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "command is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


# ---------------------------------------------------------------------------
# ApiProbe
# ---------------------------------------------------------------------------


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server convention
        if self.path == "/ok":
            body = b'{"status":"ok","data":[1,2,3]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
        else:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server error")

    def log_message(self, *args, **kwargs) -> None:  # noqa: D401, ANN002, ANN003
        return  # silence test output


def test_api_probe_happy_path() -> None:
    with _tiny_http_server(_OkHandler) as base_url:
        check = ApiProbe(method="GET", path="/ok", expect_status=200, expect_body_contains="\"ok\"")
        evidence = run_check(check, project_dir=Path("/tmp"), base_url=base_url)
    assert evidence.passed is True
    assert evidence.raw["status_code"] == 200
    assert "\"ok\"" in evidence.raw["response_text"]


def test_api_probe_status_mismatch() -> None:
    with _tiny_http_server(_OkHandler) as base_url:
        check = ApiProbe(method="GET", path="/missing", expect_status=200)
        evidence = run_check(check, project_dir=Path("/tmp"), base_url=base_url)
    assert evidence.passed is False
    assert evidence.raw["status_code"] == 404


def test_api_probe_body_mismatch() -> None:
    with _tiny_http_server(_OkHandler) as base_url:
        check = ApiProbe(
            method="GET", path="/ok", expect_status=200, expect_body_contains="will-not-find-this"
        )
        evidence = run_check(check, project_dir=Path("/tmp"), base_url=base_url)
    assert evidence.passed is False
    assert "body_contains" in evidence.detail


def test_api_probe_missing_base_url_is_informational() -> None:
    """v2.1: ApiProbe with no base_url → informational PASS. The check
    pipeline doesn't always boot a server; treating this as a failure
    blocked entire slices on missing infrastructure rather than missing
    behavior."""
    check = ApiProbe(method="GET", path="/", expect_status=200)
    evidence = run_check(check, project_dir=Path("/tmp"), base_url=None)
    assert evidence.passed is True
    assert "base_url" in evidence.detail
    assert evidence.raw["malformed_check"] is True


def test_api_probe_connection_error() -> None:
    # 127.0.0.1:1 is the discard port — refuses connections
    check = ApiProbe(method="GET", path="/", expect_status=200, timeout_s=2)
    evidence = run_check(check, project_dir=Path("/tmp"), base_url="http://127.0.0.1:1")
    assert evidence.passed is False
    assert "connection error" in evidence.detail or "error" in evidence.raw


# ---------------------------------------------------------------------------
# StateInvariant
# ---------------------------------------------------------------------------


def test_state_invariant_true_expression(tmp_path: Path) -> None:
    check = StateInvariant(description="trivial truth", expression="1 + 1 == 2")
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True


def test_state_invariant_false_expression(tmp_path: Path) -> None:
    check = StateInvariant(description="false", expression="1 == 2")
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False


def test_state_invariant_filesystem_helpers(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("# entry", encoding="utf-8")
    check = StateInvariant(
        description="single app shell",
        expression="exists('app/main.py') and not is_dir('microfeed')",
    )
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True


def test_state_invariant_filesystem_helpers_resolve_from_cwd(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    worktree = tmp_path / "linked-worktree"
    project_root.mkdir()
    worktree.mkdir()
    (project_root / "package.json").write_text('{"name":"stale-root"}', encoding="utf-8")
    (worktree / "src").mkdir()
    (worktree / "src" / "app.jsx").write_text("export default null\n", encoding="utf-8")

    check = StateInvariant(
        description="linked worktree app shell",
        expression=(
            "exists('src/app.jsx') and is_file('src/app.jsx') "
            "and glob_count('src/*.jsx') == 1 "
            "and 'export default' in read_text('src/app.jsx')"
        ),
    )

    evidence = run_check(check, project_dir=project_root, cwd=worktree)
    assert evidence.passed is True


def test_state_invariant_glob_count(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("", encoding="utf-8")
    check = StateInvariant(
        description="two python files in src",
        expression="glob_count('src/*.py') == 2",
    )
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True


def test_state_invariant_non_python_expression_treated_as_informational(tmp_path: Path) -> None:
    """v2 finding F2 (R26): when expression isn't parseable Python (agent
    emitted prose), do NOT fail the slice. Treat as informational PASS;
    real damage is caught by other checks + audit's contract gate.
    See docs/intent-to-product-v2.md.
    """
    check = StateInvariant(
        description="App shell has create_app factory and database setup",
        expression="App shell has create_app factory and database setup",
    )
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True  # informational, not blocking
    assert "informational" in evidence.detail
    assert evidence.raw["non_python_expression"] is True


def test_state_invariant_empty_expression_is_informational(tmp_path: Path) -> None:
    """v2.1: empty expression → informational PASS."""
    check = StateInvariant(description="x", expression="")
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "expression is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


def test_state_invariant_http_get_when_base_url_set() -> None:
    with _tiny_http_server(_OkHandler) as base_url:
        check = StateInvariant(
            description="server returns ok",
            expression="http_get('/ok')['status'] == 200",
        )
        evidence = run_check(check, project_dir=Path("/tmp"), base_url=base_url)
    assert evidence.passed is True


def test_state_invariant_no_http_get_without_base_url_is_informational(tmp_path: Path) -> None:
    """v2 generalized F2: NameError (missing symbol in namespace) is
    informational, not slice-blocking. Eval errors mean the expression
    isn't a clean predicate, not that the predicate is false."""
    check = StateInvariant(description="x", expression="http_get('/ok')['status'] == 200")
    evidence = run_check(check, project_dir=tmp_path, base_url=None)
    assert evidence.passed is True
    assert "NameError" in evidence.detail
    assert "http_get" in evidence.detail
    assert evidence.raw["eval_error"].startswith("NameError")


# ---------------------------------------------------------------------------
# run_checks dispatch + raw_log_dir
# ---------------------------------------------------------------------------


def test_run_checks_dispatches_in_order(tmp_path: Path) -> None:
    checks = [
        RepoTestCheck(command=("python", "-c", "print(1)"), timeout_s=10),
        StateInvariant(description="true", expression="1 == 1"),
        RepoTestCheck(command=("python", "-c", "import sys; sys.exit(3)"), timeout_s=10),
    ]
    results = run_checks(checks, project_dir=tmp_path)
    assert len(results) == 3
    assert results[0][1].passed is True
    assert results[1][1].passed is True
    assert results[2][1].passed is False


def test_run_checks_writes_raw_logs_for_subprocess_kinds(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    checks: list = [
        RepoTestCheck(command=("python", "-c", "print('a')"), timeout_s=10),
        StateInvariant(description="x", expression="True"),  # no log file
        RepoTestCheck(command=("python", "-c", "print('b')"), timeout_s=10),
    ]
    run_checks(checks, project_dir=tmp_path, raw_log_dir=log_dir)
    log_files = sorted(log_dir.glob("*.log"))
    assert len(log_files) == 2  # only RepoTestCheck logs
    assert "RepoTestCheck" in log_files[0].name
    assert "a" in log_files[0].read_text(encoding="utf-8")
    assert "b" in log_files[1].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Unsupported kind safety
# ---------------------------------------------------------------------------


def test_unsupported_kind_returns_failure_evidence(tmp_path: Path) -> None:
    class FakeCheck:
        kind = "fake"
        timeout_s = 10

    evidence = run_check(FakeCheck(), project_dir=tmp_path)  # type: ignore[arg-type]
    assert evidence.passed is False
    assert "unsupported check kind" in evidence.detail


# ---------------------------------------------------------------------------
# V11: pytest selector with logical operators routes through -k
# ---------------------------------------------------------------------------


def test_pytest_selector_with_or_uses_k_expression(tmp_path: Path) -> None:
    """V11: 'tests/test_x.py::a or tests/test_x.py::b' must run BOTH tests
    via pytest -k. Previously passed verbatim, pytest interpreted the
    whole string as a single nonexistent node ID and returned exit=4.
    Observed in P3 e2e (bookmarks slice spuriously BLOCKED).
    """
    from otto.checks import run_checks
    from otto.spec_compile import PytestCheck
    # Create a test file with two passing tests.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_v11.py").write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n\n"
        "def test_gamma():\n    assert True\n",
        encoding="utf-8",
    )
    check = PytestCheck(
        selector="tests/test_v11.py::test_alpha or tests/test_v11.py::test_beta",
        timeout_s=30,
    )
    pairs = run_checks([check], project_dir=tmp_path, cwd=tmp_path)
    evidence = pairs[0][1]
    assert evidence.passed, f"V11: -k expression should match both tests; detail={evidence.detail}"
    cmd = evidence.raw.get("command") or []
    assert "-k" in cmd, f"-k flag missing; got cmd={cmd}"


# ---------------------------------------------------------------------------
# CLIProbe
# ---------------------------------------------------------------------------


def test_cli_probe_happy_path(tmp_path: Path) -> None:
    check = CLIProbe(
        command=("python", "-c", "print('hello-cli')"),
        expect_exit_code=0,
        expect_stdout_substring="hello-cli",
        timeout_s=10,
    )
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True, evidence.raw
    assert evidence.raw["exit_code"] == 0
    assert evidence.raw["stdout_match"] is True


def test_cli_probe_exit_code_mismatch_fails(tmp_path: Path) -> None:
    check = CLIProbe(
        command=("python", "-c", "import sys; sys.exit(2)"),
        expect_exit_code=0,
        timeout_s=10,
    )
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert evidence.raw["exit_code"] == 2
    assert "expected=0" in evidence.detail


def test_cli_probe_stdout_substring_miss_fails(tmp_path: Path) -> None:
    check = CLIProbe(
        command=("python", "-c", "print('actual')"),
        expect_exit_code=0,
        expect_stdout_substring="nope",
        timeout_s=10,
    )
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert evidence.raw["stdout_match"] is False


def test_cli_probe_empty_command_is_informational(tmp_path: Path) -> None:
    check = CLIProbe(command=(), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "command is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


# ---------------------------------------------------------------------------
# ImportCheck
# ---------------------------------------------------------------------------


def test_import_check_happy_path(tmp_path: Path) -> None:
    # `json` is in stdlib — always importable.
    check = ImportCheck(package_name="json", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True, evidence.raw
    assert evidence.raw["exit_code"] == 0
    assert evidence.raw["package_name"] == "json"


def test_import_check_missing_package_fails(tmp_path: Path) -> None:
    check = ImportCheck(package_name="this_package_does_not_exist_xyz_42", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert evidence.raw["exit_code"] != 0
    # The traceback ends up on stderr.
    assert "ModuleNotFoundError" in evidence.raw["stderr"] or "ImportError" in evidence.raw["stderr"]


def test_import_check_imports_local_module(tmp_path: Path) -> None:
    """ImportCheck sees project_dir on PYTHONPATH (same convention as PytestCheck)."""
    (tmp_path / "mylib.py").write_text("__version__ = '1.2.3'\n", encoding="utf-8")
    check = ImportCheck(package_name="mylib", expect_version="1.2.3", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is True, evidence.raw


def test_import_check_version_mismatch_fails(tmp_path: Path) -> None:
    (tmp_path / "mylib.py").write_text("__version__ = '0.0.1'\n", encoding="utf-8")
    check = ImportCheck(package_name="mylib", expect_version="9.9.9", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is False
    assert evidence.raw["exit_code"] != 0


def test_import_check_empty_package_is_informational(tmp_path: Path) -> None:
    check = ImportCheck(package_name="", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "package_name is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


# ---------------------------------------------------------------------------
# TypeCheck
# ---------------------------------------------------------------------------


def test_type_check_empty_paths_is_informational(tmp_path: Path) -> None:
    check = TypeCheck(paths=(), tool="mypy", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True
    assert "paths is empty" in evidence.detail
    assert evidence.raw["malformed_check"] is True


def test_type_check_unsupported_tool_fails(tmp_path: Path) -> None:
    check = TypeCheck(paths=("src/",), tool="not-a-real-checker", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "unsupported type-checker" in evidence.detail


def test_type_check_tool_unavailable_fails_cleanly(tmp_path: Path, monkeypatch) -> None:
    """When the type-checker isn't on PATH, return passed=False with a
    clean 'tool not available' detail — don't fabricate success.
    """
    import otto.checks as checks_mod

    def _fake_which(name: str) -> str | None:
        return None

    monkeypatch.setattr(checks_mod, "_which", _fake_which)
    check = TypeCheck(paths=("src/",), tool="mypy", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "not available" in evidence.detail
    assert evidence.raw["tool_available"] is False


def test_type_check_happy_path(tmp_path: Path, monkeypatch) -> None:
    """Stub out _which + _run_command so the test is hermetic — we don't
    require mypy/pyright to be installed for unit tests."""
    import subprocess as sp
    import otto.checks as checks_mod

    monkeypatch.setattr(checks_mod, "_which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, *, cwd, timeout_s, extra_pythonpath=None):  # type: ignore[no-untyped-def]
        return sp.CompletedProcess(args=cmd, returncode=0, stdout="Success: no issues found\n", stderr="")

    monkeypatch.setattr(checks_mod, "_run_command", _fake_run)
    check = TypeCheck(paths=("src/",), tool="mypy", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is True, evidence.raw
    assert evidence.raw["exit_code"] == 0
    assert evidence.raw["tool"] == "mypy"


def test_type_check_failure_reports_errors(tmp_path: Path, monkeypatch) -> None:
    import subprocess as sp
    import otto.checks as checks_mod

    monkeypatch.setattr(checks_mod, "_which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, *, cwd, timeout_s, extra_pythonpath=None):  # type: ignore[no-untyped-def]
        return sp.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="src/x.py:10: error: Incompatible return value type\n",
            stderr="",
        )

    monkeypatch.setattr(checks_mod, "_run_command", _fake_run)
    check = TypeCheck(paths=("src/",), tool="mypy", timeout_s=30)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert evidence.raw["exit_code"] == 1
    assert "Incompatible return value type" in evidence.raw["stdout"]


# ---------------------------------------------------------------------------
# V11 follow-up tests
# ---------------------------------------------------------------------------


def test_pytest_selector_single_node_id_unchanged(tmp_path: Path) -> None:
    """V11: a plain single node id (no `or`/`and`) is passed verbatim
    as a positional arg, preserving the existing happy path.
    """
    from otto.checks import run_checks
    from otto.spec_compile import PytestCheck
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_v11.py").write_text(
        "def test_one():\n    assert True\n",
        encoding="utf-8",
    )
    check = PytestCheck(selector="tests/test_v11.py::test_one", timeout_s=30)
    pairs = run_checks([check], project_dir=tmp_path, cwd=tmp_path)
    evidence = pairs[0][1]
    assert evidence.passed
    cmd = evidence.raw.get("command") or []
    assert "-k" not in cmd, f"-k should NOT be used for single node id; got {cmd}"
    assert "tests/test_v11.py::test_one" in cmd
