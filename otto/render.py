"""Render — Step 7 of the unified intent-to-product pipeline.

Composes a ProofPacket from the spec, build, merge, and audit results;
emits two artifacts:

    <session>/proof-packet.html   (primary, human)
    <session>/proof-packet.json   (machine)

The HTML is self-contained except for asset links to the per-check
evidence directory. Layout (top to bottom):

    Header           intent, project_kind, verdict, wall, cost
    Spec summary     structure decisions, slices (collapsed),
                     non_goals, done_means
    Per slice        status, owned_paths, check results table with
                     evidence thumbnails (clickable for full size),
                     branch + landed commit
    Audit            walkthrough video (if any), screenshot grid,
                     narrative report, per-slice verdicts
    Known limits     blocked slices, deferred items
    Merge state      what landed, what was rejected and why

JSON is the same content, structured. Schema versioned.

Design choice: thumbnails rendered as `<img>` with relative paths to
artifacts. Videos via `<video>` tag. The HTML is intentionally
dependency-free (no external CSS/JS frameworks); CSS is inlined in a
`<style>` block. A reviewer can open it locally without a server.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from otto.audit import AuditResult, AuditVerdict, GroupVerdict
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.spec_compile import (
    Feature,
    FeatureProofBlock,
    Finding,
    Group,
    Spec,
    WalkthroughEntry,
    build_feature_proof_blocks,
    feature_proof_block_to_html,
    feature_proof_blocks_to_dicts,
    spec_to_dict,
)

logger = logging.getLogger("otto.render")

PROOF_PACKET_HTML = "proof-packet.html"
PROOF_PACKET_JSON = "proof-packet.json"
PROOF_PACKET_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class GroupPacket:
    """Per-group data shaped for rendering."""

    group_id: str
    name: str
    status: str  # GroupStatus.value or "landed" / "blocked"
    landed: bool
    landed_commit: str
    branch: str
    owned_paths: list[str]
    check_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Each check_evidence entry has: kind, detail, passed, artifacts (list of paths)
    audit_verdict: dict[str, Any] | None = None  # {passed, detail}
    failure_narrative: str = ""
    repair_attempts: int = 0


@dataclass
class ProofPacket:
    """Top-level proof packet."""

    schema_version: int
    intent: str
    project_kind: str
    verdict: str  # "passed" | "partial" | "blocked"
    wall_s: float
    cost_usd: float
    structure: dict[str, Any]
    non_goals: list[str]
    done_means: list[str]
    groups: list[GroupPacket]
    audit_narrative: str
    walkthrough_artifacts: list[str]  # absolute paths
    blocked_group_ids: list[str]
    landed_group_ids: list[str]
    # v2.2 + phase 4: amendment chain rendered for human review
    amendments: list[dict[str, Any]] = field(default_factory=list)
    # Audit-final-quality: human-facing quality score 1-5 (0 = not assessed)
    # plus concrete UX/visual findings.
    quality_score: int = 0
    quality_findings: list[str] = field(default_factory=list)
    # A0.4: per-Feature audits — feature checklist for the proof packet.
    # `feature_audits` is the canonical key (formerly `capability_verdicts`,
    # dropped post-cutover).
    feature_audits: list[dict[str, Any]] = field(default_factory=list)

    # A3: per-Feature proof blocks (research §7). Each block is the dict
    # shape from `feature_proof_block_to_dict` — research-§7 layout
    # mirrors what `proof/features/<feature-id>/proof.json` holds.
    # Empty for legacy packets and for runs that haven't been audited
    # under the new design yet. The whole-product proof-packet.json
    # carries this list; render layer emits per-Feature mini-pages
    # from it.
    features: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _path_to_str(p: Path | str) -> str:
    return str(p)


def _evidence_to_dict(check_kind: str, evidence: Evidence) -> dict[str, Any]:
    return {
        "kind": check_kind,
        "passed": evidence.passed,
        "detail": evidence.detail,
        "duration_s": evidence.duration_s,
        "started_at": evidence.started_at,
        "artifacts": [_path_to_str(p) for p in evidence.artifacts],
        "raw": _safe_truncate(evidence.raw, 4096),
    }


def _safe_truncate(value: Any, max_chars: int) -> Any:
    """Truncate string values inside a dict for JSON safety."""
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "...[truncated]"
    if isinstance(value, dict):
        return {k: _safe_truncate(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_truncate(v, max_chars) for v in value]
    return value


def compose_proof_packet(
    spec: Spec,
    build_result: BuildResult,
    merge_result: MergeQueueResult,
    audit_result: AuditResult,
    *,
    wall_s: float,
    cost_usd: float,
) -> ProofPacket:
    """Build a ProofPacket from the four pipeline outputs."""
    audit_by_group: dict[str, GroupVerdict] = {
        v.group_id: v for v in audit_result.group_verdicts
    }
    merge_by_group: dict[str, MergeResult] = {
        r.group_id: r for r in merge_result.results
    }
    build_by_group: dict[str, GroupResult] = {
        r.group_id: r for r in build_result.group_results
    }

    group_packets: list[GroupPacket] = []
    for s in spec.groups:
        bres = build_by_group.get(s.id)
        mres = merge_by_group.get(s.id)
        averdict = audit_by_group.get(s.id)

        check_evidence: list[dict[str, Any]] = []
        # Prefer merge-time evidence (most recent) over build-time, but
        # fall back to build-time if merge didn't run for this slice.
        evidence_source = mres.group_recheck_evidence if mres else (bres.last_evidence if bres else [])
        for c, ev in zip(s.checks, evidence_source, strict=False):
            kind = type(c).__name__
            check_evidence.append(_evidence_to_dict(kind, ev))

        landed = bool(mres and mres.status == MergeStatus.LANDED)
        if landed:
            group_status = "landed"
        elif bres and bres.status == GroupStatus.PASSING:
            group_status = "passing"  # passed build but did not land
        elif bres:
            group_status = bres.status.value
        else:
            group_status = "pending"

        failure = ""
        if mres and mres.status == MergeStatus.BLOCKED:
            failure = mres.failure_narrative
        elif bres and bres.status in (GroupStatus.BLOCKED, GroupStatus.FAILED_SCOPE):
            failure = bres.failure_narrative

        group_packets.append(
            GroupPacket(
                group_id=s.id,
                name=s.name,
                status=group_status,
                landed=landed,
                landed_commit=mres.landed_commit if mres else "",
                branch=bres.branch if bres else "",
                owned_paths=list(s.owned_paths),
                check_evidence=check_evidence,
                audit_verdict={
                    "passed": averdict.passed,
                    "detail": averdict.detail,
                } if averdict else None,
                failure_narrative=failure,
                repair_attempts=mres.repair_attempts if mres else 0,
            )
        )

    # v2.2 + phase 4: amendments rendered for human review.
    amendments_render = [
        {
            "tier": a.tier,
            "actor": a.actor,
            "reason": a.reason,
            "ts": a.ts,
            "trigger_event_id": a.trigger_event_id,
            "diff_sha256_before": a.diff_sha256_before[:16] if a.diff_sha256_before else "",
            "diff_sha256_after": a.diff_sha256_after[:16] if a.diff_sha256_after else "",
        }
        for a in spec.amendments
    ]

    # A0.4: render per-Feature audits as a feature checklist.
    feature_audit_render = [
        {
            "name": fa.name,
            "status": fa.status,
            "detail": fa.detail,
            "evidence_refs": list(fa.evidence_refs),
        }
        for fa in audit_result.feature_audits
    ]

    # A3: per-Feature proof blocks (research §7). Best-effort population:
    # we map FeatureAudit entries (by name → Feature.id, falling back to
    # name → name) into the verdict-dict shape `build_feature_proof_blocks`
    # expects. Walkthrough entries flow in from
    # `audit_result.walkthrough_entries` (parsed during run_audit by
    # `_validate_walkthrough_jsonl`) so per-Feature blocks carry their
    # walkthrough trace. Empty list → helper's "no entries tagged"
    # empty-state per research §4 honesty rule.
    feature_verdict_dicts: list[dict[str, Any]] = []
    if spec.features:
        # Map FeatureAudit by name (and fallback id) so we can attach
        # status/detail/evidence_refs to the matching Feature.id.
        audits_by_key: dict[str, Any] = {}
        for fa in audit_result.feature_audits:
            audits_by_key[fa.name] = fa
        for feature in spec.features:
            fa = audits_by_key.get(feature.name) or audits_by_key.get(feature.id)
            if fa is None:
                continue
            feature_verdict_dicts.append({
                "feature_id": feature.id,
                "verdict": fa.status,
                "detail": fa.detail,
                "evidence_refs": list(fa.evidence_refs),
            })
        feature_blocks = build_feature_proof_blocks(
            spec,
            walkthrough_entries=list(audit_result.walkthrough_entries),
            feature_verdicts=feature_verdict_dicts,
        )
        feature_dicts = feature_proof_blocks_to_dicts(feature_blocks)
    else:
        feature_dicts = []

    return ProofPacket(
        schema_version=PROOF_PACKET_SCHEMA_VERSION,
        intent=spec.intent,
        project_kind=spec.project_kind,
        verdict=audit_result.verdict.value,
        wall_s=wall_s,
        cost_usd=cost_usd,
        structure=dict(spec.structure.payload or {}),
        non_goals=list(spec.non_goals),
        done_means=list(spec.done_means),
        groups=group_packets,
        audit_narrative=audit_result.narrative,
        walkthrough_artifacts=[_path_to_str(p) for p in audit_result.walkthrough_artifacts],
        blocked_group_ids=list(merge_result.blocked_ids) + list(build_result.blocked_ids),
        landed_group_ids=list(merge_result.landed_ids),
        amendments=amendments_render,
        quality_score=audit_result.quality_score,
        quality_findings=list(audit_result.quality_findings),
        feature_audits=feature_audit_render,
        features=feature_dicts,
    )


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def render_json(packet: ProofPacket) -> str:
    return json.dumps(_packet_to_dict(packet), indent=2, sort_keys=True) + "\n"


def _packet_to_dict(packet: ProofPacket) -> dict[str, Any]:
    return {
        "schema_version": packet.schema_version,
        "intent": packet.intent,
        "project_kind": packet.project_kind,
        "verdict": packet.verdict,
        "wall_s": packet.wall_s,
        "cost_usd": packet.cost_usd,
        "structure": packet.structure,
        "non_goals": packet.non_goals,
        "done_means": packet.done_means,
        "audit_narrative": packet.audit_narrative,
        "walkthrough_artifacts": packet.walkthrough_artifacts,
        "blocked_group_ids": packet.blocked_group_ids,
        "landed_group_ids": packet.landed_group_ids,
        "amendments": packet.amendments,
        "quality_score": packet.quality_score,
        "quality_findings": packet.quality_findings,
        # A0.4: canonical per-Feature audit checklist
        # (formerly `capability_verdicts`, dropped post-cutover).
        "feature_audits": packet.feature_audits,
        "features": list(packet.features),  # A3: per-Feature proof blocks
        "groups": [
            {
                "group_id": s.group_id,
                "name": s.name,
                "status": s.status,
                "landed": s.landed,
                "landed_commit": s.landed_commit,
                "branch": s.branch,
                "owned_paths": s.owned_paths,
                "check_evidence": s.check_evidence,
                "audit_verdict": s.audit_verdict,
                "failure_narrative": s.failure_narrative,
                "repair_attempts": s.repair_attempts,
            }
            for s in packet.groups
        ],
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


_HTML_CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
               Arial, sans-serif;
  max-width: 980px;
  margin: 0 auto;
  padding: 24px;
  color: #1f2328;
  line-height: 1.5;
}
h1, h2, h3 { margin-top: 1.5em; margin-bottom: 0.4em; }
h1 { font-size: 1.7em; border-bottom: 2px solid #d0d7de; padding-bottom: 0.2em; }
h2 { font-size: 1.3em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.1em; }
h3 { font-size: 1.05em; }
.verdict { display: inline-block; padding: 4px 10px; border-radius: 12px;
           font-weight: 600; font-size: 0.85em; }
.verdict.passed  { background: #dafbe1; color: #1a7f37; }
.verdict.partial { background: #fff8c5; color: #9a6700; }
.verdict.blocked { background: #ffebe9; color: #cf222e; }
.muted { color: #57606a; font-size: 0.9em; }
.kpi { display: inline-block; margin-right: 18px; }
.kpi .label { color: #57606a; font-size: 0.8em; }
.kpi .value { font-weight: 600; }
.slice {
  border: 1px solid #d0d7de;
  border-radius: 6px;
  padding: 14px 18px;
  margin: 12px 0;
  background: #f6f8fa;
}
.slice.landed  { border-left: 4px solid #1a7f37; }
.slice.blocked { border-left: 4px solid #cf222e; }
.slice.failed_scope { border-left: 4px solid #9a6700; }
.slice.passing { border-left: 4px solid #0969da; }
.slice header { display: flex; justify-content: space-between; align-items: center; }
.slice h3 { margin: 0; }
.slice .meta { color: #57606a; font-size: 0.85em; }
table.checks { width: 100%; border-collapse: collapse; margin-top: 10px; }
.checks th, .checks td {
  text-align: left; padding: 6px 8px; border-bottom: 1px solid #d0d7de;
  font-size: 0.92em;
}
.checks .pass { color: #1a7f37; font-weight: 600; }
.checks .fail { color: #cf222e; font-weight: 600; }
.thumbs img { max-width: 160px; max-height: 100px; margin: 4px; border: 1px solid #d0d7de; }
.thumbs a { display: inline-block; }
.narrative { background: #fff; border: 1px solid #d0d7de; padding: 12px;
             border-radius: 6px; white-space: pre-wrap; font-family: ui-monospace,
             SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
.code-block { background: #f6f8fa; border: 1px solid #d0d7de; padding: 10px;
              border-radius: 6px; overflow-x: auto; font-family: ui-monospace,
              SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.85em; }
video { max-width: 100%; border: 1px solid #d0d7de; border-radius: 6px; }
.failure { color: #cf222e; font-style: italic; }
"""


def render_html(packet: ProofPacket, *, session_dir: Path | None = None) -> str:
    """Render the proof packet as a self-contained HTML document.

    `session_dir` is the directory the HTML will be written to; artifact
    paths are made relative to this directory if provided, otherwise
    rendered as absolute paths.
    """
    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Proof packet — {escape(packet.intent[:80])}</title>
<style>{_HTML_CSS}</style>
</head>
<body>""")

    # Header
    parts.append(_render_header(packet))

    # Spec summary
    parts.append(_render_spec_summary(packet))

    # Per-Feature sections (A3 — research §3 atomic units, primary surface).
    # Emitted before per-Slice (legacy back-compat) so Features lead the
    # human review path. Empty `features` is a no-op section; legacy
    # packets without per-Feature blocks render unchanged below.
    parts.append(_render_feature_section(packet, session_dir=session_dir))

    # Per-slice sections (back-compat for legacy proof-of-work readers)
    parts.append("<h2>Slices</h2>")
    for s in packet.groups:
        parts.append(_render_group(s, session_dir=session_dir))

    # Audit section
    parts.append(_render_audit_section(packet, session_dir=session_dir))

    # Known limitations
    parts.append(_render_limitations(packet))

    # Merge state
    parts.append(_render_merge_state(packet))

    parts.append("</body></html>")
    return "\n".join(parts)


def _render_header(packet: ProofPacket) -> str:
    return f"""<h1>{escape(packet.intent)}</h1>
<p>
  <span class="verdict {escape(packet.verdict)}">{escape(packet.verdict.upper())}</span>
  <span class="kpi"><span class="label">project_kind</span> <span class="value">{escape(packet.project_kind)}</span></span>
  <span class="kpi"><span class="label">wall</span> <span class="value">{packet.wall_s:.0f} s</span></span>
  <span class="kpi"><span class="label">cost</span> <span class="value">${packet.cost_usd:.2f}</span></span>
  <span class="kpi"><span class="label">slices</span>
    <span class="value">{len(packet.landed_group_ids)} landed / {len(packet.groups)} total</span>
  </span>
</p>"""


def _render_spec_summary(packet: ProofPacket) -> str:
    parts = ["<h2>Spec</h2>"]
    parts.append("<details><summary>Structure decisions</summary>")
    parts.append(
        f'<pre class="code-block">{escape(json.dumps(packet.structure, indent=2, sort_keys=True))}</pre>'
    )
    parts.append("</details>")
    if packet.non_goals:
        parts.append("<h3>Non-goals</h3><ul>")
        parts.extend(f"<li>{escape(g)}</li>" for g in packet.non_goals)
        parts.append("</ul>")
    if packet.done_means:
        parts.append("<h3>Done means</h3><ul>")
        parts.extend(f"<li>{escape(g)}</li>" for g in packet.done_means)
        parts.append("</ul>")
    return "\n".join(parts)


def _feature_block_from_dict(payload: dict[str, Any]) -> FeatureProofBlock:
    """Reconstruct a FeatureProofBlock from its serialised dict shape.

    Inverse of `feature_proof_block_to_dict`. Lets render layer hand
    `packet.features[]` dicts back to the canonical HTML helper without
    duplicating template logic.
    """
    raw_entries = payload.get("walkthrough_entries") or []
    entries: list[WalkthroughEntry] = []
    core_keys = {"t", "feature_ids", "action_kind", "narrative"}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        extras = {k: v for k, v in raw.items() if k not in core_keys}
        entries.append(
            WalkthroughEntry(
                t=str(raw.get("t") or ""),
                feature_ids=[str(fid) for fid in (raw.get("feature_ids") or [])],
                action_kind=str(raw.get("action_kind") or "exploration"),
                narrative=str(raw.get("narrative") or ""),
                extras=extras,
            )
        )
    findings_raw = payload.get("findings") or []
    findings: list[Finding] = []
    for raw in findings_raw:
        if not isinstance(raw, dict):
            continue
        findings.append(
            Finding(
                severity=str(raw.get("severity") or "important"),
                text=str(raw.get("text") or ""),
                feature_id=str(raw.get("feature_id") or ""),
            )
        )
    return FeatureProofBlock(
        feature_id=str(payload.get("feature_id") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        group_id=str(payload.get("group_id") or ""),
        verdict=payload.get("verdict"),
        detail=str(payload.get("detail") or ""),
        walkthrough_entries=entries,
        shared_with=[str(s) for s in (payload.get("shared_with") or [])],
        evidence_completeness=str(payload.get("evidence_completeness") or "full"),
        coverage_confidence=str(payload.get("coverage_confidence") or "high"),
        check_evidence_refs=[str(r) for r in (payload.get("check_evidence_refs") or [])],
        files_changed=[str(f) for f in (payload.get("files_changed") or [])],
        repair_history=list(payload.get("repair_history") or []),
        audit_narrative_excerpt=str(payload.get("audit_narrative_excerpt") or ""),
        findings=findings,
    )


def _render_feature_section(packet: ProofPacket, *, session_dir: Path | None) -> str:
    """Render the per-Feature proof section (A3 — research §7).

    Emits one `<section>` per Feature (in spec order) by handing each
    serialised feature-dict to `feature_proof_block_to_html`. Cross-link
    correctness (multi-Feature entries appearing in each Feature's
    section) is preserved by `build_feature_proof_blocks` upstream.

    Empty `packet.features` produces an empty section (no header, no
    content) so legacy packets render unchanged.
    """
    if not packet.features:
        return ""
    parts: list[str] = ["<h2>Features</h2>"]
    for payload in packet.features:
        if not isinstance(payload, dict):
            continue
        block = _feature_block_from_dict(payload)
        parts.append(
            feature_proof_block_to_html(
                block, project_kind=packet.project_kind,
            )
        )
    return "\n".join(parts)


def _render_group(s: GroupPacket, *, session_dir: Path | None) -> str:
    parts = [f'<section class="slice {escape(s.status)}">']
    landed_tag = (
        f'<span class="muted">landed @ {escape(s.landed_commit)}</span>'
        if s.landed_commit else ""
    )
    parts.append(
        f'<header><h3>{escape(s.group_id)} — {escape(s.name)}</h3>'
        f'<span class="meta">{escape(s.status)}{" · " + landed_tag if landed_tag else ""}</span></header>'
    )
    if s.branch:
        parts.append(f'<div class="muted">branch: <code>{escape(s.branch)}</code></div>')
    if s.owned_paths:
        parts.append(
            "<div class=\"muted\">owned_paths: "
            + ", ".join(f"<code>{escape(p)}</code>" for p in s.owned_paths)
            + "</div>"
        )
    if s.failure_narrative:
        parts.append(f'<div class="failure">{escape(s.failure_narrative)}</div>')
    if s.repair_attempts:
        parts.append(f'<div class="muted">repair attempts: {s.repair_attempts}</div>')

    if s.check_evidence:
        parts.append('<table class="checks">')
        parts.append(
            "<thead><tr><th>kind</th><th>verdict</th><th>detail</th><th>artifacts</th></tr></thead><tbody>"
        )
        for ce in s.check_evidence:
            verdict_cls = "pass" if ce["passed"] else "fail"
            verdict_label = "PASS" if ce["passed"] else "FAIL"
            artifacts_html = _render_artifact_list(ce.get("artifacts") or [], session_dir=session_dir)
            parts.append(
                f"<tr><td><code>{escape(ce['kind'])}</code></td>"
                f'<td class="{verdict_cls}">{verdict_label}</td>'
                f"<td>{escape(str(ce.get('detail', '')))}</td>"
                f"<td>{artifacts_html}</td></tr>"
            )
        parts.append("</tbody></table>")

    if s.audit_verdict is not None:
        averdict = s.audit_verdict
        verdict_cls = "pass" if averdict["passed"] else "fail"
        parts.append(
            f"<div class=\"muted\">audit verdict: "
            f'<span class="{verdict_cls}">{("pass" if averdict["passed"] else "fail").upper()}</span>'
            + (f" — {escape(str(averdict.get('detail', '')))}" if averdict.get("detail") else "")
            + "</div>"
        )

    parts.append("</section>")
    return "\n".join(parts)


def _render_artifact_list(artifacts: list[str], *, session_dir: Path | None) -> str:
    if not artifacts:
        return '<span class="muted">none</span>'
    parts: list[str] = ['<div class="thumbs">']
    for a in artifacts:
        rel = _relative_to_session(a, session_dir)
        if rel.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            parts.append(f'<a href="{escape(rel)}" target="_blank"><img src="{escape(rel)}" alt="evidence"></a>')
        else:
            parts.append(f'<a href="{escape(rel)}" target="_blank">{escape(Path(a).name)}</a>')
    parts.append("</div>")
    return "".join(parts)


def _relative_to_session(path: str, session_dir: Path | None) -> str:
    if session_dir is None:
        return path
    try:
        rel = os.path.relpath(path, session_dir)
        return rel
    except ValueError:
        return path


def _render_audit_section(packet: ProofPacket, *, session_dir: Path | None) -> str:
    parts = ["<h2>Audit</h2>"]
    if packet.audit_narrative:
        parts.append(f'<div class="narrative">{escape(packet.audit_narrative)}</div>')
    else:
        parts.append('<p class="muted">No audit narrative produced.</p>')

    if packet.walkthrough_artifacts:
        parts.append("<h3>Walkthrough artifacts</h3>")
        videos = [a for a in packet.walkthrough_artifacts if a.lower().endswith((".webm", ".mp4", ".mov"))]
        screenshots = [a for a in packet.walkthrough_artifacts if a.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]
        others = [a for a in packet.walkthrough_artifacts if a not in videos and a not in screenshots]

        for v in videos:
            rel = _relative_to_session(v, session_dir)
            parts.append(f'<video controls src="{escape(rel)}"></video>')

        if screenshots:
            parts.append('<div class="thumbs">')
            for sc in screenshots:
                rel = _relative_to_session(sc, session_dir)
                parts.append(f'<a href="{escape(rel)}" target="_blank"><img src="{escape(rel)}" alt="screenshot"></a>')
            parts.append("</div>")

        if others:
            parts.append("<ul>")
            for o in others:
                rel = _relative_to_session(o, session_dir)
                parts.append(f'<li><a href="{escape(rel)}" target="_blank">{escape(Path(o).name)}</a></li>')
            parts.append("</ul>")
    return "\n".join(parts)


def _render_limitations(packet: ProofPacket) -> str:
    if not packet.blocked_group_ids:
        return ""
    parts = ["<h2>Known limitations</h2>", "<p>The following slices did not land:</p><ul>"]
    blocked = [s for s in packet.groups if s.group_id in set(packet.blocked_group_ids)]
    for s in blocked:
        narrative = s.failure_narrative or "blocked"
        parts.append(f"<li><code>{escape(s.group_id)}</code> — {escape(narrative)}</li>")
    parts.append("</ul>")
    return "\n".join(parts)


def _render_merge_state(packet: ProofPacket) -> str:
    parts = ["<h2>Merge state</h2><ul>"]
    for s in packet.groups:
        if s.landed:
            parts.append(
                f"<li>✅ <code>{escape(s.group_id)}</code> landed @ "
                f"<code>{escape(s.landed_commit)}</code></li>"
            )
        else:
            parts.append(
                f"<li>❌ <code>{escape(s.group_id)}</code> not landed "
                f'<span class="muted">— {escape(s.failure_narrative or s.status)}</span></li>'
            )
    parts.append("</ul>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_proof_packet(
    packet: ProofPacket,
    session_dir: Path,
) -> tuple[Path, Path]:
    """Write proof-packet.html and proof-packet.json into session_dir.

    Returns (html_path, json_path).
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    html_path = session_dir / PROOF_PACKET_HTML
    json_path = session_dir / PROOF_PACKET_JSON
    html = render_html(packet, session_dir=session_dir)
    json_text = render_json(packet)
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(json_text, encoding="utf-8")
    return html_path, json_path


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


def render_run(
    spec: Spec,
    *,
    session_dir: Path,
    build_result: BuildResult,
    merge_result: MergeQueueResult,
    audit_result: AuditResult,
    wall_s: float,
    cost_usd: float,
) -> tuple[Path, Path]:
    """Compose + write both formats. Returns (html_path, json_path)."""
    packet = compose_proof_packet(
        spec, build_result, merge_result, audit_result,
        wall_s=wall_s, cost_usd=cost_usd,
    )
    return write_proof_packet(packet, session_dir)


__all__ = [
    "PROOF_PACKET_HTML",
    "PROOF_PACKET_JSON",
    "PROOF_PACKET_SCHEMA_VERSION",
    "ProofPacket",
    "GroupPacket",
    "compose_proof_packet",
    "render_html",
    "render_json",
    "render_run",
    "write_proof_packet",
]


# Pin imports the dispatch flow needs but doesn't reference at module top level.
_ = (Iterable, AuditVerdict, time, Group, spec_to_dict, Feature)
