"""Otto product certifier — independent, evidence-based product evaluation.

Evaluates any software product against its original intent. Builder-blind:
doesn't know if otto, bare CC, or a human built it.

Architecture:
  run_agentic_certifier() — single agent reads, installs, tests, reports
"""

from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from otto.logstream import summarize_browser_efficiency
from otto.redaction import redact_text

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    tomllib = None

if TYPE_CHECKING:
    from otto.budget import RunBudget

logger = logging.getLogger("otto.certifier")

_SCHEMA_VERSION = 1
_GENERATOR = "otto certifier"

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


def _render_certifier_prompt(
    *,
    mode: str,
    intent: str,
    evidence_dir: Path,
    focus: str | None = None,
    target: str | None = None,
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
    return render_prompt(
        prompt_name,
        intent=intent,
        evidence_dir=str(evidence_dir),
        focus_section=focus_section,
        spec_section=spec_section,
        target=target or "",
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
    if story.get("warn"):
        return "warn"
    return "pass" if story.get("passed") else "fail"


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
    return {
        "passed_count": passed_count,
        "failed_count": failed_count,
        "warn_count": warn_count,
    }


def _story_residual_risk(status: str, certifier_mode: str) -> str:
    if status == "fail":
        return "Defect confirmed via live UI events. Re-test after the fix lands."
    if status == "warn":
        return "Non-blocking concern surfaced; confirm whether the extra scope or warning is acceptable."
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
    return ({"fail": 0, "warn": 1, "pass": 2}.get(str(story.get("status") or ""), 3), str(story.get("story_id") or ""))


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
    label = str(story.get("status") or "").upper()
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


def _story_detail_sections(stories: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    ordered = _ordered_stories(stories)
    return {
        "failing": [story for story in ordered if story.get("status") == "fail"],
        "remaining": [story for story in ordered if story.get("status") != "fail"],
    }


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
        failing_story_ids = [
            story.get("story_id", "")
            for story in round_stories
            if story.get("status") == "fail"
        ]
        warn_story_ids = [
            story.get("story_id", "")
            for story in round_stories
            if story.get("status") == "warn"
        ]
        round_verdict = round_data.get("verdict")
        if isinstance(round_verdict, bool):
            verdict_text = "passed" if round_verdict else "failed"
        else:
            verdict_text = "failed" if failing_story_ids else "passed"
        round_diagnosis = _diagnosis_text(round_data.get("diagnosis", ""), verdict_text)
        duration_s = durations[index] if index < len(durations) else 0.0
        cost_usd = raw_costs[index] if index < len(raw_costs) else 0.0
        fix_commits = list(round_data.get("fix_commits", []) or [])
        fix_diff_stat = str(round_data.get("fix_diff_stat", "") or "")
        still_failing_after_fix = list(round_data.get("still_failing_after_fix", []) or [])
        subagent_errors = list(round_data.get("subagent_errors", []) or [])
        history.append(
            {
                "round": round_data.get("round", index + 1),
                "verdict": verdict_text,
                "stories_tested": round_data.get("tested", len(round_stories)),
                "passed_count": counts["passed_count"],
                "failed_count": counts["failed_count"],
                "warn_count": counts["warn_count"],
                "failing_story_ids": failing_story_ids,
                "warn_story_ids": warn_story_ids,
                "diagnosis": round_diagnosis,
                "duration_s": duration_s,
                "duration_human": _human_duration(duration_s),
                "cost_usd": round(cost_usd, 4),
                "cost_estimated": not bool(round_timings),
                "fix_commits": fix_commits,
                "fix_diff_stat": fix_diff_stat,
                "still_failing_after_fix": still_failing_after_fix,
                "subagent_errors": subagent_errors,
            }
        )
    return history


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
        {"name": path.name, "path": str(path), "href": _relative_href(base_dir, path)}
        for path in screenshots
    ]
    videos = [
        {"name": path.name, "path": str(path), "href": _relative_href(base_dir, path)}
        for path in recordings
    ]
    recording = next((item for item in videos if item["name"] == "recording.webm"), videos[0] if videos else None)
    assigned: dict[str, str] = {}
    for image in images:
        forced_story = next(
            (
                str(story.get("story_id") or "")
                for story in stories
                if Path(str(story.get("failure_evidence") or "").strip()).name.lower() == image["name"].lower()
            ),
            "",
        )
        if forced_story:
            assigned[image["name"]] = forced_story
            continue
        best_story_id = ""
        best_score = 0
        for story in stories:
            score = _visual_match_story(image["name"], story)
            if score > best_score:
                best_score = score
                best_story_id = str(story.get("story_id") or "")
        if best_story_id and best_score > 0:
            assigned[image["name"]] = best_story_id

    buckets: list[dict[str, Any]] = []
    for story in stories:
        bucket_items = []
        for image in images:
            if assigned.get(image["name"]) != story.get("story_id"):
                continue
            bucket_items.append(
                {
                    **image,
                    "caption": _visual_caption(image, story),
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
            **image,
            "caption": _visual_caption(image, None),
        }
        for image in images
        if image["name"] not in assigned
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


def _cost_summary(certifier_cost_usd: float, total_cost_usd: float) -> dict[str, Any]:
    certifier = round(float(certifier_cost_usd or 0.0), 2)
    total = round(float(total_cost_usd or 0.0), 2)
    if certifier == total:
        return {
            "display": f"Cost: ${total:.2f}",
            "lines": [f"Cost: ${total:.2f}"],
            "certifier_equals_total": True,
        }
    return {
        "display": f"Cost: certifier ${certifier:.2f}, total ${total:.2f}",
        "lines": [f"Certifier cost: ${certifier:.2f}", f"Total cost: ${total:.2f}"],
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
    profile = _mode_profile(certifier_mode)
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
    artifacts = _artifacts(
        report_dir=report_dir,
        log_dir=log_dir,
        session_dir=session_dir,
        spec_path=str(spec_context.get("spec_path") or ""),
        evidence_dir=evidence_dir,
    )
    cost_summary = _cost_summary(certifier_cost_usd, total_cost_usd)
    verdict_label = "PASS with warnings" if outcome == "passed" and counts["warn_count"] > 0 else ("PASS" if outcome == "passed" else "FAIL")
    data = {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": generated_iso,
        "generated_local": generated_local,
        "generator": _GENERATOR,
        "outcome": outcome,
        "verdict_label": verdict_label,
        "one_line_interpretation": redact_text(
            _one_line_interpretation(outcome, counts["warn_count"], certifier_mode)
        ),
        "duration_s": duration_s,
        "duration_human": _human_duration(duration_s),
        "certifier_cost_usd": round(float(certifier_cost_usd or 0.0), 4),
        "total_cost_usd": round(float(total_cost_usd or 0.0), 4),
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
        "diagnosis": redact_text(_diagnosis_text(diagnosis, outcome)),
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
    }
    data["next_actions"] = _next_actions(
        outcome=outcome,
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


def _render_pow_markdown(report: dict[str, Any]) -> str:
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
        f"- Stories: {report['passed_count']} pass, {report['failed_count']} fail, {report['warn_count']} warn",
        "- Full report: [proof-of-work.html](proof-of-work.html)",
    ]
    for action in report["next_actions"]:
        if action.get("command"):
            lines.append(f"- Next: {action['label']} via `{action['command']}`")
        else:
            lines.append(f"- Next: {action['label']} in the HTML report")

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
                label = f"{bucket['story_id']}: {item['name']}"
                image_items.append(f"- {label} ({bucket['status']}): [{item['name']}]({item['href']})")
        for item in visual["unassigned"]:
            image_items.append(f"- Unassigned: [{item['name']}]({item['href']})")
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
        parts = [
            f"<article class='story {esc(story['status'])}'>",
            "<div class='story-header'>",
            f"<span class='badge {esc(story['status'])}'>{esc(_story_status_label(story, methodology_summary))}</span>",
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
        ".meta-label { display: block; font-size: 0.8rem; color: #475569; text-transform: uppercase; letter-spacing: 0.04em; }",
        ".meta-value { font-weight: 700; font-size: 1.05rem; }",
        ".badge { display: inline-block; padding: 0.2rem 0.55rem; border-radius: 999px; font-size: 0.78rem; font-weight: 700; }",
        ".badge.pass { background: #dcfce7; color: #166534; }",
        ".badge.fail { background: #fee2e2; color: #991b1b; }",
        ".badge.warn { background: #fef3c7; color: #92400e; }",
        ".table { width: 100%; border-collapse: collapse; }",
        ".table th, .table td { text-align: left; padding: 0.65rem; border-top: 1px solid #e2e8f0; vertical-align: top; }",
        ".table th { color: #475569; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.04em; }",
        ".story { border: 1px solid #dbe4ee; border-left-width: 6px; border-radius: 14px; background: white; padding: 1rem 1.1rem; margin-top: 1rem; }",
        ".story.pass { border-left-color: #22c55e; }",
        ".story.fail { border-left-color: #ef4444; }",
        ".story.warn { border-left-color: #f59e0b; }",
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
            f"<span>{report['passed_count']} pass / {report['failed_count']} fail / {report['warn_count']} warn</span>",
            f"<span>{esc(report['certifier_mode'])}</span>",
            "</div>",
            "<div class='meta-grid'>",
            f"<div><span class='meta-label'>Duration</span><span class='meta-value'>{esc(report['duration_human'])}</span></div>",
            f"<div><span class='meta-label'>Generated</span><span class='meta-value'>{esc(report['generated_local'])}</span></div>",
            f"<div><span class='meta-label'>Certifier Mode</span><span class='meta-value'>{esc(report['certifier_mode'])}</span></div>",
            f"<div><span class='meta-label'>Cost</span><span class='meta-value'>{esc(report['cost_summary']['display'])}</span></div>",
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
            "<section class='card'>",
            "<h2>Story Summary</h2>",
            "<table class='table'>",
            "<thead><tr><th>Story</th><th>Status</th><th>Surface</th><th>Key Finding</th><th>Evidence</th></tr></thead>",
            "<tbody>",
        ]
    )
    for story in stories:
        html_lines.append(
            "<tr>"
            f"<td>{esc(story['story_id'])}</td>"
            f"<td><span class='badge {esc(story['status'])}'>{esc(_story_status_label(story, methodology_summary))}</span></td>"
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
                    caption = f" — {item['caption']}" if item.get("caption") else ""
                    html_lines.extend(
                        [
                            "<div class='visual-item'>",
                            f"<a href='{esc(item['href'])}'>{esc(item['name'])}</a>{esc(caption)}",
                            f"<img src='{esc(item['href'])}' alt='{esc(item['name'])}'>",
                            "</div>",
                        ]
                    )
                html_lines.extend(["</div>", "</div>"])
            if visual["unassigned"]:
                html_lines.extend(["<div class='visual-group'>", "<h3>Unassigned</h3>", "<div class='visual-grid'>"])
                for item in visual["unassigned"]:
                    caption = f" — {item['caption']}" if item.get("caption") else ""
                    html_lines.extend(
                        [
                            "<div class='visual-item'>",
                            f"<a href='{esc(item['href'])}'>{esc(item['name'])}</a>{esc(caption)}",
                            f"<img src='{esc(item['href'])}' alt='{esc(item['name'])}'>",
                            "</div>",
                        ]
                    )
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
    html_lines.append(f"<div><span class='meta-label'>Cost</span>" + "".join(f"<div>{esc(line)}</div>" for line in report["cost_summary"]["lines"]) + "</div>")
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
    session_id: str | None = None,
    write_session_summary: bool = True,
    write_history: bool = True,
    verbose: bool = False,
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

    from otto import paths
    if session_id is None:
        session_id = paths.new_session_id(project_dir)
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

    prompt = _render_certifier_prompt(
        mode=mode,
        intent=intent,
        evidence_dir=evidence_dir,
        focus=focus,
        target=target,
        project_dir=project_dir,
        run_id=run_id,
        prior_session_ids=prior_session_ids,
    )

    from otto.memory import inject_memory

    prompt = inject_memory(prompt, project_dir, config)
    options = make_agent_options(project_dir, config, agent_type="certifier")

    logger.info("Running agentic certifier on %s", project_dir)
    timeout = budget.for_call() if budget is not None else None

    text, cost, agent_session_id, breakdown = await run_agent_with_timeout(
        prompt,
        options,
        log_dir=report_dir,
        phase_name="CERTIFY",
        timeout=timeout,
        project_dir=project_dir,
        on_terminal_event=console.print,
        verbose=verbose,
    )

    total_duration = round(time.monotonic() - start_time, 1)
    parsed = parse_certifier_markers(text or "", certifier_mode=mode)
    if not parsed.stories and not parsed.verdict_seen:
        raise MalformedCertifierOutputError(
            "Certifier produced no structured output — see narrative.log"
        )
    story_results = compact_story_results(parsed.stories)
    has_failures = any(not story["passed"] for story in story_results)
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
        diagnosis=parsed.diagnosis,
        child_session_ids=list(breakdown.get("child_session_ids", []) or []),
        subagent_errors=list(breakdown.get("subagent_errors", []) or []),
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

    if write_history:
        try:
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
            )
        except Exception as exc:
            logger.warning("Failed to append standalone certify history: %s", exc)

    if write_session_summary:
        from otto.pipeline import _write_session_summary

        breakdown_summary = {
            "certify": {
                "duration_s": total_duration,
                "cost_usd": float(cost or 0.0),
                "rounds": certify_rounds_count,
            }
        }
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

    return report


def _generate_agentic_html_pow(
    output_dir: Path,
    story_results: list[dict[str, Any]],
    outcome: str,
    duration: float,
    cost: float,
    passed: int,
    total: int,
    *,
    diagnosis: str = "",
    round_history: list[dict[str, Any]] | None = None,
    evidence_dir: Path | None = None,
    certifier_cost: float | None = None,
    coverage_observed: list[str] | None = None,
    coverage_gaps: list[str] | None = None,
    coverage_emitted: bool | None = None,
) -> None:
    """Compatibility wrapper used by legacy tests."""
    report = _build_pow_report_data(
        project_dir=output_dir,
        report_dir=output_dir,
        log_dir=output_dir,
        run_id="ad-hoc-report",
        session_id="",
        pipeline_mode="agentic_v3",
        certifier_mode="standard",
        outcome=outcome,
        story_results=story_results,
        diagnosis=diagnosis,
        certify_rounds=[
            {
                "round": item.get("round", index + 1),
                "stories": [],
                "verdict": item.get("verdict") == "passed" if isinstance(item.get("verdict"), str) else item.get("verdict"),
                "diagnosis": item.get("diagnosis", ""),
                "tested": item.get("stories_tested", item.get("stories_count", 0)),
            }
            for index, item in enumerate(round_history or [])
        ],
        duration_s=duration,
        certifier_cost_usd=float(certifier_cost if certifier_cost is not None else cost),
        total_cost_usd=float(cost),
        intent="",
        options=type("Opts", (), {"provider": "", "model": None, "effort": None})(),
        evidence_dir=evidence_dir,
        stories_tested=total,
        stories_passed=passed,
        coverage_observed=coverage_observed,
        coverage_gaps=coverage_gaps,
        coverage_emitted=coverage_emitted,
        round_timings=None,
    )
    from otto.observability import write_text_atomic

    write_text_atomic(output_dir / "proof-of-work.html", _render_pow_html(report))
