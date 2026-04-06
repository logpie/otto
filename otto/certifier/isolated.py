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

    configured_port = port_override if port_override is not None else config.get("port_override")
    if configured_port is not None:
        logger.warning(
            "Ignoring port_override=%s in isolated certifier mode; isolated runs must start their own app",
            configured_port,
        )
        config.pop("port_override", None)

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
            port_override=None,
            skip_story_ids=skip_story_ids,
        )

        # Copy reports before the temporary worktree is cleaned up.
        _copy_reports(wt_dir, project_dir)

    duration = round(time.monotonic() - start, 1)
    logger.info("Isolated certifier done: outcome=%s, %.1fs, $%.3f",
                report.outcome.value, duration, report.cost_usd)

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
    """Copy ALL certifier logs from worktree back to main project.

    Copies files AND directories (stories/, evidence-*/, batch logs)
    so certifier behavior can be audited after the worktree is cleaned up.
    """
    src = wt_dir / "otto_logs" / "certifier"
    dst = project_dir / "otto_logs" / "certifier"
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            dest_path = dst / item.name
            if item.is_file():
                shutil.copy2(item, dest_path)
            elif item.is_dir():
                if dest_path.exists():
                    shutil.rmtree(dest_path, ignore_errors=True)
                shutil.copytree(item, dest_path)


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
    from otto.certifier.report import CertificationOutcome, CertificationReport, Finding

    for attempt in range(max_retries + 1):
        try:
            report = run_isolated_certifier(
                intent=intent,
                candidate_sha=candidate_sha,
                project_dir=project_dir,
                config=config,
                cache_dir=cache_dir,
                port_override=port_override,
                skip_story_ids=skip_story_ids,
            )
        except Exception as exc:
            logger.exception("Isolated certifier crashed")
            report = CertificationReport(
                product_type="unknown",
                interaction="unknown",
                findings=[
                    Finding(
                        tier=0,
                        severity="warning",
                        category="harness",
                        description="Isolated certifier crashed unexpectedly",
                        diagnosis=str(exc),
                        fix_suggestion="Inspect certifier infrastructure and retry",
                    )
                ],
                outcome=CertificationOutcome.INFRA_ERROR,
                cost_usd=0.0,
                duration_s=0.0,
            )
        if report.outcome != CertificationOutcome.INFRA_ERROR:
            break
        if attempt < max_retries:
            logger.warning("Certifier infra error (attempt %d/%d), retrying in 5s...",
                           attempt + 1, max_retries + 1)
            time.sleep(5)

    return report
