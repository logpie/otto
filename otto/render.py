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

from otto.audit import AuditResult, AuditVerdict, SliceVerdict
from otto.build import BuildResult, SliceResult, SliceStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.spec_compile import Slice, Spec, spec_to_dict

logger = logging.getLogger("otto.render")

PROOF_PACKET_HTML = "proof-packet.html"
PROOF_PACKET_JSON = "proof-packet.json"
PROOF_PACKET_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class SlicePacket:
    """Per-slice data shaped for rendering."""

    slice_id: str
    title: str
    status: str  # SliceStatus.value or "landed" / "blocked"
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
    slices: list[SlicePacket]
    audit_narrative: str
    walkthrough_artifacts: list[str]  # absolute paths
    blocked_slice_ids: list[str]
    landed_slice_ids: list[str]


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
    audit_by_slice: dict[str, SliceVerdict] = {
        v.slice_id: v for v in audit_result.slice_verdicts
    }
    merge_by_slice: dict[str, MergeResult] = {
        r.slice_id: r for r in merge_result.results
    }
    build_by_slice: dict[str, SliceResult] = {
        r.slice_id: r for r in build_result.slice_results
    }

    slice_packets: list[SlicePacket] = []
    for s in spec.slices:
        bres = build_by_slice.get(s.id)
        mres = merge_by_slice.get(s.id)
        averdict = audit_by_slice.get(s.id)

        check_evidence: list[dict[str, Any]] = []
        # Prefer merge-time evidence (most recent) over build-time, but
        # fall back to build-time if merge didn't run for this slice.
        evidence_source = mres.slice_recheck_evidence if mres else (bres.last_evidence if bres else [])
        for c, ev in zip(s.checks, evidence_source, strict=False):
            kind = type(c).__name__
            check_evidence.append(_evidence_to_dict(kind, ev))

        landed = bool(mres and mres.status == MergeStatus.LANDED)
        if landed:
            slice_status = "landed"
        elif bres and bres.status == SliceStatus.PASSING:
            slice_status = "passing"  # passed build but did not land
        elif bres:
            slice_status = bres.status.value
        else:
            slice_status = "pending"

        failure = ""
        if mres and mres.status == MergeStatus.BLOCKED:
            failure = mres.failure_narrative
        elif bres and bres.status in (SliceStatus.BLOCKED, SliceStatus.FAILED_SCOPE):
            failure = bres.failure_narrative

        slice_packets.append(
            SlicePacket(
                slice_id=s.id,
                title=s.title,
                status=slice_status,
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
        slices=slice_packets,
        audit_narrative=audit_result.narrative,
        walkthrough_artifacts=[_path_to_str(p) for p in audit_result.walkthrough_artifacts],
        blocked_slice_ids=list(merge_result.blocked_ids) + list(build_result.blocked_ids),
        landed_slice_ids=list(merge_result.landed_ids),
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
        "blocked_slice_ids": packet.blocked_slice_ids,
        "landed_slice_ids": packet.landed_slice_ids,
        "slices": [
            {
                "slice_id": s.slice_id,
                "title": s.title,
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
            for s in packet.slices
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

    # Per-slice sections
    parts.append("<h2>Slices</h2>")
    for s in packet.slices:
        parts.append(_render_slice(s, session_dir=session_dir))

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
    <span class="value">{len(packet.landed_slice_ids)} landed / {len(packet.slices)} total</span>
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


def _render_slice(s: SlicePacket, *, session_dir: Path | None) -> str:
    parts = [f'<section class="slice {escape(s.status)}">']
    landed_tag = (
        f'<span class="muted">landed @ {escape(s.landed_commit)}</span>'
        if s.landed_commit else ""
    )
    parts.append(
        f'<header><h3>{escape(s.slice_id)} — {escape(s.title)}</h3>'
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
    if not packet.blocked_slice_ids:
        return ""
    parts = ["<h2>Known limitations</h2>", "<p>The following slices did not land:</p><ul>"]
    blocked = [s for s in packet.slices if s.slice_id in set(packet.blocked_slice_ids)]
    for s in blocked:
        narrative = s.failure_narrative or "blocked"
        parts.append(f"<li><code>{escape(s.slice_id)}</code> — {escape(narrative)}</li>")
    parts.append("</ul>")
    return "\n".join(parts)


def _render_merge_state(packet: ProofPacket) -> str:
    parts = ["<h2>Merge state</h2><ul>"]
    for s in packet.slices:
        if s.landed:
            parts.append(
                f"<li>✅ <code>{escape(s.slice_id)}</code> landed @ "
                f"<code>{escape(s.landed_commit)}</code></li>"
            )
        else:
            parts.append(
                f"<li>❌ <code>{escape(s.slice_id)}</code> not landed "
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
    "SlicePacket",
    "compose_proof_packet",
    "render_html",
    "render_json",
    "render_run",
    "write_proof_packet",
]


# Pin imports the dispatch flow needs but doesn't reference at module top level.
_ = (Iterable, AuditVerdict, time, Slice, spec_to_dict)
