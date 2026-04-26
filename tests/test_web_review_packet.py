from __future__ import annotations

import json
from pathlib import Path

from otto import paths

from tests._web_mc_helpers import _client, _init_repo, _write_run


def test_web_review_packet_includes_story_details_and_html_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_run(repo)
    certify_dir = paths.certify_dir(repo, "build-web")
    certify_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = certify_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "homepage.png").write_bytes(b"fake-png")
    (certify_dir / "proof-of-work.html").write_text(
        '<html><body>Proof report <img src="evidence/homepage.png"><a href="../build/narrative.log">log</a></body></html>',
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
    assert packet["certification"]["proof_report"]["html_url"] == "/api/runs/build-web/proof-report"
    handoff = packet["product_handoff"]
    assert handoff["task_summary"] == "build the web surface"
    assert [flow["title"] for flow in handoff["task_flows"][:2]] == [
        "Users can save a filtered dashboard view.",
        "Users can restore a saved dashboard view.",
    ]
    report = client.get("/api/runs/build-web/proof-report")
    assert report.status_code == 200
    assert "Proof report" in report.text
    assert "/api/runs/build-web/proof-assets/evidence%2Fhomepage.png" in report.text
    assert "/api/runs/build-web/proof-assets/..%2Fbuild%2Fnarrative.log" in report.text
    assert client.get("/api/runs/build-web/proof-assets/evidence%2Fhomepage.png").content == b"fake-png"
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
    assert {"label": "Start server", "command": "flask --app expense_portal run --port 5000"} in handoff["launch"]
    assert {"label": "Reset demo data", "command": "flask --app expense_portal init-db"} in handoff["reset"]
    assert handoff["task_summary"] == "build the web surface"
    assert handoff["task_flows"][0]["title"] == "Try this task: build the web surface"
    assert any("Maya Chen" in item["value"] for item in handoff["sample_data"])
    assert handoff["try_flows"][0]["title"] == "Open the app"
