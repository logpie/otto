#!/usr/bin/env python3
"""Replay Otto spec/QA against a saved bare-run worktree snapshot.

Usage:
  python tools/replay_bare_output_into_otto_qa.py \
    --bare-result-dir bench/pressure/results/<label>/<project> \
    --otto-tasks bench/pressure/results/<otto-label>/<project>/tasks.yaml

This restores the bare runner's saved worktree snapshot into a temp dir,
loads the exact spec from Otto's tasks.yaml, then runs Otto QA on the bare
output without rerunning implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from otto.config import DEFAULT_CONFIG, detect_test_command
from otto.qa import run_qa
from otto.tasks import load_tasks


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--bare-result-dir", required=True, type=Path)
    p.add_argument("--otto-tasks", required=True, type=Path)
    p.add_argument("--provider", default="codex")
    return p.parse_args()


def _load_single_task(tasks_path: Path) -> dict:
    tasks = load_tasks(tasks_path)
    if len(tasks) != 1:
        raise SystemExit(f"Expected exactly one task in {tasks_path}, found {len(tasks)}")
    return tasks[0]


async def _main() -> int:
    args = _parse_args()
    bare_dir = args.bare_result_dir.resolve()
    tasks_path = args.otto_tasks.resolve()

    snapshot = bare_dir / "worktree-snapshot.tgz"
    if not snapshot.exists():
        raise SystemExit(f"Missing snapshot: {snapshot}")

    task = _load_single_task(tasks_path)
    project_name = bare_dir.name

    replay_root = Path(tempfile.mkdtemp(prefix=f"otto-qa-replay-{project_name}-"))
    try:
        with tarfile.open(snapshot, "r:gz") as tf:
            tf.extractall(replay_root)

        config = dict(DEFAULT_CONFIG)
        config["provider"] = args.provider
        config["planner_provider"] = None
        config["planner_effort"] = "high"
        config["test_command"] = detect_test_command(replay_root)

        replay_log_dir = bare_dir / "otto-replay-qa"
        replay_log_dir.mkdir(parents=True, exist_ok=True)

        qa_task = {
            "key": str(task.get("key", "replay")),
            "prompt": str(task.get("prompt", "")),
            "spec": list(task.get("spec") or []),
        }

        result = await run_qa(
            [qa_task],
            config,
            replay_root,
            diff="",
            log_dir=replay_log_dir,
        )

        qa_log_text = ""
        qa_log_path = replay_log_dir / "qa-agent.log"
        if qa_log_path.exists():
            qa_log_text = qa_log_path.read_text()

        spec_count = len(qa_task["spec"])
        must_pass_markers = len(re.findall(r"(?:^|\n)(?:SPEC\s+\d+:\s+PASS|spec_\d+[^=\n]*=PASS)", qa_log_text))
        fail_markers = len(re.findall(r"(?:^|\n)(?:SPEC\s+\d+:\s+FAIL|spec_\d+[^=\n]*=FAIL|✗ \[must\])", qa_log_text))
        verdict_parse_error = "[Verdict file parse error:" in qa_log_text
        qa_agent_error = "[QA agent error:" in qa_log_text

        summary = {
            "project": project_name,
            "provider": args.provider,
            "must_passed": bool(result.get("must_passed")),
            "test_suite_passed": bool(result.get("test_suite_passed", False)),
            "infrastructure_error": bool(result.get("infrastructure_error")),
            "proof_count": int(result.get("proof_count", 0) or 0),
            "proof_coverage": result.get("proof_coverage", ""),
            "failed_must_items": [
                item for item in (result.get("verdict", {}) or {}).get("must_items", [])
                if item.get("status") == "fail"
            ],
            "log_must_pass_markers": must_pass_markers,
            "log_fail_markers": fail_markers,
            "spec_count": spec_count,
            "verdict_parse_error": verdict_parse_error,
            "qa_agent_error": qa_agent_error,
            "log_suggests_all_musts_passed": (
                must_pass_markers >= spec_count and fail_markers == 0 and spec_count > 0
            ),
        }
        summary["effective_must_passed"] = bool(summary["must_passed"]) or (
            summary["proof_coverage"] == f"{spec_count}/{spec_count}"
            and not summary["failed_must_items"]
            and summary["test_suite_passed"]
        )
        (replay_log_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        shutil.rmtree(replay_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
