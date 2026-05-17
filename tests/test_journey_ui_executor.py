from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto import journey_contracts, journey_ui_executor
from otto.journey_ui_executor import _git_diff_dirty
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


def _journey(
    *,
    accessible_only: bool = False,
    expect_second_network: bool = False,
    scoped_observable: bool = False,
) -> dict[str, Any]:
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
        "text": "Acme Workspace",
        "ui_effect": "Acme Workspace appears in the workspace list.",
    }
    if not accessible_only:
        observable["selector"] = "[data-testid='workspace-card']"
    if scoped_observable:
        observable["container_selector"] = "#workspace-list"
    action: dict[str, Any] = {
        "id": "workspaces.create",
        "state_changing": True,
        "covers_primary_actions": ["workspaces.create"],
        "network_expectations": [
            {"method": "POST", "path": "/api/workspaces", "status": 201}
        ],
        "success_observables": [observable],
    }
    final_assertion = {
        "kind": "persisted_data_visible",
        "primary_action_id": "workspaces.create",
        "description": (
            "The created workspace card Acme Workspace remains "
            "visible in the UI."
        ),
        "text": "Acme Workspace",
    }
    if accessible_only:
        action.update({
            "role": "button",
            "name": "Create workspace",
            "inputs": [
                {"label": "Workspace name", "value": "Acme Workspace"},
                {"label": "Workspace slug", "value": "acme-workspace"},
            ],
        })
    else:
        action.update({
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
        })
        final_assertion["selector"] = "[data-testid='workspace-card']"
    if scoped_observable:
        final_assertion["container_selector"] = "#workspace-list"
    if expect_second_network:
        action["network_expectations"].append(
            {"method": "POST", "path": "/api/audit-log", "status": 201}
        )
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
                    **action,
                },
            ],
            "success_observables": [observable],
            "ready_policy": {
                "route": "/workspaces",
                "wait_for": "interactive",
                "timeout_ms": 3000,
            },
            "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 3000},
            "network_expectations": [],
            "final_dom_assertions": [final_assertion],
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
      <label>Workspace name <input data-testid="workspace-name" value=""></label>
      <label>Workspace slug <input data-testid="workspace-slug" value=""></label>
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

PREEXISTING_NETWORK_ONLY_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <p>Try the Acme Workspace starter template.</p>
      <label>Workspace name <input aria-label="Workspace name" value=""></label>
      <label>Workspace slug <input aria-label="Workspace slug" value=""></label>
      <button>Create workspace</button>
      <section id="workspace-list"><div>Acme Workspace</div></section>
    </main>
    <script>
      document.querySelector("button").addEventListener("click", async () => {{
        await fetch("/api/workspaces", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{name: "Acme Workspace", slug: "acme-workspace"}})
        }});
      }});
    </script>
  </body>
</html>'''

PREEXISTING_ACCESSIBLE_OBSERVABLE_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <label>Workspace name <input aria-label="Workspace name" value=""></label>
      <label>Workspace slug <input aria-label="Workspace slug" value=""></label>
      <button>Create workspace</button>
      <section id="workspace-list">
        <div role="status" aria-label="Saved">Saved</div>
        <div>Saved</div>
        <label>Saved <input value=""></label>
      </section>
    </main>
    <script>
      document.querySelector("button").addEventListener("click", async () => {{
        await fetch("/api/workspaces", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{name: "Acme Workspace", slug: "acme-workspace"}})
        }});
      }});
    </script>
  </body>
</html>'''

NEW_ACCESSIBLE_OBSERVABLE_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <label>Workspace name <input aria-label="Workspace name" value=""></label>
      <label>Workspace slug <input aria-label="Workspace slug" value=""></label>
      <button>Create workspace</button>
      <section id="workspace-list">
        <div role="status" aria-label="Ready">Ready</div>
      </section>
    </main>
    <script>
      document.querySelector("button").addEventListener("click", async () => {{
        await fetch("/api/workspaces", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{name: "Acme Workspace", slug: "acme-workspace"}})
        }});
        const list = document.getElementById("workspace-list");
        const status = document.createElement("div");
        status.setAttribute("role", "status");
        status.setAttribute("aria-label", "Saved");
        status.textContent = "Saved";
        const named = document.createElement("div");
        named.textContent = "Saved";
        const label = document.createElement("label");
        label.textContent = "Saved ";
        label.appendChild(document.createElement("input"));
        list.append(status, named, label);
      }});
    </script>
  </body>
</html>'''

FUZZY_INCIDENTAL_COPY_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <label>Workspace name <input aria-label="Workspace name" value=""></label>
      <label>Workspace slug <input aria-label="Workspace slug" value=""></label>
      <button>Create workspace</button>
      <section id="workspace-list"></section>
    </main>
    <script>
      document.querySelector("button").addEventListener("click", async () => {{
        await fetch("/api/workspaces", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{name: "Acme Workspace", slug: "acme-workspace"}})
        }});
        const card = document.createElement("div");
        card.textContent = "Acme Workspace template";
        document.getElementById("workspace-list").appendChild(card);
      }});
    </script>
  </body>
</html>'''

DEAD_BUTTON_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <label>Workspace name <input data-testid="workspace-name" value=""></label>
      <label>Workspace slug <input data-testid="workspace-slug" value=""></label>
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
                "preexisting_network_only": PREEXISTING_NETWORK_ONLY_HTML,
                "preexisting_accessible_observable": PREEXISTING_ACCESSIBLE_OBSERVABLE_HTML,
                "new_accessible_observable": NEW_ACCESSIBLE_OBSERVABLE_HTML,
                "fuzzy_incidental_copy": FUZZY_INCIDENTAL_COPY_HTML,
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


def _run_ui_probe(
    project: Path,
    artifact_dir: Path,
    *,
    journey: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    result = verify_from_clean_oracle(
        project,
        scope="subtree",
        timeout_s=10,
        port_wait_s=2,
        journey_scope="root_integration",
        behavior_journeys=[journey or _journey()],
        journey_artifact_dir=artifact_dir,
    )
    step = next(step for step in result.steps if step.id == "ui_journeys")
    verdict_path = next(
        Path(path)
        for path in step.artifact_paths
        if Path(path).name == "journey-verdicts.json"
    )
    return result.passed, json.loads(verdict_path.read_text(encoding="utf-8"))


def _accessible_observable_journey(locator: dict[str, str]) -> dict[str, Any]:
    journey = _journey(accessible_only=True, scoped_observable=True)
    action_observable = {
        "kind": "network_and_ui_effect",
        "primary_action_id": "workspaces.create",
        "description": "Saving shows an accessible confirmation in the workspace list.",
        "method": "POST",
        "path": "/api/workspaces",
        "status": 201,
        "ui_effect": "Saved appears in the workspace list.",
        "container_selector": "#workspace-list",
        **locator,
    }
    final_assertion = {
        "kind": "persisted_data_visible",
        "primary_action_id": "workspaces.create",
        "description": "The accessible confirmation remains visible in the workspace list.",
        "container_selector": "#workspace-list",
        **locator,
    }
    action = journey["pass_model"]["actions"][0]
    action["success_observables"] = [action_observable]
    journey["pass_model"]["success_observables"] = [final_assertion]
    journey["pass_model"]["final_dom_assertions"] = [final_assertion]
    return journey


def test_ui_validator_and_executor_observable_locator_sets_agree() -> None:
    assert (
        journey_contracts.UI_ASSERTION_KEYS
        == journey_ui_executor.ENFORCED_UI_OBSERVABLE_LOCATOR_KEYS
    )


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
    assert verdict_payload["source"] == "journey_verdict_sink"
    assert verdict_payload["executor_results"][0]["source"] == "ui_executor"
    assert _git(project, "diff", "--exit-code").returncode == 0


def test_accessible_only_ui_pass_model_passes_working_ui_and_fails_dead_button(tmp_path: Path) -> None:
    journey = _journey(accessible_only=True)
    assert "selector" not in json.dumps(journey)
    working = tmp_path / "accessible-working"
    dead = tmp_path / "accessible-dead"
    _write_fixture_project(working, mode="working", port=_free_port())
    _write_fixture_project(dead, mode="dead_button", port=_free_port())

    working_passed, working_payload = _run_ui_probe(
        working,
        tmp_path / "journey-artifacts-working",
        journey=journey,
    )
    dead_passed, dead_payload = _run_ui_probe(
        dead,
        tmp_path / "journey-artifacts-dead",
        journey=journey,
    )

    assert working_passed is True
    assert working_payload["journey_verdicts"][0]["passed"] is True
    assert dead_passed is False
    assert dead_payload["journey_verdicts"][0]["passed"] is False
    assert "no observed network/DOM effect" in dead_payload["journey_verdicts"][0]["detail"]


def test_accessible_ui_probe_requires_new_scoped_observable_after_action(tmp_path: Path) -> None:
    project = tmp_path / "preexisting-network-only"
    _write_fixture_project(project, mode="preexisting_network_only", port=_free_port())

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=_journey(accessible_only=True, scoped_observable=True),
    )

    assert passed is False
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["passed"] is False
    assert "no new scoped observable" in verdict["detail"]


@pytest.mark.parametrize(
    "locator",
    [
        {"role": "status"},
        {"name": "Saved"},
        {"label": "Saved"},
    ],
)
def test_accessible_locator_only_action_observable_requires_delta(
    tmp_path: Path,
    locator: dict[str, str],
) -> None:
    project = tmp_path / "preexisting-accessible-observable"
    _write_fixture_project(project, mode="preexisting_accessible_observable", port=_free_port())

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=_accessible_observable_journey(locator),
    )

    assert passed is False
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["passed"] is False
    assert "no new scoped observable" in verdict["detail"]


@pytest.mark.parametrize(
    "locator",
    [
        {"role": "status", "name": "Saved"},
        {"accessible_name": "Saved"},
        {"label": "Saved"},
    ],
)
def test_accessible_locator_only_action_observable_passes_new_scoped_element(
    tmp_path: Path,
    locator: dict[str, str],
) -> None:
    project = tmp_path / "new-accessible-observable"
    _write_fixture_project(project, mode="new_accessible_observable", port=_free_port())

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=_accessible_observable_journey(locator),
    )

    assert passed is True
    assert verdict_payload["journey_verdicts"][0]["passed"] is True


def test_final_accessible_dom_assertion_is_presence_not_delta(tmp_path: Path) -> None:
    project = tmp_path / "final-presence-accessible-observable"
    _write_fixture_project(project, mode="new_accessible_observable", port=_free_port())
    journey = _accessible_observable_journey({"role": "status", "name": "Saved"})
    journey["pass_model"]["final_dom_assertions"] = [
        {
            "kind": "persisted_data_visible",
            "primary_action_id": "workspaces.create",
            "description": "The pre-existing ready status remains visible.",
            "container_selector": "#workspace-list",
            "role": "status",
            "name": "Ready",
        }
    ]

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=journey,
    )

    assert passed is True
    assert verdict_payload["journey_verdicts"][0]["passed"] is True


def test_accessible_ui_probe_uses_exact_text_not_fuzzy_incidental_copy(tmp_path: Path) -> None:
    project = tmp_path / "fuzzy-incidental-copy"
    _write_fixture_project(project, mode="fuzzy_incidental_copy", port=_free_port())

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=_journey(accessible_only=True, scoped_observable=True),
    )

    assert passed is False
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["passed"] is False
    assert "not visible inside #workspace-list" in verdict["detail"]


def test_accessible_ui_probe_passes_genuine_new_scoped_element(tmp_path: Path) -> None:
    project = tmp_path / "scoped-working"
    _write_fixture_project(project, mode="working", port=_free_port())

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=_journey(accessible_only=True, scoped_observable=True),
    )

    assert passed is True
    assert verdict_payload["journey_verdicts"][0]["passed"] is True


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


def test_ui_probe_requires_every_expected_network_response(tmp_path: Path) -> None:
    project = tmp_path / "missing-second-network"
    _write_fixture_project(project, mode="working", port=_free_port())

    passed, verdict_payload = _run_ui_probe(
        project,
        tmp_path / "journey-artifacts",
        journey=_journey(accessible_only=True, expect_second_network=True),
    )

    assert passed is False
    verdict = verdict_payload["journey_verdicts"][0]
    assert verdict["passed"] is False
    assert "POST /api/audit-log status=201" in verdict["detail"]


def test_ui_probe_dirty_check_detects_untracked_files(tmp_path: Path) -> None:
    project = tmp_path / "dirty"
    _write_fixture_project(project, mode="working", port=_free_port())
    (project / "generated.txt").write_text("untracked\n", encoding="utf-8")

    dirty = _git_diff_dirty(project)

    assert "?? generated.txt" in dirty
