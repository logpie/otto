"""Scoped merge-conflict reapply for verified task candidates."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, run_agent_query, _subprocess_env
from otto.config import detect_test_command
from otto.git_ops import build_candidate_commit
from otto.observability import append_text_log
from otto.tasks import load_tasks
from otto.testing import run_test_suite


def _git(
    project_dir: Path,
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
        raise RuntimeError(stderr)
    return result


def _cleanup_failure(project_dir: Path, default_branch: str, temp_branch: str) -> None:
    _git(project_dir, "reset", "--hard")
    _git(project_dir, "checkout", default_branch)
    _git(project_dir, "branch", "-D", temp_branch)


def _abort_cherry_pick(project_dir: Path) -> None:
    abort = _git(project_dir, "cherry-pick", "--abort")
    if abort.returncode != 0:
        _git(project_dir, "reset", "--hard")


def _resolve_candidate_sha(project_dir: Path, candidate_ref: str) -> str:
    return _git(project_dir, "rev-parse", candidate_ref, check=True).stdout.strip()


def _commit_current_tree(project_dir: Path, base_sha: str) -> str:
    candidate_sha = build_candidate_commit(project_dir, base_sha)
    diff_check = subprocess.run(
        ["git", "diff", "--quiet", f"{base_sha}..{candidate_sha}"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if diff_check.returncode == 0:
        raise RuntimeError("scoped reapply produced no changes")
    return candidate_sha


def _verify_result(
    task_key: str,
    candidate_sha: str,
    config: dict[str, Any],
    project_dir: Path,
    tasks_file: Path,
) -> bool:
    if config.get("skip_test"):
        return True

    task_meta = next(
        (task for task in load_tasks(tasks_file) if task.get("key") == task_key),
        {},
    )
    test_result = run_test_suite(
        project_dir=project_dir,
        candidate_sha=candidate_sha,
        test_command=config.get("test_command") or detect_test_command(project_dir),
        custom_test_cmd=task_meta.get("verify"),
        timeout=config.get("verify_timeout", 300),
    )
    return test_result.passed


def _build_agent_prompt(
    task_key: str,
    candidate_ref: str,
    base_sha: str,
    candidate_sha: str,
    patch_text: str,
    conflict_files: str,
    conflict_status: str,
    conflict_diff: str,
) -> str:
    return f"""\
You are a merge conflict resolver. Your ONLY job is to apply a known patch to the current codebase.

Apply the known patch for task `{task_key}` to the current checked-out codebase.

Constraints:
- Do not re-explore the repository.
- Do not re-implement the feature from scratch.
- Do not broaden the change.
- Do not create commits.
- Stop after the patch is correctly applied to the working tree.

Context:
- Candidate ref: `{candidate_ref}`
- Candidate SHA: `{candidate_sha}`
- Original base SHA: `{base_sha}`
- Mechanical cherry-pick failed on updated main.

Conflicted files from the failed cherry-pick:
```text
{conflict_files or "(none reported)"}
```

Git status from the failed cherry-pick:
```text
{conflict_status or "(empty)"}
```

Conflicted working-tree diff from the failed cherry-pick:
```diff
{conflict_diff or ""}
```

Full patch to apply:
```diff
{patch_text}
```
"""


async def scoped_reapply(
    task_key: str,
    candidate_ref: str,
    base_sha: str,
    config: dict,
    project_dir: Path,
    tasks_file: Path,
    *,
    return_metadata: bool = False,
) -> tuple[bool, str] | tuple[bool, str, dict[str, Any]]:
    """Apply a task's verified diff onto updated main via a scoped agent."""
    default_branch = config["default_branch"]
    log_path = project_dir / "otto_logs" / task_key / "merge-resolve.log"
    started_at = time.monotonic()
    metadata: dict[str, Any] = {
        "patch_lines": 0,
        "cherry_pick": "not_attempted",
        "agent_fallback_triggered": False,
        "agent_max_turns": 0,
        "agent_result": "not_needed",
        "verification_passed": False,
        "time_s": 0.0,
    }

    def _log(*lines: str) -> None:
        append_text_log(log_path, lines)

    def _finish(success: bool, sha: str) -> tuple[bool, str] | tuple[bool, str, dict[str, Any]]:
        metadata["time_s"] = round(time.monotonic() - started_at, 1)
        _log(f"total time: {metadata['time_s']:.1f}s", "")
        if return_metadata:
            return success, sha, metadata
        return success, sha

    candidate_sha = _resolve_candidate_sha(project_dir, candidate_ref)
    patch_text = _git(project_dir, "diff", f"{base_sha}..{candidate_sha}", check=True).stdout
    metadata["patch_lines"] = len(patch_text.splitlines())
    _log(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] scoped reapply for {task_key}",
        f"patch size: {metadata['patch_lines']} lines",
        f"candidate ref: {candidate_ref}",
    )
    temp_branch = f"otto/_scoped_reapply_{task_key}"

    _git(project_dir, "checkout", default_branch, check=True)
    _git(project_dir, "branch", "-D", temp_branch)
    _git(project_dir, "checkout", "-b", temp_branch, check=True)
    temp_base_sha = _git(project_dir, "rev-parse", "HEAD", check=True).stdout.strip()

    cherry_pick = _git(project_dir, "cherry-pick", "--no-commit", candidate_sha)
    if cherry_pick.returncode == 0:
        metadata["cherry_pick"] = "success"
        _log("cherry-pick attempted: success")
        try:
            new_sha = _commit_current_tree(
                project_dir,
                temp_base_sha,
            )
        except Exception:
            _cleanup_failure(project_dir, default_branch, temp_branch)
            _log("result: failed to create scoped commit")
            return _finish(False, "")
        verified = _verify_result(task_key, new_sha, config, project_dir, tasks_file)
        metadata["verification_passed"] = verified
        _log(f"test verification: {'pass' if verified else 'fail'}")
        if verified:
            _git(project_dir, "checkout", default_branch, check=True)
            _git(project_dir, "branch", "-D", temp_branch)
            return _finish(True, new_sha)
        _cleanup_failure(project_dir, default_branch, temp_branch)
        return _finish(False, "")

    metadata["cherry_pick"] = "fail"
    conflict_files = _git(project_dir, "diff", "--name-only", "--diff-filter=U").stdout.strip()
    conflict_status = _git(project_dir, "status", "--short").stdout.strip()
    conflict_diff = _git(project_dir, "diff").stdout.strip()
    _log("cherry-pick attempted: fail")
    _abort_cherry_pick(project_dir)

    coding_settings = config.get("coding_agent_settings", "project").split(",")
    metadata["agent_fallback_triggered"] = True
    metadata["agent_max_turns"] = 30
    _log(f"agent fallback: triggered (max_turns={metadata['agent_max_turns']})")
    agent_opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        setting_sources=coding_settings,
        env=_subprocess_env(),
        max_turns=30,
        effort="low",
        # Use CC's preset to keep built-in tool guidance (Glob over find, etc.)
        # The merge resolver instructions are in the prompt itself, not system_prompt.
        # CRITICAL: system_prompt=None would blank CC's defaults.
        # "append" is NOT a real SDK field — don't use it.
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    prompt = _build_agent_prompt(
        task_key=task_key,
        candidate_ref=candidate_ref,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        patch_text=patch_text,
        conflict_files=conflict_files,
        conflict_status=conflict_status,
        conflict_diff=conflict_diff,
    )

    try:
        _, _, result_msg = await run_agent_query(prompt, agent_opts)
    except Exception:
        metadata["agent_result"] = "exception"
        _log("agent fallback result: exception")
        _cleanup_failure(project_dir, default_branch, temp_branch)
        return _finish(False, "")
    if getattr(result_msg, "is_error", False):
        metadata["agent_result"] = "error"
        _log("agent fallback result: error")
        _cleanup_failure(project_dir, default_branch, temp_branch)
        return _finish(False, "")

    try:
        new_sha = _commit_current_tree(
            project_dir,
            temp_base_sha,
        )
    except Exception:
        metadata["agent_result"] = "no_changes"
        _log("agent fallback result: no scoped commit created")
        _cleanup_failure(project_dir, default_branch, temp_branch)
        return _finish(False, "")

    verified = _verify_result(task_key, new_sha, config, project_dir, tasks_file)
    metadata["verification_passed"] = verified
    metadata["agent_result"] = "success" if verified else "verification_failed"
    _log(
        f"agent fallback result: {metadata['agent_result']}",
        f"test verification: {'pass' if verified else 'fail'}",
    )
    if verified:
        _git(project_dir, "checkout", default_branch, check=True)
        _git(project_dir, "branch", "-D", temp_branch)
        return _finish(True, new_sha)

    _cleanup_failure(project_dir, default_branch, temp_branch)
    return _finish(False, "")
