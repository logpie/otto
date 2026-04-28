from __future__ import annotations

import json
from pathlib import Path

from otto import paths
from otto.mission_control.model import _token_usage_from_summary_paths
from otto.pipeline import _write_session_summary
from otto.queue.runner import _token_usage_from_summary
from otto.token_usage import format_token_spend, token_spend_summary
from tests._helpers import init_repo


def _write_phase_usage(session_dir: Path, phase: str, usage: dict[str, int]) -> None:
    phase_dir = session_dir / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "messages.jsonl").write_text(
        json.dumps({"type": "phase_end", "phase": phase, "usage": usage}) + "\n",
        encoding="utf-8",
    )


def _write_codex_subset_cache_messages(session_dir: Path, phase: str = "build") -> None:
    phase_dir = session_dir / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "messages.jsonl").write_text(
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
                "phase": phase,
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


def _write_claude_result_and_inflated_phase_messages(session_dir: Path, phase: str = "build") -> None:
    phase_dir = session_dir / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "messages.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "assistant",
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 10_000,
                    "cache_read_input_tokens": 200_000,
                    "output_tokens": 100,
                },
            }),
            json.dumps({
                "type": "result",
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 1_000,
                    "cache_read_input_tokens": 2_000,
                    "output_tokens": 30,
                },
            }),
            json.dumps({
                "type": "phase_end",
                "phase": phase,
                "usage": {
                    "input_tokens": 120,
                    "cache_creation_input_tokens": 11_000,
                    "cache_read_input_tokens": 202_000,
                    "output_tokens": 130,
                    "total_tokens": 213_250,
                },
            }),
        ]) + "\n",
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
    _write_codex_subset_cache_messages(session_dir)
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
    _write_codex_subset_cache_messages(session_dir)
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


def test_claude_result_usage_overrides_inflated_phase_end_tokens(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_id = "claude-inflated-phase-run"
    session_dir = paths.session_dir(repo, run_id)
    _write_claude_result_and_inflated_phase_messages(session_dir)
    paths.session_summary(repo, run_id).write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    usage = _token_usage_from_summary_paths([paths.session_summary(repo, run_id)], base_dir=None)

    assert usage == {
        "input_tokens": 20,
        "cache_creation_input_tokens": 1_000,
        "cache_read_input_tokens": 2_000,
        "cached_input_tokens": 3_000,
        "output_tokens": 30,
        "total_tokens": 3_050,
    }


def test_token_spend_uses_half_up_compact_rounding() -> None:
    assert format_token_spend({"total_tokens": 3_050}) == "3.1K tokens"


def test_token_spend_separates_claude_cache_read_from_fresh_tokens() -> None:
    usage = {
        "input_tokens": 40,
        "cache_creation_input_tokens": 66_155,
        "cache_read_input_tokens": 1_816_849,
        "output_tokens": 9_796,
    }

    summary = token_spend_summary(usage)
    assert summary["total"] == 1_892_840
    assert summary["fresh"] == 75_991
    assert summary["cached"] == 1_816_849
    assert round(float(summary["cache_hit_rate"] or 0), 3) == 0.965
    assert format_token_spend(usage) == "76K fresh + 1.8M cached · 96% hit"


def test_token_spend_handles_cached_input_as_provider_subset() -> None:
    usage = {"input_tokens": 1_000, "cached_input_tokens": 800, "output_tokens": 50}

    assert token_spend_summary(usage) == {
        "total": 1_050,
        "fresh": 250,
        "cached": 800,
        "cache_hit_rate": 0.8,
    }
    assert format_token_spend(usage) == "250 fresh + 800 cached · 80% hit"


def test_write_session_summary_phase_messages_replace_stale_breakdown_tokens(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_id = "stale-breakdown-cache-run"
    session_dir = paths.session_dir(repo, run_id)
    _write_codex_subset_cache_messages(session_dir)

    _write_session_summary(
        repo,
        run_id,
        verdict="passed",
        passed=True,
        cost=0.0,
        duration=1.0,
        stories_passed=1,
        stories_tested=1,
        rounds=1,
        breakdown={
            "build": {
                "duration_s": 1.0,
                "input_tokens": 1_000,
                "cache_read_input_tokens": 800,
                "cached_input_tokens": 800,
                "output_tokens": 50,
                "total_tokens": 1_850,
            }
        },
    )

    summary = json.loads(paths.session_summary(repo, run_id).read_text(encoding="utf-8"))

    assert summary["token_usage"] == {
        "input_tokens": 1_000,
        "cached_input_tokens": 800,
        "output_tokens": 50,
        "total_tokens": 1_050,
    }
    assert summary["breakdown"]["build"] == {
        "duration_s": 1.0,
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
