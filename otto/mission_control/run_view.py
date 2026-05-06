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
import re
from pathlib import Path
from typing import Any

from otto.token_usage import (
    message_file_breakdown_from_messages,
    phase_breakdown_from_messages,
    total_token_usage_from_phases,
)


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

    status = _derive_status(
        verdict_field,
        state_events,
        live_state,
        session_dir=session_dir,
    )

    groups = _build_groups(spec, proof, state_events, live_state)
    features = _build_features(spec, proof, live_state, state_events)
    components = _build_components(spec, proof, state_events, live_state)
    guardrails = _build_guardrails(spec, proof)
    phase_usage = _build_phase_usage(session_dir, proof)
    token_usage = _build_token_usage(proof, phase_usage)
    agent_usage_top = _build_agent_usage_top(session_dir, proof)
    stages = _build_stages(
        state_events,
        live_state,
        spec,
        proof=proof,
        session_dir=session_dir,
        phase_usage=phase_usage,
    )
    findings = _build_findings(proof)

    cost_usd = float(
        (proof.get("cost_usd") if proof else None)
        or (live_state.get("cost_usd") if live_state else 0.0)
        or 0.0
    )
    wall_s = float(
        (proof.get("wall_s") if proof else None)
        or (live_state.get("wall_s") if live_state else None)
        or (live_state.get("duration_s") if live_state else 0.0)
        or 0.0
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
        "token_usage": token_usage,
        "phase_usage": phase_usage,
        "agent_usage_top": agent_usage_top,
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


def _build_phase_usage(
    session_dir: Path,
    proof: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    raw = proof.get("phase_usage") if proof else None
    if isinstance(raw, dict) and raw:
        return {
            str(phase): dict(data)
            for phase, data in raw.items()
            if isinstance(data, dict)
        }
    return phase_breakdown_from_messages(session_dir)


def _build_token_usage(
    proof: dict[str, Any] | None,
    phase_usage: dict[str, dict[str, Any]],
) -> dict[str, int]:
    raw = proof.get("token_usage") if proof else None
    if isinstance(raw, dict) and any(raw.values()):
        return {
            str(key): int(value)
            for key, value in raw.items()
            if isinstance(value, int | float) and value
        }
    return total_token_usage_from_phases(phase_usage)


def _build_agent_usage_top(
    session_dir: Path,
    proof: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    raw = proof.get("agent_usage_top") if proof else None
    if isinstance(raw, list) and raw:
        return [dict(item) for item in raw if isinstance(item, dict)]
    return message_file_breakdown_from_messages(session_dir)[:10]


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------


def _build_features(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
    live_state: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
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
    group_status_by_id = _group_statuses_for_features(spec, proof, state_events)
    group_name_by_id = _group_names_for_features(spec, proof)
    # Prefer proof-packet's features[] (post-Render shape from A3 wiring),
    # but fall back per-feature to spec evidence_kinds when proof's is empty.
    if proof and isinstance(proof.get("features"), list) and proof["features"]:
        return [
            _feature_to_view(
                f,
                spec_evidence_by_id,
                group_status_by_id=group_status_by_id,
                group_name_by_id=group_name_by_id,
            )
            for f in proof["features"]
        ]
    if not spec:
        return []
    raw_features = spec.get("features") or []
    if isinstance(raw_features, list) and raw_features:
        return [
            _feature_to_view(
                f,
                spec_evidence_by_id,
                group_status_by_id=group_status_by_id,
                group_name_by_id=group_name_by_id,
            )
            for f in raw_features
        ]
    return _features_from_groups(spec, state_events, proof)


def _features_from_groups(
    spec: dict[str, Any],
    state_events: list[dict[str, Any]],
    proof: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
        group_id = _group_id(group)
        group_name = str(group.get("name") or group.get("title") or group_id)
        evidence_kinds = _evidence_kinds_from_checks(group.get("checks"))
        proof_group = _proof_group_by_id(proof).get(group_id)
        evidence_refs = _group_evidence_refs(group_id, state_events, proof_group)
        build_status = _group_status(group, group_id, state_events)
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
                    "group_name": group_name,
                    "build_status": build_status,
                    "verdict": None,
                    "evidence_completeness": "full",
                    "coverage_confidence": "high",
                    "multi_actor_required": False,
                    "audit_pre_merge": False,
                    "evidence_refs": list(evidence_refs),
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
        kind = _normalize_evidence_kind(raw)
        if kind and kind not in mapped:
            mapped.append(kind)
    return mapped


def _normalize_evidence_kind(raw: Any) -> str | None:
    key = re.sub(r"(?<!^)(?=[A-Z])", "_", str(raw or ""))
    key = key.replace("-", "_").replace(" ", "_").lower()
    return {
        "browser_journey": "BrowserJourney",
        "browser": "BrowserJourney",
        "api_probe": "ApiProbe",
        "api": "ApiProbe",
        "state_invariant": "StateInvariant",
        "state": "StateInvariant",
        "repo_test": "RepoTestCheck",
        "repo_test_check": "RepoTestCheck",
        "pytest": "RepoTestCheck",
        "pytest_check": "RepoTestCheck",
        "py_test": "RepoTestCheck",
        "test": "RepoTestCheck",
        "cli_probe": "CLIProbe",
        "import_check": "ImportCheck",
        "type_check": "TypeCheck",
    }.get(key)


def _proof_group_by_id(proof: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw_groups = proof.get("groups") if proof else []
    if not isinstance(raw_groups, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_id = _group_id(group)
        if group_id:
            out[group_id] = group
    return out


def _group_evidence_refs(
    group_id: str,
    state_events: list[dict[str, Any]],
    proof_group: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    if not group_id:
        return []
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    if proof_group:
        raw_evidence = proof_group.get("check_evidence") or []
        if isinstance(raw_evidence, list):
            for entry in raw_evidence:
                if not isinstance(entry, dict):
                    continue
                kind = _normalize_evidence_kind(entry.get("kind")) or "RepoTestCheck"
                raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
                path = str(raw.get("selector") or entry.get("selector") or "")
                if not path:
                    artifacts = entry.get("artifacts")
                    if isinstance(artifacts, list) and artifacts:
                        path = str(artifacts[0])
                summary = str(entry.get("detail") or "")
                _append_evidence_ref(refs, seen, kind=kind, path=path, summary=summary)
    for event in state_events:
        if str(event.get("group_id") or "") != group_id:
            continue
        kind = str(event.get("event") or event.get("kind") or "")
        if kind != "group.check.finished":
            continue
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        details = extra.get("details") if isinstance(extra.get("details"), list) else []
        if not details:
            details = [event.get("detail") or "group check finished"]
        for raw_detail in details:
            detail = str(raw_detail or "")
            selector = _selector_from_check_detail(detail)
            _append_evidence_ref(
                refs,
                seen,
                kind="RepoTestCheck",
                path=selector,
                summary=detail,
            )
    return refs


def _selector_from_check_detail(detail: str) -> str:
    match = re.search(r"selector=(['\"])(?P<selector>.+?)\1", detail)
    if match:
        return match.group("selector")
    match = re.search(r"path=(['\"])(?P<path>.+?)\1", detail)
    if match:
        return match.group("path")
    return ""


def _append_evidence_ref(
    refs: list[dict[str, str]],
    seen: set[tuple[str, str, str]],
    *,
    kind: str,
    path: str,
    summary: str,
) -> None:
    item = (kind, path, summary)
    if item in seen:
        return
    seen.add(item)
    refs.append({"kind": kind, "path": path, "summary": summary})


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
    *,
    group_status_by_id: dict[str, str] | None = None,
    group_name_by_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map proof/spec feature dict to FeatureView shape."""
    feature_id = str(payload.get("feature_id") or payload.get("id") or "")
    payload_kinds = [str(k) for k in (payload.get("evidence_kinds") or [])]
    if not payload_kinds and spec_evidence_by_id:
        payload_kinds = list(spec_evidence_by_id.get(feature_id, []))
    group_id = str(payload.get("group_id") or "")
    build_status = str(
        payload.get("build_status")
        or (group_status_by_id or {}).get(group_id)
        or "pending"
    )
    return {
        "id": feature_id,
        "name": str(payload.get("name") or ""),
        "description": str(payload.get("description") or ""),
        "acceptance_detail": str(payload.get("acceptance_detail") or ""),
        "evidence_kinds": payload_kinds,
        "group_id": group_id,
        "group_name": str(
            payload.get("group_name")
            or (group_name_by_id or {}).get(group_id)
            or ""
        ),
        "build_status": build_status,
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
    proof_groups = proof.get("groups") if proof else None
    spec_groups = (
        (spec.get("groups") if spec else None)
        or (spec.get("slices") if spec else None)  # legacy
        or []
    )
    raw_groups = proof_groups or spec_groups or []
    if not isinstance(raw_groups, list):
        return []
    if isinstance(proof_groups, list) and isinstance(spec_groups, list):
        raw_groups = _merge_proof_groups_with_spec(raw_groups, spec_groups)
    return [_group_to_view(g, state_events) for g in raw_groups]


def _merge_proof_groups_with_spec(
    proof_groups: list[Any],
    spec_groups: list[Any],
) -> list[dict[str, Any]]:
    """Overlay proof outcomes onto spec group metadata.

    Proof packets intentionally focus on runtime outcome fields and older
    packets omit spec-only metadata such as ``feature_ids`` and dependencies.
    The run drawer needs both: proof status plus the original product plan.
    """
    spec_by_id = {
        _group_id(group): group
        for group in spec_groups
        if isinstance(group, dict) and _group_id(group)
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for proof_group in proof_groups:
        if not isinstance(proof_group, dict):
            continue
        group_id = _group_id(proof_group)
        spec_group = spec_by_id.get(group_id, {})
        merged = dict(spec_group)
        merged.update(proof_group)
        if not merged.get("feature_ids") and spec_group.get("feature_ids"):
            merged["feature_ids"] = spec_group["feature_ids"]
        if not merged.get("dependencies") and spec_group.get("dependencies"):
            merged["dependencies"] = spec_group["dependencies"]
        if not merged.get("deps") and spec_group.get("deps"):
            merged["deps"] = spec_group["deps"]
        out.append(merged)
        if group_id:
            seen.add(group_id)
    for spec_group in spec_groups:
        if not isinstance(spec_group, dict):
            continue
        group_id = _group_id(spec_group)
        if group_id and group_id not in seen:
            out.append(dict(spec_group))
    return out


def _group_to_view(
    payload: dict[str, Any],
    state_events: list[dict[str, Any]],
) -> dict[str, Any]:
    group_id = _group_id(payload)
    event_summary = _group_event_summary(group_id, state_events)
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
        "branch": str(payload.get("branch") or event_summary.get("branch") or ""),
        "owned_paths": [
            str(p) for p in (payload.get("owned_paths") or [])
        ],
        "dependencies": [
            str(d) for d in (payload.get("dependencies") or payload.get("deps") or [])
        ],
        "cost_usd": float(payload.get("cost_usd") or event_summary.get("cost_usd") or 0.0),
        "wall_s": float(payload.get("wall_s") or event_summary.get("wall_s") or 0.0),
        "repair_attempts": int(payload.get("repair_attempts") or 0),
    }


def _group_names_for_features(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
) -> dict[str, str]:
    raw_groups = (
        (proof.get("groups") if proof else None)
        or (spec.get("groups") if spec else None)
        or (spec.get("slices") if spec else None)
        or []
    )
    if not isinstance(raw_groups, list):
        return {}
    out: dict[str, str] = {}
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_id = _group_id(group)
        if group_id:
            out[group_id] = str(group.get("name") or group.get("title") or group_id)
    return out


def _group_statuses_for_features(
    spec: dict[str, Any] | None,
    proof: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
) -> dict[str, str]:
    raw_groups = (
        (proof.get("groups") if proof else None)
        or (spec.get("groups") if spec else None)
        or (spec.get("slices") if spec else None)
        or []
    )
    if not isinstance(raw_groups, list):
        return {}
    out: dict[str, str] = {}
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_id = _group_id(group)
        if group_id:
            out[group_id] = _group_status(group, group_id, state_events)
    return out


def _group_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("id")
        or payload.get("group_id")
        or payload.get("slice_id")
        or ""
    )


def _group_event_summary(
    group_id: str,
    state_events: list[dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if not group_id:
        return summary
    for event in state_events:
        if str(event.get("group_id") or "") != group_id:
            continue
        kind = str(event.get("event") or event.get("kind") or "")
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        if kind == "group.started":
            branch = extra.get("branch")
            if branch:
                summary["branch"] = str(branch)
        if kind in {"group.merge.eligible", "group.check.finished", "group.blocked"}:
            if extra.get("wall_s") is not None:
                summary["wall_s"] = extra.get("wall_s")
            if extra.get("cost_usd") is not None:
                summary["cost_usd"] = extra.get("cost_usd")
    return summary


def _group_status(
    payload: dict[str, Any],
    group_id: str,
    state_events: list[dict[str, Any]],
) -> str:
    status = str(payload.get("status") or "").lower()
    landed = bool(payload.get("landed"))
    if landed:
        return "landed"
    event_status = _group_status_from_events(group_id, state_events)
    if event_status in {"blocked", "landed", "failed_scope"}:
        return event_status
    if status in ("passing", "blocked", "in_progress", "failed_scope"):
        return status
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
    session_dir: Path | None = None,
    phase_usage: dict[str, dict[str, Any]] | None = None,
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
            "token_usage": {},
            "started_at": None,
            "finished_at": None,
        }
        for name in canonical
    }

    for event in state_events:
        kind = str(event.get("event") or event.get("kind") or "")
        if _is_legacy_run_start_audit_event(event):
            continue
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
    elif (
        stages["compile"]["status"] == "pending"
        and _normalize_live_status(live_state) == "queued"
        and session_dir is not None
        and _has_compile_activity(session_dir)
    ):
        stages["compile"]["status"] = "active"
        stages["compile"]["started_at"] = (
            str(live_state.get("started_at") or "") if live_state else None
        )

    if (
        stages["spec_review"]["status"] == "pending"
        and _spec_review_completed(session_dir, state_events)
    ):
        _mark_stage_done(
            stages["spec_review"],
            _last_event_ts(state_events) or _first_event_ts(state_events),
        )

    terminal_verdict = _terminal_verdict(proof, state_events)
    terminal_ts = _terminal_ts(state_events)
    if terminal_verdict in {"passed", "partial", "blocked"}:
        if _has_group_events(state_events):
            _mark_stage_done(
                stages["build"],
                _last_event_ts(
                    state_events,
                    kinds={
                        "group.check.finished",
                        "group.merge.eligible",
                        "group.merge.started",
                        "group.merge.landed",
                    },
                )
                or terminal_ts,
            )
        if stages["audit"]["status"] in {"pending", "active"} and (
            proof is not None or _has_event_kind(state_events, "audit.finished")
        ):
            _mark_stage_done(stages["audit"], terminal_ts)
        if proof is not None:
            _mark_stage_done(stages["render"], terminal_ts)
        if _has_landed_groups(proof, state_events):
            _mark_stage_done(
                stages["land"],
                _last_event_ts(state_events, kinds={"group.merge.landed"}) or terminal_ts,
            )

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

    _apply_phase_usage_to_stages(stages, phase_usage or {})
    return [stages[name] for name in canonical]


def _apply_phase_usage_to_stages(
    stages: dict[str, dict[str, Any]],
    phase_usage: dict[str, dict[str, Any]],
) -> None:
    for stage_name, phase_names in {
        "compile": ("compile", "spec"),
        "build": ("build",),
        "audit": ("audit", "certify"),
        "render": ("render",),
        "land": ("merge", "land"),
    }.items():
        data = _merge_phase_usage_entries(phase_usage, phase_names)
        if not data or stage_name not in stages:
            continue
        token_usage = {
            key: int(data.get(key, 0) or 0)
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
            if int(data.get(key, 0) or 0)
        }
        stages[stage_name]["token_usage"] = token_usage
        if stages[stage_name].get("duration_s") is None and data.get("duration_s") is not None:
            stages[stage_name]["duration_s"] = float(data["duration_s"])
        if stages[stage_name].get("cost_usd") is None and data.get("cost_usd") is not None:
            stages[stage_name]["cost_usd"] = float(data["cost_usd"])


def _merge_phase_usage_entries(
    phase_usage: dict[str, dict[str, Any]],
    phase_names: tuple[str, ...],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for name in phase_names:
        data = phase_usage.get(name)
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if not isinstance(value, int | float):
                continue
            merged[key] = float(merged.get(key, 0.0) or 0.0) + float(value)
    return merged


# Stage names that emit bare "<stage>.<sub>" lifecycle events (no
# "stage." prefix). New i2p journals use bare seed/audit events; older
# stage.<name>.<sub> events are still accepted by _stage_event_target().
_BARE_STAGE_EVENT_PREFIXES: tuple[str, ...] = ("seed", "audit")


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


def _is_legacy_run_start_audit_event(event: dict[str, Any]) -> bool:
    kind = str(event.get("event") or event.get("kind") or "")
    detail = str(event.get("detail") or "").strip().lower()
    return kind == "audit.started" and detail == "run start"


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
    *,
    kinds: set[str] | None = None,
) -> str | None:
    for event in reversed(state_events):
        kind = str(event.get("event") or event.get("kind") or "")
        if kinds is not None and kind not in kinds:
            continue
        ts = event.get("ts") or event.get("timestamp")
        if ts:
            return str(ts)
    return None


def _has_group_events(state_events: list[dict[str, Any]]) -> bool:
    return any(
        str(event.get("event") or event.get("kind") or "").startswith("group.")
        for event in state_events
    )


def _has_event_kind(state_events: list[dict[str, Any]], kind: str) -> bool:
    return any(
        str(event.get("event") or event.get("kind") or "") == kind
        for event in state_events
    )


def _spec_review_completed(
    session_dir: Path | None,
    state_events: list[dict[str, Any]],
) -> bool:
    if any(
        str(event.get("event") or event.get("kind") or "")
        in {"spec.approved", "spec.review_approved"}
        for event in state_events
    ):
        return True
    if session_dir is None:
        return False
    path = session_dir / "spec" / "lifecycle.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    lifecycle = str(payload.get("lifecycle") or "").strip().lower()
    return lifecycle in {"approved", "editing_in_flight"}


def _terminal_verdict(
    proof: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
) -> str:
    if proof and proof.get("verdict") not in (None, ""):
        return str(proof.get("verdict")).strip().lower()
    for event in reversed(state_events):
        kind = str(event.get("event") or event.get("kind") or "")
        if kind != "run.finished":
            continue
        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        verdict = extra.get("verdict") or event.get("verdict")
        if verdict:
            return str(verdict).strip().lower()
    return ""


def _terminal_ts(state_events: list[dict[str, Any]]) -> str | None:
    return _last_event_ts(state_events, kinds={"run.finished", "audit.finished"})


def _has_landed_groups(
    proof: dict[str, Any] | None,
    state_events: list[dict[str, Any]],
) -> bool:
    proof_groups = proof.get("groups") if proof else None
    if isinstance(proof_groups, list) and proof_groups:
        group_dicts = [group for group in proof_groups if isinstance(group, dict)]
        return bool(group_dicts) and all(
            bool(group.get("landed")) for group in group_dicts
        )
    return _has_event_kind(state_events, "group.merge.landed")


def _mark_stage_done(stage: dict[str, Any], ts: str | None) -> None:
    if stage["status"] == "failed":
        return
    stage["status"] = "done"
    if ts and not stage.get("finished_at"):
        stage["finished_at"] = ts


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
    session_dir: Path | None = None,
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
        if _is_legacy_run_start_audit_event(event):
            continue
        if kind == "group.started":
            saw_group_started = True
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
    if (
        live_status == "queued"
        and not state_events
        and session_dir is not None
        and _has_compile_activity(session_dir)
    ):
        return "compiling"
    if live_status:
        return live_status
    return "queued"


def _has_compile_activity(session_dir: Path) -> bool:
    compile_dir = session_dir / "spec" / "compile-agent"
    return any(
        (compile_dir / name).exists()
        for name in ("narrative.log", "messages.jsonl", "live.log")
    )


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
