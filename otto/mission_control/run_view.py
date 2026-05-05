"""Backend emitter for the new design's RunView (research §7 + A4 contract).

`build_run_view(session_dir, *, live_state=None)` reads a session dir and
returns a dict shaped to match `otto/web/client/src/types/run.ts:RunView`.
The dict is what the frontend `<RunDrawer/>` component consumes.

Source-of-truth precedence (research §9.1):
  1. proof-packet.json — preferred when present (post-Render)
  2. spec.json + state events — fallback for in-flight runs
  3. Empty/None for fields not yet computed (mid-Audit features have
     verdict=null, mid-Build stages have duration_s=null, etc.)

The emitter is read-only and pure: no LLM, no side effects. Re-runnable
to refresh the RunView shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_run_view(
    session_dir: Path,
    *,
    live_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return RunView-shaped dict for a session.

    Args:
        session_dir: path to `otto_logs/sessions/<id>/`.
        live_state: optional in-flight state snapshot. If provided,
            takes precedence for stages and feature verdicts (mid-Run
            renders).

    Returns:
        dict matching the RunView TS interface; safe to pass through
        json.dumps for HTTP response.
    """
    proof = _read_proof_packet(session_dir)
    spec = _read_spec(session_dir)
    state_events = _read_state_events(session_dir)

    run_id = session_dir.name
    intent = _str_or_empty(
        (proof.get("intent") if proof else None)
        or (spec.get("intent") if spec else None)
    )
    project_kind = _str_or_empty(
        (proof.get("project_kind") if proof else None)
        or (spec.get("project_kind") if spec else None)
        or "webapp"
    )

    verdict = proof.get("verdict") if proof else None
    if verdict in (None, ""):
        verdict_field: str | None = None
    else:
        verdict_field = str(verdict)

    status = _derive_status(verdict_field, state_events, live_state)

    features = _build_features(spec, proof, live_state)
    groups = _build_groups(spec, proof, state_events, live_state)
    components = _build_components(spec, proof, state_events, live_state)
    guardrails = _build_guardrails(spec, proof)
    stages = _build_stages(state_events, live_state)
    findings = _build_findings(proof)

    cost_usd = float(
        (proof.get("cost_usd") if proof else None)
        or (live_state.get("cost_usd") if live_state else 0.0)
        or 0.0
    )
    wall_s = float(
        (proof.get("wall_s") if proof else None)
        or (live_state.get("wall_s") if live_state else 0.0)
        or 0.0
    )

    meta = {
        "session_id": run_id,
        "spec_path": _string_path_or_empty(session_dir / "spec" / "spec.json"),
        "spec_version": int((spec.get("schema_version") or 1) if spec else 1),
        "proof_packet_html": _proof_packet_path(session_dir, "html"),
        "proof_packet_json": _proof_packet_path(session_dir, "json"),
        "started_at": _started_at(state_events),
        "finished_at": _finished_at(state_events, verdict_field),
        "intent_hash": _str_or_empty(spec.get("intent_hash") if spec else None),
    }

    return {
        "run_id": run_id,
        "status": status,
        "intent": intent,
        "project_kind": project_kind,
        "verdict": verdict_field,
        "features": features,
        "groups": groups,
        "components": components,
        "guardrails": guardrails,
        "stages": stages,
        "cost_usd": cost_usd,
        "wall_s": wall_s,
        "meta": meta,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------


def _read_proof_packet(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "proof-packet.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_spec(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "spec" / "spec.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_state_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "spec-state.jsonl"
    if not path.exists():
        path = session_dir / "state.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    except OSError:
        return []
    return events


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------


def _build_features(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
    live_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Emit FeatureView list for the RunView.

    Evidence-kinds precedence (per-feature merge):
      1. proof-packet entry's `evidence_kinds` if non-empty
      2. spec.features[*].evidence_kinds for the same feature_id
      3. empty list if neither has data
    This prevents the FeatureDrilldown from showing "No evidence kinds
    declared" when the spec declared them but the proof packet entry
    omits them (A4 RUA report bug).
    """
    spec_evidence_by_id = _spec_evidence_kinds_by_id(spec)
    # Prefer proof-packet's features[] (post-Render shape from A3 wiring),
    # but fall back per-feature to spec evidence_kinds when proof's is empty.
    if proof and isinstance(proof.get("features"), list) and proof["features"]:
        return [
            _feature_to_view(f, spec_evidence_by_id) for f in proof["features"]
        ]
    if not spec:
        return []
    raw_features = spec.get("features") or []
    if not isinstance(raw_features, list):
        return []
    return [_feature_to_view(f, spec_evidence_by_id) for f in raw_features]


def _spec_evidence_kinds_by_id(
    spec: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """Index spec.features[*].evidence_kinds by feature id."""
    if not spec:
        return {}
    raw = spec.get("features") or []
    if not isinstance(raw, list):
        return {}
    out: dict[str, list[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("feature_id") or entry.get("id") or "")
        if not fid:
            continue
        kinds = entry.get("evidence_kinds") or []
        if isinstance(kinds, list):
            out[fid] = [str(k) for k in kinds]
    return out


def _feature_to_view(
    payload: dict[str, Any],
    spec_evidence_by_id: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Map proof/spec feature dict to FeatureView shape."""
    feature_id = str(payload.get("feature_id") or payload.get("id") or "")
    payload_kinds = [str(k) for k in (payload.get("evidence_kinds") or [])]
    if not payload_kinds and spec_evidence_by_id:
        payload_kinds = list(spec_evidence_by_id.get(feature_id, []))
    return {
        "id": feature_id,
        "name": str(payload.get("name") or ""),
        "description": str(payload.get("description") or ""),
        "acceptance_detail": str(payload.get("acceptance_detail") or ""),
        "evidence_kinds": payload_kinds,
        "group_id": str(payload.get("group_id") or ""),
        "verdict": payload.get("verdict"),
        "evidence_completeness": str(
            payload.get("evidence_completeness") or "full"
        ),
        "coverage_confidence": str(
            payload.get("coverage_confidence") or "high"
        ),
        "multi_actor_required": bool(payload.get("multi_actor_required", False)),
        "audit_pre_merge": bool(payload.get("audit_pre_merge", False)),
        "evidence_refs": [
            {
                "kind": str(r.get("kind") or "WalkthroughSegment"),
                "path": str(r.get("path") or r) if not isinstance(r, dict) else str(r.get("path") or ""),
                "summary": str(r.get("summary") or "") if isinstance(r, dict) else "",
            }
            for r in (payload.get("evidence_refs") or [])
            if r is not None
        ],
    }


def _build_groups(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
    live_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Emit GroupView list."""
    raw_groups = (
        (proof.get("groups") if proof else None)
        or (spec.get("groups") if spec else None)
        or (spec.get("slices") if spec else None)  # legacy
        or []
    )
    if not isinstance(raw_groups, list):
        return []
    return [_group_to_view(g, state_events) for g in raw_groups]


def _group_to_view(
    payload: dict[str, Any],
    state_events: list[dict[str, Any]],
) -> dict[str, Any]:
    group_id = str(payload.get("id") or payload.get("slice_id") or "")
    return {
        "id": group_id,
        "name": str(
            payload.get("name") or payload.get("title") or group_id
        ),
        "description": str(payload.get("description") or ""),
        "feature_ids": [
            str(fid) for fid in (payload.get("feature_ids") or [])
        ],
        "status": _group_status(payload),
        "branch": str(payload.get("branch") or ""),
        "owned_paths": [
            str(p) for p in (payload.get("owned_paths") or [])
        ],
        "dependencies": [
            str(d) for d in (payload.get("dependencies") or payload.get("deps") or [])
        ],
        "cost_usd": float(payload.get("cost_usd") or 0.0),
        "wall_s": float(payload.get("wall_s") or 0.0),
        "repair_attempts": int(payload.get("repair_attempts") or 0),
    }


def _group_status(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").lower()
    landed = bool(payload.get("landed"))
    if landed:
        return "landed"
    if status in ("passing", "blocked", "in_progress", "pending", "failed_scope"):
        return status
    return "pending"


def _build_components(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
    live_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Emit ComponentView list."""
    raw_components = (
        (proof.get("components") if proof else None)
        or (spec.get("components") if spec else None)
        or []
    )
    if not isinstance(raw_components, list):
        return []
    return [_component_to_view(c) for c in raw_components]


def _component_to_view(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("id") or "")
    return {
        "id": cid,
        "name": str(payload.get("name") or cid),
        "description": str(payload.get("description") or ""),
        "status": str(payload.get("status") or "pending"),
        "owned_paths": [str(p) for p in (payload.get("owned_paths") or [])],
        "dependencies": [str(d) for d in (payload.get("dependencies") or [])],
        "consumed_by": [str(f) for f in (payload.get("consumed_by") or [])],
        "cost_usd": float(payload.get("cost_usd") or 0.0),
        "wall_s": float(payload.get("wall_s") or 0.0),
    }


def _build_guardrails(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Emit GuardrailView list."""
    raw = (
        (proof.get("guardrails") if proof else None)
        or (spec.get("guardrails") if spec else None)
        or []
    )
    if not isinstance(raw, list):
        return []
    return [
        {
            "id": str(g.get("id") or ""),
            "text": str(g.get("text") or ""),
            "applies_to": str(g.get("applies_to") or "*"),
            "verified": g.get("verified"),  # None pre-Audit; bool post-Audit
        }
        for g in raw
        if isinstance(g, dict)
    ]


def _build_stages(
    state_events: list[dict[str, Any]],
    live_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Emit StageView list from state events.

    Each Stage's name comes from the canonical pipeline order:
    compile → spec_review → build → seed → audit → render → land.
    Status is derived from event presence:
      - active if started but not finished
      - done if finished
      - failed if explicit failure event
      - skipped if marked skipped
      - pending if no start event
    """
    canonical = ("compile", "spec_review", "build", "seed", "audit", "render", "land")
    stages: dict[str, dict[str, Any]] = {
        name: {
            "name": name,
            "status": "pending",
            "duration_s": None,
            "cost_usd": None,
            "started_at": None,
            "finished_at": None,
        }
        for name in canonical
    }

    for event in state_events:
        kind = str(event.get("event") or event.get("kind") or "")
        stage_name, sub = _stage_event_target(kind)
        if stage_name is None or sub is None:
            continue
        if stage_name not in stages:
            continue
        ts = event.get("ts") or event.get("timestamp")
        if sub == "started":
            stages[stage_name]["started_at"] = ts
            stages[stage_name]["status"] = "active"
        elif sub == "finished":
            stages[stage_name]["finished_at"] = ts
            # Two-shape finish: stage.<x>.finished is always success;
            # for bare "<stage>.finished" we look at extra.succeeded so a
            # seed that bails out lands in "failed", not "done".
            succeeded = _event_succeeded(event)
            stages[stage_name]["status"] = "done" if succeeded else "failed"
            duration = event.get("duration_s")
            if duration is not None:
                stages[stage_name]["duration_s"] = float(duration)
            cost = event.get("cost_usd")
            if cost is not None:
                stages[stage_name]["cost_usd"] = float(cost)
        elif sub == "failed":
            stages[stage_name]["status"] = "failed"
            stages[stage_name]["finished_at"] = ts
        elif sub == "skipped":
            stages[stage_name]["status"] = "skipped"

    return [stages[name] for name in canonical]


# Stage names that emit bare "<stage>.<sub>" lifecycle events (no
# "stage." prefix). Currently only `seed` — other stages still use
# the "stage.<name>.<sub>" pattern for orchestrator-level lifecycle.
_BARE_STAGE_EVENT_PREFIXES: tuple[str, ...] = ("seed",)


def _stage_event_target(kind: str) -> tuple[str | None, str | None]:
    """Resolve an event kind to (stage_name, sub-event) or (None, None).

    Recognises both:
      * "stage.<name>.<sub>"   — orchestrator-level pattern (build/audit/...)
      * "<name>.<sub>"         — per-stage emit (currently `seed.*`)
    """
    if kind.startswith("stage."):
        parts = kind.split(".", 2)
        if len(parts) < 3:
            return None, None
        return parts[1], parts[2]
    for prefix in _BARE_STAGE_EVENT_PREFIXES:
        if kind.startswith(prefix + "."):
            sub = kind[len(prefix) + 1 :]
            return prefix, sub
    return None, None


def _event_succeeded(event: dict[str, Any]) -> bool:
    """Decide whether a *.finished event represents success.

    Reads the `extra.succeeded` flag emitted by `otto.seed`; absent flag
    → assume success (matches existing stage.<x>.finished semantics).
    """
    extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
    if extra and "succeeded" in extra:
        return bool(extra.get("succeeded"))
    if "succeeded" in event:
        return bool(event.get("succeeded"))
    return True


# Severity vocabulary normalization (research §4 severity ladder).
#
# Canonical severities are `critical` / `important` / `polish` — matching the
# TS `FindingSeverity` union (`otto/web/client/src/types/run.ts`) and the
# `FINDING_SEVERITIES` tuple in `otto/spec_compile.py`. Legacy proof packets
# (and seed fixtures pre-dating the rename) emit synonyms like `blocking`,
# `high`, `low`. Translate at parse-time so the frontend always sees the
# canonical names — `VerdictHeader` filters on `severity === "critical"` and
# would otherwise count zero blocking findings (RUA tick 62 W6-C).
_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "blocking": "critical",   # legacy synonym
    "high": "important",       # legacy synonym
    "important": "important",
    "medium": "important",     # legacy synonym
    "polish": "polish",
    "low": "polish",           # legacy synonym
    "minor": "polish",         # legacy synonym
}
_SEVERITY_DEFAULT = "important"


def _normalize_severity(raw: Any) -> str:
    """Map legacy/synonym severity strings to the canonical vocabulary.

    Unknown values fall back to `important` (the conservative middle of the
    ladder) rather than dropping the finding entirely.
    """
    if raw is None:
        return _SEVERITY_DEFAULT
    key = str(raw).strip().lower()
    if not key:
        return _SEVERITY_DEFAULT
    return _SEVERITY_ALIASES.get(key, _SEVERITY_DEFAULT)


def _build_findings(proof: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Emit FindingView list (severity-tagged quality findings, research §4).

    Severity is normalized to the canonical `critical`/`important`/`polish`
    vocabulary — see `_SEVERITY_ALIASES` for accepted legacy synonyms.
    """
    if not proof:
        return []
    raw = proof.get("quality_findings") or proof.get("findings") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for f in raw:
        if isinstance(f, str):
            # Legacy: bare string finding; default severity=important
            out.append(
                {"severity": _SEVERITY_DEFAULT, "text": f, "feature_id": ""}
            )
        elif isinstance(f, dict):
            out.append({
                "severity": _normalize_severity(f.get("severity")),
                "text": str(f.get("text") or f.get("message") or ""),
                "feature_id": str(f.get("feature_id") or ""),
            })
    return out


# ---------------------------------------------------------------------------
# Status / timing derivation
# ---------------------------------------------------------------------------


def _derive_status(
    verdict: str | None,
    state_events: list[dict[str, Any]],
    live_state: dict[str, Any] | None,
) -> str:
    """Compute RunStatus from verdict + events."""
    if verdict == "passed":
        return "passed"
    if verdict == "partial":
        return "partial"
    if verdict == "blocked":
        return "blocked"
    # In-flight or pre-verdict — derive from latest stage event
    if live_state and live_state.get("status"):
        return str(live_state["status"])
    last_started = None
    for event in state_events:
        kind = str(event.get("event") or event.get("kind") or "")
        stage_name, sub = _stage_event_target(kind)
        if stage_name is not None and sub == "started":
            last_started = stage_name
    if last_started:
        # Map stage name to RunStatus enum value
        return {
            "compile": "compiling",
            "spec_review": "awaiting_spec_review",
            "build": "building",
            "seed": "building",
            "audit": "auditing",
            "render": "rendering",
            "land": "landing",
        }.get(last_started, "queued")
    return "queued"


def _started_at(state_events: list[dict[str, Any]]) -> str:
    for event in state_events:
        if str(event.get("event") or event.get("kind") or "") == "run.started":
            return str(event.get("ts") or event.get("timestamp") or "")
    return ""


def _finished_at(
    state_events: list[dict[str, Any]],
    verdict: str | None,
) -> str | None:
    if verdict is None:
        return None
    for event in reversed(state_events):
        kind = str(event.get("event") or event.get("kind") or "")
        if kind == "run.finished":
            return str(event.get("ts") or event.get("timestamp") or "")
    return None


# ---------------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------------


def _str_or_empty(value: Any) -> str:
    return str(value) if value is not None else ""


def _string_path_or_empty(path: Path) -> str:
    return str(path) if path.exists() else ""


def _proof_packet_path(session_dir: Path, ext: str) -> str | None:
    p = session_dir / f"proof-packet.{ext}"
    return str(p) if p.exists() else None


__all__ = ["build_run_view"]
