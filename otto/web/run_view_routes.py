"""FastAPI routes for the new design's RunView (research §7 + A4).

Mounts `GET /api/runs` and `GET /api/runs/<session_id>`. The latter
returns a JSON RunView produced by `otto.mission_control.run_view.build_run_view`.

Compatible with launcher mode (multiple projects) via
`project_dir_provider` — same pattern as `install_i2p_routes`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from otto.mission_control.actions import (
    execute_abort_group,
    execute_pause_run,
    execute_resume_run,
)
from otto.mission_control.run_view import build_run_view
from otto.mission_control.serializers import serialize_project
from otto.web.session_resolver import (
    queue_state_for_session,
    resolve_session_dir,
    session_dirs,
)

logger = logging.getLogger("otto.web.run_view")


def install_run_view_routes(
    app: FastAPI,
    *,
    project_dir: Path | None = None,
    project_dir_provider: Callable[[], Path | None] | None = None,
) -> None:
    """Mount `/api/runs/*` routes onto the given FastAPI app.

    Args:
        app: FastAPI instance.
        project_dir: legacy fixed-project mode (mutually exclusive with provider).
        project_dir_provider: V19-style dynamic project resolver. Called per
            request so launcher-mode projects pick up the currently-selected
            project_dir.
    """
    if project_dir_provider is None and project_dir is None:
        raise ValueError(
            "install_run_view_routes needs project_dir or project_dir_provider"
        )

    def _resolve_project_dir() -> Path:
        if project_dir_provider is not None:
            resolved = project_dir_provider()
        else:
            resolved = project_dir
        if resolved is None:
            raise HTTPException(
                status_code=409,
                detail="No project selected.",
            )
        return resolved

    # Use /api/run-view/* to avoid collision with the legacy
    # /api/runs/{run_id}/... route surface for artifacts/logs/diff/etc.
    # Legacy /api/runs/* will be deleted in Phase C; until then, the new
    # design lives at /api/run-view/*.
    router = APIRouter(prefix="/api/run-view", tags=["run-view"])

    @router.get("")
    def list_runs() -> JSONResponse:
        """List all session ids under the current project's otto_logs/sessions/.

        Returns both the legacy ``runs: [str]`` array (kept for back-compat
        with existing callers/tests) and a richer ``sessions: [obj]`` array
        used by the new RunListLanding cards (B4). Each session object
        carries the fields the landing page renders without needing a
        per-session round trip — intent, status, counts, wall, cost,
        finished_at, and lifecycle.
        """
        project = _resolve_project_dir()
        discovered = session_dirs(project)
        if not discovered:
            return JSONResponse({"runs": [], "sessions": []})
        ids = sorted(discovered.keys(), reverse=True)
        sessions = [_summarize_session(discovered[sid], sid) for sid in ids]
        return JSONResponse({"runs": ids, "sessions": sessions})

    @router.get("/{session_id}")
    def get_run(session_id: str) -> JSONResponse:
        """Return RunView JSON for the given session.

        404 if the session id doesn't exist or escapes the sessions dir.
        """
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        view = build_run_view(
            session_dir,
            live_state=queue_state_for_session(project, session_id),
            runtime_defaults=_run_view_runtime_defaults(project),
        )
        return JSONResponse(view)

    # ---- A7: pause / resume / abort verbs --------------------------------
    # The run-view API reads from the spec-state.jsonl journal; these
    # endpoints append to the same journal so the read paths reflect the
    # operator action on the next poll. There's no separate command queue
    # (unlike the legacy queue/atomic cancel surfaces) because the runner
    # already polls the journal between phases for spec.review_approved /
    # group.invalidated_by_spec_edit; we ride the same poll for pause and
    # abort.

    @router.post("/{session_id}/actions/pause")
    def pause_run(session_id: str, payload: dict = Body(default_factory=dict)) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        note = str((payload or {}).get("note") or "").strip()
        result = execute_pause_run(session_dir, note=note)
        return JSONResponse(_action_to_json(result), status_code=200 if result.ok else 409)

    @router.post("/{session_id}/actions/resume")
    def resume_run(session_id: str, payload: dict = Body(default_factory=dict)) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        note = str((payload or {}).get("note") or "").strip()
        result = execute_resume_run(session_dir, note=note)
        return JSONResponse(_action_to_json(result), status_code=200 if result.ok else 409)

    @router.post("/{session_id}/groups/{group_id}/abort")
    def abort_group(
        session_id: str,
        group_id: str,
        payload: dict = Body(default_factory=dict),
    ) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        reason = str((payload or {}).get("reason") or "").strip()
        result = execute_abort_group(session_dir, group_id, reason=reason)
        return JSONResponse(_action_to_json(result), status_code=200 if result.ok else 409)

    @router.get("/{session_id}/proof-packet.html", include_in_schema=False)
    def proof_packet_html(session_id: str) -> HTMLResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        path = session_dir / "proof-packet.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="proof packet has not been produced")
        try:
            html = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"proof packet unreadable: {exc}") from exc
        return HTMLResponse(
            _rewrite_proof_asset_urls(html, session_id=session_id),
            media_type="text/html",
        )

    @router.get("/{session_id}/evidence")
    def get_evidence(session_id: str, path: str) -> FileResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        target = (session_dir / path).resolve(strict=False)
        if not _is_allowed_evidence_target(target, session_dir):
            raise HTTPException(status_code=400, detail="evidence path escapes session/worktree")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="evidence not found")
        return FileResponse(target)

    @router.get("/{session_id}/diff")
    def get_diff(session_id: str, group_id: str | None = None) -> PlainTextResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        return PlainTextResponse(
            _session_diff_text(session_dir, group_id=group_id),
            media_type="text/plain",
        )

    @router.get("/{session_id}/logs")
    def get_logs(session_id: str, group_id: str | None = None) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        return JSONResponse(_session_logs_payload(session_dir, group_id=group_id))

    @router.get("/{session_id}/events")
    def get_events(session_id: str, after: int = -1, limit: int = 500) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        return JSONResponse(
            _session_provider_events_payload(session_dir, after=after, limit=limit)
        )

    @router.get("/{session_id}/groups/{group_id}/logs")
    def get_group_logs(session_id: str, group_id: str) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        safe_group_id = _safe_group_id(group_id)
        return JSONResponse(
            _session_logs_payload(session_dir, group_id=safe_group_id)
        )

    @router.get("/{session_id}/groups/{group_id}/diff")
    def get_group_diff(session_id: str, group_id: str) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        safe_group_id = _safe_group_id(group_id)
        view = build_run_view(
            session_dir,
            live_state=queue_state_for_session(project, session_id),
            runtime_defaults=_run_view_runtime_defaults(project),
        )
        group = next(
            (g for g in view.get("groups", []) if g.get("id") == safe_group_id),
            None,
        )
        if group is None:
            raise HTTPException(status_code=404, detail="group not found")
        return JSONResponse(_group_diff_payload(session_dir, group))

    @router.get("/{session_id}/files")
    def get_files(session_id: str) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        return JSONResponse(_session_files_payload(session_dir))

    app.include_router(router)


def _run_view_runtime_defaults(project: Path) -> dict[str, Any]:
    try:
        defaults = serialize_project(project).get("defaults")
    except Exception:
        return {}
    return defaults if isinstance(defaults, dict) else {}


def _action_to_json(result) -> dict:
    return {
        "ok": result.ok,
        "message": result.message,
        "severity": result.severity,
    }


def _summarize_session(session_dir: Path, session_id: str) -> dict[str, Any]:
    """Build a compact session-summary dict for the landing-page card list (B4).

    Reads ``summary.json`` (preferred), ``proof-packet.json``,
    ``spec/spec.json``, and ``spec/lifecycle.json`` directly — avoids the
    full ``build_run_view`` cost when N sessions are listed.

    All fields are best-effort: missing files yield ``None`` rather than
    raising, so the landing page can render ``"—"`` placeholders without
    failing the whole list.
    """
    summary = _read_json(session_dir / "summary.json") or {}
    proof = _read_json(session_dir / "proof-packet.json") or {}
    spec = _read_json(session_dir / "spec" / "spec.json") or {}
    lifecycle = _read_json(session_dir / "spec" / "lifecycle.json") or {}

    intent = summary.get("intent") or proof.get("intent") or spec.get("intent")
    verdict = summary.get("verdict") or proof.get("verdict")
    status = summary.get("status") or verdict
    cost_usd = summary.get("cost_usd")
    if cost_usd is None:
        cost_usd = proof.get("cost_usd")
    wall_s = summary.get("wall_s")
    if wall_s is None:
        wall_s = proof.get("wall_s")

    features = proof.get("features") if isinstance(proof.get("features"), list) else []
    if not features and isinstance(spec.get("features"), list):
        features = spec.get("features", [])
    feature_total = len(features)
    feature_passed = sum(
        1 for f in features
        if isinstance(f, dict) and str(f.get("verdict") or "") == "passed"
    )

    findings = (
        proof.get("quality_findings")
        or proof.get("findings")
        or []
    )
    critical_findings = 0
    if isinstance(findings, list):
        for f in findings:
            sev = ""
            if isinstance(f, dict):
                sev = str(f.get("severity") or "").lower()
            elif isinstance(f, str):
                sev = ""
            if sev in ("critical", "blocking"):
                critical_findings += 1

    quality_score = proof.get("quality_score") or summary.get("quality_score")

    # R3-B10: surface the wireframe's "Built in N groups" subline. Prefer the
    # spec.json count (concrete, set at compile time); fall back to the
    # proof packet's landed_group_ids when spec.json is absent on legacy
    # sessions. Returns None when neither source exists so the frontend can
    # suppress the subline rather than rendering "0 groups".
    spec_groups = spec.get("groups") if isinstance(spec.get("groups"), list) else None
    if spec_groups is not None:
        group_count: int | None = len(spec_groups)
    else:
        landed = proof.get("landed_group_ids")
        blocked = proof.get("blocked_group_ids")
        if isinstance(landed, list) or isinstance(blocked, list):
            group_count = len(landed or []) + len(blocked or [])
        else:
            group_count = None

    return {
        "id": session_id,
        "intent": intent,
        "status": status,
        "verdict": verdict,
        "cost_usd": cost_usd,
        "wall_s": wall_s,
        "feature_total": feature_total,
        "feature_passed": feature_passed,
        "critical_findings": critical_findings,
        "quality_score": quality_score,
        "group_count": group_count,
        "finished_at": _session_finished_at(session_dir, summary, verdict),
        "lifecycle": lifecycle.get("lifecycle"),
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _session_finished_at(
    session_dir: Path,
    summary: dict[str, Any],
    verdict: Any,
) -> str | None:
    """Best-effort finished_at: summary.json field, then journal scan, then mtime.

    Pre-verdict (in-flight) sessions return ``None``.
    """
    if summary.get("finished_at"):
        return str(summary["finished_at"])
    if verdict in (None, ""):
        return None
    journal = session_dir / "spec-state.jsonl"
    if not journal.exists():
        journal = session_dir / "state.jsonl"
    if journal.exists():
        try:
            for line in reversed(journal.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = str(event.get("event") or event.get("kind") or "")
                if kind == "run.finished":
                    ts = event.get("ts") or event.get("timestamp")
                    if ts:
                        return str(ts)
        except OSError:
            pass
    # Fallback: mtime of proof-packet (when the run actually wrote its
    # final artifact) or summary.json.
    for candidate in (
        session_dir / "proof-packet.json",
        session_dir / "summary.json",
    ):
        if candidate.exists():
            try:
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(
                    candidate.stat().st_mtime, tz=timezone.utc
                )
                return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                continue
    return None


_LOG_NAME_LIMIT = 64
_LOG_TEXT_LIMIT = 32_000
_FILE_LIST_LIMIT = 300
_DIFF_TEXT_LIMIT = 120_000
_SAFE_GROUP_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_group_id(group_id: str) -> str:
    normalized = str(group_id or "").strip()
    if not normalized or not _SAFE_GROUP_RE.fullmatch(normalized):
        raise HTTPException(status_code=404, detail="group not found")
    return normalized


def _session_logs_payload(session_dir: Path, *, group_id: str | None = None) -> dict[str, Any]:
    safe_group_id = _safe_resource_id(group_id)
    candidates = [
        session_dir / "spec-state.jsonl",
        session_dir / "state.jsonl",
    ]
    if safe_group_id:
        for pattern in (
            f"build/{safe_group_id}/**/*.log",
            f"build/{safe_group_id}/**/*.jsonl",
            f"build/{safe_group_id}/**/*.md",
            f"audit/{safe_group_id}/**/*.log",
            f"audit/{safe_group_id}/**/*.jsonl",
        ):
            candidates.extend(sorted(session_dir.glob(pattern)))
    else:
        candidates.extend([
            session_dir / "summary.json",
            session_dir / "proof-packet.json",
        ])
        for pattern in (
            "spec/**/*.log",
            "spec/**/*.jsonl",
            "spec/**/*.md",
            "build/**/*.log",
            "build/**/*.jsonl",
            "certify/**/*.log",
            "certify/**/*.jsonl",
            "audit/**/*.log",
            "audit/**/*.jsonl",
        ):
            candidates.extend(sorted(session_dir.glob(pattern)))

    seen: set[Path] = set()
    logs: list[dict[str, Any]] = []
    for path in candidates:
        resolved = path.resolve(strict=False)
        if resolved in seen or not path.exists() or not path.is_file():
            continue
        seen.add(resolved)
        label = str(path.relative_to(session_dir))
        stat = path.stat()
        logs.append(
            {
                "label": label[:_LOG_NAME_LIMIT],
                "path": label,
                "size_bytes": stat.st_size,
                "text": _tail_text(path, _LOG_TEXT_LIMIT),
                "truncated": stat.st_size > _LOG_TEXT_LIMIT,
            }
        )
    payload: dict[str, Any] = {
        "session_id": session_dir.name,
        "logs": logs,
        "empty": not logs,
    }
    if safe_group_id:
        payload["group_id"] = safe_group_id
    return payload


def _session_provider_events_payload(
    session_dir: Path,
    *,
    after: int = -1,
    limit: int = 500,
) -> dict[str, Any]:
    """Return sequence-aware provider events without prompt/log text.

    This is deliberately narrower than `/logs`: consumers get metadata
    rows that are safe to poll frequently, while full transcripts remain
    behind the explicit logs panel.
    """
    events: list[dict[str, Any]] = []
    seq = -1
    max_items = max(1, min(int(limit or 500), 1000))
    threshold = int(after)

    for path in sorted(session_dir.rglob("messages.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("type") not in {"provider_event", "result"}:
                        continue
                    if record.get("type") == "result" and not record.get("structured_output_error"):
                        continue
                    seq += 1
                    if seq <= threshold:
                        continue
                    projected = _project_provider_event_record(record)
                    projected["seq"] = seq
                    projected["path"] = str(path.relative_to(session_dir))
                    events.append(projected)
                    if len(events) >= max_items:
                        return {
                            "session_id": session_dir.name,
                            "events": events,
                            "next_after": events[-1]["seq"],
                            "truncated": True,
                        }
        except OSError:
            continue
    return {
        "session_id": session_dir.name,
        "events": events,
        "next_after": events[-1]["seq"] if events else threshold,
        "truncated": False,
    }


def _project_provider_event_record(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("type") == "result":
        return {
            "type": "result",
            "ts": record.get("ts"),
            "elapsed_s": record.get("elapsed_s"),
            "session_id": record.get("session_id") or "",
            "event": "structured_output_error",
            "provider": "",
            "method": "",
            "turn_id": "",
            "status": "error",
            "usage": record.get("usage") if isinstance(record.get("usage"), dict) else {},
            "data": {
                "structured_output_error": str(record.get("structured_output_error") or ""),
            },
        }
    return {
        "type": "provider_event",
        "ts": record.get("ts"),
        "elapsed_s": record.get("elapsed_s"),
        "session_id": record.get("session_id") or "",
        "event": record.get("event") or "",
        "provider": record.get("provider") or "",
        "method": record.get("method") or "",
        "turn_id": record.get("turn_id") or "",
        "status": record.get("status") or "",
        "usage": record.get("usage") if isinstance(record.get("usage"), dict) else {},
        "data": record.get("data") if isinstance(record.get("data"), dict) else {},
    }


def _group_diff_payload(session_dir: Path, group: dict[str, Any]) -> dict[str, Any]:
    group_id = str(group.get("id") or "")
    branch = str(group.get("branch") or "").strip()
    try:
        text = _session_diff_text(session_dir, group_id=group_id)
    except HTTPException:
        raise
    except Exception as exc:
        return {
            "session_id": session_dir.name,
            "group_id": group_id,
            "branch": branch,
            "diff": "",
            "truncated": False,
            "error": str(exc),
        }
    truncated = len(text) > _DIFF_TEXT_LIMIT
    return {
        "session_id": session_dir.name,
        "group_id": group_id,
        "branch": branch,
        "diff": text[:_DIFF_TEXT_LIMIT],
        "truncated": truncated,
        "error": None,
    }


def _session_files_payload(session_dir: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(session_dir.rglob("*")):
        if not path.is_file():
            continue
        if len(files) >= _FILE_LIST_LIMIT:
            truncated = True
            break
        rel = str(path.relative_to(session_dir))
        files.append(
            {
                "path": rel,
                "size_bytes": path.stat().st_size,
                "kind": _file_kind(path),
            }
        )
    return {
        "session_id": session_dir.name,
        "files": files,
        "truncated": truncated,
    }


_ASSET_ATTR_RE = re.compile(r'\b(?P<attr>src|href)="(?P<url>[^"]+)"')
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _rewrite_proof_asset_urls(html: str, *, session_id: str) -> str:
    """Route proof-packet relative artifact links back through RunView."""

    def repl(match: re.Match[str]) -> str:
        attr = match.group("attr")
        url = match.group("url")
        if (
            not url
            or url.startswith(("#", "/", "?"))
            or _URL_SCHEME_RE.match(url)
        ):
            return match.group(0)
        rewritten = (
            f"/api/run-view/{quote(session_id, safe='')}/evidence"
            f"?path={quote(url, safe='')}"
        )
        return f'{attr}="{rewritten}"'

    return _ASSET_ATTR_RE.sub(repl, html)


def _session_worktree_root(session_dir: Path) -> Path:
    """Return the worktree root that owns a session directory."""

    parts = session_dir.parts
    try:
        sessions_index = len(parts) - 1 - list(reversed(parts)).index("sessions")
    except ValueError:
        return session_dir
    # Expected: <worktree>/otto_logs/sessions/<session_id>.
    if sessions_index >= 1 and parts[sessions_index - 1] == "otto_logs":
        return Path(*parts[: sessions_index - 1])
    return session_dir


def _is_allowed_evidence_target(target: Path, session_dir: Path) -> bool:
    allowed_roots = [session_dir.resolve(strict=False)]
    worktree_root = _session_worktree_root(session_dir).resolve(strict=False)
    if worktree_root not in allowed_roots:
        allowed_roots.append(worktree_root)
    for root in allowed_roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _session_diff_text(session_dir: Path, *, group_id: str | None = None) -> str:
    safe_group_id = _safe_resource_id(group_id)
    provider_patches = _provider_diff_patches(session_dir, group_id=safe_group_id)
    worktree = _diff_worktree_root(session_dir, safe_group_id)
    if not (worktree / ".git").exists():
        if provider_patches:
            return _provider_diff_section(
                provider_patches,
                group_id=safe_group_id,
                prefix="No git worktree is available for this session.",
            )
        return "No git worktree is available for this session.\n"
    pathspecs = (
        _group_pathspecs(session_dir, safe_group_id)
        if safe_group_id
        else _whole_run_pathspecs()
    )
    path_args = ["--", *pathspecs] if pathspecs else []
    base = _choose_diff_base(worktree)
    status = _git_text(["status", "--short", "--untracked-files=all", *path_args], worktree)
    sections = [
        f"Scope: group {safe_group_id}" if safe_group_id else "Scope: whole run",
        f"Base: {base}" if base else "Base: not found",
        "",
    ]
    if status.strip():
        sections.extend(["Working tree status:", status.rstrip(), ""])
    if not base:
        sections.append("No diff base found.\n")
        return "\n".join(sections).rstrip() + "\n"

    stat = _git_text(["diff", "--stat", f"{base}...HEAD", *path_args], worktree)
    names = _git_text(["diff", "--name-status", f"{base}...HEAD", *path_args], worktree)
    patch = _git_text(["diff", "--find-renames", f"{base}...HEAD", *path_args], worktree)
    if stat.strip():
        sections.extend(["Committed branch summary:", stat.rstrip(), ""])
    if names.strip():
        sections.extend(["Committed branch files:", names.rstrip(), ""])
    if patch.strip():
        sections.extend(["Committed branch patch:", patch.rstrip(), ""])

    work_stat = _git_text(["diff", "--stat", "HEAD", *path_args], worktree)
    work_patch = _git_text(["diff", "--find-renames", "HEAD", *path_args], worktree)
    if work_stat.strip():
        sections.extend(["Uncommitted tracked summary:", work_stat.rstrip(), ""])
    if work_patch.strip():
        sections.extend(["Uncommitted tracked patch:", work_patch.rstrip(), ""])

    untracked = _git_text(["ls-files", "--others", "--exclude-standard", *path_args], worktree)
    untracked_paths = [line.strip() for line in untracked.splitlines() if line.strip()]
    if untracked_paths:
        sections.extend(["Untracked files:", "\n".join(untracked_paths), ""])
        preview = _untracked_previews(worktree, untracked_paths)
        if preview:
            sections.extend(["Untracked file previews:", preview.rstrip(), ""])

    if provider_patches:
        sections.extend([
            "Provider live diff patch:",
            _join_provider_patches(provider_patches).rstrip(),
            "",
        ])

    if not any(section.strip() for section in sections[3:]):
        if safe_group_id:
            sections.append("No changes from base or working tree.\n")
        else:
            group_ids = _group_worktree_ids(session_dir)
            if group_ids:
                sections.append(
                    "No whole-run diff is available yet because group branches "
                    "have not been integrated into the task branch. Open a "
                    f"group Diff below for: {', '.join(group_ids)}.\n"
                )
            else:
                sections.append("No changes from base or working tree.\n")
    return "\n".join(sections).rstrip() + "\n"

def _diff_worktree_root(session_dir: Path, group_id: str | None) -> Path:
    if group_id:
        group_worktree = session_dir / "worktrees" / group_id
        if (group_worktree / ".git").exists():
            return group_worktree
    return _session_worktree_root(session_dir)


def _group_worktree_ids(session_dir: Path) -> list[str]:
    root = session_dir / "worktrees"
    if not root.is_dir():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / ".git").exists()
    )


def _whole_run_pathspecs() -> list[str]:
    return [
        ".",
        ":(exclude)otto_logs/**",
        ":(exclude).worktrees/**",
        ":(exclude)_otto_build_logs/**",
        ":(exclude).otto/**",
        ":(exclude)otto_artifacts/**",
    ]


def _provider_diff_patches(session_dir: Path, *, group_id: str | None = None) -> list[Path]:
    if group_id:
        root = session_dir / "build" / group_id
        if not root.exists():
            return []
        return sorted(
            p for p in root.rglob("codex-app-server-diff.patch")
            if p.is_file()
        )
    return sorted(
        p for p in session_dir.rglob("codex-app-server-diff.patch")
        if p.is_file()
    )


def _provider_diff_section(
    patches: list[Path],
    *,
    group_id: str | None,
    prefix: str,
) -> str:
    scope = f"Scope: group {group_id}" if group_id else "Scope: whole run"
    return "\n".join([
        scope,
        prefix,
        "",
        "Provider live diff patch:",
        _join_provider_patches(patches).rstrip(),
        "",
    ]).rstrip() + "\n"


def _join_provider_patches(patches: list[Path]) -> str:
    chunks: list[str] = []
    for patch_path in patches:
        try:
            text = patch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        chunks.append(f"# {patch_path.name} ({patch_path.parent.name})\n{text.rstrip()}")
    return "\n\n".join(chunks) + ("\n" if chunks else "")


_SAFE_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_UNTRACKED_PREVIEW_BYTES = 80_000
_UNTRACKED_FILE_BYTES = 12_000


def _safe_resource_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not _SAFE_RESOURCE_ID_RE.match(text):
        raise HTTPException(status_code=400, detail="invalid group_id")
    return text


def _group_pathspecs(session_dir: Path, group_id: str | None) -> list[str]:
    if not group_id:
        return []
    spec = _read_json(session_dir / "spec" / "spec.json") or {}
    groups = spec.get("groups") if isinstance(spec.get("groups"), list) else []
    for group in groups:
        if not isinstance(group, dict):
            continue
        raw_id = str(group.get("id") or group.get("group_id") or group.get("slice_id") or "")
        if raw_id != group_id:
            continue
        out: list[str] = []
        for raw in group.get("owned_paths") or []:
            pathspec = _safe_pathspec(str(raw))
            if pathspec:
                out.append(pathspec)
        return out
    return []


def _safe_pathspec(value: str) -> str | None:
    text = value.strip()
    if not text or text.startswith("/") or "\x00" in text:
        return None
    parts = Path(text).parts
    if any(part == ".." for part in parts):
        return None
    return text


def _untracked_previews(worktree: Path, paths: list[str]) -> str:
    chunks: list[str] = []
    total = 0
    for rel in paths:
        if total >= _UNTRACKED_PREVIEW_BYTES:
            chunks.append("... preview truncated ...")
            break
        path = (worktree / rel).resolve(strict=False)
        if not _is_allowed_worktree_child(path, worktree) or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            body = f"diff --git a/{rel} b/{rel}\nnew binary file: {rel}\n"
        else:
            text = raw[:_UNTRACKED_FILE_BYTES].decode("utf-8", errors="replace")
            lines = [f"+{line}" for line in text.splitlines()]
            if len(raw) > _UNTRACKED_FILE_BYTES:
                lines.append("+... file preview truncated ...")
            body = "\n".join([
                f"diff --git a/{rel} b/{rel}",
                "new file mode 100644",
                "--- /dev/null",
                f"+++ b/{rel}",
                *lines,
                "",
            ])
        chunks.append(body)
        total += len(body)
    return "\n".join(chunks)


def _is_allowed_worktree_child(path: Path, worktree: Path) -> bool:
    try:
        path.relative_to(worktree.resolve(strict=False))
        return True
    except ValueError:
        return False


def _choose_diff_base(worktree: Path) -> str | None:
    for candidate in ("main", "master"):
        if _git_ok(["rev-parse", "--verify", candidate], worktree):
            return candidate
    remote_head = _git_text(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], worktree).strip()
    if remote_head:
        return remote_head
    return None


def _git_ok(args: list[str], cwd: Path) -> bool:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def _git_text(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return f"git {' '.join(args)} failed: {exc}\n"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return f"git {' '.join(args)} failed: {detail}\n"
    return proc.stdout or ""


def _tail_text(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - limit, 0))
            data = handle.read(limit)
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".json", ".jsonl", ".log", ".md", ".txt"}:
        return suffix.removeprefix(".")
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if suffix in {".mp4", ".webm", ".mov"}:
        return "video"
    return "file"


__all__ = ["install_run_view_routes"]
