from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

from otto.v5_clean_verify import verify_from_clean_oracle


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _journey() -> dict[str, Any]:
    observable = {
        "kind": "network_and_ui_effect",
        "primary_action_id": "workspaces.create",
        "description": (
            "Creating a workspace posts to the workspace API and the created "
            "workspace name becomes visible in the UI."
        ),
        "method": "POST",
        "path": "/api/workspaces",
        "status": 201,
        "selector": "[data-testid='workspace-card']",
        "text": "Acme Workspace",
        "ui_effect": "Acme Workspace appears in the workspace list.",
    }
    return {
        "id": "new_user_onboard",
        "role": "illustrative",
        "description": "New user creates a workspace and sees it in the UI.",
        "covers_primary_actions": ["workspaces.create"],
        "start_state": "authenticated_seeded_user",
        "entry_route": "/workspaces",
        "verification_level": "ui",
        "pass_model": {
            "start_state": "authenticated_seeded_user",
            "setup": [],
            "actions": [
                {
                    "id": "workspaces.create",
                    "state_changing": True,
                    "covers_primary_actions": ["workspaces.create"],
                    "selector": "[data-testid='create-workspace']",
                    "inputs": [
                        {
                            "selector": "[data-testid='workspace-name']",
                            "value": "Acme Workspace",
                        },
                        {
                            "selector": "[data-testid='workspace-slug']",
                            "value": "acme-workspace",
                        },
                    ],
                    "network_expectations": [
                        {"method": "POST", "path": "/api/workspaces", "status": 201}
                    ],
                    "success_observables": [observable],
                }
            ],
            "success_observables": [observable],
            "ready_policy": {
                "route": "/workspaces",
                "wait_for": "interactive",
                "timeout_ms": 3000,
            },
            "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 3000},
            "network_expectations": [],
            "final_dom_assertions": [
                {
                    "kind": "persisted_data_visible",
                    "primary_action_id": "workspaces.create",
                    "description": (
                        "The created workspace card Acme Workspace remains "
                        "visible in the UI."
                    ),
                    "selector": "[data-testid='workspace-card']",
                    "text": "Acme Workspace",
                }
            ],
        },
    }


def _write_fixture_project(project: Path, *, mode: str, port: int) -> None:
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@e.st")
    _git(project, "config", "user.name", "t")
    (project / ".gitignore").write_text("otto_logs/\n", encoding="utf-8")
    (project / "CHARTER.md").write_text(
        "# CHARTER\n\n"
        "| Surface | Port | Env |\n"
        "| --- | ---: | --- |\n"
        f"| Frontend dev server | {port} | `FE_PORT` |\n",
        encoding="utf-8",
    )
    (project / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"FE_PORT=\"${{FE_PORT:-{port}}}\"\n"
        "export FE_PORT\n"
        "python3 server.py\n",
        encoding="utf-8",
    )
    (project / "start.sh").chmod(0o755)
    (project / "server.py").write_text(
        f"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = {mode!r}
PORT = int(os.environ.get("FE_PORT", "{port}"))

WORKING_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <input data-testid="workspace-name" value="">
      <input data-testid="workspace-slug" value="">
      <button data-testid="create-workspace">Create workspace</button>
      <section id="workspace-list"></section>
    </main>
    <script>
      document.querySelector("[data-testid='create-workspace']").addEventListener("click", async () => {{
        const name = document.querySelector("[data-testid='workspace-name']").value;
        const slug = document.querySelector("[data-testid='workspace-slug']").value;
        const res = await fetch("/api/workspaces", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{name, slug}})
        }});
        if (res.ok) {{
          const data = await res.json();
          const card = document.createElement("div");
          card.setAttribute("data-testid", "workspace-card");
          card.textContent = data.name;
          document.getElementById("workspace-list").appendChild(card);
        }}
      }});
    </script>
  </body>
</html>'''

DEAD_BUTTON_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <input data-testid="workspace-name" value="">
      <input data-testid="workspace-slug" value="">
      <button data-testid="create-workspace">Create workspace</button>
      <section id="workspace-list"></section>
    </main>
  </body>
</html>'''

SKELETON_HTML = '''<!doctype html>
<html>
  <body>
    <main aria-busy="true">
      <div data-testid="loading-skeleton">Loading workspaces...</div>
    </main>
  </body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/workspaces":
            html = {{
                "working": WORKING_HTML,
                "dead_button": DEAD_BUTTON_HTML,
                "skeleton": SKELETON_HTML,
            }}[MODE]
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/workspaces":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{{}}"
            payload = json.loads(raw.decode("utf-8"))
            body = json.dumps({{"name": payload.get("name"), "slug": payload.get("slug")}}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "fixture")


def _run_ui_probe(project: Path, artifact_dir: Path) -> tuple[bool, dict[str, Any]]:
    result = verify_from_clean_oracle(
        project,
        scope="subtree",
        timeout_s=10,
        port_wait_s=2,
        journey_scope="root_integration",
        behavior_journeys=[_journey()],
        journey_artifact_dir=artifact_dir,
    )
    step = next(step for step in result.steps if step.id == "ui_journeys")
    verdict_path = next(
        Path(path)
        for path in step.artifact_paths
        if Path(path).name == "journey-verdicts.json"
    )
    return result.passed, json.loads(verdict_path.read_text(encoding="utf-8"))


def test_ui_probe_fails_persistent_skeleton_without_claiming_http_failure(tmp_path: Path) -> None:
    project = tmp_path / "skeleton"
    _write_fixture_project(project, mode="skeleton", port=_free_port())

    passed, verdict_payload = _run_ui_probe(project, tmp_path / "journey-artifacts")

    assert passed is False
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["id"] == "new_user_onboard"
    assert verdict["passed"] is False
    assert verdict["source"] == "ui_executor"
    assert "stuck/blank" in verdict["detail"]
    assert "HTTP 200" not in verdict["detail"]
    assert _git(project, "diff", "--exit-code").returncode == 0


def test_ui_probe_passes_working_onboarding_ui(tmp_path: Path) -> None:
    project = tmp_path / "working"
    _write_fixture_project(project, mode="working", port=_free_port())

    passed, verdict_payload = _run_ui_probe(project, tmp_path / "journey-artifacts")

    assert passed is True
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["passed"] is True
    assert verdict["source"] == "ui_executor"
    assert _git(project, "diff", "--exit-code").returncode == 0


def test_ui_probe_fails_dead_button_with_no_network_or_dom_effect(tmp_path: Path) -> None:
    project = tmp_path / "dead-button"
    _write_fixture_project(project, mode="dead_button", port=_free_port())

    passed, verdict_payload = _run_ui_probe(project, tmp_path / "journey-artifacts")

    assert passed is False
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["passed"] is False
    assert verdict["source"] == "ui_executor"
    assert "no observed network/DOM effect" in verdict["detail"]
    assert _git(project, "diff", "--exit-code").returncode == 0
