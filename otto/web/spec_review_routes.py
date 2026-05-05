"""FastAPI routes for the spec-review surface (research §2.1, A5).

Mounts three endpoints under `/api/specs/<session_id>`:

* `GET    /api/specs/<session_id>/markdown` — return the spec as a
  `SpecMdView` payload (markdown + intent_hash + lifecycle).
* `POST   /api/specs/<session_id>/edit` — accept edited markdown,
  parse it back to a `Spec`, archive the prior version as
  `spec-v<N>.json/.md`, and overwrite `spec.json` + `spec.md`.
* `POST   /api/specs/<session_id>/approve` — flip the spec's
  lifecycle from `draft` to `approved`. Idempotent.

The `spec_id` URL parameter is currently the `session_id` (one spec
per session). Lifecycle is stored alongside the spec in
`<session>/spec/lifecycle.json` to avoid mutating the canonical
`Spec` dataclass schema.

Tier-1 invariants (intent + intent_hash) are enforced via the
client-supplied `intent_hash` mismatch check (HTTP 409) — pre-approval
edits cannot drop or replace the bedrock intent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from otto.paths import session_dir, spec_dir
from otto.spec_amend import compute_invalidation
from otto.spec_compile import (
    Spec,
    load_spec,
    parse_spec_md,
    render_spec_md,
    spec_to_dict,
)
from otto.spec_state import emit as emit_state_event, is_run_paused_by_user

logger = logging.getLogger("otto.web.spec_review")

LIFECYCLE_FILENAME = "lifecycle.json"
SPEC_JSON_FILENAME = "spec.json"
SPEC_MD_FILENAME = "spec.md"

VALID_LIFECYCLES = ("draft", "approved", "amended", "editing_in_flight")


class SpecEditPayload(BaseModel):
    intent_hash: str
    markdown: str


def install_spec_review_routes(
    app: FastAPI,
    *,
    project_dir: Path | None = None,
    project_dir_provider: Callable[[], Path | None] | None = None,
) -> None:
    """Mount `/api/specs/*` routes onto the given FastAPI app."""
    if project_dir_provider is None and project_dir is None:
        raise ValueError(
            "install_spec_review_routes needs project_dir or project_dir_provider"
        )

    def _resolve_project_dir() -> Path:
        if project_dir_provider is not None:
            resolved = project_dir_provider()
        else:
            resolved = project_dir
        if resolved is None:
            raise HTTPException(status_code=409, detail="No project selected.")
        return resolved

    router = APIRouter(prefix="/api/specs", tags=["spec-review"])

    @router.get("/{session_id}/markdown")
    def get_markdown(session_id: str) -> JSONResponse:
        project = _resolve_project_dir()
        sd = _resolve_spec_dir(project, session_id)
        spec = _load_or_404(sd)
        markdown = _read_or_render_md(sd, spec)
        lifecycle = _read_lifecycle(sd)
        # Emit spec.review.opened on first GET so downstream observers
        # (Mission Control timeline, audit trail) see the user-facing
        # review session start. We dedupe at the journal level: only
        # emit if no prior spec.review.opened exists for this session.
        _emit_review_opened_once(project, session_id)
        view = _build_view(
            session_id=session_id,
            spec=spec,
            markdown=markdown,
            lifecycle=lifecycle,
            updated_at=_mtime_iso(sd / SPEC_JSON_FILENAME),
        )
        return JSONResponse(view)

    @router.post("/{session_id}/edit")
    def post_edit(session_id: str, payload: SpecEditPayload) -> JSONResponse:
        project = _resolve_project_dir()
        sd = _resolve_spec_dir(project, session_id)
        spec = _load_or_404(sd)

        if payload.intent_hash != spec.intent_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "intent_hash mismatch — your edit is based on a stale "
                    "spec snapshot. Reload and reapply."
                ),
            )

        lifecycle = _read_lifecycle(sd)
        # A6: post-approval edits are normally rejected (the user is
        # supposed to use the amendment flow). The exception is
        # `editing_in_flight` — the runner sets this lifecycle while
        # a build is actively running, opening a single-window edit
        # path that can invalidate in-flight Groups via journal events.
        if lifecycle == "approved":
            raise HTTPException(
                status_code=409,
                detail=(
                    "spec is already approved; post-approval edits go through "
                    "the amendment flow, not this endpoint"
                ),
            )
        # Round-3 audit gap 2: if the underlying run is operator-paused
        # (`run.paused_by_user` not yet cleared), refuse edits — the
        # invariant is "edit lands in the journal between phase boundaries
        # while the runner is awake to act on it". Emitting
        # `group.invalidated_by_spec_edit` against a paused runner just
        # accumulates dead invalidations that the operator must then
        # disentangle on resume.
        if is_run_paused_by_user(session_dir(project, session_id)):
            raise HTTPException(
                status_code=409,
                detail=(
                    "session is paused — resume the run before editing the spec"
                ),
            )

        try:
            edited, warnings = parse_spec_md(payload.markdown, base=spec)
        except Exception as exc:  # noqa: BLE001 — surface parse errors verbatim
            raise HTTPException(
                status_code=400,
                detail=f"failed to parse spec markdown: {exc}",
            ) from exc

        invalidation = (
            compute_invalidation(spec, edited)
            if lifecycle == "editing_in_flight"
            else None
        )

        _archive_current_version(sd)
        _write_spec(sd, edited, payload.markdown)

        sess_dir = session_dir(project, session_id)
        emit_state_event(
            sess_dir,
            "spec.edited",
            detail=f"warnings={len(warnings)}",
            warnings=list(warnings),
        )
        if invalidation is not None:
            for entry in invalidation.entries:
                emit_state_event(
                    sess_dir,
                    "group.invalidated_by_spec_edit",
                    group_id=entry.group_id,
                    detail=entry.reason,
                    direct=entry.direct,
                )

        view = _build_view(
            session_id=session_id,
            spec=edited,
            markdown=payload.markdown,
            lifecycle=lifecycle,
            updated_at=_mtime_iso(sd / SPEC_JSON_FILENAME),
        )
        return JSONResponse({"view": view, "warnings": list(warnings)})

    @router.post("/{session_id}/approve")
    def post_approve(session_id: str) -> JSONResponse:
        project = _resolve_project_dir()
        sd = _resolve_spec_dir(project, session_id)
        spec = _load_or_404(sd)
        # Round-3 audit gap 2: lifecycle precondition.
        # Approval is only valid from `draft` (initial gate) or `approved`
        # (idempotent re-confirm — the A13 review-gate may need a second
        # `spec.review_approved` event after a runner restart). All other
        # lifecycles indicate concurrent surgery that should not be
        # rubber-stamped via /approve:
        #   - `editing_in_flight`: a build is running and the spec is
        #     mid-edit window. Approving from this state would race the
        #     runner's `_set_lifecycle_best_effort("approved")` finally
        #     branch.
        #   - `amended`: amendment flow owns the lifecycle transition.
        current_lifecycle = _read_lifecycle(sd)
        if current_lifecycle not in ("draft", "approved"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot approve from lifecycle={current_lifecycle!r}; "
                    "approve is allowed only from draft or approved"
                ),
            )
        # Refuse approval while the run is paused — the runner is asleep
        # at a phase boundary; emitting `spec.review_approved` will be
        # delivered eventually but the operator's expectation that
        # approve unblocks "now" is broken. Force them to resume first.
        if is_run_paused_by_user(session_dir(project, session_id)):
            raise HTTPException(
                status_code=409,
                detail=(
                    "session is paused — resume the run before approving "
                    "the spec"
                ),
            )
        was_already_approved = current_lifecycle == "approved"
        _write_lifecycle(sd, "approved")
        # Only emit spec.approved on the lifecycle transition. Repeated
        # approve calls are idempotent at the data layer; we don't want
        # to spam the journal with duplicate approval events.
        if not was_already_approved:
            emit_state_event(
                session_dir(project, session_id),
                "spec.approved",
                detail=f"intent_hash={spec.intent_hash[:16]}",
            )
        # A13 — review-gate signal. Always emit on POST /approve so a
        # runner blocked at the gate wakes up even if the spec was
        # already lifecycle-approved (e.g. user clicks Approve again
        # after a transient runner restart). The runner reads "any
        # spec.review_approved exists" as the resume signal; repeats
        # are bounded by user clicks and harmless at the journal layer.
        emit_state_event(
            session_dir(project, session_id),
            "spec.review_approved",
            detail=f"intent_hash={spec.intent_hash[:16]}",
        )
        markdown = _read_or_render_md(sd, spec)
        view = _build_view(
            session_id=session_id,
            spec=spec,
            markdown=markdown,
            lifecycle="approved",
            updated_at=_mtime_iso(sd / LIFECYCLE_FILENAME),
        )
        return JSONResponse({"view": view})

    @router.get("/{session_id}/versions")
    def get_versions(session_id: str) -> JSONResponse:
        """List archived spec versions (wireframe 4d).

        Returns a sorted ascending list of integer version numbers
        derived from filenames matching `spec-v<N>.json` in the
        session's spec dir. The live `spec.json` is *not* included —
        only archived prior versions. Empty list if no edits have
        been made yet.
        """
        project = _resolve_project_dir()
        sd = _resolve_spec_dir(project, session_id)
        versions = sorted(_list_archived_versions(sd))
        return JSONResponse({"session_id": session_id, "versions": versions})

    @router.get("/{session_id}/diff")
    def get_diff(
        session_id: str,
        from_version: str = Query(..., alias="from"),
        to_version: str = Query(..., alias="to"),
    ) -> JSONResponse:
        """Return paired markdown + parsed JSON for two spec versions
        (wireframe 4d). Each side may be:

        * an integer ``N`` referencing an archived ``spec-v<N>.{md,json}``
        * the literal string ``"current"`` referencing the live working
          ``spec.{json,md}`` (B28)

        ``from_version``/``to_version`` in the response are the integer
        version when archived, or the string ``"current"`` for the live
        working spec — the frontend renders both. The frontend is
        responsible for rendering the line-level diff.
        """
        project = _resolve_project_dir()
        sd = _resolve_spec_dir(project, session_id)
        from_md, from_json, from_label = _resolve_diff_side(sd, from_version)
        to_md, to_json, to_label = _resolve_diff_side(sd, to_version)
        return JSONResponse({
            "from_version": from_label,
            "to_version": to_label,
            "from_md": from_md,
            "to_md": to_md,
            "from_json": from_json,
            "to_json": to_json,
        })

    app.include_router(router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_spec_dir(project_dir: Path, session_id: str) -> Path:
    """Resolve `<project>/otto_logs/sessions/<session_id>/spec/`.

    Rejects path-traversal (e.g. `"../etc"`).
    """
    sessions_root = (project_dir / "otto_logs" / "sessions").resolve()
    candidate_session = (sessions_root / session_id).resolve()
    try:
        candidate_session.relative_to(sessions_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"session id rejected: {session_id!r}",
        ) from exc
    sd = spec_dir(project_dir, session_id).resolve()
    if not sd.exists() or not sd.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"spec dir not found for session {session_id!r}",
        )
    return sd


def _load_or_404(sd: Path) -> Spec:
    spec_path = sd / SPEC_JSON_FILENAME
    if not spec_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"spec.json missing in {sd}",
        )
    return load_spec(spec_path)


def _read_or_render_md(sd: Path, spec: Spec) -> str:
    md_path = sd / SPEC_MD_FILENAME
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return render_spec_md(spec)


def _archive_current_version(sd: Path) -> None:
    """Snapshot the current spec.json/spec.md as spec-v<N>.{json,md}."""
    n = _next_version_index(sd)
    cur_json = sd / SPEC_JSON_FILENAME
    cur_md = sd / SPEC_MD_FILENAME
    if cur_json.exists():
        (sd / f"spec-v{n}.json").write_text(
            cur_json.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if cur_md.exists():
        (sd / f"spec-v{n}.md").write_text(
            cur_md.read_text(encoding="utf-8"), encoding="utf-8"
        )


def _next_version_index(sd: Path) -> int:
    return max(_list_archived_versions(sd), default=0) + 1


def _list_archived_versions(sd: Path) -> list[int]:
    """Return all integer N for which `spec-v<N>.json` exists in ``sd``."""
    nums: list[int] = []
    for p in sd.glob("spec-v*.json"):
        try:
            nums.append(int(p.stem.removeprefix("spec-v")))
        except ValueError:
            continue
    return nums


def _resolve_diff_side(sd: Path, value: str) -> tuple[str, dict, int | str]:
    """Resolve a diff-side query value to (markdown, parsed_json, label).

    ``value`` may be:
      * an integer string referencing an archived ``spec-v<N>.{md,json}``
      * the literal ``"current"`` referencing the live working spec
        (``spec.json`` + ``spec.md``)

    Returns a 3-tuple where the third element is either the int version
    (for archived sides) or the string ``"current"`` so the response
    payload echoes back exactly what the client requested.
    """
    if value == "current":
        spec_path = sd / SPEC_JSON_FILENAME
        md_path = sd / SPEC_MD_FILENAME
        if not spec_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"current spec.json missing in {sd.name}",
            )
        try:
            parsed = json.loads(spec_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"current spec.json is malformed: {exc}",
            ) from exc
        if md_path.exists():
            md = md_path.read_text(encoding="utf-8")
        else:
            # Fall back to rendering from the parsed spec when spec.md is
            # absent — keeps the diff endpoint functional even when the
            # markdown shadow file was never written.
            try:
                spec_obj = load_spec(spec_path)
                md = render_spec_md(spec_obj)
            except Exception:
                md = ""
        return md, parsed, "current"
    try:
        n = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid version {value!r}; expected an integer or "
                f"the literal 'current'"
            ),
        ) from exc
    if n < 1:
        raise HTTPException(
            status_code=400,
            detail=f"version must be >= 1, got {n}",
        )
    md, parsed = _read_archived_version(sd, n)
    return md, parsed, n


def _read_archived_version(sd: Path, n: int) -> tuple[str, dict]:
    """Read `spec-v<N>.md` + `spec-v<N>.json` or 404."""
    md_path = sd / f"spec-v{n}.md"
    json_path = sd / f"spec-v{n}.json"
    if not md_path.exists() or not json_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"spec version v{n} not found in {sd.name}",
        )
    md = md_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"spec-v{n}.json is malformed: {exc}",
        ) from exc
    return md, parsed


def _write_spec(sd: Path, spec: Spec, markdown: str) -> None:
    (sd / SPEC_JSON_FILENAME).write_text(
        json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (sd / SPEC_MD_FILENAME).write_text(markdown, encoding="utf-8")


def _read_lifecycle(sd: Path) -> str:
    p = sd / LIFECYCLE_FILENAME
    if not p.exists():
        return "draft"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "draft"
    raw = data.get("lifecycle", "draft")
    return raw if raw in VALID_LIFECYCLES else "draft"


def _write_lifecycle(sd: Path, lifecycle: str) -> None:
    if lifecycle not in VALID_LIFECYCLES:
        raise ValueError(f"invalid lifecycle {lifecycle!r}")
    (sd / LIFECYCLE_FILENAME).write_text(
        json.dumps(
            {
                "lifecycle": lifecycle,
                "updated_at": _now_iso(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _build_view(
    *,
    session_id: str,
    spec: Spec,
    markdown: str,
    lifecycle: str,
    updated_at: str,
) -> dict[str, object]:
    return {
        "spec_id": session_id,
        "session_id": session_id,
        "markdown": markdown,
        "intent_hash": spec.intent_hash,
        "lifecycle": lifecycle,
        "updated_at": updated_at,
    }


def _emit_review_opened_once(project_dir: Path, session_id: str) -> None:
    """Emit `spec.review.opened` at most once per session.

    Reads the journal (cheap line scan) and skips emission if any
    `spec.review.opened` event already exists. Repeat GETs from the
    frontend (e.g. user reloads the page) must not multiply the event.
    """
    sd_for_journal = session_dir(project_dir, session_id)
    journal = sd_for_journal / "spec-state.jsonl"
    if journal.exists():
        try:
            text = journal.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if '"kind": "spec.review.opened"' in text:
            return
    emit_state_event(
        sd_for_journal,
        "spec.review.opened",
    )


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime_iso(path: Path) -> str:
    if not path.exists():
        return _now_iso()
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["install_spec_review_routes"]
