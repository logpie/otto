from __future__ import annotations

import json
import subprocess
from pathlib import Path

from otto import paths
from otto.queue.schema import write_state as write_queue_state

from tests._web_mc_helpers import (
    _append_queue_task,
    _client,
    _create_branch_file,
    _init_repo,
    _set_origin_head,
    _write_run,
)


def test_web_review_packet_includes_story_details_and_html_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)
    certify_dir = paths.certify_dir(repo, "build-web")
    certify_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = certify_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "homepage.png").write_bytes(b"fake-png")
    (evidence_dir / "recording.webm").write_bytes(b"fake-video")
    (certify_dir / "proof-of-work.html").write_text(
        '<html><body>Proof report <img src="evidence/homepage.png"><video src="evidence/recording.webm"></video><a href="../build/narrative.log">log</a></body></html>',
        encoding="utf-8",
    )
    (certify_dir / "proof-of-work.json").write_text(
        json.dumps(
            {
                "stories_tested": 2,
                "stories_passed": 1,
                "stories": [
                    {
                        "story_id": "save-filter",
                        "status": "pass",
                        "claim": "Users can save a filtered dashboard view.",
                        "observed_result": "Saved view appeared in the view switcher.",
                        "methodology": "live-ui-events",
                    },
                    {
                        "story_id": "restore-filter",
                        "status": "fail",
                        "claim": "Users can restore a saved dashboard view.",
                        "failure_evidence": "Restore did not apply the owner filter.",
                        "methodology": "live-ui-events",
                    },
                ],
                "demo_evidence": {
                    "schema_version": 1,
                    "app_kind": "web",
                    "demo_required": True,
                    "demo_status": "strong",
                    "demo_reason": "Story-specific browser proof was recorded.",
                    "primary_demo": {
                        "name": "recording.webm",
                        "kind": "video",
                        "href": "evidence/recording.webm",
                        "caption": "task walkthrough",
                    },
                    "stories": [
                        {
                            "id": "save-filter",
                            "title": "Users can save a filtered dashboard view.",
                            "status": "pass",
                            "needs_visual": True,
                            "needs_file_validation": False,
                            "has_text_evidence": True,
                            "has_file_validation": False,
                            "proof_level": "story video",
                            "visual_items": [],
                        }
                    ],
                    "counts": {"story_videos": 1, "raw_artifacts": 3},
                },
            }
        ),
        encoding="utf-8",
    )

    client = _client(repo)
    packet = client.get("/api/runs/build-web").json()["review_packet"]

    assert packet["certification"]["stories_tested"] == 2
    assert packet["certification"]["stories_passed"] == 1
    assert packet["certification"]["stories"][0]["id"] == "save-filter"
    assert packet["certification"]["stories"][1]["status"] == "fail"
    assert packet["certification"]["demo_evidence"]["demo_status"] == "strong"
    assert packet["certification"]["demo_evidence"]["primary_demo"]["href"] == "evidence/recording.webm"
    assert packet["certification"]["proof_report"]["html_url"] == "/api/runs/build-web/proof-report"
    handoff = packet["product_handoff"]
    assert handoff["task_summary"] == "build the web surface"
    assert handoff["preview_available"] is False
    assert handoff["preview_label"] == "Preview product"
    assert "No product URL" in handoff["preview_reason"]
    assert [flow["title"] for flow in handoff["task_flows"][:2]] == [
        "Users can save a filtered dashboard view.",
        "Users can restore a saved dashboard view.",
    ]
    report = client.get("/api/runs/build-web/proof-report")
    assert report.status_code == 200
    assert "Proof report" in report.text
    assert "/api/runs/build-web/proof-assets/evidence%2Fhomepage.png" in report.text
    assert "/api/runs/build-web/proof-assets/evidence%2Frecording.webm" in report.text
    assert "/api/runs/build-web/proof-assets/..%2Fbuild%2Fnarrative.log" in report.text
    assert client.get("/api/runs/build-web/proof-assets/evidence%2Fhomepage.png").content == b"fake-png"
    assert client.get("/api/runs/build-web/proof-assets/evidence%2Frecording.webm").content == b"fake-video"
    assert "STORY_RESULT: web PASS" in client.get("/api/runs/build-web/proof-assets/..%2Fbuild%2Fnarrative.log").text
    assert client.get("/api/runs/build-web/evidence/homepage.png").content == b"fake-png"


def test_web_review_packet_includes_explicit_product_handoff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff_dir = repo / ".otto"
    handoff_dir.mkdir()
    (handoff_dir / "product-handoff.json").write_text(
        json.dumps(
            {
                "kind": "cli",
                "summary": "Try the expense importer CLI.",
                "urls": "http://127.0.0.1:9001",
                "launch": [{"label": "Show help", "command": "expense-import --help"}],
                "try_flows": [{"title": "Import CSV", "steps": "Run sample import"}],
                "sample_data": [{"label": "Fixture", "value": "examples/expenses.csv"}],
                "reset": [{"label": "Clear output", "command": "rm -f out.json"}],
                "notes": "Use the fixture before trying a custom file.",
            }
        ),
        encoding="utf-8",
    )
    _write_run(repo)

    packet = _client(repo).get("/api/runs/build-web").json()["review_packet"]
    handoff = packet["product_handoff"]

    assert handoff["kind"] == "cli"
    assert handoff["label"] == "CLI tool"
    assert handoff["summary"] == "Try the expense importer CLI."
    assert handoff["preview_available"] is True
    assert handoff["preview_label"] == "Run product"
    assert handoff["launch"] == [{"label": "Show help", "command": "expense-import --help"}]
    assert handoff["task_summary"] == "build the web surface"
    assert handoff["task_flows"][0]["title"].startswith("Try this task:")
    assert handoff["try_flows"][0]["title"] == "Import CSV"
    assert handoff["try_flows"][0]["steps"] == ["Run sample import"]
    assert handoff["urls"] == ["http://127.0.0.1:9001"]
    assert handoff["notes"] == ["Use the fixture before trying a custom file."]
    assert handoff["sample_data"][0]["value"] == "examples/expenses.csv"
    assert handoff["reset"][0]["command"] == "rm -f out.json"


def test_web_review_packet_detects_product_handoff_from_readme(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text(
        "\n".join(
            [
                "# Expense Portal",
                "",
                "A browser dashboard for reviewing employee expenses.",
                "",
                "## Quick Start",
                "flask --app expense_portal run --port 5000",
                "flask --app expense_portal init-db",
                "",
                "Seed users include Maya Chen manager and Alex Kim employee.",
            ]
        ),
        encoding="utf-8",
    )
    (repo / "expense_portal").mkdir()
    _write_run(repo)

    packet = _client(repo).get("/api/runs/build-web").json()["review_packet"]
    handoff = packet["product_handoff"]

    assert handoff["kind"] == "web"
    assert handoff["label"] == "Web app"
    assert handoff["summary"] == "Expense Portal"
    assert handoff["preview_available"] is True
    assert handoff["preview_label"] == "Preview product"
    assert {"label": "Start server", "command": "flask --app expense_portal run --port 5000"} in handoff["launch"]
    assert {"label": "Reset demo data", "command": "flask --app expense_portal init-db"} in handoff["reset"]
    assert handoff["task_summary"] == "build the web surface"
    assert handoff["task_flows"][0]["title"] == "Try this task: build the web surface"
    assert any("Maya Chen" in item["value"] for item in handoff["sample_data"])
    assert handoff["try_flows"][0]["title"] == "Open the app"


def test_web_review_packet_hides_preview_for_test_only_smoke_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text(
        "\n".join(
            [
                "# Expense Portal",
                "",
                "http://127.0.0.1:5000",
                "",
                "## Quick Start",
                "flask --app expense_portal run",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "document launch"], cwd=repo, check=True)
    _set_origin_head(repo, "main")
    (repo / "tests").mkdir()
    _create_branch_file(
        repo,
        "build/add-smoke-test",
        filename="tests/test_pdf_export.py",
        content="def test_pdf_export_smoke():\n    assert True\n",
    )
    _write_run(
        repo,
        run_id="smoke-test",
        branch="build/add-smoke-test",
        intent_summary="Add a PDF export smoke test. Keep app behavior unchanged. Include tests.",
        status="done",
    )

    packet = _client(repo).get("/api/runs/smoke-test").json()["review_packet"]
    handoff = packet["product_handoff"]

    assert handoff["task_changed_files"] == ["tests/test_pdf_export.py"]
    assert handoff["urls"] == ["http://127.0.0.1:5000"]
    assert handoff["preview_available"] is False
    assert "changed only tests" in handoff["preview_reason"]


def test_web_review_packet_treats_certification_only_run_as_proof_not_landing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_origin_head(repo, "main")
    subprocess.run(["git", "branch", "certify/pdf-export"], cwd=repo, check=True)
    certify_dir = paths.certify_dir(repo, "cert-only")
    certify_dir.mkdir(parents=True, exist_ok=True)
    (certify_dir / "proof-of-work.html").write_text("<html><body>cert proof</body></html>", encoding="utf-8")
    (certify_dir / "proof-of-work.json").write_text(
        json.dumps(
            {
                "outcome": "passed",
                "stories_tested": 1,
                "stories_passed": 1,
                "stories": [
                    {
                        "story_id": "pdf-export-ui-flow",
                        "status": "pass",
                        "claim": "PDF export can be certified from the browser UI.",
                        "observed_result": "Dashboard export generated a PDF.",
                        "methodology": "live-ui-events",
                        "surface": "DOM / screenshot",
                    }
                ],
                "demo_evidence": {
                    "schema_version": 1,
                    "app_kind": "web",
                    "demo_required": True,
                    "demo_status": "strong",
                    "demo_reason": "Proof maps the task stories to concrete evidence.",
                    "primary_demo": None,
                    "stories": [],
                    "counts": {"story_videos": 1},
                },
                "evidence_gate": {"schema_version": 1, "status": "pass", "blocks_pass": False},
            }
        ),
        encoding="utf-8",
    )
    missing_intent = repo / ".otto" / "live" / "runs" / "cert-only-intent.txt"
    missing_checkpoint = repo / ".otto" / "live" / "runs" / "cert-only-checkpoint.json"
    _write_run(
        repo,
        run_id="cert-only",
        branch="certify/pdf-export",
        intent_summary="Certify the existing PDF export feature.",
        status="done",
        domain="queue",
        run_type="queue",
        command="certify",
        source={"argv": ["certify", "the existing PDF export feature"]},
        intent_extra={"intent_path": str(missing_intent)},
        artifacts_extra={"checkpoint_path": str(missing_checkpoint)},
    )

    packet = _client(repo).get("/api/runs/cert-only").json()["review_packet"]
    checks = {check["key"]: check for check in packet["checks"]}

    assert packet["headline"] == "Certification complete"
    assert packet["readiness"]["state"] == "reviewed"
    assert packet["next_action"]["action_key"] is None
    assert "do not land code" in packet["next_action"]["reason"]
    assert packet["changes"]["file_count"] == 0
    assert checks["changes"]["status"] == "pass"
    assert checks["changes"]["detail"] == "No code changes expected for a certification-only run."
    assert checks["evidence"]["status"] == "pass"
    assert checks["landing"]["label"] == "Merge action"
    assert checks["landing"]["status"] == "pass"
    assert "No merge action" in checks["landing"]["detail"]


def test_landing_status_marks_certification_only_queue_task_reviewed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_origin_head(repo, "main")
    _append_queue_task(
        repo,
        "certify-pdf-export",
        command_argv=["certify", "the existing PDF export feature"],
        branch="certify/pdf-export",
        resolved_intent="Certify the existing PDF export feature.",
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "certify-pdf-export": {
                    "status": "done",
                    "attempt_run_id": "cert-only",
                    "duration_s": 180,
                    "stories_passed": 5,
                    "stories_tested": 5,
                }
            },
        },
    )

    landing = _client(repo).get("/api/state").json()["landing"]
    item = landing["items"][0]

    assert landing["counts"]["ready"] == 0
    assert landing["counts"]["reviewed"] == 1
    assert landing["counts"]["blocked"] == 0
    assert item["landing_state"] == "reviewed"
    assert item["label"] == "Certified"
    assert item["changed_file_count"] == 0
    assert item["diff_error"] is None
