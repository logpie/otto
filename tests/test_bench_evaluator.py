"""Unit tests for scripts/bench_evaluator.py."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_evaluator as be  # noqa: E402


def _ctx(tmp_path: Path) -> be.EvaluatorContext:
    return be.EvaluatorContext(
        project_dir=tmp_path, python=Path(sys.executable),
        project_kind="webapp", timeout_s=30,
    )


# ---------------------------------------------------------------------------
# eval_contract_test
# ---------------------------------------------------------------------------


def test_contract_test_passing(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "run_acceptance.py").write_text(
        "print('acceptance:thing:PASS')\n"
        "import sys; sys.exit(0)\n"
    )
    result = be.eval_contract_test(_ctx(tmp_path))
    assert result.status == "passed"
    assert any("PASS" in f.message for f in result.findings)


def test_contract_test_failing(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "run_acceptance.py").write_text(
        "print('FAIL: thing broken')\n"
        "import sys; sys.exit(2)\n"
    )
    result = be.eval_contract_test(_ctx(tmp_path))
    assert result.status == "blocked"
    assert "exit=2" in result.summary


def test_contract_test_missing_skipped(tmp_path: Path) -> None:
    """No test script and no otto.yaml → skipped, not crash."""
    result = be.eval_contract_test(_ctx(tmp_path))
    assert result.status == "skipped"


# ---------------------------------------------------------------------------
# eval_code_health
# ---------------------------------------------------------------------------


def test_code_health_clean_passes(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def hello() -> str:\n"
        "    return 'world'\n"
    )
    result = be.eval_code_health(_ctx(tmp_path))
    assert result.status == "passed"


def test_code_health_flags_todos(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "# TODO: implement\n"
        "def x():\n"
        "    pass\n"
    )
    result = be.eval_code_health(_ctx(tmp_path))
    # 1 TODO warning; passed because <=5 warnings.
    assert result.status == "passed"
    assert any("TODO" in f.message for f in result.findings)


def test_code_health_blocks_on_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(
        "def x(:\n  pass\n"
    )
    result = be.eval_code_health(_ctx(tmp_path))
    assert result.status == "blocked"
    assert any("syntax error" in f.message for f in result.findings)


def test_code_health_partial_on_many_warnings(tmp_path: Path) -> None:
    for i in range(7):
        (tmp_path / f"f{i}.py").write_text(
            f"# TODO: file {i}\n"
            "raise NotImplementedError()\n"
        )
    result = be.eval_code_health(_ctx(tmp_path))
    assert result.status == "partial"


# ---------------------------------------------------------------------------
# eval_edge_cases_cli
# ---------------------------------------------------------------------------


def test_edge_cases_cli_clean(tmp_path: Path) -> None:
    cli_path = tmp_path / "cli.py"
    cli_path.write_text(
        "import sys\n"
        "if '--help' in sys.argv:\n"
        "    print('Usage: cli [--option]')\n"
        "    sys.exit(0)\n"
        "if '--bogus-flag-xyz' in sys.argv:\n"
        "    print('error: unknown flag', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "print('default behavior')\n"
        "sys.exit(0)\n"
    )
    result = be.eval_edge_cases_cli(
        _ctx(tmp_path), entry_argv=[sys.executable, str(cli_path)],
    )
    assert result.status == "passed"


def test_edge_cases_cli_flags_traceback(tmp_path: Path) -> None:
    cli_path = tmp_path / "cli.py"
    cli_path.write_text(
        "import sys\n"
        "if '--help' in sys.argv: print('help'); sys.exit(0)\n"
        "if '--bogus-flag-xyz' in sys.argv: raise ValueError('broken')\n"
        "sys.exit(0)\n"
    )
    result = be.eval_edge_cases_cli(
        _ctx(tmp_path), entry_argv=[sys.executable, str(cli_path)],
    )
    assert result.status in ("partial", "blocked")
    assert any("traceback" in f.message.lower() for f in result.findings)


# ---------------------------------------------------------------------------
# eval_edge_cases_webapp + security + performance — need a tiny test server
# ---------------------------------------------------------------------------


class _TinyHandler(BaseHTTPRequestHandler):
    """Tiny HTTP server that responds sanely to most edge inputs."""
    def do_POST(self):  # noqa: N802 — required by stdlib
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        # If we receive a SQL-shaped string, respond with 400 (validation error)
        # rather than crashing.
        text = data.get("text", "") + data.get("display_name", "")
        if "DROP TABLE" in text or "<script>" in text:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid input"}')
            return
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/this-route-does-not-exist"):
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>404 Not Found</h1>")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        # Echo SAFE_TOKEN_42 (escaped) to prove no XSS leak
        self.wfile.write(b"<html><body>hello SAFE_TOKEN_42</body></html>")

    def log_message(self, *args, **kwargs):  # silence
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def tiny_server():
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _TinyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_edge_cases_webapp_clean(tmp_path: Path, tiny_server: str) -> None:
    result = be.eval_edge_cases_webapp(_ctx(tmp_path), tiny_server)
    assert result.status == "passed"


def test_security_baseline_webapp_clean(tmp_path: Path, tiny_server: str) -> None:
    result = be.eval_security_baseline_webapp(_ctx(tmp_path), tiny_server)
    assert result.status == "passed"


def test_performance_webapp_under_budget(tmp_path: Path, tiny_server: str) -> None:
    result = be.eval_performance_webapp(_ctx(tmp_path), tiny_server)
    # Tiny server is fast; should pass.
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def test_aggregate_all_pass() -> None:
    results = [
        be.EvalResult(name="a", status="passed"),
        be.EvalResult(name="b", status="passed"),
    ]
    assert be.aggregate_status(results) == "passed"


def test_aggregate_skipped_excluded() -> None:
    results = [
        be.EvalResult(name="a", status="passed"),
        be.EvalResult(name="b", status="skipped"),
    ]
    assert be.aggregate_status(results) == "passed"


def test_aggregate_blocked_dominates() -> None:
    results = [
        be.EvalResult(name="a", status="passed"),
        be.EvalResult(name="b", status="blocked"),
    ]
    assert be.aggregate_status(results) == "blocked"


def test_aggregate_majority_partial() -> None:
    results = [
        be.EvalResult(name="a", status="partial"),
        be.EvalResult(name="b", status="partial"),
        be.EvalResult(name="c", status="passed"),
    ]
    assert be.aggregate_status(results) == "partial"


def test_run_evaluators_isolates_crashes(tmp_path: Path) -> None:
    def crashing(ctx):
        raise RuntimeError("oops")

    def passing(ctx):
        return be.EvalResult(name="ok", status="passed")

    results = be.run_evaluators(_ctx(tmp_path), [crashing, passing])
    assert results[0].status == "error"
    assert "oops" in results[0].summary
    assert results[1].status == "passed"
