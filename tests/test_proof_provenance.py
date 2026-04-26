"""Proof / artifact / certification provenance contract tests.

Cluster ``codex-evidence-trustworthiness`` items #3, #4, #6, #7, #8.

Coverage:

  - ``/api/runs/{id}`` (review_packet.certification.proof_report) carries
    ``generated_at``, ``run_id``, ``session_id``, ``branch``, ``head_sha``,
    ``file_mtime``, ``sha256``, and a ``run_id_matches`` flag that warns
    when the proof file's recorded run differs from the run record.
  - ``review_packet.certification.rounds`` mirrors ``round_history`` from
    ``proof-of-work.json`` so multi-round certs are visible.
  - ``/api/runs/{id}/artifacts`` includes ``size_bytes``, ``mtime``, and
    ``sha256`` per artifact.
  - ``/api/runs/{id}/artifacts/{i}/content`` returns ``previewable``,
    ``mime_type``, ``size_bytes`` and skips text decoding for binary
    payloads (PNG, WEBM, PDF, NUL-byte files).
  - The certifier writes a sibling ``<artifact>.manifest.json`` next to
    each visual artifact at proof-of-work generation time.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \
        uv run pytest tests/test_proof_provenance.py -v
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from otto import paths
from otto.runs.registry import make_run_record, write_record
from otto.web.app import create_app


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "prov@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Prov Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# prov\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _write_atomic_run(repo: Path, *, run_id: str = "run-prov") -> Path:
    """Seed a minimal atomic-build run record + summary file. Returns session dir."""

    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("BUILD\nSTORY_RESULT: x PASS\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build prov",
        status="done",
        cwd=repo,
        source={"argv": ["build", "x"]},
        git={"branch": "main"},
        intent={"summary": "prov"},
        artifacts={
            "summary_path": str(summary_path),
            "primary_log_path": str(primary_log),
        },
        adapter_key="atomic.build",
    )
    write_record(repo, record)
    return paths.session_dir(repo, run_id)


def _write_proof_of_work(
    repo: Path,
    run_id: str,
    *,
    proof_run_id: str | None = None,
    rounds: list[dict] | None = None,
) -> Path:
    """Write a proof-of-work.json + .html into the run's certify dir."""

    certify_dir = paths.certify_dir(repo, run_id)
    certify_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": "2026-04-25T12:34:56Z",
        "outcome": "passed",
        "run_context": {
            "run_id": proof_run_id or run_id,
            "session_id": "sess-abc",
            "git_branch": "audit/prov",
            "git_commit_sha": "deadbee",
        },
        "round_history": rounds
        if rounds is not None
        else [
            {
                "round": 1,
                "verdict": "failed",
                "stories_tested": 2,
                "passed_count": 1,
                "failed_count": 1,
                "warn_count": 0,
                "failing_story_ids": ["restore-filter"],
                "warn_story_ids": [],
                "diagnosis": "Restore did not apply filter.",
                "duration_s": 12.0,
                "duration_human": "12s",
                "cost_usd": 0.05,
                "cost_estimated": False,
                "fix_commits": ["abc1234"],
                "fix_diff_stat": "1 file changed",
                "still_failing_after_fix": [],
                "subagent_errors": [],
            },
            {
                "round": 2,
                "verdict": "passed",
                "stories_tested": 2,
                "passed_count": 2,
                "failed_count": 0,
                "warn_count": 0,
                "failing_story_ids": [],
                "warn_story_ids": [],
                "diagnosis": "All stories passed after fix.",
                "duration_s": 10.0,
                "duration_human": "10s",
                "cost_usd": 0.04,
                "cost_estimated": False,
                "fix_commits": [],
                "fix_diff_stat": "",
                "still_failing_after_fix": [],
                "subagent_errors": [],
            },
        ],
        "stories": [
            {
                "story_id": "save-filter",
                "status": "pass",
                "claim": "Save filter",
                "methodology": "live-ui-events",
            },
            {
                "story_id": "restore-filter",
                "status": "pass",
                "claim": "Restore filter",
                "methodology": "live-ui-events",
            },
        ],
    }
    json_path = certify_dir / "proof-of-work.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    (certify_dir / "proof-of-work.html").write_text(
        "<html><body>Proof report</body></html>", encoding="utf-8"
    )
    return json_path


# --------------------------------------------------------------------------- #
# #3 — proof provenance fields
# --------------------------------------------------------------------------- #


def test_proof_response_includes_provenance_fields(tmp_path: Path) -> None:
    """Detail's certification.proof_report exposes generated_at + ids + hash."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_atomic_run(repo, run_id="run-prov")
    json_path = _write_proof_of_work(repo, "run-prov")

    client = TestClient(create_app(repo))
    detail = client.get("/api/runs/run-prov").json()
    proof = detail["review_packet"]["certification"]["proof_report"]

    assert proof["available"] is True
    assert proof["generated_at"] == "2026-04-25T12:34:56Z"
    assert proof["run_id"] == "run-prov"
    assert proof["session_id"] == "sess-abc"
    assert proof["branch"] == "audit/prov"
    assert proof["head_sha"] == "deadbee"
    # File mtime is a UTC ISO timestamp.
    assert proof["file_mtime"] is not None
    assert ISO_RE.match(proof["file_mtime"]), proof["file_mtime"]
    # SHA-256 is the hex digest of the JSON file contents.
    expected_sha = hashlib.sha256(json_path.read_bytes()).hexdigest()
    assert proof["sha256"] == expected_sha
    # The recorded run id matches the run record.
    assert proof["run_id_matches"] is True


def test_proof_provenance_warns_on_mismatch(tmp_path: Path) -> None:
    """A proof file whose recorded run_id differs from the record sets the flag."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_atomic_run(repo, run_id="run-prov")
    _write_proof_of_work(repo, "run-prov", proof_run_id="some-other-run")

    client = TestClient(create_app(repo))
    detail = client.get("/api/runs/run-prov").json()
    proof = detail["review_packet"]["certification"]["proof_report"]

    assert proof["run_id"] == "some-other-run"
    assert proof["run_id_matches"] is False


# --------------------------------------------------------------------------- #
# #4 — round_history threaded into review packet
# --------------------------------------------------------------------------- #


def test_certification_includes_round_history(tmp_path: Path) -> None:
    """``review_packet.certification.rounds`` mirrors ``round_history``."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_atomic_run(repo, run_id="run-rounds")
    _write_proof_of_work(repo, "run-rounds")

    client = TestClient(create_app(repo))
    detail = client.get("/api/runs/run-rounds").json()
    rounds = detail["review_packet"]["certification"]["rounds"]

    assert isinstance(rounds, list)
    assert len(rounds) == 2

    first = rounds[0]
    assert first["round"] == 1
    assert first["verdict"] == "failed"
    assert first["stories_tested"] == 2
    assert first["failed_count"] == 1
    assert first["failing_story_ids"] == ["restore-filter"]
    assert first["fix_commits"] == ["abc1234"]
    assert first["diagnosis"] == "Restore did not apply filter."

    second = rounds[1]
    assert second["round"] == 2
    assert second["verdict"] == "passed"
    assert second["passed_count"] == 2
    assert second["fix_commits"] == []


def test_certification_rounds_default_when_missing(tmp_path: Path) -> None:
    """Runs without a proof_of_work.json simply have an empty rounds list."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_atomic_run(repo, run_id="run-norounds")

    client = TestClient(create_app(repo))
    rounds = client.get("/api/runs/run-norounds").json()["review_packet"]["certification"]["rounds"]
    assert rounds == []


# --------------------------------------------------------------------------- #
# #7 — artifact list provenance fields
# --------------------------------------------------------------------------- #


def test_artifact_list_includes_size_mtime_sha(tmp_path: Path) -> None:
    """``/api/runs/{id}/artifacts`` exposes size_bytes / mtime / sha256."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_atomic_run(repo, run_id="run-art")

    client = TestClient(create_app(repo))
    artifacts = client.get("/api/runs/run-art/artifacts").json()["artifacts"]

    summary = next(item for item in artifacts if item["label"] == "summary")
    assert summary["size_bytes"] is not None and summary["size_bytes"] > 0
    assert summary["mtime"] is not None
    assert ISO_RE.match(summary["mtime"]), summary["mtime"]
    assert summary["sha256"] is not None
    assert SHA256_RE.match(summary["sha256"]), summary["sha256"]
    expected = hashlib.sha256(
        Path(summary["path"]).read_bytes()
    ).hexdigest()
    assert summary["sha256"] == expected


# --------------------------------------------------------------------------- #
# #6 — binary artifact preview metadata
# --------------------------------------------------------------------------- #


def _seed_binary_artifact_run(repo: Path) -> Path:
    """Seed a run with a primary log + a PNG artifact under the session dir."""

    run_id = "run-bin"
    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("BUILD\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    # Drop a synthetic PNG (just the magic bytes are enough for sniffing).
    png_path = paths.certify_dir(repo, run_id) / "evidence" / "shot.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="binary",
        status="done",
        cwd=repo,
        source={"argv": ["build", "binary"]},
        git={"branch": "main"},
        intent={"summary": "binary"},
        artifacts={
            "summary_path": str(summary_path),
            "primary_log_path": str(primary_log),
            "extra_log_paths": [str(png_path)],
        },
        adapter_key="atomic.build",
    )
    write_record(repo, record)
    return png_path


def test_artifact_content_image_marks_not_previewable(tmp_path: Path) -> None:
    """PNG artifacts return ``previewable=False`` with image MIME, no garbage text."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_binary_artifact_run(repo)

    client = TestClient(create_app(repo))
    artifacts = client.get("/api/runs/run-bin/artifacts").json()["artifacts"]
    png = next(item for item in artifacts if item["path"].endswith(".png"))
    body = client.get(f"/api/runs/run-bin/artifacts/{png['index']}/content").json()

    assert body["previewable"] is False
    assert body["mime_type"] == "image/png"
    assert body["size_bytes"] > 0
    # Critically: no garbage text payload.
    assert body["content"] == ""
    assert body["truncated"] is False


def test_artifact_content_text_marks_previewable(tmp_path: Path) -> None:
    """Plain text artifacts still get a decoded body + previewable=True."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_atomic_run(repo, run_id="run-txt")
    client = TestClient(create_app(repo))
    artifacts = client.get("/api/runs/run-txt/artifacts").json()["artifacts"]
    summary = next(item for item in artifacts if item["label"] == "summary")
    body = client.get(f"/api/runs/run-txt/artifacts/{summary['index']}/content").json()

    assert body["previewable"] is True
    assert body["mime_type"] in {"application/json", "text/plain"}
    assert '"verdict"' in body["content"]


def test_artifact_raw_endpoint_serves_image_bytes(tmp_path: Path) -> None:
    """The ``/raw`` endpoint streams the file bytes with the detected MIME."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    png_path = _seed_binary_artifact_run(repo)

    client = TestClient(create_app(repo))
    artifacts = client.get("/api/runs/run-bin/artifacts").json()["artifacts"]
    png = next(item for item in artifacts if item["path"].endswith(".png"))
    response = client.get(f"/api/runs/run-bin/artifacts/{png['index']}/raw")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == png_path.read_bytes()


# --------------------------------------------------------------------------- #
# #8 — visual evidence manifests written at certifier time
# --------------------------------------------------------------------------- #


def test_visual_evidence_manifest_written_at_capture(tmp_path: Path) -> None:
    """``_build_pow_report_data`` writes ``<artifact>.manifest.json`` siblings."""

    from otto.certifier import _build_pow_report_data, _write_pow_report

    repo = tmp_path / "repo"
    _init_repo(repo)

    run_id = "run-visual"
    report_dir = paths.certify_dir(repo, run_id)
    evidence_dir = report_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    png = evidence_dir / "save-filter.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    webm = evidence_dir / "recording.webm"
    webm.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 32)

    class _Options:
        provider = "anthropic"
        model = "claude-sonnet"
        effort = "medium"

    pow_data = _build_pow_report_data(
        project_dir=repo,
        report_dir=report_dir,
        log_dir=report_dir,
        run_id=run_id,
        session_id="sess-visual",
        pipeline_mode="agentic_certifier",
        certifier_mode="standard",
        outcome="passed",
        story_results=[
            {
                "story_id": "save-filter",
                "status": "pass",
                "claim": "Save filter",
                "methodology": "live-ui-events",
                "failure_evidence": "save-filter.png",
            }
        ],
        diagnosis="ok",
        certify_rounds=[],
        duration_s=10.0,
        certifier_cost_usd=0.05,
        total_cost_usd=0.05,
        intent="visual evidence test",
        options=_Options(),
        evidence_dir=evidence_dir,
        stories_tested=1,
        stories_passed=1,
        coverage_observed=[],
        coverage_gaps=[],
        coverage_emitted=False,
    )
    _write_pow_report(report_dir, pow_data)

    png_manifest = evidence_dir / "save-filter.png.manifest.json"
    webm_manifest = evidence_dir / "recording.webm.manifest.json"
    assert png_manifest.exists(), list(evidence_dir.iterdir())
    assert webm_manifest.exists(), list(evidence_dir.iterdir())

    png_data = json.loads(png_manifest.read_text(encoding="utf-8"))
    assert png_data["run_id"] == run_id
    assert png_data["session_id"] == "sess-visual"
    assert png_data["round"] == 1
    assert png_data["story_id"] == "save-filter"
    assert png_data["sha256"] == hashlib.sha256(png.read_bytes()).hexdigest()
    assert png_data["kind"] == "screenshot"
    assert png_data["captured_at"] is not None

    webm_data = json.loads(webm_manifest.read_text(encoding="utf-8"))
    assert webm_data["kind"] == "recording"
    assert webm_data["run_id"] == run_id
    # Recording is unassigned to a story (no failure_evidence pointing at it).
    assert webm_data["story_id"] == ""
