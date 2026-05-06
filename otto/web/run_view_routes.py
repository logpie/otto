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

from fastapi import APIRouter, Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from otto.mission_control.actions import (
    execute_abort_group,
    execute_pause_run,
    execute_resume_run,
)
from otto.mission_control.run_view import build_run_view
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
    def proof_packet_html(session_id: str) -> FileResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        path = session_dir / "proof-packet.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="proof packet has not been produced")
        return FileResponse(path, media_type="text/html")

    @router.get("/{session_id}/logs")
    def get_logs(session_id: str) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = resolve_session_dir(project, session_id)
        return JSONResponse(_session_logs_payload(session_dir))

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
    candidates = [
        session_dir / "spec-state.jsonl",
        session_dir / "state.jsonl",
    ]
    if group_id:
        for pattern in (
            f"build/{group_id}/**/*.log",
            f"build/{group_id}/**/*.jsonl",
            f"build/{group_id}/**/*.md",
            f"audit/{group_id}/**/*.log",
            f"audit/{group_id}/**/*.jsonl",
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
    if group_id:
        payload["group_id"] = group_id
    return payload


def _group_diff_payload(session_dir: Path, group: dict[str, Any]) -> dict[str, Any]:
    group_id = str(group.get("id") or "")
    branch = str(group.get("branch") or "").strip()
    candidate_refs = [branch] if branch else []
    fallback_ref = f"i2p/{session_dir.name}/{group_id}" if group_id else ""
    if fallback_ref and fallback_ref not in candidate_refs:
        candidate_refs.append(fallback_ref)
    worktree = session_dir.parents[2]
    last_error = ""
    for ref in candidate_refs:
        try:
            result = subprocess.run(
                [
                    "git",
                    "show",
                    "--format=fuller",
                    "--stat",
                    "--patch",
                    "--find-renames",
                    ref,
                ],
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            text = result.stdout
            truncated = len(text) > _DIFF_TEXT_LIMIT
            return {
                "session_id": session_dir.name,
                "group_id": group_id,
                "branch": ref,
                "diff": text[:_DIFF_TEXT_LIMIT],
                "truncated": truncated,
                "error": None,
            }
        last_error = (result.stderr or result.stdout or "").strip()
    return {
        "session_id": session_dir.name,
        "group_id": group_id,
        "branch": branch,
        "diff": "",
        "truncated": False,
        "error": last_error or "No branch/commit found for this group.",
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
