from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from otto.journey_api_executor import run_api_journey_executor
from otto.journey_verdict_sink import resolve_journey_verdicts
from otto.lead_verify import run_verify_for_lead


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _StatefulApiHandler(BaseHTTPRequestHandler):
    items: dict[str, dict[str, Any]] = {}

    def log_message(self, format: str, *_args: object) -> None:
        del format
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if self.path == "/login":
            self._json(200, {"token": "secret-token", "user": payload.get("username")})
            return
        if self.path == "/items":
            if self.headers.get("Authorization") != "Bearer secret-token":
                self._json(401, {"error": "missing auth"})
                return
            item = {"id": "item-1", "name": payload.get("name"), "status": "created"}
            self.items[item["id"]] = item
            self._json(201, item)
            return
        self._json(404, {"error": "not found"})

    def do_GET(self) -> None:
        if self.path == "/items/item-1":
            if self.headers.get("Authorization") != "Bearer secret-token":
                self._json(401, {"error": "missing auth"})
                return
            self._json(200, self.items.get("item-1", {"error": "missing"}))
            return
        self._json(404, {"error": "not found"})


def _start_http_server(handler: type[BaseHTTPRequestHandler]) -> tuple[str, ThreadingHTTPServer]:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", server


def _stateful_http_journey() -> dict[str, Any]:
    return {
        "id": "stateful_item_flow",
        "verification_level": "api",
        "probe_kind": "http_api",
        "covers_primary_actions": ["items.create"],
        "pass_model": {
            "steps": [
                {
                    "id": "login",
                    "method": "POST",
                    "path": "/login",
                    "json": {"username": "ada"},
                    "expect_status": 200,
                    "expect_json": {"token": "secret-token", "user": "ada"},
                    "extract": {"token": "$.token"},
                },
                {
                    "id": "create_item",
                    "method": "POST",
                    "path": "/items",
                    "headers": {"Authorization": "Bearer {{token}}"},
                    "json": {"name": "Widget"},
                    "expect_status": 201,
                    "expect_json": {"name": "Widget", "status": "created"},
                    "extract": {"item_id": "$.id"},
                },
                {
                    "id": "read_item",
                    "method": "GET",
                    "path": "/items/{{item_id}}",
                    "headers": {"Authorization": "Bearer {{token}}"},
                    "expect_status": 200,
                    "expect_json": {"id": "{{item_id}}", "name": "Widget"},
                },
            ]
        },
    }


def test_stateful_http_api_journey_carries_auth_and_state(tmp_path: Path) -> None:
    base_url, server = _start_http_server(_StatefulApiHandler)
    try:
        run = run_api_journey_executor(
            journeys=[_stateful_http_journey()],
            project_dir=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            base_url=base_url,
            timeout_s=5,
        )
    finally:
        server.shutdown()

    verdicts = resolve_journey_verdicts(
        journeys=[_stateful_http_journey()],
        execution_scope="leaf",
        executor_results=run.executor_results,
        registered_executor_levels={"api"},
    )
    assert verdicts[0]["passed"] is True
    assert verdicts[0]["source"] == "api_executor"
    assert run.artifact_paths


def test_api_journey_fails_closed_on_malformed_or_unsupported_lowering(tmp_path: Path) -> None:
    journeys = [
        {
            "id": "malformed",
            "verification_level": "api",
            "probe_kind": "http_api",
            "pass_model": {"steps": []},
        },
        {
            "id": "unsupported",
            "verification_level": "api",
            "probe_kind": "not_a_probe",
            "pass_model": {},
        },
    ]

    run = run_api_journey_executor(
        journeys=journeys,
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        base_url="http://127.0.0.1:1",
        timeout_s=1,
    )

    verdicts = resolve_journey_verdicts(
        journeys=journeys,
        execution_scope="leaf",
        executor_results=run.executor_results,
        registered_executor_levels={"api"},
    )
    assert [verdict["passed"] for verdict in verdicts] == [False, False]
    assert {verdict["status"] for verdict in verdicts} == {"unverified"}
    assert all(verdict["source"] == "api_executor" for verdict in verdicts)


def test_api_executors_fail_closed_on_informational_pass_models(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    weak_journeys = [
        {
            "id": "http_status_only",
            "verification_level": "api",
            "probe_kind": "http_api",
            "pass_model": {"steps": [{"method": "GET", "path": "/health", "expect_status": 200}]},
        },
        {
            "id": "cli_exit_only",
            "verification_level": "api",
            "probe_kind": "cli_command",
            "pass_model": {"command": ["python3", "-c", "print('ok')"], "expect_exit_code": 0},
        },
        {
            "id": "library_no_exception_only",
            "verification_level": "api",
            "probe_kind": "library_call",
            "pass_model": {"module": "calc", "function": "add", "args": [1, 2]},
        },
        {
            "id": "service_status_only",
            "verification_level": "api",
            "probe_kind": "service_health",
            "pass_model": {
                "start_command": ["python3", "-m", "http.server", "0"],
                "health_url": "http://127.0.0.1:1/health",
                "expect_status": 200,
            },
        },
    ]

    run = run_api_journey_executor(
        journeys=weak_journeys,
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        base_url="http://127.0.0.1:1",
        timeout_s=1,
    )

    verdicts = resolve_journey_verdicts(
        journeys=weak_journeys,
        execution_scope="leaf",
        executor_results=run.executor_results,
        registered_executor_levels={"api"},
    )
    assert [verdict["passed"] for verdict in verdicts] == [False, False, False, False]
    assert {verdict["status"] for verdict in verdicts} == {"unverified"}
    assert all("requires" in verdict["detail"] for verdict in verdicts)


def test_cli_and_library_api_journeys_pass_without_http_base_url(tmp_path: Path) -> None:
    (tmp_path / "tool.py").write_text(
        "from pathlib import Path\n"
        "Path('cli-output.txt').write_text('cli effect ok\\n', encoding='utf-8')\n"
        "print('cli done')\n",
        encoding="utf-8",
    )
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n"
        "    return {'total': a + b}\n",
        encoding="utf-8",
    )
    journeys = [
        {
            "id": "cli_flow",
            "verification_level": "api",
            "probe_kind": "cli_command",
            "pass_model": {
                "command": ["python3", "tool.py"],
                "expect_exit_code": 0,
                "stdout_contains": "cli done",
                "fs_effects": [
                    {"path": "cli-output.txt", "exists": True, "contains": "cli effect ok"}
                ],
            },
        },
        {
            "id": "library_flow",
            "verification_level": "api",
            "probe_kind": "library_call",
            "pass_model": {
                "module": "calc",
                "function": "add",
                "args": [2, 5],
                "expect_return": {"total": 7},
            },
        },
    ]

    run = run_api_journey_executor(
        journeys=journeys,
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        timeout_s=5,
    )
    verdicts = resolve_journey_verdicts(
        journeys=journeys,
        execution_scope="leaf",
        executor_results=run.executor_results,
        registered_executor_levels={"api"},
    )

    assert [verdict["passed"] for verdict in verdicts] == [True, True]
    assert all(verdict["source"] == "api_executor" for verdict in verdicts)


def test_service_health_journey_starts_service_and_asserts_health(tmp_path: Path) -> None:
    port = _free_port()
    (tmp_path / "service.py").write_text(
        "import os\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def log_message(self, *_args): return\n"
        "    def do_GET(self):\n"
        "        body = b'healthy'\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n",
        encoding="utf-8",
    )
    journey = {
        "id": "service_ready",
        "verification_level": "api",
        "probe_kind": "service_health",
        "pass_model": {
            "start_command": ["python3", "service.py"],
            "env": {"PORT": str(port)},
            "health_url": f"http://127.0.0.1:{port}/health",
            "expect_status": 200,
            "expect_body_contains": "healthy",
        },
    }

    run = run_api_journey_executor(
        journeys=[journey],
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        timeout_s=5,
    )

    assert run.executor_results[0]["status"] == "pass"
    assert run.executor_results[0]["proof_usable"] is True


def test_registered_api_journey_without_executor_result_fails_closed() -> None:
    verdicts = resolve_journey_verdicts(
        journeys=[{"id": "api_without_executor", "verification_level": "api"}],
        execution_scope="leaf",
        executor_results=[],
        registered_executor_levels={"ui", "api"},
    )

    assert verdicts[0]["passed"] is False
    assert verdicts[0]["source"] == "journey_verdict_sink"
    assert "produced no usable result" in verdicts[0]["detail"]


def test_leaf_verify_filters_api_journeys_by_owned_primary_actions(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text(
        "def add(a, b): return {'total': a + b}\n"
        "def sub(a, b): return {'total': a - b}\n",
        encoding="utf-8",
    )
    session_dir = tmp_path / "otto_logs" / "sessions" / "s-api"
    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "intent": "calculator",
                "project_kind": "library",
                "behavior_journeys": [
                    {
                        "id": "add_flow",
                        "description": "Add numbers.",
                        "covers_primary_actions": ["calc.add"],
                        "verification_level": "api",
                        "probe_kind": "library_call",
                        "pass_model": {
                            "module": "calc",
                            "function": "add",
                            "args": [3, 4],
                            "expect_return": {"total": 7},
                        },
                    },
                    {
                        "id": "sub_flow",
                        "description": "Subtract numbers.",
                        "covers_primary_actions": ["calc.sub"],
                        "verification_level": "api",
                        "probe_kind": "library_call",
                        "pass_model": {
                            "module": "calc",
                            "function": "sub",
                            "args": [3, 4],
                            "expect_return": {"total": -1},
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = asyncio.run(
        run_verify_for_lead(
            task_id="leaf",
            project_dir=tmp_path,
            session_dir=session_dir,
            feature_scope_ids=["calc.add"],
        )
    )

    assert result["verdict"] == "pass"
    assert [journey["id"] for journey in result["journeys"]] == ["add_flow"]
