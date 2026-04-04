"""Run certifier in an isolated worktree — true physical isolation.

The certifier tests an immutable candidate snapshot in a detached worktree.
The coding agent's workspace is never touched. Deps are installed fresh in
the worktree. Certifier caches live in orchestrator-owned storage.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("otto.certifier.isolated")

# Default cache directory — outside any project tree
DEFAULT_CACHE_DIR = Path.home() / ".otto" / "certifier-cache"


def run_isolated_certifier(
    intent: str,
    candidate_sha: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    cache_dir: Path | None = None,
    port_override: int | None = None,
    skip_story_ids: set[str] | None = None,
) -> Any:
    """Run certifier in an isolated worktree at the candidate ref.

    - Creates a temporary worktree at candidate_sha
    - Runs certifier entirely within that worktree
    - Certifier caches stored in cache_dir (not project_dir)
    - Worktree cleaned up after certification
    - Agent's workspace is untouched

    Returns CertificationReport.
    """
    from otto.certifier import run_unified_certifier

    config = dict(config or {})
    effective_cache = cache_dir or DEFAULT_CACHE_DIR
    effective_cache.mkdir(parents=True, exist_ok=True)

    # Thread cache_dir through config so certifier loaders use it
    config["certifier_cache_dir"] = str(effective_cache)

    start = time.monotonic()
    logger.info("Running isolated certifier: sha=%s, cache=%s", candidate_sha[:8], effective_cache)

    with _certifier_worktree(project_dir, candidate_sha) as wt_dir:
        logger.info("Certifier worktree created: %s", wt_dir)

        report = run_unified_certifier(
            intent=intent,
            project_dir=wt_dir,
            config=config,
            port_override=port_override,
            skip_story_ids=skip_story_ids,
        )

    duration = round(time.monotonic() - start, 1)
    logger.info("Isolated certifier done: outcome=%s, %.1fs, $%.3f",
                report.outcome.value, duration, report.cost_usd)

    # Copy reports back to main project's otto_logs for observability
    _copy_reports(wt_dir, project_dir)

    return report


@contextmanager
def _certifier_worktree(
    project_dir: Path,
    candidate_sha: str,
) -> Generator[Path, None, None]:
    """Create a temporary worktree at the candidate SHA for certification.

    The worktree is in .otto-worktrees/certifier-{sha[:8]}/ and cleaned up on exit.
    """
    wt_name = f"certifier-{candidate_sha[:8]}"
    wt_dir = project_dir / ".otto-worktrees" / wt_name
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    # Clean up stale worktree if exists
    if wt_dir.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=project_dir, capture_output=True,
        )
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)

    # Create detached worktree at candidate SHA
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_dir), candidate_sha],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create certifier worktree: {result.stderr}")

    try:
        yield wt_dir
    finally:
        # Clean up worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=project_dir, capture_output=True,
        )
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_dir, capture_output=True,
        )


def _copy_reports(wt_dir: Path, project_dir: Path) -> None:
    """Copy certifier reports from worktree back to main project for observability."""
    src = wt_dir / "otto_logs" / "certifier"
    dst = project_dir / "otto_logs" / "certifier"
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dest_path = dst / item.name
            if item.is_file():
                shutil.copy2(item, dest_path)

    # Also copy journey agent logs if they exist
    src_reports = wt_dir / "certifier-reports"
    if src_reports.exists():
        dst_reports = project_dir / "otto_logs" / "certifier"
        dst_reports.mkdir(parents=True, exist_ok=True)
        for item in src_reports.iterdir():
            if item.is_file():
                shutil.copy2(item, dst_reports / item.name)


def certify_with_retry(
    intent: str,
    candidate_sha: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    max_retries: int = 1,
    cache_dir: Path | None = None,
    port_override: int | None = None,
    skip_story_ids: set[str] | None = None,
) -> Any:
    """Run isolated certifier with retry on infra errors."""
    from otto.certifier.report import CertificationOutcome

    for attempt in range(max_retries + 1):
        report = run_isolated_certifier(
            intent=intent,
            candidate_sha=candidate_sha,
            project_dir=project_dir,
            config=config,
            cache_dir=cache_dir,
            port_override=port_override,
            skip_story_ids=skip_story_ids,
        )
        if not hasattr(CertificationOutcome, "INFRA_ERROR"):
            # INFRA_ERROR not yet in enum — treat BLOCKED as potential infra
            break
        if report.outcome != CertificationOutcome.INFRA_ERROR:
            break
        if attempt < max_retries:
            logger.warning("Certifier infra error (attempt %d/%d), retrying in 5s...",
                           attempt + 1, max_retries + 1)
            time.sleep(5)

    return report
