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
from typing import Any, Callable


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
    compile_active = _compile_logs_active(session_dir, spec, state_events, proof)

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

    status = _derive_status(
        verdict_field,
        state_events,
        live_state,
        compile_active=compile_active,
    )

    features = _build_features(spec, proof, live_state)
    groups = _build_groups(spec, proof, state_events, live_state)
    components = _build_components(spec, proof, state_events, live_state)
    guardrails = _build_guardrails(spec, proof)
    stages = _build_stages(
        state_events,
        live_state,
        spec,
        proof=proof,
        compile_active=compile_active,
    )
    findings = _build_findings(proof)
    aggregate_group_cost_usd = sum(
        _float_or_zero(group.get("cost_usd")) for group in groups
    )
    aggregate_group_wall_s = sum(
        _float_or_zero(group.get("wall_s")) for group in groups
    )

    cost_usd = _first_nonzero_float(
        proof.get("cost_usd") if proof else None,
        live_state.get("cost_usd") if live_state else None,
        aggregate_group_cost_usd,
    )
    wall_s = _first_nonzero_float(
        proof.get("wall_s") if proof else None,
        live_state.get("wall_s") if live_state else None,
        live_state.get("duration_s") if live_state else None,
        aggregate_group_wall_s,
    )

    meta = {
        "session_id": run_id,
        "spec_path": _string_path_or_empty(session_dir / "spec" / "spec.json"),
        "spec_version": int((spec.get("schema_version") or 1) if spec else 1),
        "proof_packet_html": _proof_packet_path(session_dir, "html"),
        "proof_packet_json": _proof_packet_path(session_dir, "json"),
        "started_at": _started_at(state_events, live_state),
        "finished_at": _finished_at(state_events, verdict_field, live_state),
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


def _compile_logs_active(
    session_dir: Path,
    spec: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
    proof: dict[str, Any] | None,
) -> bool:
    if spec or proof:
        return False
    if any(
        _stage_event_target(str(event.get("event") or event.get("kind") or ""))[0] == "compile"
        for event in state_events
    ):
        return False
    compile_dir = session_dir / "spec" / "compile-agent"
    for name in ("messages.jsonl", "narrative.log", "live.log"):
        path = compile_dir / name
        try:
            if path.exists() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


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
    if isinstance(raw_features, list) and raw_features:
        return [_feature_to_view(f, spec_evidence_by_id) for f in raw_features]
    return _features_from_groups(spec)


def _features_from_groups(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive user-facing features from Group.feature_ids when needed.

    Some early i2p compiler outputs left top-level ``features`` empty while
    preserving the product slices in each group's ``feature_ids``. Showing
    "Features 0/0" for those sessions hides the user's actual requested
    product work, so we synthesize pre-audit FeatureViews from the grouped
    feature ids and declared group checks.
    """

    raw_groups = spec.get("groups") or spec.get("slices") or []
    if not isinstance(raw_groups, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id") or group.get("slice_id") or "")
        group_name = str(group.get("name") or group.get("title") or group_id)
        evidence_kinds = _evidence_kinds_from_checks(group.get("checks"))
        for raw_feature_id in group.get("feature_ids") or []:
            feature_id = str(raw_feature_id).strip()
            if not feature_id or feature_id in seen:
                continue
            seen.add(feature_id)
            out.append(
                {
                    "id": feature_id,
                    "name": _title_from_feature_id(feature_id),
                    "description": f"Part of {group_name}." if group_name else "",
                    "acceptance_detail": "",
                    "evidence_kinds": list(evidence_kinds),
                    "group_id": group_id,
                    "verdict": None,
                    "evidence_completeness": "full",
                    "coverage_confidence": "high",
                    "multi_actor_required": False,
                    "audit_pre_merge": False,
                    "evidence_refs": [],
                }
            )
    return out


def _title_from_feature_id(feature_id: str) -> str:
    cleaned = feature_id.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return feature_id
    return cleaned[:1].upper() + cleaned[1:]


def _evidence_kinds_from_checks(raw_checks: Any) -> list[str]:
    if not isinstance(raw_checks, list):
        return []
    mapped: list[str] = []
    for check in raw_checks:
        if not isinstance(check, dict):
            continue
        raw = str(check.get("kind") or check.get("type") or check.get("check_type") or "")
        key = raw.replace("-", "_").replace(" ", "_").lower()
        kind = {
            "browser_journey": "BrowserJourney",
            "browser": "BrowserJourney",
            "api_probe": "ApiProbe",
            "api": "ApiProbe",
            "state_invariant": "StateInvariant",
            "state": "StateInvariant",
            "repo_test": "RepoTestCheck",
            "repo_test_check": "RepoTestCheck",
            "test": "RepoTestCheck",
            "cli_probe": "CLIProbe",
            "import_check": "ImportCheck",
            "type_check": "TypeCheck",
        }.get(key)
        if kind and kind not in mapped:
            mapped.append(kind)
    return mapped


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
    group_id = str(
        payload.get("id") or payload.get("group_id") or payload.get("slice_id") or ""
    )
    metrics = _group_metrics_from_events(group_id, state_events)
    return {
        "id": group_id,
        "name": str(
            payload.get("name") or payload.get("title") or group_id
        ),
        "description": str(payload.get("description") or ""),
        "feature_ids": [
            str(fid) for fid in (payload.get("feature_ids") or [])
        ],
        "status": _group_status(payload, group_id, state_events),
        "branch": str(payload.get("branch") or ""),
        "owned_paths": [
            str(p) for p in (payload.get("owned_paths") or [])
        ],
        "dependencies": [
            str(d) for d in (payload.get("dependencies") or payload.get("deps") or [])
        ],
        "cost_usd": _first_float(payload.get("cost_usd"), metrics["cost_usd"]),
        "wall_s": _first_float(payload.get("wall_s"), metrics["wall_s"]),
        "repair_attempts": int(payload.get("repair_attempts") or 0),
    }


def _group_status(
    payload: dict[str, Any],
    group_id: str,
    state_events: list[dict[str, Any]],
) -> str:
    status = str(payload.get("status") or "").lower()
    landed = bool(payload.get("landed"))
    if landed:
        return "landed"
    if status in ("passing", "blocked", "in_progress", "failed_scope"):
        return status
    event_status = _group_status_from_events(group_id, state_events)
    if event_status:
        return event_status
    if status == "pending":
        return status
    return "pending"


def _group_status_from_events(
    group_id: str,
    state_events: list[dict[str, Any]],
) -> str | None:
    if not group_id:
        return None
    status: str | None = None
    for event in state_events:
        if str(event.get("group_id") or "") != group_id:
            continue
        kind = str(event.get("event") or event.get("kind") or "")
        if kind in {"group.started", "group.check.started", "group.repair.started"}:
            status = "in_progress"
        elif kind in {"group.check.finished", "group.merge.eligible", "group.scope.passed"}:
            status = "passing"
        elif kind in {"group.merge.landed", "group.landed"}:
            status = "landed"
        elif kind in {"group.scope.failed"}:
            status = "failed_scope"
        elif kind in {
            "group.blocked",
            "group.failed",
            "group.check.failed",
            "group.merge.failed",
            "group.aborted_by_user",
            "group.invalidated_by_spec_edit",
        }:
            status = "blocked"
    return status


def _group_metrics_from_events(
    group_id: str,
    state_events: list[dict[str, Any]],
) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {"cost_usd": None, "wall_s": None}
    if not group_id:
        return metrics
    for event in state_events:
        if str(event.get("group_id") or "") != group_id:
            continue
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        for key in metrics:
            value = _float_or_none(extra.get(key))
            if value is None:
                value = _float_or_none(event.get(key))
            if value is not None:
                metrics[key] = value
    return metrics


def _first_float(*values: Any) -> float:
    for value in values:
        converted = _float_or_none(value)
        if converted is not None:
            return converted
    return 0.0


def _first_nonzero_float(*values: Any) -> float:
    for value in values:
        converted = _float_or_none(value)
        if converted is None:
            continue
        if converted != 0.0:
            return converted
    return 0.0


def _float_or_zero(value: Any) -> float:
    return _float_or_none(value) or 0.0


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    spec: dict[str, Any] | None,
    *,
    proof: dict[str, Any] | None = None,
    compile_active: bool = False,
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
        if kind == "group.started":
            ts = event.get("ts") or event.get("timestamp")
            stages["build"]["started_at"] = stages["build"]["started_at"] or ts
            if stages["build"]["status"] == "pending":
                stages["build"]["status"] = "active"
            continue
        if kind in {"group.merge.eligible", "group.check.finished"}:
            ts = event.get("ts") or event.get("timestamp")
            stages["build"]["started_at"] = stages["build"]["started_at"] or ts
            if stages["build"]["status"] == "pending":
                stages["build"]["status"] = "active"
            continue
        if kind == "audit.started" and str(event.get("detail") or "") != "run start":
            ts = event.get("ts") or event.get("timestamp")
            if stages["build"]["status"] == "active":
                stages["build"]["status"] = "done"
                stages["build"]["finished_at"] = stages["build"]["finished_at"] or ts
            stages["audit"]["started_at"] = ts
            stages["audit"]["status"] = "active"
            continue
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

    if spec and stages["compile"]["status"] == "pending":
        first_ts = _first_event_ts(state_events)
        stages["compile"]["status"] = "done"
        stages["compile"]["finished_at"] = first_ts
    elif compile_active and stages["compile"]["status"] == "pending":
        stages["compile"]["status"] = "active"

    live_status = _normalize_live_status(live_state)
    if live_status in {"interrupted", "aborted", "failed"}:
        for name in ("build", "audit", "render", "land"):
            if stages[name]["status"] == "active":
                stages[name]["status"] = "failed"
                stages[name]["finished_at"] = str(live_state.get("finished_at") or "") if live_state else None
                break
        else:
            if stages["build"]["status"] == "pending" and _has_group_events(state_events):
                stages["build"]["status"] = "failed"
                stages["build"]["finished_at"] = str(live_state.get("finished_at") or "") if live_state else None

    _reconcile_terminal_stages(stages, state_events, proof)

    return [stages[name] for name in canonical]


# Stage names that emit bare "<stage>.<sub>" lifecycle events (no
# "stage." prefix). Currently only `seed` — other stages still use
# the "stage.<name>.<sub>" pattern for orchestrator-level lifecycle.
_BARE_STAGE_EVENT_PREFIXES: tuple[str, ...] = ("seed", "audit")


def _reconcile_terminal_stages(
    stages: dict[str, dict[str, Any]],
    state_events: list[dict[str, Any]],
    proof: dict[str, Any] | None,
) -> None:
    """Normalize stage display once a proof packet/verdict exists.

    I2P runs currently emit group-level build/merge events and bare audit
    events, then write a proof packet without a dedicated render lifecycle
    event. Without this reconciliation, terminal RunView responses can say
    ``status=passed`` while Build/Audit remain visually active.
    """
    if not proof or str(proof.get("verdict") or "") not in {"passed", "partial", "blocked"}:
        return

    finished_ts = _last_event_ts(state_events)
    audit_started_ts = _last_event_ts(
        state_events,
        lambda event: str(event.get("event") or event.get("kind") or "") == "audit.started",
    )
    last_group_ts = _last_event_ts(
        state_events,
        lambda event: str(event.get("event") or event.get("kind") or "").startswith("group."),
    )

    if stages["build"]["status"] in {"pending", "active"} and _has_group_events(state_events):
        stages["build"]["status"] = "done"
        stages["build"]["finished_at"] = stages["build"]["finished_at"] or audit_started_ts or last_group_ts or finished_ts
        stages["build"]["started_at"] = stages["build"]["started_at"] or _first_group_event_ts(state_events)

    if stages["audit"]["status"] == "active" and finished_ts:
        stages["audit"]["status"] = "done"
        stages["audit"]["finished_at"] = stages["audit"]["finished_at"] or finished_ts

    if stages["render"]["status"] == "pending" and proof:
        stages["render"]["status"] = "done"
        stages["render"]["started_at"] = stages["render"]["started_at"] or finished_ts
        stages["render"]["finished_at"] = stages["render"]["finished_at"] or finished_ts


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


def _first_event_ts(state_events: list[dict[str, Any]]) -> str | None:
    for event in state_events:
        ts = event.get("ts") or event.get("timestamp")
        if ts:
            return str(ts)
    return None


def _last_event_ts(
    state_events: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool] | None = None,
) -> str | None:
    for event in reversed(state_events):
        if predicate is not None and not predicate(event):
            continue
        ts = event.get("ts") or event.get("timestamp")
        if ts:
            return str(ts)
    return None


def _first_group_event_ts(state_events: list[dict[str, Any]]) -> str | None:
    for event in state_events:
        if str(event.get("event") or event.get("kind") or "").startswith("group."):
            ts = event.get("ts") or event.get("timestamp")
            if ts:
                return str(ts)
    return None


def _has_group_events(state_events: list[dict[str, Any]]) -> bool:
    return any(
        str(event.get("event") or event.get("kind") or "").startswith("group.")
        for event in state_events
    )


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
    *,
    compile_active: bool = False,
) -> str:
    """Compute RunStatus from verdict + events."""
    if verdict == "passed":
        return "passed"
    if verdict == "partial":
        return "partial"
    if verdict == "blocked":
        return "blocked"
    live_status = _normalize_live_status(live_state)
    if live_status in {"interrupted", "aborted", "failed", "landed"}:
        return live_status
    # In-flight or pre-verdict — derive from latest stage event
    last_started = None
    saw_group_started = False
    for event in state_events:
        kind = str(event.get("event") or event.get("kind") or "")
        if kind == "group.started":
            saw_group_started = True
        if kind == "audit.started" and str(event.get("detail") or "") != "run start":
            last_started = "audit"
            continue
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
    if saw_group_started:
        return "building"
    if compile_active:
        return "compiling"
    if live_status:
        return live_status
    return "queued"


def _normalize_live_status(live_state: dict[str, Any] | None) -> str | None:
    if not live_state:
        return None
    raw = str(live_state.get("status") or "").strip().lower()
    if raw in {"interrupted"}:
        return "interrupted"
    if raw in {"cancelled", "canceled", "terminating", "removed"}:
        return "aborted"
    if raw in {"failed", "stale"}:
        return "failed"
    if raw in {"done"}:
        return "landed"
    if raw in {"queued", "starting", "initializing"}:
        return "queued"
    if raw == "running":
        return None
    if raw in {
        "compiling",
        "awaiting_spec_review",
        "building",
        "auditing",
        "rendering",
        "landing",
        "blocked",
        "partial",
        "passed",
        "landed",
        "aborted",
    }:
        return raw
    return None


def _started_at(
    state_events: list[dict[str, Any]],
    live_state: dict[str, Any] | None,
) -> str:
    if live_state and live_state.get("started_at"):
        return str(live_state["started_at"])
    for event in state_events:
        if str(event.get("event") or event.get("kind") or "") == "run.started":
            return str(event.get("ts") or event.get("timestamp") or "")
    first = _first_event_ts(state_events)
    if first:
        return first
    return ""


def _finished_at(
    state_events: list[dict[str, Any]],
    verdict: str | None,
    live_state: dict[str, Any] | None,
) -> str | None:
    live_status = _normalize_live_status(live_state)
    if live_state and live_state.get("finished_at") and live_status in {
        "interrupted",
        "aborted",
        "failed",
        "landed",
    }:
        return str(live_state["finished_at"])
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
