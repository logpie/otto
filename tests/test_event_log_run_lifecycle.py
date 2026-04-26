"""Server-side regression for W13-IMPORTANT-2 — Mission Control event log
must record run lifecycle events, not just watcher.started.

Source: live W13 dogfood — after a successful build-and-cert run finished
during a simulated backend outage, ``/api/events`` showed only two
``watcher.started`` rows. The build's start and successful completion
were invisible to the post-outage UI. The fix in
``otto/mission_control/service.py`` adds per-refresh lifecycle detection:
when a previously-unknown live record appears in a non-terminal status,
emit ``run.started``; when a previously non-terminal record transitions
to a terminal status, emit ``run.<terminal_outcome>`` (success →
``run.success``, failed → ``run.failed``, etc.). See
``docs/mc-audit/live-findings.md`` (search "W13-IMPORTANT-2").

Invariants pinned by this test:

1. Bootstrap pass does NOT emit a flurry of stale events. The first
   ``state()`` call after MC startup sees existing live records but
   does not emit ``run.started`` for them (we have no idea when they
   actually started — emitting now would lie).
2. A new run appearing after bootstrap emits exactly one
   ``run.started`` event with the run-id in payload.
3. A live run transitioning to ``status=success`` emits exactly one
   ``run.success`` event with severity ``success``.
4. A live run transitioning to ``status=failed`` emits exactly one
   ``run.failed`` event with severity ``error``.
5. A live run transitioning to ``status=cancelled`` emits exactly one
   ``run.cancelled`` event with severity ``warning``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from otto import paths
from otto.mission_control.service import MissionControlService
from otto.runs.registry import finalize_record, make_run_record, write_record


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "lifecycle@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Lifecycle Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _write_running_record(repo: Path, run_id: str, *, display_name: str = "build something") -> None:
    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("BUILD starting\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "running"}), encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name=display_name,
        status="running",
        cwd=repo,
        source={"argv": ["build", "x"], "provider": "codex", "model": "gpt-5.4"},
        git={"branch": "main", "worktree": None},
        intent={"summary": display_name},
        artifacts={
            "summary_path": str(summary_path),
            "primary_log_path": str(primary_log),
        },
        metrics={"cost_usd": 0.0},
        adapter_key="atomic.build",
        last_event="running",
    )
    write_record(repo, record)


def _events_kinds(service: MissionControlService) -> list[str]:
    items = service.events(limit=200).get("items") or []
    return [str(item.get("kind") or "") for item in items]


def test_bootstrap_does_not_emit_stale_run_started(tmp_path: Path) -> None:
    """First state() call after startup must NOT emit run.started for
    pre-existing live records — we don't know when they actually started."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_running_record(repo, "2026-04-26-100000-aaaaaa", display_name="pre-existing")

    service = MissionControlService(repo, queue_compat=False)

    state = service.state()
    assert state["live"]["total_count"] == 1, "live record should be visible"
    kinds = _events_kinds(service)
    assert "run.started" not in kinds, (
        f"bootstrap pass must not emit run.started — got {kinds!r}"
    )


def test_new_run_after_bootstrap_emits_run_started(tmp_path: Path) -> None:
    """A run appearing after the first refresh emits exactly one run.started."""

    repo = tmp_path / "repo"
    _init_repo(repo)

    service = MissionControlService(repo, queue_compat=False)
    # Bootstrap pass with no live records.
    service.state()
    assert "run.started" not in _events_kinds(service)

    # Now write a new running record.
    _write_running_record(repo, "2026-04-26-100100-bbbbbb", display_name="fresh build")
    service.state()

    items = service.events(limit=200).get("items") or []
    started = [item for item in items if item.get("kind") == "run.started"]
    assert len(started) == 1, f"expected exactly one run.started, got {started!r}"
    event = started[0]
    assert event.get("run_id") == "2026-04-26-100100-bbbbbb"
    details = event.get("details") or {}
    assert details.get("domain") == "atomic"
    assert details.get("command") == "build"


def test_terminal_success_emits_run_success_event(tmp_path: Path) -> None:
    """Transition running → success emits one run.success event."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "2026-04-26-100200-cccccc"
    _write_running_record(repo, run_id, display_name="finishing build")

    service = MissionControlService(repo, queue_compat=False)
    service.state()  # bootstrap (sees the running record)

    # Status is the terminal lifecycle state (`done`); terminal_outcome
    # captures the success/failure verdict. Our event-kind builder uses the
    # terminal_outcome when present so the event names stay user-friendly.
    finalize_record(repo, run_id, status="done", terminal_outcome="success")
    service.state()

    items = service.events(limit=200).get("items") or []
    successes = [item for item in items if item.get("kind") == "run.success"]
    assert len(successes) == 1, f"expected one run.success, got {successes!r}"
    event = successes[0]
    assert event.get("run_id") == run_id
    assert event.get("severity") == "success"
    details = event.get("details") or {}
    assert details.get("terminal_outcome") == "success"


def test_terminal_failure_emits_run_failed_event(tmp_path: Path) -> None:
    """Transition running → failed emits one run.failed (severity=error)."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "2026-04-26-100300-dddddd"
    _write_running_record(repo, run_id, display_name="failing build")

    service = MissionControlService(repo, queue_compat=False)
    service.state()

    finalize_record(repo, run_id, status="failed", terminal_outcome="failed")
    service.state()

    items = service.events(limit=200).get("items") or []
    failures = [item for item in items if item.get("kind") == "run.failed"]
    assert len(failures) == 1, f"expected one run.failed, got {failures!r}"
    assert failures[0].get("severity") == "error"


def test_terminal_cancellation_emits_run_cancelled_event(tmp_path: Path) -> None:
    """Transition running → cancelled emits one run.cancelled (severity=warning)."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    run_id = "2026-04-26-100400-eeeeee"
    _write_running_record(repo, run_id, display_name="cancelled build")

    service = MissionControlService(repo, queue_compat=False)
    service.state()

    finalize_record(repo, run_id, status="cancelled", terminal_outcome="cancelled")
    service.state()

    items = service.events(limit=200).get("items") or []
    cancellations = [item for item in items if item.get("kind") == "run.cancelled"]
    assert len(cancellations) == 1, f"expected one run.cancelled, got {cancellations!r}"
    assert cancellations[0].get("severity") == "warning"


def test_repeated_state_calls_do_not_re_emit(tmp_path: Path) -> None:
    """Each transition emits exactly once — not on every subsequent refresh."""

    repo = tmp_path / "repo"
    _init_repo(repo)

    service = MissionControlService(repo, queue_compat=False)
    service.state()  # bootstrap

    run_id = "2026-04-26-100500-ffffff"
    _write_running_record(repo, run_id, display_name="single emit")
    service.state()
    service.state()
    service.state()

    items = service.events(limit=200).get("items") or []
    started = [item for item in items if item.get("kind") == "run.started" and item.get("run_id") == run_id]
    assert len(started) == 1, f"run.started should fire exactly once across multiple refreshes, got {started!r}"

    finalize_record(repo, run_id, status="done", terminal_outcome="success")
    service.state()
    service.state()
    service.state()

    items = service.events(limit=200).get("items") or []
    successes = [item for item in items if item.get("kind") == "run.success" and item.get("run_id") == run_id]
    assert len(successes) == 1, f"run.success should fire exactly once across multiple refreshes, got {successes!r}"
