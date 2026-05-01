from __future__ import annotations

import json
import subprocess
from pathlib import Path

from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
from otto import paths
from otto.queue.schema import write_state as write_queue_state
from otto.runs.registry import make_run_record, write_record

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


def test_web_review_packet_omits_placeholder_story_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)
    certify_dir = paths.certify_dir(repo, "build-web")
    certify_dir.mkdir(parents=True, exist_ok=True)
    (certify_dir / "proof-of-work.html").write_text("<html>proof</html>", encoding="utf-8")
    (certify_dir / "proof-of-work.json").write_text(
        json.dumps(
            {
                "stories_tested": 1,
                "stories_passed": 1,
                "stories": [
                    {
                        "story_id": "json-notifications",
                        "status": "pass",
                        "claim": "JSON API returns notifications",
                        "evidence": "none",
                        "methodology": "JSON API",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    story = _client(repo).get("/api/runs/build-web").json()["review_packet"]["certification"]["stories"][0]

    assert story["detail"] == ""
    assert story["evidence_excerpt"] == ""
    assert story["evidence_command"] == ""
    assert story["evidence_output"] == ""


def test_web_proof_assets_survive_cleaned_queue_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(
        repo,
        run_id="queue-cleaned",
        artifacts_extra={"session_dir": str(repo / ".worktrees" / "queue-task" / "otto_logs" / "sessions" / "queue-cleaned")},
    )
    certify_dir = paths.certify_dir(repo, "queue-cleaned")
    evidence_dir = certify_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "recording.webm").write_bytes(b"durable-video")
    (certify_dir / "proof-of-work.html").write_text(
        '<html><body><video src="evidence/recording.webm"></video></body></html>',
        encoding="utf-8",
    )
    (certify_dir / "proof-of-work.json").write_text(json.dumps({"run_context": {"run_id": "queue-cleaned"}}), encoding="utf-8")

    client = _client(repo)
    report = client.get("/api/runs/queue-cleaned/proof-report")

    assert report.status_code == 200
    assert "/api/runs/queue-cleaned/proof-assets/evidence%2Frecording.webm" in report.text
    asset = client.get("/api/runs/queue-cleaned/proof-assets/evidence%2Frecording.webm")
    assert asset.status_code == 200
    assert asset.content == b"durable-video"


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
    (repo / "expense_portal" / "app.py").write_text(
        "from flask import Flask\n\napp = Flask(__name__)\n\n@app.get('/')\ndef index():\n    return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "templates").mkdir()
    (repo / "templates" / "index.html").write_text("<h1>Expense Portal</h1>\n", encoding="utf-8")
    (repo / "static").mkdir()
    (repo / "static" / "app.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
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
    assert "Python" in handoff["tech_stack"]
    assert "Flask" in handoff["tech_stack"]
    assert "HTML templates" in handoff["tech_stack"]
    assert "CSS" in handoff["tech_stack"]
    assert handoff["code_stats"]["files"] >= 4
    assert handoff["code_stats"]["lines"] >= 4


def test_web_review_packet_detects_nested_flask_web_app(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text(
        "# Microfeed\n\nA local Twitter-like microblogging web app. Python + Flask + SQLite, server-rendered HTML.\n",
        encoding="utf-8",
    )
    (repo / "run.py").write_text("from microfeed import create_app\n\napp = create_app()\napp.run()\n", encoding="utf-8")
    package_dir = repo / "microfeed"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("from flask import Flask\n\ndef create_app():\n    return Flask(__name__)\n", encoding="utf-8")
    (package_dir / "templates").mkdir()
    (package_dir / "templates" / "home.html").write_text("<h1>Microfeed</h1>\n", encoding="utf-8")
    _write_run(repo)

    handoff = _client(repo).get("/api/runs/build-web").json()["review_packet"]["product_handoff"]

    assert handoff["kind"] == "web"
    assert handoff["label"] == "Web app"
    assert {"label": "Start server", "command": "uv run python run.py"} in handoff["launch"]
    assert "Flask" in handoff["tech_stack"]
    assert "HTML templates" in handoff["tech_stack"]


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


def test_web_merged_failed_queue_run_suppresses_stale_failure_and_uses_durable_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "build/merged-task"], cwd=repo, check=True)
    (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
    subprocess.run(["git", "add", "merged.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add merged task"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "--no-ff", "-m", "land merged task", "build/merged-task"], cwd=repo, check=True)
    merge_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    write_merge_state(
        repo,
        MergeState(
            merge_id="merge-merged",
            started_at="2026-04-24T00:00:00Z",
            finished_at="2026-04-24T00:01:00Z",
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=["build/merged-task"],
            outcomes=[BranchOutcome(branch="build/merged-task", status="merged", merge_commit=merge_commit)],
        ),
    )
    stale_session = repo / ".worktrees" / "merged-task" / "otto_logs" / "sessions" / "queue-failed"
    durable_session = paths.session_dir(repo, "queue-failed")
    certify_dir = paths.certify_dir(repo, "queue-failed")
    evidence_dir = certify_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (paths.build_dir(repo, "queue-failed") / "narrative.log").parent.mkdir(parents=True, exist_ok=True)
    (paths.build_dir(repo, "queue-failed") / "narrative.log").write_text("build log\n", encoding="utf-8")
    (certify_dir / "narrative.log").write_text("certifier log\n", encoding="utf-8")
    (certify_dir / "proof-of-work.html").write_text("<html>proof</html>", encoding="utf-8")
    (certify_dir / "proof-of-work.json").write_text(json.dumps({"run_context": {"run_id": "queue-failed"}}), encoding="utf-8")
    (evidence_dir / "story.png").write_bytes(b"png")
    record = make_run_record(
        project_dir=repo,
        run_id="queue-failed",
        domain="queue",
        run_type="queue",
        command="queue",
        display_name="merged task",
        status="failed",
        cwd=repo / ".worktrees" / "merged-task",
        identity={"queue_task_id": "merged-task"},
        source={"argv": ["otto", "queue", "run"]},
        git={"branch": "build/merged-task", "worktree": str(repo / ".worktrees" / "merged-task"), "target_branch": "main"},
        intent={"summary": "merged task"},
        artifacts={
            "session_dir": str(stale_session),
            "summary_path": str(stale_session / "summary.json"),
            "primary_log_path": str(stale_session / "build" / "narrative.log"),
        },
        adapter_key="queue.attempt",
        last_event="exit_code=1",
    )
    write_record(repo, record)

    detail = _client(repo).get("/api/runs/queue-failed").json()
    packet = detail["review_packet"]
    artifact_labels = [artifact["label"] for artifact in detail["artifacts"]]

    assert durable_session.exists()
    assert packet["readiness"]["state"] == "merged"
    assert packet["failure"] is None
    assert "primary log" in artifact_labels
    assert "proof report" in artifact_labels
    assert "story.png" in artifact_labels


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
