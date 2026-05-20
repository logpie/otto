"""Regression tests for concurrent provider cost summary writes."""

from __future__ import annotations

import json
import multiprocessing as mp
import threading
from pathlib import Path
from typing import Any

import pytest

from otto.v5_provider_fallback import append_attempt


WORKER_COUNT = 8
ATTEMPTS_PER_WORKER = 5
TOTAL_ATTEMPTS = WORKER_COUNT * ATTEMPTS_PER_WORKER


def _append_attempts(
    summary_path: str,
    worker_index: int,
    attempts_per_worker: int,
    barrier: Any,
) -> None:
    barrier.wait(timeout=10)
    summary = Path(summary_path)
    for attempt_index in range(attempts_per_worker):
        sequence = worker_index * attempts_per_worker + attempt_index + 1
        append_attempt(
            summary,
            provider=f"worker-{worker_index}",
            cost_usd=float(sequence),
            outcome="pass",
            duration_s=float(attempt_index + 1),
            started_at=f"2026-05-20T00:00:{sequence:02d}Z",
        )


def _write_summary(tmp_path: Path) -> Path:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"task_id": "root"}) + "\n", encoding="utf-8")
    return summary


def _assert_complete_summary(summary: Path) -> None:
    data = json.loads(summary.read_text(encoding="utf-8"))
    attempts = data["cost_attempts"]

    assert len(attempts) == TOTAL_ATTEMPTS
    assert sorted(int(attempt["cost_usd"]) for attempt in attempts) == list(
        range(1, TOTAL_ATTEMPTS + 1)
    )
    assert data["cost_usd"] == pytest.approx(sum(range(1, TOTAL_ATTEMPTS + 1)))
    assert not summary.with_suffix(".json.tmp").exists()
    assert not list(summary.parent.glob("*.tmp"))


def test_append_attempt_is_atomic_across_processes(tmp_path: Path) -> None:
    summary = _write_summary(tmp_path)
    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(WORKER_COUNT)
    processes = [
        ctx.Process(
            target=_append_attempts,
            args=(str(summary), worker_index, ATTEMPTS_PER_WORKER, barrier),
        )
        for worker_index in range(WORKER_COUNT)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    still_running = [process.pid for process in processes if process.is_alive()]
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert still_running == []
    assert [process.exitcode for process in processes] == [0] * WORKER_COUNT
    _assert_complete_summary(summary)


def test_append_attempt_is_atomic_across_threads(tmp_path: Path) -> None:
    summary = _write_summary(tmp_path)
    barrier = threading.Barrier(WORKER_COUNT)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def run(worker_index: int) -> None:
        try:
            _append_attempts(str(summary), worker_index, ATTEMPTS_PER_WORKER, barrier)
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(worker_index,))
        for worker_index in range(WORKER_COUNT)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert [thread.name for thread in threads if thread.is_alive()] == []
    assert errors == []
    _assert_complete_summary(summary)
