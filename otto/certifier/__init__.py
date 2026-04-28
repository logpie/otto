"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  run_agentic_certifier() — single agent reads, installs, tests, reports
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from otto.logstream import summarize_browser_efficiency
from otto.redaction import redact_text
from otto.token_usage import phase_token_usage_from_messages, total_token_usage_from_phases

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    tomllib = None

if TYPE_CHECKING:
    from otto.budget import RunBudget
    from otto.certifier.report import CertificationReport

logger = logging.getLogger("otto.certifier")

_SCHEMA_VERSION = 1
_GENERATOR = "otto certifier"


def _is_queue_runner_child() -> bool:
    from otto.queue.runtime import is_queue_runner_child

    return is_queue_runner_child()

_MODE_PROFILES: dict[str, dict[str, Any]] = {
    "fast": {
        "meaning": "A quick smoke test that confirms only core happy-path behavior.",
        "tested": [
            "3-5 core user journeys or Must Have stories",
            "Real requests against the running product",
            "Basic first-pass behavior only",
        ],
        "untested": [
            "Browser event sequencing",
            "Focus and blur handling",
            "Pointer, drag, and drop interactions",
            "Visual correctness",
            "Adversarial and malformed inputs",
            "Source-level code review",
        ],
        "limitations": "no real browser-event sequencing, no blur/focus/pointer/drag/drop verification, no visual verification, no adversarial inputs, no code review.",
        "escaped_bug_classes": [
            "focus/blur regressions",
            "drag-and-drop or pointer bugs",
            "CSS/layout breakage",
            "input-validation bypasses",
            "latent security issues",
        ],
        "residual_risk": "Smoke coverage only; deeper interaction, visual, and adversarial defects can still escape.",
    },
    "standard": {
        "meaning": "A broader user-facing certification pass with subagent coverage and screenshots.",
        "tested": [
            "Parallel subagent checks across core stories",
            "Real product interactions on relevant surfaces",
            "Screenshots and recordings for web UI runs when collected",
        ],
        "untested": [
            "Deliberate adversarial probing",
            "Exhaustive error-path exploration",
            "Deep static code review",
        ],
        "limitations": "subagents + screenshots, no adversarial probing.",
        "escaped_bug_classes": [
            "hostile input handling bugs",
            "rare authorization bypass paths",
            "race conditions",
            "load-sensitive failures",
        ],
        "residual_risk": "User flows were exercised, but adversarial paths and some static risks remain underexplored.",
    },
    "thorough": {
        "meaning": "A deep certification pass that mixes behavioral probing with code review.",
        "tested": [
            "Adversarial edge cases",
            "Existing test suite behavior",
            "Targeted source review for suspicious code paths",
            "Visual evidence when applicable",
        ],
        "untested": [
            "Long-haul soak behavior",
            "Environment-specific production integrations not reproducible locally",
        ],
        "limitations": "adversarial edge cases + code review.",
        "escaped_bug_classes": [
            "production-only dependency failures",
            "time-based flaky behavior",
            "multi-hour stability regressions",
        ],
        "residual_risk": "This is the strongest local audit mode, but production-only conditions can still escape.",
    },
    "target": {
        "meaning": "A metric-focused certification that measures a target and documents bottlenecks.",
        "tested": [
            "Explicit metric measurements",
            "Target threshold comparison",
            "Supporting evidence for bottlenecks or regressions",
        ],
        "untested": [
            "General product certification beyond the target",
            "Exhaustive functional coverage",
        ],
        "limitations": "metric-focused measurement, not a full product certification sweep.",
        "escaped_bug_classes": [
            "non-target functional regressions",
            "UX and visual issues unrelated to the target metric",
        ],
        "residual_risk": "Passing the target does not imply overall product quality.",
    },
    "hillclimb": {
        "meaning": "A product-quality review oriented toward missing value and usability gaps.",
        "tested": [
            "Core user experience walkthrough",
            "Highest-impact product gaps",
            "Improvement opportunities grounded in use",
        ],
        "untested": [
            "Bug-hunt depth expected from thorough certification",
            "Security and adversarial probing",
        ],
        "limitations": "product-improvement review, not a bug-certification sweep.",
        "escaped_bug_classes": [
            "deep correctness defects",
            "security vulnerabilities",
            "latent edge-case failures",
        ],
        "residual_risk": "Useful product advice does not guarantee functional correctness.",
    },
}


def _story_verdict(story: dict[str, Any]) -> str:
    """Return the canonical verdict for a story, with back-compat fallback."""
    verdict = story.get("verdict")
    if verdict:
        return str(verdict)
    return "PASS" if story.get("passed") else "FAIL"


def _normalize_story_result(story: dict[str, Any]) -> dict[str, Any]:
    """Ensure PoW consumers always get an explicit verdict field."""
    normalized = dict(story)
    verdict = _story_verdict(story)
    normalized["verdict"] = verdict
    normalized["passed"] = verdict in {"PASS", "WARN"}
    if verdict == "WARN":
        normalized["warn"] = True
    return normalized


def _verification_status_from_story(story: dict[str, Any]) -> str:
    verdict = _story_verdict(story).strip().upper()
    return {
        "PASS": "pass",
        "FAIL": "fail",
        "WARN": "warn",
        "SKIPPED": "skipped",
        "FLAG_FOR_HUMAN": "flag_for_human",
    }.get(verdict, "pending")


def _verification_policy_from_mode(mode: str) -> str:
    return {
        "fast": "fast",
        "thorough": "full",
        "standard": "smart",
        "hillclimb": "smart",
        "target": "smart",
    }.get(str(mode or "").strip().lower(), "smart")


def _write_certifier_verification_plan(
    *,
    report_dir: Path,
    mode: str,
    target: str | None,
    story_results: list[dict[str, Any]],
    explicit_stories: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Persist the shared verification plan for standalone certifier runs."""
    from otto.verification import VerificationCheck, VerificationPlan, write_verification_plan

    checks: list[VerificationCheck] = []
    source_stories = story_results or explicit_stories or []
    for index, story in enumerate(source_stories, start=1):
        story_id = str(
            story.get("story_id")
            or story.get("id")
            or story.get("name")
            or story.get("summary")
            or f"story-{index}"
        )
        label = str(story.get("claim") or story.get("summary") or story.get("name") or story_id)
        status = _verification_status_from_story(story) if story_results else "pending"
        reason = str(
            story.get("observed_result")
            or story.get("key_finding")
            or story.get("evidence")
            or story.get("failure_evidence")
            or ""
        )
        checks.append(
            VerificationCheck(
                id=story_id,
                label=label,
                action="CHECK",
                status=status,  # type: ignore[arg-type]
                reason=reason,
                source=str(story.get("source_branch") or ""),
                metadata={
                    "surface": story.get("surface") or "",
                    "methodology": story.get("methodology") or story.get("interaction_method") or "",
                    "verdict": _story_verdict(story) if story_results else "",
                },
            )
        )
    if not checks:
        checks = [
            VerificationCheck(
                id="certifier-scope",
                label="Certifier scope",
                action="PLAN_AND_CHECK",
                status="pending",
                reason="Certifier was asked to derive checks from the product intent.",
            )
        ]
    profile = _mode_profile(mode)
    plan = VerificationPlan(
        scope="certify",
        target=target or "",
        policy=_verification_policy_from_mode(mode),  # type: ignore[arg-type]
        risk_level=str(mode or "standard"),
        verification_level=str(mode or "standard"),
        allow_skip=False,
        reasons=[
            str(profile.get("meaning") or f"{mode} certification"),
            *[str(item) for item in profile.get("tested", [])[:3]],
        ],
        checks=checks,
        metadata={"certifier_mode": mode},
    )
    plan_path = report_dir / "verification-plan.json"
    write_verification_plan(plan_path, plan)
    return plan.to_dict()


def _story_verdict_display(story: dict[str, Any]) -> tuple[str, str, str]:
    """Return (verdict, icon, css_class) for PoW rendering."""
    verdict = _story_verdict(story)
    return {
        "PASS": ("PASS", "✓", "pass"),
        "FAIL": ("FAIL", "✗", "fail"),
        "WARN": ("WARN", "!", "warn"),
        "SKIPPED": ("SKIPPED", "–", "skipped"),
        "FLAG_FOR_HUMAN": ("FLAG_FOR_HUMAN", "⚠", "flag"),
    }.get(verdict, (verdict, "?", "unknown"))


def _format_stories_section(
    stories: list[dict[str, Any]] | None,
    merge_context: dict[str, Any] | None = None,
) -> str:
    """Format the optional must-verify story list.

    When None or empty, returns "". When provided, renders a "## Stories
    to Verify" block listing each story plus an instruction to run ONLY
    these stories (no additional checklist planning).

    Used by `otto merge`'s post-merge cert phase to verify the union of
    merged branches' stories without redoing exploratory test discovery.

    `merge_context` (optional) carries `target` (str), `diff_files`
    (list[str]), and `allow_skip` (bool) from a multi-branch merge.
    When set, prepends a preamble that lets the cert agent skip stories
    whose feature lives entirely in files not touched by the merge,
    unless `allow_skip=False`. It still preserves contradiction flagging
    for human review. This replaces the former separate planning pass
    with the same pruning logic and no extra LLM call.
    """
    if not stories:
        return ""
    lines: list[str] = []
    if merge_context:
        target = merge_context.get("target", "the target branch")
        diff_files = merge_context.get("diff_files") or []
        allow_skip = merge_context.get("allow_skip", True)
        files_block = "\n".join(f"- `{f}`" for f in diff_files) or "(no files in merge diff)"
        lines += [
            "## Merge Verification Context",
            "",
            f"These stories came from a multi-branch merge into `{target}`.",
            "",
            "Files touched by the merge:",
            files_block,
            "",
            "For each story below, decide before testing:",
            "",
        ]
        if allow_skip:
            lines += [
                "- **SKIPPED** — the story's feature lives entirely in files NOT touched by",
                "  the merge. Don't run the test; the behavior can't have regressed.",
                "  Emit: `STORY_RESULT: <name> | SKIPPED | no overlap with merge diff`",
                "",
            ]
        lines += [
            "- **FLAG_FOR_HUMAN** — the story is genuinely contradicted by another",
            "  merged branch (e.g., one branch added a feature another branch deleted).",
            "  Don't try to resolve.",
            "  Emit: `STORY_RESULT: <name> | FLAG_FOR_HUMAN | <one-sentence reason>`",
            "",
            "- **Otherwise** — test as you normally would, emit PASS/FAIL.",
            "",
        ]
        if allow_skip:
            lines += [
                "When in doubt, test it. False positives (testing an unaffected story)",
                "are cheap; false negatives (skipping a regression) defeat the purpose",
                "of merge verification.",
                "",
            ]
        else:
            lines += [
                "Test every story below; do not skip on file overlap.",
                "",
            ]
    lines += [
        "## Stories to Verify (REQUIRED)",
        "",
        "Run ONLY these stories. Do NOT plan additional ones from "
        "the standard checklist; the union of these stories is the "
        "complete contract for this run.",
        "",
    ]
    for i, story in enumerate(stories, 1):
        name = story.get("name") or story.get("summary") or story.get("story_id") or f"story-{i}"
        desc = story.get("description") or ""
        src = story.get("source_branch")
        header = f"{i}. **{name}**"
        if src:
            header += f"  _(from `{src}`)_"
        lines.append(header)
        if desc:
            lines.append(f"   {desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_merge_section(merge_context: dict[str, Any] | None) -> str:
    """Format merge-specific verification scope for the merge cert prompt."""
    if not merge_context:
        return ""
    allow_skip = bool(merge_context.get("allow_skip", True))
    plan_text = str(merge_context.get("plan_text") or "").strip()
    if plan_text:
        return plan_text + "\n" + _merge_story_scope_rule(allow_skip)

    target = merge_context.get("target", "the target branch")
    diff_files = merge_context.get("diff_files") or []
    files_block = "\n".join(f"- `{f}`" for f in diff_files) or "(no files in merge diff)"
    return "\n".join(
        [
            "## Merge Verification Plan",
            "",
            f"- Target: `{target}`",
            f"- Story skipping allowed: `{'yes' if allow_skip else 'no'}`",
            "",
            "Files touched by the merge:",
            files_block,
            "",
        ]
    ) + _merge_story_scope_rule(allow_skip)


def _merge_story_scope_rule(allow_skip: bool) -> str:
    if allow_skip:
        return (
            "\nStory scope rule:\n"
            "- Stories marked `SKIP_ALLOWED` may be emitted as `SKIPPED` only when the plan's reason still holds.\n"
            "- A prior task's proof-of-work can justify `SKIPPED`; it cannot justify `PASS` for a merge integration check.\n"
            "\n"
        )
    return (
        "\nStory scope rule:\n"
        "- Skipping is disabled for this merge. Test every story, warn on non-blocking gaps, fail on reproduced defects, or flag genuine cross-branch contradictions.\n"
        "\n"
    )


def _render_certifier_prompt(
    *,
    mode: str,
    intent: str,
    evidence_dir: Path,
    focus: str | None = None,
    target: str | None = None,
    stories: list[dict[str, Any]] | None = None,
    merge_context: dict[str, Any] | None = None,
    project_dir: Path | None = None,
    run_id: str | None = None,
    prior_session_ids: list[str] | None = None,
) -> str:
    """Render a standalone certifier prompt with safe placeholder defaults."""
    from otto.config import validate_certifier_mode
    from otto.prompts import render_prompt
    from otto.spec import format_spec_section

    mode = validate_certifier_mode(mode)
    focus_section = f"## Improvement Focus\n{focus}" if focus else ""
    stories_section = _format_stories_section(stories)
    spec_section = ""
    if project_dir is not None:
        spec_section = format_spec_section(
            _resolve_certifier_spec_content(
                project_dir,
                run_id=run_id or "",
                prior_session_ids=prior_session_ids or [],
            )
        )
    prompt_name = {
        "standard": "certifier.md",
        "fast": "certifier-fast.md",
        "thorough": "certifier-thorough.md",
        "hillclimb": "certifier-hillclimb.md",
        "target": "certifier-target.md",
    }[mode]
    if merge_context:
        prompt_name = "certifier-merge-integration.md"
    story_verdict_options = (
        "PASS or FAIL or WARN or SKIPPED or FLAG_FOR_HUMAN"
        if not merge_context or bool(merge_context.get("allow_skip", True))
        else "PASS or FAIL or WARN or FLAG_FOR_HUMAN"
    )
    story_evidence_scope = (
        "you check or intentionally skip"
        if not merge_context or bool(merge_context.get("allow_skip", True))
        else "you check or flag for human review"
    )
    return render_prompt(
        prompt_name,
        intent=intent,
        evidence_dir=str(evidence_dir),
        focus_section=focus_section,
        stories_section=stories_section,
        spec_section=spec_section,
        target=target or "",
        merge_section=_format_merge_section(merge_context),
        story_verdict_options=story_verdict_options,
        story_evidence_scope=story_evidence_scope,
    )


def _resolve_certifier_spec_content(
    project_dir: Path,
    *,
    run_id: str,
    prior_session_ids: list[str],
) -> str:
    from otto import paths
    from otto.history import tail_jsonl_entries

    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path | None) -> None:
        if path is None:
            return
        resolved = path.resolve(strict=False)
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    def _spec_from_session_id(session_id: str) -> None:
        if not session_id:
            return
        session_dir = paths.session_dir(project_dir, session_id)
        _add(session_dir / "spec" / "spec.md")
        for meta_path in (paths.session_checkpoint(project_dir, session_id), paths.session_summary(project_dir, session_id)):
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text())
            except Exception:
                continue
            spec_path = str(data.get("spec_path") or "").strip()
            if spec_path:
                _add(Path(spec_path))

    _spec_from_session_id(run_id)
    for session_id in prior_session_ids:
        _spec_from_session_id(session_id)

    root_spec = project_dir / "spec.md"
    _add(root_spec if root_spec.exists() else None)

    history_path = paths.history_jsonl(project_dir)
    if history_path.exists():
        for _, line in reversed(tail_jsonl_entries(history_path, limit=10)):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            _spec_from_session_id(str(entry.get("run_id") or entry.get("build_id") or "").strip())

    for path in candidates:
        try:
            content = path.read_text().strip()
        except OSError:
            continue
        if content:
            return content
    return ""


def _human_duration(seconds: float) -> str:
    total = max(int(round(float(seconds or 0))), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _human_int(value: int | float) -> str:
    return f"{int(value):,}"


def _safe_git(project_dir: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


_DEV_SERVER_COMMAND_MARKERS = (
    "flask",
    "uvicorn",
    "hypercorn",
    "daphne",
    "fastapi",
    "manage.py runserver",
    "django",
    "python -m http.server",
    "http-server",
    "vite",
    "next dev",
    "nuxt dev",
    "astro dev",
    "svelte-kit",
    "npm run dev",
    "pnpm dev",
    "yarn dev",
    "bun run dev",
    "rails server",
    "bin/rails s",
    "mix phx.server",
    "phoenix.server",
)


def _listening_process_pids() -> set[int] | None:
    """Return PIDs that currently own listening TCP sockets.

    This is intentionally best-effort. Process cleanup is a guardrail around
    provider-run certification, not a hard dependency for certifier execution.
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("p"):
            continue
        try:
            pids.add(int(line[1:]))
        except ValueError:
            continue
    return pids


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _process_cwd(pid: int) -> Path | None:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return Path(line[1:])
    return None


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _looks_like_project_dev_server(command: str) -> bool:
    lowered = f" {command.lower()} "
    return any(marker in lowered for marker in _DEV_SERVER_COMMAND_MARKERS)


def _process_belongs_to_project(project_dir: Path, command: str, cwd: Path | None) -> bool:
    try:
        project_root = project_dir.resolve()
    except OSError:
        project_root = project_dir
    if str(project_root) in command:
        return True
    return cwd is not None and _path_is_relative_to(cwd, project_root)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    for _ in range(30):
        if not _process_exists(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return not _process_exists(pid)


def _cleanup_certifier_background_servers(
    project_dir: Path,
    baseline_listening_pids: set[int] | None,
) -> list[dict[str, str | int]]:
    """Terminate new project-scoped dev servers left behind by provider tools."""
    cleaned: list[dict[str, str | int]] = []
    if baseline_listening_pids is None:
        return cleaned
    current_pids = _listening_process_pids()
    if current_pids is None:
        return cleaned
    for pid in sorted(current_pids - baseline_listening_pids):
        command = _process_command(pid)
        cwd = _process_cwd(pid)
        if not command:
            continue
        if not _looks_like_project_dev_server(command):
            continue
        if not _process_belongs_to_project(project_dir, command, cwd):
            continue
        if _terminate_process(pid):
            cleaned.append({
                "pid": pid,
                "command": command,
                "cwd": str(cwd) if cwd is not None else "",
            })
    return cleaned


def _project_name(project_dir: Path) -> str:
    if tomllib is not None:
        pyproject = project_dir / "pyproject.toml"
        if pyproject.exists():
            try:
                data = tomllib.loads(pyproject.read_text())
            except Exception:
                data = {}
            project = data.get("project")
            if isinstance(project, dict):
                name = project.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return project_dir.name


def _intent_excerpt(intent: str, limit: int = 200) -> str:
    lines = [line.strip() for line in (intent or "").splitlines()]
    while lines and (not lines[0] or lines[0].startswith("#")):
        lines.pop(0)
    text = " ".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _status_for_story(story: dict[str, Any]) -> str:
    verdict = _story_verdict(story)
    return {
        "PASS": "pass",
        "FAIL": "fail",
        "WARN": "warn",
        "SKIPPED": "skipped",
        "FLAG_FOR_HUMAN": "flag",
    }.get(verdict, "unknown")


def _mode_profile(certifier_mode: str) -> dict[str, Any]:
    from otto.config import validate_certifier_mode

    return _MODE_PROFILES[validate_certifier_mode(certifier_mode)]


def _coverage_footer_line(certifier_mode: str) -> str:
    profile = _mode_profile(certifier_mode)
    return f"Mode: {certifier_mode} — {profile['meaning']} See README for mode differences."


def _counts_from_stories(stories: list[dict[str, Any]]) -> dict[str, int]:
    passed_count = sum(1 for story in stories if _status_for_story(story) == "pass")
    failed_count = sum(1 for story in stories if _status_for_story(story) == "fail")
    warn_count = sum(1 for story in stories if _status_for_story(story) == "warn")
    skipped_count = sum(1 for story in stories if _status_for_story(story) == "skipped")
    flag_count = sum(1 for story in stories if _status_for_story(story) == "flag")
    return {
        "passed_count": passed_count,
        "failed_count": failed_count,
        "warn_count": warn_count,
        "skipped_count": skipped_count,
        "flag_count": flag_count,
    }


def _story_residual_risk(status: str, certifier_mode: str) -> str:
    if status == "fail":
        return "Defect confirmed via live UI events. Re-test after the fix lands."
    if status == "warn":
        return "Non-blocking concern surfaced; confirm whether the extra scope or warning is acceptable."
    if status == "skipped":
        return "Story skipped because the merge diff did not overlap the relevant surface."
    if status == "flag":
        return "Story flagged for human review because merged branches conflict semantically."
    return ""


def _normalize_methodology(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace(" ", "-")


def _story_claims_ui_behavior(story: dict[str, Any]) -> bool:
    surface = str(story.get("surface") or "").lower()
    if any(token in surface for token in ("dom", "localstorage", "screenshot", "video")):
        return True
    corpus = " ".join(
        [
            str(story.get("claim") or ""),
            str(story.get("observed_result") or ""),
            str(story.get("summary") or ""),
            " ".join(str(step) for step in story.get("observed_steps", []) or []),
        ]
    ).lower()
    ui_terms = (
        "click",
        "type",
        "press",
        "submit",
        "keyboard",
        "focus",
        "blur",
        "drag",
        "drop",
        "button",
        "input",
        "form",
        "page",
        "modal",
        "ui",
    )
    return any(term in corpus for term in ui_terms)


def _story_methodology_caveat(story: dict[str, Any]) -> str:
    methodology = _normalize_methodology(
        str(story.get("methodology") or story.get("interaction_method") or "")
    )
    if methodology not in {"javascript-eval", "jsdom-simulated"}:
        return ""
    if not _story_claims_ui_behavior(story):
        return ""
    return f"Methodology: {methodology} — UI event handlers were not verified."


def _normalize_story(story: dict[str, Any], certifier_mode: str) -> dict[str, Any]:
    normalized = dict(story)
    status = _status_for_story(story)
    claim = str(story.get("claim") or story.get("summary") or story.get("story_id") or "").strip()
    observed_result = str(story.get("observed_result") or story.get("summary") or "").strip()
    observed_steps = [
        str(step).strip()
        for step in story.get("observed_steps", []) or []
        if str(step).strip()
    ]
    evidence = str(story.get("evidence") or "").strip()
    failure_evidence = str(story.get("failure_evidence") or "").strip()
    surface = str(story.get("surface") or "").strip()
    methodology = str(story.get("methodology") or story.get("interaction_method") or "").strip()
    key_finding = str(story.get("key_finding") or observed_result or story.get("summary") or "").strip()
    methodology_caveat = _story_methodology_caveat(story)
    normalized.update(
        {
            "verdict": _story_verdict(story),
            "status": status,
            "claim": claim,
            "observed_steps": observed_steps,
            "observed_result": observed_result,
            "surface": surface,
            "surface_display": surface or "not specified by certifier",
            "methodology": methodology,
            "interaction_method": methodology,
            "methodology_display": methodology or "not specified by certifier",
            "methodology_caveat": methodology_caveat,
            "evidence": evidence,
            "has_evidence": bool(evidence),
            "failure_evidence": failure_evidence,
            "key_finding": key_finding,
            "residual_risk": _story_residual_risk(status, certifier_mode),
        }
    )
    return normalized


def _diagnosis_text(diagnosis: str, outcome: str) -> str:
    del outcome
    return (diagnosis or "").strip()


def _load_spec_context(project_dir: Path, run_id: str) -> dict[str, Any]:
    from otto import paths

    for candidate in (
        paths.session_checkpoint(project_dir, run_id),
        paths.session_summary(project_dir, run_id),
    ):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text())
        except Exception:
            continue
        return {
            "spec_path": data.get("spec_path") or "",
            "spec_hash": data.get("spec_hash") or "",
            "spec_version": data.get("spec_version") or 0,
        }
    return {"spec_path": "", "spec_hash": "", "spec_version": 0}


def _relative_href(base_dir: Path, target: Path) -> str:
    return os.path.relpath(target, base_dir).replace(os.sep, "/")


def _artifact_entry(base_dir: Path, label: str, target: Path, *, present: bool | None = None) -> dict[str, Any]:
    return {
        "label": label,
        "path": str(target),
        "href": _relative_href(base_dir, target),
        "present": target.exists() if present is None else present,
    }


def _runtime_model_name(provider: str | None) -> str | None:
    provider = (provider or "").strip().lower()
    if provider == "codex" and tomllib is not None:
        for path in (
            Path.home() / ".codex" / "config.toml",
            Path.home() / ".config" / "codex" / "config.toml",
        ):
            try:
                data = tomllib.loads(path.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                continue
            model = data.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    if provider == "claude":
        for path in (
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".claude" / "settings.local.json",
        ):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            for key in ("model", "defaultModel"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _clean_runtime_value(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "none", "provider-default", "not recorded", "not present"}:
        return ""
    return text


def _resolved_model_name(provider: Any, model: Any) -> str:
    explicit = _clean_runtime_value(model)
    if explicit:
        return explicit
    return _runtime_model_name(_clean_runtime_value(provider)) or ""


def _story_status_sort_key(story: dict[str, Any]) -> tuple[int, str]:
    return (
        {
            "fail": 0,
            "flag": 1,
            "warn": 2,
            "skipped": 3,
            "pass": 4,
        }.get(str(story.get("status") or ""), 5),
        str(story.get("story_id") or ""),
    )


def _ordered_stories(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(stories, key=_story_status_sort_key)


def _methodology_summary(stories: list[dict[str, Any]]) -> dict[str, Any]:
    values = [str(story.get("methodology") or "").strip() for story in stories]
    non_empty = [value for value in values if value]
    unique = list(dict.fromkeys(non_empty))
    uniform = bool(stories) and len(non_empty) == len(stories) and len(unique) == 1
    return {
        "uniform": uniform,
        "value": unique[0] if uniform else "",
        "show_inline": not uniform and bool(non_empty),
    }


def _story_status_label(story: dict[str, Any], methodology_summary: dict[str, Any]) -> str:
    label = _story_verdict_display(story)[0]
    methodology = str(story.get("methodology") or "").strip()
    if methodology_summary.get("show_inline") and methodology:
        return f"{label} · {methodology}"
    return label


def _story_evidence_label(story: dict[str, Any]) -> str:
    has_failure = bool(str(story.get("failure_evidence") or "").strip())
    has_notes = bool(story.get("has_evidence"))
    if has_failure and has_notes:
        return "failure screenshot + notes"
    if has_failure:
        return "failure screenshot"
    if has_notes:
        return "notes"
    return "not provided"


def _truncate_text(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _story_corpus(story: dict[str, Any]) -> str:
    return " ".join(
        [
            str(story.get("story_id") or ""),
            str(story.get("claim") or ""),
            str(story.get("observed_result") or ""),
            str(story.get("summary") or ""),
            str(story.get("evidence") or ""),
            " ".join(str(step) for step in story.get("observed_steps", []) or []),
        ]
    ).lower()


def _visual_name_tokens(name: str) -> set[str]:
    stem = Path(name).stem.lower()
    tokens = {token for token in stem.replace("_", "-").split("-") if token}
    return tokens - {"failure", "bug", "error", "screenshot", "screen", "capture", "image", "img", "state"}


def _visual_match_story(image_name: str, story: dict[str, Any]) -> int:
    image_stem = Path(image_name).stem.lower()
    story_id = str(story.get("story_id") or "").strip().lower()
    failure_name = Path(str(story.get("failure_evidence") or "").strip()).name.lower()
    if failure_name and image_name.lower() == failure_name:
        return 100
    if story_id and (image_stem == story_id or image_stem.startswith(f"{story_id}-")):
        return 90
    tokens = _visual_name_tokens(image_name)
    if not tokens:
        return 0
    corpus = _story_corpus(story)
    score = sum(1 for token in tokens if token in corpus)
    return score


def _visual_caption(item: dict[str, Any], story: dict[str, Any] | None) -> str:
    name = str(item.get("name") or "")
    stem = Path(name).stem.lower()
    failure_name = ""
    if story:
        failure_name = Path(str(story.get("failure_evidence") or "").strip()).name.lower()
    if failure_name and name.lower() == failure_name:
        return "failure captured"
    if any(token in stem for token in ("failure", "bug", "error", "missing", "wrong", "duplicate")):
        return "failure captured"
    if story and story.get("status") in {"fail", "warn"}:
        return "walkthrough"
    return ""


def _coverage_render_data(report: dict[str, Any]) -> dict[str, Any]:
    coverage = dict(report.get("coverage") or {})
    observed = [
        str(item).strip() for item in report.get("coverage_observed", []) or []
        if str(item).strip()
    ]
    gaps = [
        str(item).strip() for item in report.get("coverage_gaps", []) or []
        if str(item).strip()
    ]
    limitations = str(coverage.get("limitations") or "").strip()
    if not limitations:
        limitations = _coverage_footer_line(str(report.get("certifier_mode") or "standard"))
    legacy_tested = [str(item).strip() for item in coverage.get("tested", []) or [] if str(item).strip()]
    legacy_untested = [str(item).strip() for item in coverage.get("untested", []) or [] if str(item).strip()]
    legacy_escaped = [
        str(item).strip()
        for item in coverage.get("escaped_bug_classes", []) or []
        if str(item).strip()
    ]
    legacy_mode = not observed and not gaps and bool(legacy_tested or legacy_untested or legacy_escaped)
    per_run_emitted = coverage.get("per_run_emitted")
    if per_run_emitted is None:
        per_run_emitted = bool(observed or gaps)
    note = ""
    if (
        str(report.get("certifier_mode") or "").strip().lower() == "fast"
        and not bool(per_run_emitted)
        and not legacy_mode
    ):
        note = "Per-run coverage not emitted (fast mode)"
    return {
        "observed": observed,
        "gaps": gaps,
        "limitations": limitations,
        "uniform_methodology": str(coverage.get("uniform_methodology") or "").strip(),
        "legacy_mode": legacy_mode,
        "legacy_tested": legacy_tested,
        "legacy_untested": legacy_untested,
        "legacy_escaped_bug_classes": legacy_escaped,
        "note": note,
    }


def _efficiency_render_data(report: dict[str, Any]) -> dict[str, Any]:
    raw = dict(report.get("efficiency") or {})
    suppressed = bool(raw.get("suppress_browser_stats"))
    stories_total = max(int(report.get("stories_total_count") or report.get("stories_tested") or 0), 0)
    total_browser_calls = max(int(raw.get("total_browser_calls") or 0), 0)
    distinct_sessions = max(int(raw.get("distinct_sessions") or 0), 0)
    avg_calls = (float(total_browser_calls) / stories_total) if stories_total > 0 else 0.0
    verb_counts = {
        str(name): int(count)
        for name, count in (raw.get("verb_counts") or {}).items()
        if str(name).strip() and isinstance(count, int | float)
    }
    top_verbs = sorted(verb_counts.items(), key=lambda item: (-item[1], item[0]))[:4]
    return {
        "stories_total": stories_total,
        "story_label": "story" if stories_total == 1 else "stories",
        "total_browser_calls": total_browser_calls,
        "distinct_sessions": distinct_sessions,
        "avg_calls_per_story": avg_calls,
        "top_verbs": top_verbs,
        "top_verbs_text": ", ".join(f"{name} ({count})" for name, count in top_verbs) or "none recorded",
        "outlier": bool(raw.get("outlier")),
        "outlier_reason": str(raw.get("outlier_reason") or "").strip(),
        "suppressed": suppressed,
    }


def _run_context_display(report: dict[str, Any]) -> dict[str, Any]:
    run_context = dict(report["run_context"])
    provider = _clean_runtime_value(run_context.get("provider"))
    model = _resolved_model_name(run_context.get("provider"), run_context.get("model"))
    effort = _clean_runtime_value(run_context.get("effort"))
    execution_note = _clean_runtime_value(run_context.get("certifier_execution"))
    spec = dict(run_context.get("spec") or {})
    spec_path = _clean_runtime_value(spec.get("path"))
    spec_hash = _clean_runtime_value(spec.get("hash"))
    spec_version = spec.get("version") or 0
    spec_present = bool(spec_path or spec_hash or spec_version)
    provider_parts = []
    if provider:
        provider_parts.append(provider)
    if model:
        provider_parts.append(model)
    if effort:
        provider_parts.append(effort)
    return {
        "run_id": _clean_runtime_value(run_context.get("run_id")),
        "git_branch": _clean_runtime_value(run_context.get("git_branch")),
        "git_commit_sha": _clean_runtime_value(run_context.get("git_commit_sha")),
        "intent_excerpt": _clean_runtime_value(run_context.get("intent_excerpt")),
        "generated_at": _clean_runtime_value(run_context.get("generated_at")),
        "generated_local": _clean_runtime_value(run_context.get("generated_local")),
        "duration_human": _clean_runtime_value(run_context.get("duration_human")),
        "provider_model_effort": " / ".join(provider_parts),
        "provider": provider,
        "model": model,
        "effort": effort,
        "execution_note": execution_note,
        "spec_present": spec_present,
        "spec_text": (
            f"{spec_path or '(path unavailable)'} | version {spec_version or 'unknown'} | {spec_hash or '(hash unavailable)'}"
            if spec_present
            else "not used (run without --spec)"
        ),
    }


def _round_history(
    *,
    certify_rounds: list[dict[str, Any]],
    stories: list[dict[str, Any]],
    outcome: str,
    diagnosis: str,
    total_duration_s: float,
    certifier_cost_usd: float,
    round_timings: list[tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    rounds = list(certify_rounds or [])
    if not rounds:
        rounds = [{
            "round": 1,
            "stories": stories,
            "verdict": outcome == "passed",
            "diagnosis": diagnosis,
            "tested": len(stories),
        }]

    durations: list[float] = []
    if round_timings and len(round_timings) >= len(rounds):
        durations = [max(round(end - start, 1), 0.0) for start, end in round_timings[: len(rounds)]]
    else:
        count = len(rounds) or 1
        per_round = round(float(total_duration_s or 0.0) / count, 1)
        durations = [per_round] * count

    duration_total = sum(durations)
    if certifier_cost_usd > 0 and duration_total > 0:
        raw_costs = [certifier_cost_usd * (duration / duration_total) for duration in durations]
    else:
        per_round_cost = float(certifier_cost_usd or 0.0) / max(len(rounds), 1)
        raw_costs = [per_round_cost] * len(rounds)

    history: list[dict[str, Any]] = []
    for index, round_data in enumerate(rounds):
        round_stories = [
            _normalize_story(story, "standard")
            for story in round_data.get("stories", []) or []
        ]
        counts = _counts_from_stories(round_stories)
        failing_story_ids = (
            [
                story.get("story_id", "")
                for story in round_stories
                if story.get("status") == "fail"
            ]
            if round_stories
            else [str(item) for item in round_data.get("failing_story_ids", []) or [] if str(item)]
        )
        warn_story_ids = (
            [
                story.get("story_id", "")
                for story in round_stories
                if story.get("status") == "warn"
            ]
            if round_stories
            else [str(item) for item in round_data.get("warn_story_ids", []) or [] if str(item)]
        )
        round_verdict = round_data.get("verdict")
        if isinstance(round_verdict, bool):
            verdict_text = "passed" if round_verdict else "failed"
        else:
            result_text = str(round_data.get("result") or "").strip().lower()
            verdict_text = (
                "failed"
                if failing_story_ids or result_text.startswith("fail") or result_text.startswith("not met")
                else "passed"
            )
        round_diagnosis = _diagnosis_text(round_data.get("diagnosis", ""), verdict_text)
        if round_stories:
            tested_count = round_data.get("tested", len(round_stories))
            passed_count = counts["passed_count"]
            failed_count = counts["failed_count"]
            warn_count = counts["warn_count"]
        else:
            tested_count = int(round_data.get("stories_tested", round_data.get("tested", 0)) or 0)
            passed_count = int(round_data.get("stories_passed", 0) or 0)
            warn_count = int(round_data.get("warn_count", 0) or 0)
            failed_count = int(round_data.get("failed_count", 0) or 0)
            if failed_count == 0 and tested_count:
                failed_count = max(tested_count - passed_count - warn_count, len(failing_story_ids))
        duration_value = round_data.get("duration_s")
        explicit_duration = isinstance(duration_value, int | float)
        duration_s = (
            max(float(duration_value), 0.0)
            if explicit_duration
            else durations[index] if index < len(durations) else 0.0
        )
        cost_value = round_data.get("cost_usd", round_data.get("cost"))
        explicit_cost = isinstance(cost_value, int | float)
        cost_usd = (
            max(float(cost_value), 0.0)
            if explicit_cost
            else raw_costs[index] if index < len(raw_costs) else 0.0
        )
        fix_commits = list(round_data.get("fix_commits", []) or [])
        fix_diff_stat = str(round_data.get("fix_diff_stat", "") or "")
        still_failing_after_fix = list(round_data.get("still_failing_after_fix", []) or [])
        subagent_errors = list(round_data.get("subagent_errors", []) or [])
        history.append(
            {
                "round": round_data.get("round", index + 1),
                "verdict": verdict_text,
                "stories_tested": tested_count,
                "passed_count": passed_count,
                "failed_count": failed_count,
                "warn_count": warn_count,
                "failing_story_ids": failing_story_ids,
                "warn_story_ids": warn_story_ids,
                "diagnosis": round_diagnosis,
                "duration_s": duration_s,
                "duration_human": _human_duration(duration_s),
                "cost_usd": round(cost_usd, 4),
                "cost_estimated": not bool(round_timings) and not explicit_cost,
                "fix_commits": fix_commits,
                "fix_diff_stat": fix_diff_stat,
                "still_failing_after_fix": still_failing_after_fix,
                "subagent_errors": subagent_errors,
            }
        )
    return history


def _write_visual_evidence_manifests(
    *,
    evidence_dir: Path | None,
    base_dir: Path,
    stories: list[dict[str, Any]],
    run_id: str,
    session_id: str,
    round_history: list[dict[str, Any]],
    certifier_mode: str,
) -> None:
    """Write a sibling ``<artifact>.manifest.json`` next to each visual artifact.

    Cluster-evidence-trustworthiness #8: Mission Control was discovering
    screenshots/recordings via globbing and rendering only ``name + path
    + href``; the operator could not verify capture time, story, round,
    or run identity. We now write a deterministic sibling manifest at
    proof-of-work generation time so:

    * the UI can validate ``manifest.run_id == record.run_id`` and warn
      on mismatch (someone copied an old screenshot into this run's
      evidence dir, etc.), and
    * downstream tools (audit, replay) can recover the story/round
      context without re-parsing the proof JSON.

    The manifest is best-effort: failures are logged but do not break
    the proof-of-work generation. We deliberately do not overwrite an
    existing manifest with the same SHA (idempotent re-runs).

    NOTE on capture time: the agent SDK doesn't surface a hook we can
    use to write a manifest at the moment a screenshot is taken. Writing
    here, when we know the round + story assignment, is the highest-
    fidelity place we own.
    """
    root = evidence_dir or (base_dir / "evidence")
    if not root.exists() or not root.is_dir():
        return
    last_round = round_history[-1].get("round") if round_history else 1
    artifacts = sorted(root.glob("*.png")) + sorted(root.glob("*.webm"))
    for artifact_path in artifacts:
        manifest_path = artifact_path.with_name(artifact_path.name + ".manifest.json")
        try:
            captured_at = (
                datetime.fromtimestamp(artifact_path.stat().st_mtime, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except OSError:
            captured_at = None
        try:
            sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except OSError:
            sha256 = None
        story_id = ""
        story_status = ""
        for story in stories:
            failure_evidence = Path(str(story.get("failure_evidence") or "").strip()).name.lower()
            if failure_evidence and failure_evidence == artifact_path.name.lower():
                story_id = str(story.get("story_id") or "")
                story_status = str(story.get("status") or "")
                break
        manifest = {
            "schema_version": 1,
            "captured_at": captured_at,
            "run_id": run_id,
            "session_id": session_id,
            "round": last_round,
            "story_id": story_id,
            "story_status": story_status,
            "sha256": sha256,
            "size_bytes": (artifact_path.stat().st_size if artifact_path.exists() else None),
            "viewport": None,  # filled in if/when we hook the capture path
            "browser": None,
            "certifier_mode": certifier_mode,
            "artifact_name": artifact_path.name,
            "kind": "screenshot" if artifact_path.suffix.lower() == ".png" else "recording",
        }
        try:
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to write visual-evidence manifest %s: %s", manifest_path, exc)


def _visual_evidence(
    base_dir: Path,
    evidence_dir: Path | None,
    certifier_mode: str,
    stories: list[dict[str, Any]],
) -> dict[str, Any]:
    def _story_looks_visual(story: dict[str, Any]) -> bool:
        surface = str(story.get("surface") or "").strip().lower()
        methodology = str(story.get("methodology") or "").strip().lower()
        visual_surfaces = {"dom", "screenshot", "video", "localstorage"}
        visual_methods = {"live-ui-events", "javascript-eval", "visual-only"}
        return (
            surface in visual_surfaces
            or methodology in visual_methods
            or Path(str(story.get("failure_evidence") or "").strip()).suffix.lower() == ".png"
        )

    evidence_root = evidence_dir or (base_dir / "evidence")
    screenshots = sorted(evidence_root.glob("*.png")) if evidence_root.exists() else []
    recordings = sorted(evidence_root.glob("*.webm")) if evidence_root.exists() else []
    images = [
        {"name": path.name, "path": str(path), "href": _relative_href(base_dir, path), "kind": "image"}
        for path in screenshots
    ]
    videos = [
        {"name": path.name, "path": str(path), "href": _relative_href(base_dir, path), "kind": "video"}
        for path in recordings
    ]
    visual_items = images + videos
    recording = next((item for item in videos if item["name"] == "recording.webm"), None)
    assigned: dict[str, str] = {}
    for item in visual_items:
        forced_story = next(
            (
                str(story.get("story_id") or "")
                for story in stories
                if Path(str(story.get("failure_evidence") or "").strip()).name.lower() == item["name"].lower()
            ),
            "",
        )
        if forced_story:
            assigned[item["name"]] = forced_story
            continue
        best_story_id = ""
        best_score = 0
        for story in stories:
            score = _visual_match_story(item["name"], story)
            if score > best_score:
                best_score = score
                best_story_id = str(story.get("story_id") or "")
        if best_story_id and best_score > 0:
            assigned[item["name"]] = best_story_id

    buckets: list[dict[str, Any]] = []
    for story in stories:
        bucket_items = []
        for item in visual_items:
            if assigned.get(item["name"]) != story.get("story_id"):
                continue
            bucket_items.append(
                {
                    **item,
                    "caption": _visual_caption(item, story),
                }
            )
        if bucket_items:
            buckets.append(
                {
                    "story_id": story.get("story_id", ""),
                    "status": story.get("status", ""),
                    "items": bucket_items,
                }
            )

    unassigned = [
        {
            **item,
            "caption": _visual_caption(item, None),
        }
        for item in visual_items
        if item["name"] not in assigned and item["name"] != "recording.webm"
    ]
    non_visual_product = not images and not videos and not any(
        _story_looks_visual(story) for story in stories
    )
    if images or videos:
        absence_reason = ""
        visible = True
        suppress_browser_stats = False
    elif non_visual_product:
        absence_reason = ""
        visible = False
        suppress_browser_stats = True
    else:
        absence_reason = f"not collected (mode={certifier_mode})"
        visible = True
        suppress_browser_stats = False
    return {
        "images": images,
        "videos": videos,
        "recording": recording,
        "buckets": buckets,
        "unassigned": unassigned,
        "absence_reason": absence_reason,
        "visible": visible,
        "suppress_browser_stats": suppress_browser_stats,
        "evidence_dir": str(evidence_root),
        "evidence_dir_href": _relative_href(base_dir, evidence_root) if evidence_root.exists() else "",
    }


def _story_proof_rows(stories: list[dict[str, Any]], visual: dict[str, Any]) -> list[dict[str, Any]]:
    """Return per-story proof coverage rows for the report.

    This does not decide pass/fail; it makes evidence quality visible. A
    generic full-walkthrough video is useful context, but story-specific
    screenshots or clips are stronger proof that each intent path was touched.
    """
    by_story: dict[str, list[dict[str, Any]]] = {}
    for bucket in visual.get("buckets", []) or []:
        story_id = str(bucket.get("story_id") or "")
        if not story_id:
            continue
        by_story.setdefault(story_id, []).extend(
            item for item in bucket.get("items", []) or [] if isinstance(item, dict)
        )
    has_general_recording = bool(visual.get("recording"))
    rows: list[dict[str, Any]] = []
    for story in stories:
        story_id = str(story.get("story_id") or "")
        items = by_story.get(story_id, [])
        image_count = sum(1 for item in items if str(item.get("kind") or "") == "image")
        video_count = sum(1 for item in items if str(item.get("kind") or "") == "video")
        if video_count:
            visual_status = f"{video_count} story video{'' if video_count == 1 else 's'}"
        elif image_count:
            visual_status = f"{image_count} story screenshot{'' if image_count == 1 else 's'}"
        elif has_general_recording:
            visual_status = "general walkthrough only"
        else:
            visual_status = "none"
        rows.append(
            {
                "story_id": story_id,
                "status": story.get("status") or "",
                "claim": story.get("claim") or story.get("summary") or story_id,
                "methodology": story.get("methodology") or story.get("interaction_method") or "",
                "surface": story.get("surface_display") or story.get("surface") or "",
                "text_evidence": bool(story.get("has_evidence") or story.get("evidence")),
                "visual_status": visual_status,
            }
        )
    return rows


def _story_visual_items_by_id(visual: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_story: dict[str, list[dict[str, Any]]] = {}
    for bucket in visual.get("buckets", []) or []:
        if not isinstance(bucket, dict):
            continue
        story_id = str(bucket.get("story_id") or "").strip()
        if not story_id:
            continue
        items = [item for item in bucket.get("items", []) or [] if isinstance(item, dict)]
        if items:
            by_story.setdefault(story_id, []).extend(items)
    return by_story


def _story_is_web_ui(story: dict[str, Any]) -> bool:
    surface = str(story.get("surface") or story.get("surface_display") or "").lower()
    methodology = _normalize_methodology(
        str(story.get("methodology") or story.get("interaction_method") or "")
    )
    if any(token in surface for token in ("dom", "browser", "page", "screenshot", "video", "localstorage")):
        return True
    if methodology in {"live-ui-events", "visual-only", "javascript-eval", "browser"}:
        return True
    return _story_claims_ui_behavior(story)


def _story_is_file_or_download(story: dict[str, Any]) -> bool:
    corpus = _story_corpus(story)
    return any(
        token in corpus
        for token in (
            "download",
            "export",
            "pdf",
            "csv",
            "xlsx",
            "file",
            "filename",
            "mime",
            "content-type",
            "bytes",
            "attachment",
        )
    )


def _report_looks_test_only(intent: str, stories: list[dict[str, Any]]) -> bool:
    corpus = " ".join(
        [
            intent,
            " ".join(str(story.get("story_id") or "") for story in stories),
            " ".join(str(story.get("claim") or "") for story in stories),
        ]
    ).lower()
    indicators = (
        "smoke test",
        "regression test",
        "test-only",
        "add test",
        "add tests",
        "test coverage",
        "ci ",
        "docs",
        "documentation",
        "proof/evidence",
        "certification",
    )
    product_indicators = ("build", "feature", "ui", "button", "page", "workflow", "portal")
    if not any(indicator in corpus for indicator in indicators):
        return False
    return not any(indicator in corpus for indicator in product_indicators)


def _demo_app_kind(intent: str, stories: list[dict[str, Any]]) -> str:
    corpus = " ".join([intent, *(_story_corpus(story) for story in stories)]).lower()
    has_web = any(_story_is_web_ui(story) for story in stories) or any(
        token in corpus
        for token in ("web app", "browser", "dashboard", "page", "button", "form", "modal", "download")
    )
    has_file = any(_story_is_file_or_download(story) for story in stories)
    has_cli = any(token in corpus for token in (" cli", "command", "stdout", "stderr", "exit code"))
    has_library = any(token in corpus for token in ("library", "import ", "public api"))
    has_worker = any(token in corpus for token in ("queue", "worker", "pipeline", "batch"))
    has_api = any(token in corpus for token in ("rest api", "http ", "endpoint", "curl", "json response"))
    if has_web and has_file:
        return "mixed"
    if has_web:
        return "web"
    if has_file:
        return "file_export"
    if has_cli:
        return "cli"
    if has_library:
        return "library"
    if has_worker:
        return "worker"
    if has_api:
        return "api"
    return "unknown"


def _primary_demo_item(visual: dict[str, Any]) -> dict[str, Any] | None:
    bucket_items = [
        item
        for bucket in visual.get("buckets", []) or []
        if isinstance(bucket, dict)
        for item in bucket.get("items", []) or []
        if isinstance(item, dict)
    ]
    for item in bucket_items:
        if str(item.get("kind") or "").lower() == "video":
            return item
    if visual.get("recording"):
        return visual["recording"]
    for item in bucket_items:
        if str(item.get("kind") or "").lower() == "image":
            return item
    for item in visual.get("unassigned", []) or []:
        if isinstance(item, dict):
            return item
    return None


def _demo_item_payload(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        "name": item.get("name") or "",
        "kind": item.get("kind") or "",
        "href": item.get("href") or "",
        "caption": item.get("caption") or "",
    }


def _demo_evidence(
    *,
    intent: str,
    certifier_mode: str,
    stories: list[dict[str, Any]],
    visual: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize whether the proof packet demonstrates the task intent.

    This is deliberately higher level than the raw artifact list. It gives
    Mission Control a stable contract for "what should a human look at first?"
    and prevents generic recordings or file counts from masquerading as proof.
    """
    app_kind = _demo_app_kind(intent, stories)
    test_only = _report_looks_test_only(intent, stories)
    mode = str(certifier_mode or "").strip().lower()
    visual_required = app_kind in {"web", "mixed"} and mode != "fast" and not test_only
    file_required = app_kind in {"mixed", "file_export"} and not test_only
    by_story = _story_visual_items_by_id(visual)
    general_recording = bool(visual.get("recording"))
    story_rows: list[dict[str, Any]] = []
    missing_visual = 0
    generic_only = 0
    missing_file = 0
    visual_story_count = 0
    story_video_count = 0
    story_image_count = 0
    for story in stories:
        story_id = str(story.get("story_id") or "").strip()
        items = by_story.get(story_id, [])
        visual_items = [_demo_item_payload(item) for item in items]
        visual_items = [item for item in visual_items if item is not None]
        video_count = sum(1 for item in visual_items if item.get("kind") == "video")
        image_count = sum(1 for item in visual_items if item.get("kind") == "image")
        story_video_count += video_count
        story_image_count += image_count
        story_needs_visual = visual_required and _story_is_web_ui(story)
        story_needs_file = file_required and _story_is_file_or_download(story)
        if story_needs_visual:
            visual_story_count += 1
        evidence_text = str(story.get("evidence") or "").lower()
        file_proof = bool(
            story_needs_file
            and any(token in evidence_text for token in ("pdf", "csv", "download", "filename", "content-type", "bytes", "pdftotext", "mime"))
        )
        if story_needs_visual and not visual_items:
            if general_recording:
                generic_only += 1
            else:
                missing_visual += 1
        if story_needs_file and not file_proof:
            missing_file += 1
        if video_count:
            proof_level = "story video"
        elif image_count:
            proof_level = "story screenshot"
        elif story_needs_visual and general_recording:
            proof_level = "generic recording only"
        elif file_proof:
            proof_level = "file validation"
        elif story.get("has_evidence"):
            proof_level = "text evidence"
        else:
            proof_level = "not recorded"
        story_rows.append(
            {
                "id": story_id,
                "title": story.get("claim") or story.get("summary") or story_id,
                "status": story.get("status") or "",
                "needs_visual": story_needs_visual,
                "needs_file_validation": story_needs_file,
                "visual_items": visual_items,
                "has_text_evidence": bool(story.get("has_evidence")),
                "has_file_validation": file_proof,
                "proof_level": proof_level,
            }
        )

    if mode == "fast":
        demo_required = False
        demo_status = "not_applicable"
        reason = "Fast certification records command/request evidence only; video is intentionally skipped."
    elif test_only:
        demo_required = False
        demo_status = "not_applicable"
        reason = "This run certifies tests, docs, or support work rather than a new product interaction."
    elif not visual_required and not file_required:
        demo_required = False
        demo_status = "not_applicable"
        reason = "This product surface is better proven with command, API, or source evidence than video."
    else:
        demo_required = True
        if visual_required and missing_visual and not general_recording:
            demo_status = "missing"
            reason = "User-facing stories need browser proof, but no matching video or screenshot was recorded."
        elif (visual_required and (missing_visual or generic_only)) or (file_required and missing_file):
            demo_status = "partial"
            pieces = []
            if generic_only:
                pieces.append("some stories only have a generic walkthrough")
            if missing_visual:
                pieces.append("some visual stories lack story-specific media")
            if missing_file:
                pieces.append("some file/export stories lack file validation details")
            reason = "; ".join(pieces) or "Proof is incomplete."
        elif visual_required and visual_story_count and story_video_count == 0:
            demo_status = "partial"
            if general_recording:
                reason = (
                    "Browser video is a generic walkthrough; story-specific visual proof "
                    "is screenshots only."
                )
            else:
                reason = "Visual stories have screenshots but no video walkthrough."
        elif visual_required and not (story_video_count or story_image_count or general_recording):
            demo_status = "missing"
            reason = "No browser visual proof was recorded."
        else:
            demo_status = "strong"
            reason = "Proof maps the task stories to concrete evidence."

    raw_count = sum(1 for artifact in artifacts if artifact.get("present"))
    review_count = sum(1 for artifact in artifacts if artifact.get("present") and artifact.get("label") not in {"session"})
    return {
        "schema_version": 1,
        "app_kind": app_kind,
        "demo_required": demo_required,
        "demo_status": demo_status,
        "demo_reason": reason,
        "primary_demo": _demo_item_payload(_primary_demo_item(visual)),
        "stories": story_rows,
        "counts": {
            "story_videos": story_video_count,
            "story_screenshots": story_image_count,
            "generic_recordings": 1 if general_recording else 0,
            "raw_artifacts": raw_count,
            "review_artifacts": review_count,
        },
    }


def _demo_evidence_gate(demo_evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Return the certification gate implied by structured demo evidence.

    The certifier agent can still emit ``VERDICT: PASS`` after only reading
    tests or source. For standard/thorough user-facing web work, that is not a
    complete proof packet: if a browser/product demo was required and not
    recorded, the run should not silently land as green.
    """
    demo = demo_evidence if isinstance(demo_evidence, dict) else {}
    required = bool(demo.get("demo_required"))
    status = str(demo.get("demo_status") or "").strip().lower()
    reason = str(demo.get("demo_reason") or "").strip()
    if required and status == "missing":
        return {
            "schema_version": 1,
            "status": "fail",
            "blocks_pass": True,
            "reason": reason or "Required product demo proof is missing.",
        }
    if required and status == "partial":
        return {
            "schema_version": 1,
            "status": "warn",
            "blocks_pass": False,
            "reason": reason or "Product demo proof is incomplete.",
        }
    return {
        "schema_version": 1,
        "status": "pass" if required else "not_applicable",
        "blocks_pass": False,
        "reason": reason,
    }


def _append_demo_evidence_gate_diagnosis(diagnosis: str, gate: dict[str, Any]) -> str:
    if not isinstance(gate, dict) or not gate.get("blocks_pass"):
        return diagnosis
    reason = str(gate.get("reason") or "Required product demo proof is missing.").strip()
    note = f"Required demo proof gate failed: {reason}"
    diagnosis = str(diagnosis or "").strip()
    if not diagnosis:
        return note
    if note in diagnosis:
        return diagnosis
    return f"{diagnosis}\n\n{note}"


def _artifacts(
    *,
    report_dir: Path,
    log_dir: Path,
    session_dir: Path,
    spec_path: str,
    evidence_dir: Path | None,
) -> list[dict[str, Any]]:
    artifacts = [
        _artifact_entry(report_dir, "proof-of-work.html", report_dir / "proof-of-work.html", present=True),
        _artifact_entry(report_dir, "proof-of-work.md", report_dir / "proof-of-work.md", present=True),
        _artifact_entry(report_dir, "proof-of-work.json", report_dir / "proof-of-work.json", present=True),
        _artifact_entry(report_dir, "narrative.log", log_dir / "narrative.log"),
        _artifact_entry(report_dir, "messages.jsonl", log_dir / "messages.jsonl"),
        _artifact_entry(report_dir, "runtime.json", session_dir / "runtime.json"),
        _artifact_entry(report_dir, "session", session_dir),
    ]
    if spec_path:
        artifacts.append(_artifact_entry(report_dir, "spec.md", Path(spec_path)))
    root = evidence_dir or (report_dir / "evidence")
    if root.exists():
        artifacts.append(_artifact_entry(report_dir, "evidence", root))
    return artifacts


def _next_actions(
    *,
    outcome: str,
    artifacts: list[dict[str, Any]],
    has_failing_stories: bool = True,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if outcome == "passed":
        actions.extend(
            [
                {"label": "Re-run in standard mode", "command": "otto certify --standard"},
                {"label": "Re-run in thorough mode", "command": "otto certify --thorough"},
            ]
        )
    else:
        if has_failing_stories:
            actions.append({"label": "Jump to failing stories", "href": "#story-details"})
        actions.append({"label": "Retry after fix", "command": "otto improve bugs"})
        actions.append({"label": "Re-run in thorough mode", "command": "otto certify --thorough"})
    return actions


def _one_line_interpretation(outcome: str, warn_count: int, certifier_mode: str) -> str:
    if outcome == "passed" and warn_count > 0:
        return f"Core checks passed in {certifier_mode} mode, but non-blocking warnings still need review."
    if outcome == "passed":
        return f"The product cleared the checks that {certifier_mode} mode is designed to cover."
    return f"The product failed at least one check within the {certifier_mode} mode coverage envelope."


def _show_round_timeline(round_history: list[dict[str, Any]]) -> bool:
    return len(round_history) > 1


def _token_usage_summary(messages_jsonl: Path) -> dict[str, int]:
    """Return total token usage from phase events, falling back to result usage."""
    return total_token_usage_from_phases(phase_token_usage_from_messages(messages_jsonl.parent))


def _token_usage_lines(token_usage: dict[str, int]) -> list[str]:
    if not token_usage:
        return []
    input_tokens = token_usage.get("input_tokens", 0)
    cached_tokens = token_usage.get("cached_input_tokens", 0)
    output_tokens = token_usage.get("output_tokens", 0)
    if cached_tokens:
        input_part = (
            f"{_human_int(input_tokens)} input "
            f"({_human_int(cached_tokens)} cached)"
        )
    else:
        input_part = f"{_human_int(input_tokens)} input"
    return [f"Tokens: {input_part}, {_human_int(output_tokens)} output"]


def _cost_summary(
    certifier_cost_usd: float,
    total_cost_usd: float,
    *,
    token_usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    certifier = round(float(certifier_cost_usd or 0.0), 2)
    total = round(float(total_cost_usd or 0.0), 2)
    token_lines = _token_usage_lines(token_usage or {})
    spend_display = token_lines[0] if token_lines else "Tokens: not recorded"
    if certifier == total:
        lines = [spend_display]
        return {
            "display": spend_display,
            "lines": lines,
            "certifier_equals_total": True,
        }
    return {
        "display": spend_display,
        "lines": [
            spend_display,
            f"Provider-reported cost: certifier ${certifier:.2f}, total ${total:.2f}",
        ],
        "certifier_equals_total": False,
    }


def _overall_diagnosis_summary(report: dict[str, Any]) -> str:
    return str(report.get("diagnosis") or "").strip()


def _redact_report_in_place(value: Any, seen: set[int] | None = None) -> Any:
    seen = seen or set()
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        marker = id(value)
        if marker in seen:
            return value
        seen.add(marker)
        for index, item in enumerate(value):
            value[index] = _redact_report_in_place(item, seen)
        return value
    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            return value
        seen.add(marker)
        for key, item in list(value.items()):
            value[key] = _redact_report_in_place(item, seen)
        return value
    return value


def _build_pow_report_data(
    *,
    project_dir: Path,
    report_dir: Path,
    log_dir: Path,
    run_id: str,
    session_id: str,
    pipeline_mode: str,
    certifier_mode: str,
    outcome: str,
    story_results: list[dict[str, Any]],
    diagnosis: str,
    certify_rounds: list[dict[str, Any]] | None,
    duration_s: float,
    certifier_cost_usd: float,
    total_cost_usd: float,
    intent: str,
    options: Any,
    evidence_dir: Path | None,
    stories_tested: int,
    stories_passed: int,
    coverage_observed: list[str] | None = None,
    coverage_gaps: list[str] | None = None,
    coverage_emitted: bool | None = None,
    metric_value: str = "",
    metric_met: bool | None = None,
    round_timings: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    from otto import paths
    report_story_limit = 200

    generated_dt = datetime.now(timezone.utc)
    generated_iso = generated_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    generated_local = generated_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    all_stories = [_normalize_story(story, certifier_mode) for story in story_results]
    hidden_story_count = max(0, len(all_stories) - report_story_limit)
    visible_stories = all_stories[:report_story_limit]
    ordered_stories = _ordered_stories(visible_stories)
    counts = _counts_from_stories(all_stories)
    failing_story_ids = [
        story.get("story_id", "")
        for story in all_stories
        if story.get("status") == "fail"
    ]
    warn_story_ids = [
        story.get("story_id", "")
        for story in all_stories
        if story.get("status") == "warn"
    ]
    session_dir = paths.session_dir(project_dir, run_id)
    spec_context = _load_spec_context(project_dir, run_id)
    methodology_summary = _methodology_summary(visible_stories)
    visual = _visual_evidence(report_dir, evidence_dir, certifier_mode, ordered_stories)
    coverage_observed = [
        str(item).strip() for item in (coverage_observed or []) if str(item).strip()
    ]
    coverage_gaps = [
        str(item).strip() for item in (coverage_gaps or []) if str(item).strip()
    ]
    efficiency = summarize_browser_efficiency(
        log_dir / "messages.jsonl",
        certifier_mode=certifier_mode,
        story_ids=[str(story.get("story_id") or "").strip() for story in story_results],
        story_claims={
            str(story.get("story_id") or "").strip(): str(
                story.get("claim") or story.get("summary") or ""
            ).strip()
            for story in story_results
            if str(story.get("story_id") or "").strip()
        },
    )
    if visual.get("suppress_browser_stats"):
        efficiency["suppress_browser_stats"] = True
    round_history = _round_history(
        certify_rounds=certify_rounds or [],
        stories=story_results,
        outcome=outcome,
        diagnosis=diagnosis,
        total_duration_s=duration_s,
        certifier_cost_usd=certifier_cost_usd,
        round_timings=round_timings,
    )
    # Cluster-evidence-trustworthiness #8: write a sibling manifest next
    # to each screenshot/recording so Mission Control can validate that
    # the visual evidence belongs to this run/round/story instead of
    # trusting a glob-and-pray discovery path.
    try:
        _write_visual_evidence_manifests(
            evidence_dir=evidence_dir,
            base_dir=report_dir,
            stories=story_results,
            run_id=run_id,
            session_id=session_id,
            round_history=round_history,
            certifier_mode=certifier_mode,
        )
    except Exception as exc:  # pragma: no cover — defensive, never block report
        logger.warning("visual-evidence manifest write failed: %s", exc)
    artifacts = _artifacts(
        report_dir=report_dir,
        log_dir=log_dir,
        session_dir=session_dir,
        spec_path=str(spec_context.get("spec_path") or ""),
        evidence_dir=evidence_dir,
    )
    demo_evidence = _demo_evidence(
        intent=intent,
        certifier_mode=certifier_mode,
        stories=ordered_stories,
        visual=visual,
        artifacts=artifacts,
    )
    evidence_gate = _demo_evidence_gate(demo_evidence)
    effective_outcome = "failed" if outcome == "passed" and evidence_gate.get("blocks_pass") else outcome
    effective_diagnosis = _append_demo_evidence_gate_diagnosis(diagnosis, evidence_gate)
    token_usage = _token_usage_summary(log_dir / "messages.jsonl")
    cost_summary = _cost_summary(
        certifier_cost_usd,
        total_cost_usd,
        token_usage=token_usage,
    )
    has_warnings = counts["warn_count"] > 0 or evidence_gate.get("status") == "warn"
    verdict_label = (
        "PASS with warnings"
        if effective_outcome == "passed" and has_warnings
        else ("PASS" if effective_outcome == "passed" else "FAIL")
    )
    data = {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": generated_iso,
        "generated_local": generated_local,
        "generator": _GENERATOR,
        "outcome": effective_outcome,
        "agent_outcome": outcome,
        "verdict_label": verdict_label,
        "one_line_interpretation": redact_text(
            _one_line_interpretation(effective_outcome, counts["warn_count"], certifier_mode)
        ),
        "duration_s": duration_s,
        "duration_human": _human_duration(duration_s),
        "certifier_cost_usd": round(float(certifier_cost_usd or 0.0), 4),
        "total_cost_usd": round(float(total_cost_usd or 0.0), 4),
        "token_usage": token_usage,
        "pipeline_mode": pipeline_mode,
        "mode": pipeline_mode,
        "certifier_mode": certifier_mode,
        "metric_value": metric_value,
        "metric_met": metric_met,
        "stories_tested": stories_tested or len(all_stories),
        "stories_passed": stories_passed or (counts["passed_count"] + counts["warn_count"]),
        "passed_count": counts["passed_count"],
        "failed_count": counts["failed_count"],
        "warn_count": counts["warn_count"],
        "skipped_count": counts["skipped_count"],
        "flag_count": counts["flag_count"],
        "diagnosis": redact_text(_diagnosis_text(effective_diagnosis, effective_outcome)),
        "failing_story_ids": failing_story_ids,
        "warn_story_ids": warn_story_ids,
        "stories_hidden_count": hidden_story_count,
        "stories_total_count": len(all_stories),
        "round_history": round_history,
        "show_round_timeline": _show_round_timeline(round_history),
        "cost_summary": cost_summary,
        "project": {
            "name": _project_name(project_dir),
            "path": str(project_dir),
        },
        "coverage": {
            "limitations": _coverage_footer_line(certifier_mode),
            "per_run_emitted": bool(coverage_emitted),
            "uniform_methodology": methodology_summary["value"] if methodology_summary["uniform"] else "",
        },
        "coverage_observed": coverage_observed,
        "coverage_gaps": coverage_gaps,
        "efficiency": efficiency,
        "run_context": {
            "run_id": run_id,
            "session_id": session_id,
            "project_name": _project_name(project_dir),
            "project_path": str(project_dir),
            "git_branch": _safe_git(project_dir, "branch", "--show-current"),
            "git_commit_sha": _safe_git(project_dir, "rev-parse", "--short", "HEAD"),
            "intent_excerpt": redact_text(_intent_excerpt(intent)),
            "spec": {
                "path": str(spec_context.get("spec_path") or ""),
                "version": spec_context.get("spec_version") or 0,
                "hash": str(spec_context.get("spec_hash") or ""),
            },
            "provider": getattr(options, "provider", None) or "",
            "model": _resolved_model_name(getattr(options, "provider", None), getattr(options, "model", None)),
            "effort": getattr(options, "effort", None),
            "certifier_execution": (
                "certifier verified within build agent session; provider/model/effort shown here are the build agent settings"
                if pipeline_mode == "agentic_v3"
                else ""
            ),
            "generated_at": generated_iso,
            "generated_local": generated_local,
            "duration_human": _human_duration(duration_s),
            "certifier_cost_usd": round(float(certifier_cost_usd or 0.0), 4),
            "total_cost_usd": round(float(total_cost_usd or 0.0), 4),
        },
        "artifacts": artifacts,
        "stories": ordered_stories,
        "stories_ordered": ordered_stories,
        "story_methodology_summary": methodology_summary,
        "visual_evidence": visual,
        "proof_coverage": _story_proof_rows(ordered_stories, visual),
        "demo_evidence": demo_evidence,
        "evidence_gate": evidence_gate,
    }
    data["next_actions"] = _next_actions(
        outcome=effective_outcome,
        artifacts=artifacts,
        has_failing_stories=any(not story.get("passed") for story in story_results),
    )
    _redact_report_in_place(data)
    return data


def _render_artifact_links_md(artifacts: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        if artifact.get("present"):
            lines.append(f"- [{artifact['label']}]({artifact['href']})")
    return lines


def _demo_status_label(status: Any) -> str:
    return {
        "strong": "Strong",
        "partial": "Partial",
        "missing": "Missing",
        "not_applicable": "Not required",
    }.get(str(status or "").strip().lower(), "Unknown")


def _demo_kind_label(kind: Any) -> str:
    return {
        "web": "Web UI",
        "mixed": "Web UI + file/export",
        "file_export": "File/export",
        "api": "API",
        "cli": "CLI",
        "library": "Library",
        "worker": "Worker/service",
        "unknown": "Unknown",
    }.get(str(kind or "").strip().lower(), str(kind or "Unknown"))


def _render_pow_markdown(
    report: dict[str, Any] | list[dict[str, Any]],
    *,
    outcome: str | None = None,
    duration: float | None = None,
    cost: float | None = None,
    stories_passed: int | None = None,
    stories_tested: int | None = None,
) -> str:
    if isinstance(report, list):
        legacy_stories = [_normalize_story_result(story) for story in report]
        md_lines = [
            "# Proof-of-Work Certification Report",
            "",
            f"> **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"> **Outcome:** {outcome or 'failed'}",
            f"> **Duration:** {float(duration or 0.0):.0f}s",
            f"> **Cost:** ${float(cost or 0.0):.2f}",
            f"> **Stories:** {stories_passed or 0}/{stories_tested or len(legacy_stories)}",
            "",
        ]
        for story in legacy_stories:
            verdict, icon, _status_class = _story_verdict_display(story)
            md_lines.append(
                f"- **{icon} {verdict}** {story.get('story_id', '')}: {story.get('summary', '')}"
            )
        md_lines.append("")
        return "\n".join(md_lines)

    stories = report.get("stories_ordered") or _ordered_stories(report["stories"])
    methodology_summary = report.get("story_methodology_summary") or _methodology_summary(stories)
    coverage = _coverage_render_data(report)
    efficiency = _efficiency_render_data(report)
    visual = report["visual_evidence"]
    run_context = _run_context_display(report)
    lines = [
        f"# Otto Certification Report — {report['project']['name']} — {report['verdict_label']} ({report['certifier_mode']})",
        "",
        "## Hero",
        "",
        f"- Verdict: {report['verdict_label']}",
        f"- Mode: `{report['certifier_mode']}`",
        (
            f"- Stories: {report['passed_count']} pass, {report['failed_count']} fail, "
            f"{report['warn_count']} warn, {report.get('skipped_count', 0)} skipped, "
            f"{report.get('flag_count', 0)} flagged"
        ),
        "- Full report: [proof-of-work.html](proof-of-work.html)",
    ]
    for action in report["next_actions"]:
        if action.get("command"):
            lines.append(f"- Next: {action['label']} via `{action['command']}`")
        else:
            lines.append(f"- Next: {action['label']} in the HTML report")

    demo = dict(report.get("demo_evidence") or {})
    if demo:
        primary = demo.get("primary_demo") if isinstance(demo.get("primary_demo"), dict) else None
        lines.extend(
            [
                "",
                "## Demo Proof",
                "",
                f"- Status: {_demo_status_label(demo.get('demo_status'))}",
                f"- Surface: {_demo_kind_label(demo.get('app_kind'))}",
                f"- Required: {'yes' if demo.get('demo_required') else 'no'}",
                f"- Reason: {demo.get('demo_reason') or 'not recorded'}",
            ]
        )
        if primary and primary.get("href"):
            lines.append(f"- Primary demo: [{primary.get('name') or primary.get('kind')}]({primary.get('href')})")
        story_demos = [row for row in demo.get("stories", []) or [] if isinstance(row, dict)]
        if story_demos:
            lines.extend(
                [
                    "",
                    "| Story | Proof | File validation | Text evidence |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for row in story_demos:
                lines.append(
                    f"| {row.get('id', '')} | {row.get('proof_level', 'not recorded')} | "
                    f"{'yes' if row.get('has_file_validation') else 'no'} | "
                    f"{'yes' if row.get('has_text_evidence') else 'no'} |"
                )

    lines.extend(
        [
            "",
            "## Story Summary",
            "",
            "| Story | Status | Surface | Key finding | Evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for story in stories:
        lines.append(
            f"| {story['story_id']} | {_story_status_label(story, methodology_summary)} | {story['surface_display']} | "
            f"{_truncate_text(story['key_finding']) if story['status'] in {'fail', 'warn'} else '—'} | {_story_evidence_label(story)} |"
        )
    if report.get("stories_hidden_count"):
        lines.append("")
        lines.append(
            f"_Showing first {len(stories)} stories. "
            f"{report['stories_hidden_count']} more not shown._"
        )

    proof_rows = list(report.get("proof_coverage") or [])
    if proof_rows:
        lines.extend(
            [
                "",
                "## Intent Proof Matrix",
                "",
                "| Story | Status | Methodology | Visual proof | Text evidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in proof_rows:
            lines.append(
                f"| {row.get('story_id', '')} | {row.get('status', '')} | "
                f"{row.get('methodology', '') or row.get('surface', '') or 'not specified'} | "
                f"{row.get('visual_status', 'none')} | {'yes' if row.get('text_evidence') else 'no'} |"
            )

    if report.get("diagnosis"):
        lines.extend(["", "## Diagnosis", "", f"- {report['diagnosis']}"])

    lines.extend(
        [
            "",
            "## Story Details",
            "",
        ]
    )
    for story in _ordered_stories(stories):
        lines.extend(
            [
                f"### {story['story_id']} ({_story_status_label(story, methodology_summary)})",
                "",
                f"- Claim: {story['claim'] or 'not provided'}",
                f"- Observed Result: {story['observed_result'] or 'not provided'}",
                f"- Surface: {story['surface_display']}",
            ]
        )
        if story["observed_steps"]:
            lines.append(f"- Observed Steps: {'; '.join(story['observed_steps'])}")
        else:
            lines.append("- Observed Steps: not provided")
        if story.get("failure_evidence"):
            lines.append(f"- Failure Screenshot: {Path(str(story['failure_evidence'])).name}")
        if story["methodology_caveat"]:
            lines.append(f"- Methodology Caveat: {story['methodology_caveat']}")
        if story["has_evidence"]:
            lines.append("- Evidence:")
            lines.append("```text")
            lines.append(story["evidence"])
            lines.append("```")
        else:
            lines.append("- Evidence: not provided by certifier.")
        lines.append("")

    if visual.get("visible", True):
        lines.extend(["## Visual Evidence", ""])
        if visual["recording"]:
            lines.append(f"- Recording: [{visual['recording']['name']}]({visual['recording']['href']})")
        image_items = []
        for bucket in visual["buckets"]:
            for item in bucket["items"]:
                media = "Video" if item.get("kind") == "video" else "Image"
                label = f"{bucket['story_id']}: {media} {item['name']}"
                image_items.append(f"- {label} ({bucket['status']}): [{item['name']}]({item['href']})")
        for item in visual["unassigned"]:
            media = "Video" if item.get("kind") == "video" else "Image"
            image_items.append(f"- Unassigned {media}: [{item['name']}]({item['href']})")
        if image_items:
            lines.extend(image_items)
        else:
            lines.append(f"- Visual evidence: {visual['absence_reason']}")

    if not efficiency.get("suppressed"):
        lines.extend(["", "## Efficiency", ""])
        lines.append(
            f"- Total browser calls: {efficiency['total_browser_calls']} across "
            f"{efficiency['stories_total']} {efficiency['story_label']} "
            f"({efficiency['avg_calls_per_story']:.1f} per story)"
        )
        lines.append(f"- Distinct browser sessions: {efficiency['distinct_sessions']}")
        lines.append(f"- Top verbs: {efficiency['top_verbs_text']}")
        if efficiency["outlier"] and efficiency["outlier_reason"]:
            lines.append(f"- ⚠ Efficiency note: {efficiency['outlier_reason']}")

    lines.extend(["", "## Coverage and Limitations", ""])
    if coverage["legacy_mode"]:
        if coverage["legacy_tested"]:
            lines.append("### Legacy Tested Coverage")
            lines.extend(f"- {item}" for item in coverage["legacy_tested"])
            lines.append("")
        if coverage["legacy_untested"]:
            lines.append("### Legacy Untested Coverage")
            lines.extend(f"- {item}" for item in coverage["legacy_untested"])
            lines.append("")
        if coverage["legacy_escaped_bug_classes"]:
            lines.append("### Legacy Escaped Bug Classes")
            lines.extend(f"- {item}" for item in coverage["legacy_escaped_bug_classes"])
            lines.append("")
    else:
        lines.append("### What this run actually exercised")
        if coverage["observed"]:
            lines.extend(f"- {item}" for item in coverage["observed"])
        elif coverage["note"]:
            lines.append(f"- {coverage['note']}")
        else:
            lines.append("- Not recorded.")
        lines.append("")
        lines.append("### What this run did NOT cover")
        if coverage["gaps"]:
            lines.extend(f"- {item}" for item in coverage["gaps"])
        elif coverage["note"]:
            lines.append(f"- {coverage['note']}")
        else:
            lines.append("- Not recorded.")
        lines.append("")
    lines.append(f"- {coverage['limitations']}")
    if coverage.get("uniform_methodology"):
        lines.append(f"- All stories verified via {coverage['uniform_methodology']}.")

    lines.extend(["", "## Run Context", ""])
    if run_context["run_id"]:
        lines.append(f"- Run ID: `{run_context['run_id']}`")
    if run_context["git_branch"] or run_context["git_commit_sha"]:
        lines.append(
            f"- Git: `{run_context['git_branch'] or 'unknown'}` / `{run_context['git_commit_sha'] or 'unknown'}`"
        )
    if run_context["intent_excerpt"]:
        lines.append(f"- Intent Excerpt: {run_context['intent_excerpt']}")
    lines.append(f"- Spec: {run_context['spec_text']}")
    if run_context["execution_note"]:
        lines.append(f"- Certifier Execution: {run_context['execution_note']}")
    if run_context["provider_model_effort"]:
        lines.append(f"- Provider / Model / Effort: {run_context['provider_model_effort']}")
    if run_context["generated_at"] or run_context["generated_local"]:
        lines.append(
            f"- Generated: {run_context['generated_at'] or run_context['generated_local']}"
        )
    if run_context["duration_human"]:
        lines.append(f"- Duration: {run_context['duration_human']}")
    lines.extend(f"- {line}" for line in report["cost_summary"]["lines"])

    lines.extend(
        [
            "",
            "## Artifacts & Metadata",
            "",
            f"- Schema version: `{report['schema_version']}`",
            f"- Generator: `{report['generator']}`",
        ]
    )
    session_id = _clean_runtime_value(report["run_context"].get("session_id"))
    if session_id:
        lines.append(f"- Session ID: `{session_id}`")
    lines.extend(_render_artifact_links_md(report["artifacts"]))
    return "\n".join(lines)


def _render_pow_html(report: dict[str, Any]) -> str:
    stories = report.get("stories_ordered") or _ordered_stories(report["stories"])
    coverage = _coverage_render_data(report)
    efficiency = _efficiency_render_data(report)
    run_context = _run_context_display(report)
    visual = report["visual_evidence"]
    methodology_summary = report.get("story_methodology_summary") or _methodology_summary(stories)
    has_evidence = any(story["has_evidence"] for story in stories)
    banner_class = "fail" if report["outcome"] == "failed" else ("warn" if report["warn_count"] > 0 else "pass")
    title = f"Otto Certification Report — {report['project']['name']} — {report['verdict_label']} ({report['certifier_mode']})"

    def esc(value: Any) -> str:
        return html.escape(str(value))

    def render_story_fields(story: dict[str, Any], *, compressed: bool) -> list[str]:
        parts = [
            "<div class='story-fields'>",
            f"<div class='story-field'><strong>Claim</strong><div>{esc(story['claim'] or 'not provided')}</div></div>",
            f"<div class='story-field'><strong>Observed Result</strong><div>{esc(story['observed_result'] or 'not provided')}</div></div>",
        ]
        if not compressed:
            parts.append("<div class='story-field'><strong>Observed Steps</strong>")
            if story["observed_steps"]:
                parts.append("<ul class='story-steps'>")
                for step in story["observed_steps"]:
                    parts.append(f"<li>{esc(step)}</li>")
                parts.append("</ul>")
            else:
                parts.append("<div>not provided</div>")
            parts.append("</div>")
            parts.append(f"<div class='story-field'><strong>Surface</strong><div>{esc(story['surface_display'])}</div></div>")
            if not methodology_summary.get("uniform") and story.get("methodology"):
                parts.append(f"<div class='story-field'><strong>Methodology</strong><div>{esc(story['methodology_display'])}</div></div>")
            if story.get("failure_evidence"):
                parts.append(
                    f"<div class='story-field'><strong>Failure Screenshot</strong><div>{esc(Path(str(story['failure_evidence'])).name)}</div></div>"
                )
        parts.append("</div>")
        return parts

    def render_story_article(story: dict[str, Any], *, compressed: bool) -> list[str]:
        verdict_label, verdict_icon, _status_class = _story_verdict_display(story)
        parts = [
            f"<article class='story {esc(story['status'])}'>",
            "<div class='story-header'>",
            f"<span class='badge {esc(story['status'])}'>{esc(f'{verdict_icon} {verdict_label}')}</span>",
            f"<strong>{esc(story['story_id'])}</strong>",
            "</div>",
        ]
        parts.extend(render_story_fields(story, compressed=compressed))
        if story["methodology_caveat"]:
            parts.append(f"<div class='note warn'>{esc(story['methodology_caveat'])}</div>")
        if not compressed and story["residual_risk"]:
            parts.append(f"<div class='note'>{esc(story['residual_risk'])}</div>")
        if story["has_evidence"]:
            toggle_label = "Evidence summary" if compressed else "Toggle evidence"
            parts.extend(
                [
                    "<div class='evidence'>",
                    f"<button class='evidence-toggle' data-story-id='{esc(story['story_id'])}'>{toggle_label}</button>",
                    f"<div class='evidence-content'>{esc(story['evidence'])}</div>",
                    "</div>",
                ]
            )
        else:
            parts.append("<div class='note'>Evidence: not provided by certifier.</div>")
        parts.append("</article>")
        return parts

    def render_visual_item(item: dict[str, Any]) -> list[str]:
        caption = f" — {item['caption']}" if item.get("caption") else ""
        href = esc(item.get("href", ""))
        name = esc(item.get("name", ""))
        kind = str(item.get("kind") or "").lower()
        parts = [
            "<div class='visual-item'>",
            f"<a href='{href}'>{name}</a>{esc(caption)}",
        ]
        if kind == "video" or str(item.get("name") or "").lower().endswith(".webm"):
            parts.append(f"<video controls><source src='{href}' type='video/webm'></video>")
        else:
            parts.append(f"<img src='{href}' alt='{name}'>")
        parts.append("</div>")
        return parts

    html_lines = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>{esc(title)}</title>",
        "<style>",
        "* { box-sizing: border-box; }",
        "body { font-family: system-ui, -apple-system, sans-serif; max-width: 1100px; margin: 0 auto; padding: 2rem 1.5rem 3rem; background: #f8fafc; color: #0f172a; line-height: 1.55; }",
        "h1, h2, h3 { margin: 0; }",
        "a { color: #0f766e; text-decoration: none; }",
        "a:hover { text-decoration: underline; }",
        ".hero { border: 1px solid #cbd5e1; border-radius: 16px; padding: 1.5rem; background: white; margin-bottom: 1rem; }",
        ".hero h1 { font-size: 1.8rem; margin-bottom: 0.75rem; }",
        ".outcome-banner { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; padding: 0.9rem 1rem; border-radius: 12px; font-weight: 700; margin-bottom: 1rem; }",
        ".outcome-banner.pass { background: #dcfce7; color: #166534; border: 1px solid #22c55e; }",
        ".outcome-banner.fail { background: #fee2e2; color: #991b1b; border: 1px solid #ef4444; }",
        ".outcome-banner.warn { background: #fef3c7; color: #92400e; border: 1px solid #f59e0b; }",
        ".hero-actions { display: flex; flex-wrap: wrap; gap: 0.7rem; margin-top: 1rem; }",
        ".action-link, .action-code { display: inline-flex; align-items: center; gap: 0.45rem; border: 1px solid #cbd5e1; background: #f8fafc; border-radius: 999px; padding: 0.45rem 0.8rem; color: #0f172a; font-size: 0.92rem; }",
        ".meta-grid, .run-grid, .coverage-grid { display: grid; gap: 0.9rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }",
        ".card { border: 1px solid #dbe4ee; border-radius: 14px; background: white; padding: 1rem 1.1rem; margin-top: 1rem; }",
        ".card h2 { font-size: 1.1rem; margin-bottom: 0.85rem; }",
        ".demo-proof { border-color: #99f6e4; background: #f0fdfa; }",
        ".demo-proof-head { display: grid; gap: 0.75rem; grid-template-columns: minmax(0, 1fr) auto; align-items: start; }",
        ".demo-proof-status { display: inline-flex; align-items: center; border-radius: 999px; padding: 0.28rem 0.65rem; font-weight: 800; border: 1px solid #99f6e4; background: white; color: #0f766e; }",
        ".demo-proof-status.partial, .demo-proof-status.missing { border-color: #fbbf24; color: #92400e; }",
        ".demo-proof-status.missing { border-color: #fca5a5; color: #991b1b; }",
        ".demo-primary { margin-top: 1rem; }",
        ".demo-primary video, .demo-primary img { width: 100%; max-height: 520px; object-fit: contain; border-radius: 10px; border: 1px solid #cbd5e1; background: #0f172a; }",
        ".meta-label { display: block; font-size: 0.8rem; color: #475569; text-transform: uppercase; letter-spacing: 0.04em; }",
        ".meta-value { font-weight: 700; font-size: 1.05rem; }",
        ".badge { display: inline-block; padding: 0.2rem 0.55rem; border-radius: 999px; font-size: 0.78rem; font-weight: 700; }",
        ".badge.pass { background: #dcfce7; color: #166534; }",
        ".badge.fail { background: #fee2e2; color: #991b1b; }",
        ".badge.warn { background: #fef3c7; color: #92400e; }",
        ".badge.skipped { background: #e2e8f0; color: #334155; }",
        ".badge.flag { background: #fde68a; color: #92400e; }",
        ".table { width: 100%; border-collapse: collapse; }",
        ".table th, .table td { text-align: left; padding: 0.65rem; border-top: 1px solid #e2e8f0; vertical-align: top; }",
        ".table th { color: #475569; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.04em; }",
        ".story { border: 1px solid #dbe4ee; border-left-width: 6px; border-radius: 14px; background: white; padding: 1rem 1.1rem; margin-top: 1rem; }",
        ".story.pass { border-left-color: #22c55e; }",
        ".story.fail { border-left-color: #ef4444; }",
        ".story.warn { border-left-color: #f59e0b; }",
        ".story.skipped { border-left-color: #64748b; }",
        ".story.flag { border-left-color: #d97706; }",
        ".story-header { display: flex; gap: 0.8rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.7rem; }",
        ".story-fields { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }",
        ".story-field strong { display: block; font-size: 0.82rem; color: #475569; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 0.25rem; }",
        ".story-steps { margin: 0; padding-left: 1.1rem; }",
        ".evidence { margin-top: 0.9rem; }",
        ".evidence-toggle { border: 1px solid #94a3b8; background: #f8fafc; border-radius: 8px; padding: 0.35rem 0.7rem; cursor: pointer; }",
        ".evidence-content { display: none; margin-top: 0.6rem; background: #0f172a; color: #e2e8f0; border-radius: 10px; padding: 0.9rem; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.84rem; }",
        ".diagnosis { border: 1px solid #fdba74; background: #fff7ed; }",
        "details { border: 1px solid #cbd5e1; border-radius: 12px; background: white; padding: 0.8rem 1rem; }",
        "summary { cursor: pointer; font-weight: 700; }",
        ".round-item { border-top: 1px solid #e2e8f0; padding-top: 0.8rem; margin-top: 0.8rem; }",
        ".note { margin-top: 0.5rem; padding: 0.7rem 0.8rem; background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 10px; color: #475569; }",
        ".note.warn { border-style: solid; border-color: #f59e0b; background: #fff7ed; color: #9a3412; }",
        ".visual-group { margin-top: 1rem; }",
        ".visual-group h3 { margin-bottom: 0.6rem; }",
        ".visual-grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }",
        ".visual-item { border: 1px solid #dbe4ee; border-radius: 12px; background: #fff; padding: 0.8rem; }",
        ".visual-item img, .visual-item video { width: 100%; border-radius: 8px; margin-top: 0.5rem; }",
        ".efficiency-list { margin: 0; padding-left: 1.1rem; }",
        ".efficiency-list li + li { margin-top: 0.35rem; }",
        ".artifact-links { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.75rem; }",
        ".footer { margin-top: 1rem; font-size: 0.9rem; color: #475569; }",
        "</style>",
    ]
    if has_evidence:
        html_lines.extend(
            [
                "<script>",
                "document.addEventListener('DOMContentLoaded', function () {",
                "  document.querySelectorAll('.evidence-toggle').forEach(function (button) {",
                "    button.addEventListener('click', function () {",
                "      const evidence = button.parentElement.querySelector('.evidence-content');",
                "      if (!evidence) {",
                "        return;",
                "      }",
                "      evidence.style.display = evidence.style.display === 'block' ? 'none' : 'block';",
                "    });",
                "  });",
                "});",
                "</script>",
            ]
        )
    html_lines.extend(["</head>", "<body>"])

    html_lines.extend(
        [
            "<section class='hero'>",
            f"<h1>{esc(title)}</h1>",
            f"<div class='outcome-banner {banner_class}'>",
            f"<span>{esc(report['verdict_label'])}</span>",
            (
                f"<span>{report['passed_count']} pass / {report['failed_count']} fail / "
                f"{report['warn_count']} warn / {report.get('skipped_count', 0)} skipped / "
                f"{report.get('flag_count', 0)} flagged</span>"
            ),
            f"<span>{esc(report['certifier_mode'])}</span>",
            "</div>",
            "<div class='meta-grid'>",
            f"<div><span class='meta-label'>Duration</span><span class='meta-value'>{esc(report['duration_human'])}</span></div>",
            f"<div><span class='meta-label'>Generated</span><span class='meta-value'>{esc(report['generated_local'])}</span></div>",
            f"<div><span class='meta-label'>Certifier Mode</span><span class='meta-value'>{esc(report['certifier_mode'])}</span></div>",
            f"<div><span class='meta-label'>Spend</span><span class='meta-value'>{esc(report['cost_summary']['display'])}</span></div>",
            "</div>",
            "<div class='hero-actions'>",
        ]
    )
    for action in report["next_actions"]:
        if action.get("command"):
            html_lines.append(f"<span class='action-code'>{esc(action['label'])}: <code>{esc(action['command'])}</code></span>")
        else:
            html_lines.append(f"<a class='action-link' href='{esc(action['href'])}'>{esc(action['label'])}</a>")
    html_lines.extend(
        [
            "</div>",
            "</section>",
        ]
    )

    demo = dict(report.get("demo_evidence") or {})
    if demo:
        status = str(demo.get("demo_status") or "unknown").strip().lower()
        primary = demo.get("primary_demo") if isinstance(demo.get("primary_demo"), dict) else None
        html_lines.extend(
            [
                "<section class='card demo-proof'>",
                "<div class='demo-proof-head'>",
                "<div>",
                "<h2>Demo Proof</h2>",
                f"<div>{esc(demo.get('demo_reason') or 'No demo proof note was recorded.')}</div>",
                "</div>",
                f"<span class='demo-proof-status {esc(status)}'>{esc(_demo_status_label(status))} · {esc(_demo_kind_label(demo.get('app_kind')))}</span>",
                "</div>",
            ]
        )
        if primary and primary.get("href"):
            href = esc(primary.get("href") or "")
            name = esc(primary.get("name") or "primary demo")
            kind = str(primary.get("kind") or "").lower()
            html_lines.extend(["<div class='demo-primary'>", f"<a href='{href}'>{name}</a>"])
            if kind == "video" or name.lower().endswith((".webm", ".mp4", ".mov", ".m4v")):
                mime = "video/mp4" if name.lower().endswith((".mp4", ".m4v")) else "video/webm"
                html_lines.append(f"<video controls><source src='{href}' type='{mime}'></video>")
            elif kind == "image" or name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                html_lines.append(f"<img src='{href}' alt='{name}'>")
            html_lines.append("</div>")
        story_demos = [row for row in demo.get("stories", []) or [] if isinstance(row, dict)]
        if story_demos:
            html_lines.extend(
                [
                    "<table class='table'>",
                    "<thead><tr><th>Story</th><th>Proof</th><th>File validation</th><th>Text evidence</th></tr></thead>",
                    "<tbody>",
                ]
            )
            for row in story_demos:
                html_lines.append(
                    "<tr>"
                    f"<td>{esc(row.get('title') or row.get('id') or '')}</td>"
                    f"<td>{esc(row.get('proof_level') or 'not recorded')}</td>"
                    f"<td>{'yes' if row.get('has_file_validation') else 'no'}</td>"
                    f"<td>{'yes' if row.get('has_text_evidence') else 'no'}</td>"
                    "</tr>"
                )
            html_lines.extend(["</tbody>", "</table>"])
        html_lines.append("</section>")

    html_lines.extend(
        [
            "<section class='card'>",
            "<h2>Story Summary</h2>",
            "<table class='table'>",
            "<thead><tr><th>Story</th><th>Status</th><th>Surface</th><th>Key Finding</th><th>Evidence</th></tr></thead>",
            "<tbody>",
        ]
    )
    for story in stories:
        verdict_label, verdict_icon, _status_class = _story_verdict_display(story)
        html_lines.append(
            "<tr>"
            f"<td>{esc(story['story_id'])}</td>"
            f"<td><span class='badge {esc(story['status'])}'>{esc(f'{verdict_icon} {verdict_label}')}</span></td>"
            f"<td>{esc(story['surface_display'])}</td>"
            f"<td>{esc(_truncate_text(story['key_finding']) if story['status'] in {'fail', 'warn'} else '—')}</td>"
            f"<td>{esc(_story_evidence_label(story))}</td>"
            "</tr>"
        )
    html_lines.extend(["</tbody>", "</table>", "</section>"])
    if report.get("stories_hidden_count"):
        html_lines.extend(
            [
                "<section class='card'>",
                f"<div class='note'>Showing first {len(stories)} stories. "
                f"{report['stories_hidden_count']} more not shown.</div>",
                "</section>",
            ]
        )

    proof_rows = list(report.get("proof_coverage") or [])
    if proof_rows:
        html_lines.extend(
            [
                "<section class='card'>",
                "<h2>Intent Proof Matrix</h2>",
                "<table class='table'>",
                "<thead><tr><th>Story</th><th>Status</th><th>Methodology</th><th>Visual proof</th><th>Text evidence</th></tr></thead>",
                "<tbody>",
            ]
        )
        for row in proof_rows:
            status = esc(row.get("status") or "")
            html_lines.append(
                "<tr>"
                f"<td>{esc(row.get('story_id') or '')}</td>"
                f"<td>{status}</td>"
                f"<td>{esc(row.get('methodology') or row.get('surface') or 'not specified')}</td>"
                f"<td>{esc(row.get('visual_status') or 'none')}</td>"
                f"<td>{'yes' if row.get('text_evidence') else 'no'}</td>"
                "</tr>"
            )
        html_lines.extend(["</tbody>", "</table>", "</section>"])

    if report.get("diagnosis"):
        html_lines.extend(
            [
                "<section class='card diagnosis'>",
                "<h2>Diagnosis</h2>",
                f"<div>{esc(_overall_diagnosis_summary(report))}</div>",
                "</section>",
            ]
        )

    if stories:
        html_lines.extend(["<section class='card' id='story-details'><h2>Story Details</h2>"])
        for story in _ordered_stories(stories):
            html_lines.extend(render_story_article(story, compressed=story["status"] == "pass"))
        html_lines.append("</section>")

    if visual.get("visible", True):
        html_lines.extend(
            [
                "<section class='card'>",
                "<h2>Visual Evidence</h2>",
            ]
        )
        if visual["recording"]:
            item = visual["recording"]
            html_lines.extend(
                [
                    "<div class='visual-item'>",
                    f"<a href='{esc(item['href'])}'>{esc(item['name'])}</a> — full walkthrough",
                    f"<video controls><source src='{esc(item['href'])}' type='video/webm'></video>",
                    "</div>",
                ]
            )
        if visual["buckets"] or visual["unassigned"]:
            for bucket in visual["buckets"]:
                html_lines.extend(
                    [
                        "<div class='visual-group'>",
                        f"<h3>{esc(bucket['story_id'])} <span class='badge {esc(bucket['status'])}'>{esc(bucket['status'].upper())}</span></h3>",
                        "<div class='visual-grid'>",
                    ]
                )
                for item in bucket["items"]:
                    html_lines.extend(render_visual_item(item))
                html_lines.extend(["</div>", "</div>"])
            if visual["unassigned"]:
                html_lines.extend(["<div class='visual-group'>", "<h3>Unassigned</h3>", "<div class='visual-grid'>"])
                for item in visual["unassigned"]:
                    html_lines.extend(render_visual_item(item))
                html_lines.extend(["</div>", "</div>"])
        else:
            html_lines.append(f"<div class='note'>Visual evidence: {esc(visual['absence_reason'])}</div>")
        html_lines.extend(["</section>"])

    if not efficiency.get("suppressed"):
        html_lines.extend(
            [
                "<section class='card efficiency'>",
                "<h2>Efficiency</h2>",
                "<ul class='efficiency-list'>",
                (
                    f"<li>Total browser calls: {efficiency['total_browser_calls']} across "
                    f"{efficiency['stories_total']} {efficiency['story_label']} "
                    f"({efficiency['avg_calls_per_story']:.1f} per story)</li>"
                ),
                f"<li>Distinct browser sessions: {efficiency['distinct_sessions']}</li>",
                f"<li>Top verbs: {esc(efficiency['top_verbs_text'])}</li>",
                "</ul>",
            ]
        )
        if efficiency["outlier"] and efficiency["outlier_reason"]:
            html_lines.append(f"<div class='note warn'>Efficiency note: {esc(efficiency['outlier_reason'])}</div>")
        html_lines.append("</section>")

    if coverage["legacy_mode"]:
        html_lines.append(
            "<!-- Deprecated legacy coverage rendering: source report is missing coverage_observed/coverage_gaps. -->"
        )
    html_lines.extend(
        [
            "<section class='card'>",
            "<h2>Coverage and Limitations</h2>",
        ]
    )
    if coverage["legacy_mode"]:
        if coverage["legacy_tested"]:
            html_lines.extend(
                [
                    "<div class='visual-group'>",
                    "<h3>Legacy Tested Coverage</h3>",
                    "<ul>",
                ]
            )
            for item in coverage["legacy_tested"]:
                html_lines.append(f"<li>{esc(item)}</li>")
            html_lines.extend(["</ul>", "</div>"])
        if coverage["legacy_untested"]:
            html_lines.extend(
                [
                    "<div class='visual-group'>",
                    "<h3>Legacy Untested Coverage</h3>",
                    "<ul>",
                ]
            )
            for item in coverage["legacy_untested"]:
                html_lines.append(f"<li>{esc(item)}</li>")
            html_lines.extend(["</ul>", "</div>"])
        if coverage["legacy_escaped_bug_classes"]:
            html_lines.extend(
                [
                    "<div class='visual-group'>",
                    "<h3>Legacy Escaped Bug Classes</h3>",
                    "<ul>",
                ]
            )
            for item in coverage["legacy_escaped_bug_classes"]:
                html_lines.append(f"<li>{esc(item)}</li>")
            html_lines.extend(["</ul>", "</div>"])
    else:
        html_lines.extend(
            [
                "<h3>What this run actually exercised</h3>",
                "<ul>",
            ]
        )
        if coverage["observed"]:
            for item in coverage["observed"]:
                html_lines.append(f"<li>{esc(item)}</li>")
        elif coverage["note"]:
            html_lines.append(f"<li>{esc(coverage['note'])}</li>")
        else:
            html_lines.append("<li>Not recorded.</li>")
        html_lines.extend(["</ul>", "<h3>What this run did NOT cover</h3>", "<ul>"])
        if coverage["gaps"]:
            for item in coverage["gaps"]:
                html_lines.append(f"<li>{esc(item)}</li>")
        elif coverage["note"]:
            html_lines.append(f"<li>{esc(coverage['note'])}</li>")
        else:
            html_lines.append("<li>Not recorded.</li>")
        html_lines.append("</ul>")
    html_lines.append(f"<div class='footer'>{esc(coverage['limitations'])}</div>")
    if coverage.get("uniform_methodology"):
        html_lines.append(f"<div class='note'>All stories verified via {esc(coverage['uniform_methodology'])}.</div>")
    html_lines.append("</section>")

    html_lines.extend(["<section class='card'>", "<h2>Run Context</h2>", "<div class='run-grid'>"])
    if run_context["run_id"]:
        html_lines.append(f"<div><span class='meta-label'>Run ID</span><span class='meta-value'>{esc(run_context['run_id'])}</span></div>")
    if run_context["git_branch"] or run_context["git_commit_sha"]:
        html_lines.append(
            f"<div><span class='meta-label'>Git</span><span class='meta-value'>{esc(run_context['git_branch'] or 'unknown')}</span><div>{esc(run_context['git_commit_sha'] or 'unknown')}</div></div>"
        )
    if run_context["intent_excerpt"]:
        html_lines.append(f"<div><span class='meta-label'>Intent Excerpt</span><div>{esc(run_context['intent_excerpt'])}</div></div>")
    html_lines.append(f"<div><span class='meta-label'>Spec</span><div>{esc(run_context['spec_text'])}</div></div>")
    if run_context["execution_note"]:
        html_lines.append(
            f"<div><span class='meta-label'>Certifier Execution</span><div>{esc(run_context['execution_note'])}</div></div>"
        )
    if run_context["provider_model_effort"]:
        html_lines.append(
            f"<div><span class='meta-label'>Provider / Model / Effort</span><div>{esc(run_context['provider_model_effort'])}</div></div>"
        )
    if run_context["generated_at"] or run_context["generated_local"]:
        html_lines.append(
            f"<div><span class='meta-label'>Generated</span><div>{esc(run_context['generated_at'])}</div><div>{esc(run_context['generated_local'])}</div></div>"
        )
    if run_context["duration_human"]:
        html_lines.append(f"<div><span class='meta-label'>Duration</span><div>{esc(run_context['duration_human'])}</div></div>")
    html_lines.append("<div><span class='meta-label'>Spend</span>" + "".join(f"<div>{esc(line)}</div>" for line in report["cost_summary"]["lines"]) + "</div>")
    html_lines.extend(["</div>"])
    if report["show_round_timeline"]:
        open_attr = " open"
        html_lines.extend([f"<details{open_attr}>", f"<summary>{len(report['round_history'])} round(s) recorded</summary>"])
        for round_item in report["round_history"]:
            html_lines.extend(
                [
                    "<div class='round-item'>",
                    f"<div><strong>Round {round_item['round']}</strong> "
                    f"<span class='badge {'pass' if round_item['verdict'] == 'passed' else 'fail'}'>{esc(round_item['verdict'].upper())}</span></div>",
                    f"<div>{round_item['passed_count']} pass, {round_item['failed_count']} fail, {round_item['warn_count']} warn, {esc(round_item['duration_human'])}, ${round_item['cost_usd']:.2f}</div>",
                ]
            )
            if round_item["diagnosis"]:
                html_lines.append(f"<div>Diagnosis: {esc(round_item['diagnosis'])}</div>")
            html_lines.extend(
                [
                    f"<div>Failing stories: {esc(', '.join(round_item['failing_story_ids']) or 'none')}</div>",
                    f"<div>Warnings: {esc(', '.join(round_item['warn_story_ids']) or 'none')}</div>",
                    "</div>",
                ]
            )
        html_lines.append("</details>")
    html_lines.append("</section>")

    html_lines.extend(
        [
            "<footer class='card footer'>",
            "<h2>Artifacts &amp; Metadata</h2>",
            f"<div>Schema version: {report['schema_version']} | Generator: {esc(report['generator'])}</div>",
            "<div class='artifact-links'>",
        ]
    )
    for artifact in report["artifacts"]:
        if artifact.get("present"):
            html_lines.append(f"<a href='{esc(artifact['href'])}'>{esc(artifact['label'])}</a>")
    html_lines.append("</div>")
    session_id = _clean_runtime_value(report["run_context"].get("session_id"))
    if session_id or report["project"].get("path"):
        html_lines.extend(["<details>", "<summary>Debug metadata</summary>"])
        if session_id:
            html_lines.append(f"<div>Session ID: <code>{esc(session_id)}</code></div>")
        if report["project"].get("path"):
            html_lines.append(f"<div>Workspace path: <code>{esc(report['project']['path'])}</code></div>")
        html_lines.append("</details>")
    html_lines.extend(["</footer>", "</body>", "</html>"])
    return "\n".join(html_lines)


def _write_pow_report(output_dir: Path, report: dict[str, Any]) -> None:
    from otto.observability import write_json_file, write_text_atomic

    write_json_file(output_dir / "proof-of-work.json", report)
    write_text_atomic(output_dir / "proof-of-work.md", _render_pow_markdown(report) + "\n")
    write_text_atomic(output_dir / "proof-of-work.html", _render_pow_html(report))


def _repair_standalone_certify_history(project_dir: Path) -> None:
    from otto import paths
    from otto.runs.history import append_history_snapshot, build_terminal_snapshot
    from otto.runs.history import read_history_rows

    history_rows = read_history_rows(paths.history_jsonl(project_dir))
    seen = {
        str(row.get("dedupe_key") or "")
        for row in history_rows
        if isinstance(row, dict)
    }
    sessions_root = paths.sessions_root(project_dir)
    if not sessions_root.exists():
        return

    for summary_path in sorted(sessions_root.glob("*/summary.json")):
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if str(summary.get("command") or "") != "certify":
            continue
        run_id = str(summary.get("run_id") or summary_path.parent.name).strip()
        if not run_id:
            continue
        dedupe_key = f"terminal_snapshot:{run_id}"
        if dedupe_key in seen:
            continue
        session_dir = paths.session_dir(project_dir, run_id)
        append_history_snapshot(
            project_dir,
            build_terminal_snapshot(
                run_id=run_id,
                domain="atomic",
                run_type="certify",
                command="certify",
                intent_meta={
                    "summary": str(summary.get("intent") or "")[:200],
                    "intent_path": str(paths.session_intent(project_dir, run_id)),
                    "spec_path": str(session_dir / "spec.md") if (session_dir / "spec.md").exists() else None,
                },
                status="done" if bool(summary.get("passed")) else "failed",
                terminal_outcome="success" if bool(summary.get("passed")) else "failure",
                timing={
                    "finished_at": str(summary.get("completed_at") or "") or None,
                    "timestamp": str(summary.get("completed_at") or ""),
                    "duration_s": float(summary.get("duration_s") or 0.0),
                },
                metrics={"cost_usd": float(summary.get("cost_usd") or 0.0)},
                git={"branch": str(summary.get("branch") or "") or None},
                source={"resumable": False},
                artifacts={
                    "session_dir": str(session_dir),
                    "manifest_path": str(session_dir / "manifest.json"),
                    "checkpoint_path": None,
                    "summary_path": str(summary_path),
                    "primary_log_path": str(paths.certify_dir(project_dir, run_id) / "narrative.log"),
                    "extra_log_paths": [str(paths.certify_dir(project_dir, run_id) / "proof-of-work.html")],
                },
                extra_fields={
                    "passed": bool(summary.get("passed")),
                    "stories_passed": int(summary.get("stories_passed") or 0),
                    "stories_tested": int(summary.get("stories_tested") or 0),
                    "certifier_cost_usd": float(summary.get("cost_usd") or 0.0),
                    "certify_rounds": int(summary.get("rounds") or 0),
                },
            ),
            strict=True,
        )
        seen.add(dedupe_key)


# ---------------------------------------------------------------------------
# Agentic certifier — single agent, subagent-driven
# ---------------------------------------------------------------------------

async def run_agentic_certifier(
    intent: str,
    project_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    mode: str = "standard",
    focus: str | None = None,
    target: str | None = None,
    budget: "RunBudget | None" = None,
    stories: list[dict[str, Any]] | None = None,
    merge_context: dict[str, Any] | None = None,
    session_id: str | None = None,
    write_session_summary: bool = True,
    write_history: bool = True,
    verbose: bool = False,
    round_num: int | None = None,
) -> "CertificationReport":
    """Agentic certifier: one monolithic agent does everything.

    A single certifier agent reads the project, installs deps, starts the app,
    plans test stories, dispatches subagents for parallel testing, and reports.

    MUST run in the caller's process (not a subprocess) so the Agent tool
    is available for subagent dispatch.

    CONTRACT: infra failures propagate as exceptions and callers own the
    retry/pause decision.
    """
    from otto.agent import make_agent_options, run_agent_with_timeout
    from otto.certifier.report import CertificationOutcome, CertificationReport
    from otto.display import console
    from otto.markers import (
        MalformedCertifierOutputError,
        compact_story_results,
        parse_certifier_markers,
    )

    config = config or {}
    start_time = time.monotonic()
    from otto.runs.registry import garbage_collect_live_records

    garbage_collect_live_records(project_dir)
    if merge_context is None and write_history:
        _repair_standalone_certify_history(project_dir)

    from otto import paths
    if session_id is None:
        session_id = os.environ.get("OTTO_RUN_ID", "").strip()
    if session_id is None or not session_id:
        from otto.runs.registry import allocate_run_id
        session_id = allocate_run_id(project_dir)
    prior_session_ids = [
        resolved.name
        for resolved in (
            paths.resolve_pointer(project_dir, paths.PAUSED_POINTER),
            paths.resolve_pointer(project_dir, paths.LATEST_POINTER),
        )
        if resolved is not None and resolved.name != session_id
    ]
    paths.ensure_session_scaffold(project_dir, session_id, phase="certify")
    paths.set_pointer(project_dir, paths.LATEST_POINTER, session_id)
    from otto.pipeline import _runtime_metadata
    from otto.observability import update_input_provenance, write_runtime_metadata, sha256_text

    write_runtime_metadata(paths.session_dir(project_dir, session_id), _runtime_metadata(project_dir))
    update_input_provenance(
        paths.session_dir(project_dir, session_id),
        intent={"source": "cli-argument", "fallback_reason": "", "resolved_text": intent, "sha256": sha256_text(intent)},
        spec={"source": "none", "path": "", "sha256": ""},
    )
    run_id = session_id
    report_dir = paths.certify_dir(project_dir, session_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = report_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    from otto.queue.runtime import mark_queue_child_ready

    mark_queue_child_ready(
        project_dir,
        run_id=session_id,
        session_dir=paths.session_dir(project_dir, session_id),
        phase="certify",
        checkpoint_path=None,
    )
    publisher = None
    if (
        write_history
        and write_session_summary
        and merge_context is None
        and not _is_queue_runner_child()
    ):
        from otto.pipeline import _current_branch_name, _current_head_sha
        from otto.runs.registry import publisher_for

        publisher = publisher_for(
            "atomic",
            "certify",
            "certify",
            project_dir=project_dir,
            run_id=run_id,
            intent=intent,
            display_name=f"certify: {intent[:80]}".strip(),
            cwd=project_dir,
            source={"resumable": False},
            git={"branch": _current_branch_name(project_dir), "worktree": None, "target_branch": None, "head_sha": _current_head_sha(project_dir)},
            intent_meta={
                "intent_path": str(paths.session_intent(project_dir, run_id)),
                "spec_path": str(config.get("_spec_path") or "").strip() or None,
            },
            artifacts={
                "session_dir": str(paths.session_dir(project_dir, run_id)),
                "manifest_path": str(paths.session_dir(project_dir, run_id) / "manifest.json"),
                "checkpoint_path": None,
                "summary_path": str(paths.session_summary(project_dir, run_id)),
                "primary_log_path": str(report_dir / "narrative.log"),
                "extra_log_paths": [
                    str(report_dir / "verification-plan.json"),
                    str(report_dir / "proof-of-work.html"),
                ],
            },
            adapter_key="atomic.certify",
        )
        publisher.__enter__()

    baseline_listening_pids = _listening_process_pids()
    try:
        prompt = _render_certifier_prompt(
            mode=mode,
            intent=intent,
            evidence_dir=evidence_dir,
            focus=focus,
            target=target,
            stories=stories,
            merge_context=merge_context,
            project_dir=project_dir,
            run_id=run_id,
            prior_session_ids=prior_session_ids,
        )

        from otto.memory import inject_memory

        prompt = inject_memory(prompt, project_dir, config)
        options = make_agent_options(project_dir, config, agent_type="certifier")

        logger.info("Running agentic certifier on %s", project_dir)
        timeout = budget.for_call() if budget is not None else None
        from otto.pipeline import _make_atomic_heartbeat_callback, _make_atomic_terminal_callback
        terminal_callback = _make_atomic_terminal_callback(project_dir, run_id, console.print)
        heartbeat_callback = _make_atomic_heartbeat_callback(project_dir, run_id)

        text, cost, agent_session_id, breakdown = await run_agent_with_timeout(
            prompt,
            options,
            log_dir=report_dir,
            phase_name="CERTIFY",
            phase_label=f"CERTIFY ROUND {round_num}" if round_num else None,
            timeout=timeout,
            project_dir=project_dir,
            on_terminal_event=terminal_callback,
            on_heartbeat=heartbeat_callback,
            verbose=verbose,
        )

        total_duration = round(time.monotonic() - start_time, 1)
        parsed = parse_certifier_markers(text or "", certifier_mode=mode)
        if not parsed.stories and not parsed.verdict_seen:
            raise MalformedCertifierOutputError(
                "Certifier produced no structured output — see narrative.log"
            )
        story_results = [_normalize_story_result(s) for s in compact_story_results(parsed.stories)]
        if isinstance(merge_context, dict) and isinstance(merge_context.get("verification_plan"), dict):
            verification_plan = dict(merge_context["verification_plan"])
            from otto.verification import write_verification_plan

            write_verification_plan(report_dir / "verification-plan.json", verification_plan)
        else:
            verification_plan = _write_certifier_verification_plan(
                report_dir=report_dir,
                mode=mode,
                target=target,
                story_results=story_results,
                explicit_stories=stories,
            )
        has_failures = any(_story_verdict(story) == "FAIL" for story in story_results)
        passed = parsed.verdict_pass and not has_failures and bool(story_results)
        target_mode = mode == "target" or bool(target) or bool(config.get("_target"))
        if target_mode:
            passed = passed and parsed.metric_met is True
        outcome = CertificationOutcome.PASSED if passed else CertificationOutcome.FAILED

        report = CertificationReport(
            outcome=outcome,
            cost_usd=float(cost or 0),
            duration_s=total_duration,
            story_results=story_results,
            metric_value=parsed.metric_value,
            metric_met=parsed.metric_met,
            run_id=run_id,
            diagnosis=parsed.diagnosis,
            child_session_ids=list(breakdown.get("child_session_ids", []) or []),
            subagent_errors=list(breakdown.get("subagent_errors", []) or []),
            token_usage=_token_usage_summary(report_dir / "messages.jsonl"),
        )

        try:
            certify_rounds_count = len(parsed.certify_rounds) or 1
            pow_data = _build_pow_report_data(
                project_dir=project_dir,
                report_dir=report_dir,
                log_dir=report_dir,
                run_id=run_id,
                session_id=agent_session_id,
                pipeline_mode="agentic_certifier",
                certifier_mode=mode,
                outcome=outcome.value,
                story_results=story_results,
                diagnosis=parsed.diagnosis,
                certify_rounds=parsed.certify_rounds,
                duration_s=total_duration,
                certifier_cost_usd=float(cost or 0),
                total_cost_usd=float(cost or 0),
                intent=intent,
                options=options,
                evidence_dir=evidence_dir,
                stories_tested=parsed.stories_tested,
                stories_passed=parsed.stories_passed,
                coverage_observed=parsed.coverage_observed,
                coverage_gaps=parsed.coverage_gaps,
                coverage_emitted=(
                    parsed.coverage_observed_emitted or parsed.coverage_gaps_emitted
                ),
                metric_value=parsed.metric_value,
                metric_met=parsed.metric_met,
                round_timings=breakdown.get("round_timings", []),
            )
            evidence_gate = pow_data.get("evidence_gate") if isinstance(pow_data, dict) else None
            if passed and isinstance(evidence_gate, dict) and evidence_gate.get("blocks_pass"):
                passed = False
                outcome = CertificationOutcome.FAILED
                report.outcome = outcome
                report.diagnosis = _append_demo_evidence_gate_diagnosis(report.diagnosis, evidence_gate)
            _write_pow_report(report_dir, pow_data)
        except Exception as exc:
            logger.warning("Failed to write PoW report: %s", exc)
            certify_rounds_count = len(parsed.certify_rounds) or 1

        logger.info(
            "Agentic certifier done: %s, %d/%d stories, %.1fs, $%.3f",
            outcome.value,
            parsed.stories_passed,
            parsed.stories_tested,
            total_duration,
            float(cost or 0),
        )

        from otto.memory import record_run

        record_run(
            project_dir,
            run_id=run_id,
            command="certify",
            certifier_mode=mode,
            stories=story_results,
            cost=float(cost or 0),
        )

        if write_session_summary:
            from otto.pipeline import _write_session_summary

            usage = dict((breakdown.get("phase_usage") or {}).get("certify") or {})
            breakdown_summary = {
                "certify": {
                    "duration_s": total_duration,
                    "rounds": certify_rounds_count,
                }
            }
            if float(cost or 0.0) > 0:
                breakdown_summary["certify"]["cost_usd"] = float(cost or 0.0)
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            ):
                if isinstance(usage.get(key), (int, float)):
                    breakdown_summary["certify"][key] = int(usage[key])
            _write_session_summary(
                project_dir,
                run_id,
                verdict=outcome.value,
                passed=passed,
                cost=float(cost or 0),
                duration=total_duration,
                stories_passed=parsed.stories_passed,
                stories_tested=parsed.stories_tested,
                rounds=certify_rounds_count,
                intent=intent,
                command="certify",
                breakdown=breakdown_summary,
            )
            if verification_plan:
                from otto.observability import write_json_file

                summary_path = paths.session_summary(project_dir, run_id)
                try:
                    summary_payload = json.loads(summary_path.read_text())
                except (OSError, json.JSONDecodeError):
                    summary_payload = {}
                if isinstance(summary_payload, dict):
                    summary_payload["verification_plan"] = verification_plan
                    write_json_file(summary_path, summary_payload, strict=True)

        final_record = None
        if publisher is not None:
            final_record = publisher.finalize(
                status="done" if passed else "failed",
                terminal_outcome="success" if passed else "failure",
                updates={
                    "metrics": {
                        "cost_usd": float(cost or 0),
                        "stories_passed": parsed.stories_passed,
                        "stories_tested": parsed.stories_tested,
                    },
                    "last_event": "completed" if passed else "failed",
                },
            )
        if (
            write_history
            and merge_context is None
            and not _is_queue_runner_child()
        ):
            from otto.pipeline import _append_session_history

            _append_session_history(
                project_dir,
                run_id=run_id,
                command="certify",
                certifier_mode=mode,
                intent=intent,
                stories=story_results,
                passed=passed,
                duration_s=total_duration,
                total_cost_usd=float(cost or 0),
                certifier_cost_usd=float(cost or 0),
                rounds=certify_rounds_count,
                started_at=final_record.timing.get("started_at") if final_record is not None else None,
                finished_at=final_record.timing.get("finished_at") if final_record is not None else None,
                branch=final_record.git.get("branch") if final_record is not None else None,
                worktree=final_record.git.get("worktree") if final_record is not None else None,
                spec_path=str(config.get("_spec_path") or "").strip() or None,
            )

        return report
    finally:
        cleaned_servers = _cleanup_certifier_background_servers(
            project_dir,
            baseline_listening_pids,
        )
        if cleaned_servers:
            logger.warning(
                "Cleaned up %d project dev server(s) left running after certification: %s",
                len(cleaned_servers),
                ", ".join(
                    f"pid={item['pid']} cwd={item.get('cwd') or '?'}"
                    for item in cleaned_servers
                ),
            )
        if publisher is not None:
            publisher.stop()
