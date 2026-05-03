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
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


from otto.checks import run_check, run_checks
from otto.spec_compile import (
    ApiProbe,
    BrowserJourney,
    PytestCheck,
    RepoTestCheck,
    StateInvariant,
)


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


def test_repo_test_nonzero_exit_fails(tmp_path: Path) -> None:
    check = RepoTestCheck(command=("python", "-c", "import sys; sys.exit(7)"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "exit=7" in evidence.detail
    assert evidence.raw["exit_code"] == 7


def test_repo_test_empty_command_returns_error(tmp_path: Path) -> None:
    check = RepoTestCheck(command=(), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "command is empty" in evidence.detail


def test_repo_test_timeout_marked_as_failure(tmp_path: Path) -> None:
    check = RepoTestCheck(command=("python", "-c", "import time; time.sleep(5)"), timeout_s=1)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "timed out" in evidence.detail
    assert evidence.raw.get("timeout") is True


def test_repo_test_writes_raw_log_when_path_given(tmp_path: Path) -> None:
    log_path = tmp_path / "evidence" / "repo.log"
    check = RepoTestCheck(command=("python", "-c", "print('hello-raw')"), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path, raw_log_path=log_path)
    assert evidence.passed is True
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello-raw" in contents
    assert "exit_code=0" in contents


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


def test_pytest_check_empty_selector_returns_error(tmp_path: Path) -> None:
    check = PytestCheck(selector="", timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "selector is empty" in evidence.detail


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


def test_repo_test_check_passes_pythonpath_to_subprocess(tmp_path: Path) -> None:
    """RepoTestCheck.command sees project_dir on PYTHONPATH."""
    (tmp_path / "mymod.py").write_text("VALUE = 42\n", encoding="utf-8")
    check = RepoTestCheck(
        command=("python", "-c", "import mymod; assert mymod.VALUE == 42"),
        timeout_s=10,
    )
    evidence = run_check(check, project_dir=tmp_path, cwd=tmp_path)
    assert evidence.passed is True, evidence.raw


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


def test_browser_journey_empty_command_returns_error(tmp_path: Path) -> None:
    check = BrowserJourney(command=(), evidence_globs=(), timeout_s=10)
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "command is empty" in evidence.detail


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


def test_api_probe_missing_base_url_returns_error() -> None:
    check = ApiProbe(method="GET", path="/", expect_status=200)
    evidence = run_check(check, project_dir=Path("/tmp"), base_url=None)
    assert evidence.passed is False
    assert "base_url" in evidence.detail


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


def test_state_invariant_empty_expression_returns_error(tmp_path: Path) -> None:
    check = StateInvariant(description="x", expression="")
    evidence = run_check(check, project_dir=tmp_path)
    assert evidence.passed is False
    assert "expression is empty" in evidence.detail


def test_state_invariant_http_get_when_base_url_set() -> None:
    with _tiny_http_server(_OkHandler) as base_url:
        check = StateInvariant(
            description="server returns ok",
            expression="http_get('/ok')['status'] == 200",
        )
        evidence = run_check(check, project_dir=Path("/tmp"), base_url=base_url)
    assert evidence.passed is True


def test_state_invariant_no_http_get_without_base_url(tmp_path: Path) -> None:
    # Without base_url, http_get is not in the namespace; using it should
    # raise NameError.
    check = StateInvariant(description="x", expression="http_get('/ok')['status'] == 200")
    evidence = run_check(check, project_dir=tmp_path, base_url=None)
    assert evidence.passed is False
    assert "NameError" in evidence.detail


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
