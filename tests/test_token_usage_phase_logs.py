from __future__ import annotations

import json
from pathlib import Path

from otto import paths
from otto.mission_control.model import _token_usage_from_summary_paths
from otto.pipeline import _write_session_summary
from otto.queue.runner import _token_usage_from_summary
from tests._helpers import init_repo


def _write_phase_usage(session_dir: Path, phase: str, usage: dict[str, int]) -> None:
    phase_dir = session_dir / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "messages.jsonl").write_text(
        json.dumps({"type": "phase_end", "phase": phase, "usage": usage}) + "\n",
        encoding="utf-8",
    )


def test_session_summary_includes_tokens_from_phase_messages(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_id = "phase-token-run"
    session_dir = paths.session_dir(repo, run_id)
    _write_phase_usage(
        session_dir,
        "build",
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 30,
            "output_tokens": 4,
        },
    )
    _write_phase_usage(
        session_dir,
        "certify",
        {"input_tokens": 5, "cached_input_tokens": 7, "output_tokens": 8},
    )

    _write_session_summary(
        repo,
        run_id,
        verdict="passed",
        passed=True,
        cost=1.23,
        duration=5.0,
        stories_passed=2,
        stories_tested=2,
        rounds=1,
        breakdown={"build": {"duration_s": 3.0}, "certify": {"duration_s": 2.0}},
    )

    summary = json.loads(paths.session_summary(repo, run_id).read_text(encoding="utf-8"))
    assert summary["token_usage"] == {
        "input_tokens": 15,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 30,
        "cached_input_tokens": 39,
        "output_tokens": 12,
        "total_tokens": 59,
    }
    assert summary["breakdown"]["build"]["cache_read_input_tokens"] == 30
    assert summary["breakdown"]["certify"]["cached_input_tokens"] == 7


def test_mission_control_recovers_tokens_from_existing_phase_messages(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_id = "legacy-summary-run"
    session_dir = paths.session_dir(repo, run_id)
    _write_phase_usage(
        session_dir,
        "build",
        {"input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 9},
    )
    paths.session_summary(repo, run_id).write_text(
        json.dumps({"run_id": run_id, "cost_usd": 0.42, "breakdown": {"build": {"cost_usd": 0.42}}}),
        encoding="utf-8",
    )

    usage = _token_usage_from_summary_paths([paths.session_summary(repo, run_id)], base_dir=None)

    assert usage == {
        "input_tokens": 20,
        "cache_read_input_tokens": 100,
        "cached_input_tokens": 100,
        "output_tokens": 9,
        "total_tokens": 129,
    }


def test_codex_cached_input_tokens_are_not_double_counted(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_id = "codex-cache-run"
    session_dir = paths.session_dir(repo, run_id)
    build_dir = session_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "messages.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "result",
                "usage": {
                    "input_tokens": 1_000,
                    "cached_input_tokens": 800,
                    "output_tokens": 50,
                },
            }),
            json.dumps({
                "type": "phase_end",
                "phase": "build",
                "usage": {
                    "input_tokens": 1_000,
                    "cache_read_input_tokens": 800,
                    "cached_input_tokens": 800,
                    "output_tokens": 50,
                    "total_tokens": 1_850,
                },
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    paths.session_summary(repo, run_id).write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    usage = _token_usage_from_summary_paths([paths.session_summary(repo, run_id)], base_dir=None)

    assert usage == {
        "input_tokens": 1_000,
        "cached_input_tokens": 800,
        "output_tokens": 50,
        "total_tokens": 1_050,
    }


def test_phase_messages_override_stale_inflated_summary_tokens(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_id = "stale-summary-cache-run"
    session_dir = paths.session_dir(repo, run_id)
    build_dir = session_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "messages.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "result",
                "usage": {
                    "input_tokens": 1_000,
                    "cached_input_tokens": 800,
                    "output_tokens": 50,
                },
            }),
            json.dumps({
                "type": "phase_end",
                "phase": "build",
                "usage": {
                    "input_tokens": 1_000,
                    "cache_read_input_tokens": 800,
                    "cached_input_tokens": 800,
                    "output_tokens": 50,
                    "total_tokens": 1_850,
                },
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    paths.session_summary(repo, run_id).write_text(
        json.dumps({
            "run_id": run_id,
            "token_usage": {
                "input_tokens": 1_000,
                "cache_read_input_tokens": 800,
                "cached_input_tokens": 800,
                "output_tokens": 50,
                "total_tokens": 1_850,
            },
        }),
        encoding="utf-8",
    )

    usage = _token_usage_from_summary_paths([paths.session_summary(repo, run_id)], base_dir=None)

    assert usage == {
        "input_tokens": 1_000,
        "cached_input_tokens": 800,
        "output_tokens": 50,
        "total_tokens": 1_050,
    }


def test_queue_summary_tokens_share_provider_neutral_cache_semantics() -> None:
    usage = _token_usage_from_summary({
        "breakdown": {
            "build": {
                "input_tokens": 1_000,
                "cached_input_tokens": 800,
                "output_tokens": 50,
            },
            "certify": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 30,
                "output_tokens": 4,
            },
        }
    })

    assert usage == {
        "input_tokens": 1_010,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 30,
        "cached_input_tokens": 832,
        "output_tokens": 54,
        "total_tokens": 1_096,
    }
