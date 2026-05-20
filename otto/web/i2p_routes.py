"""Web routes for the i2p (intent-to-product) pipeline — Step 8a.

Mounted alongside the existing legacy routes; does not modify them.
Provides read-only endpoints for the new pipeline:

    GET /api/i2p/sessions
        List all sessions that contain a `spec/spec.json` (i.e. were
        created by `otto run`). Returns session metadata only — fast.

    GET /api/i2p/sessions/{session_id}
        Return the full spec.json + replayed state.jsonl summary +
        proof-packet (if present).

    GET /api/i2p/sessions/{session_id}/proof-packet.html
        Serve the rendered proof packet HTML directly. The HTML is
        self-contained (CSS inlined). Artifact links are relative to the
        session directory, served by the `evidence` route below.

    GET /api/i2p/sessions/{session_id}/proof-packet.json
        Same content, machine-readable.

    GET /api/i2p/sessions/{session_id}/evidence/{path:path}
        Serve an evidence artifact (screenshot / video / log) under the
        session dir. Path traversal is bounded inside session_dir.

Editable spec review is deferred: this is the read-only listing + proof
packet viewer. Step 8a's full SpecReviewWorkspace port from codex-feats
lands in a follow-up PR.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from otto.paths import PROOF_PACKET_HTML_FILENAME, PROOF_PACKET_JSON_FILENAME
from otto.spec_state import journal_path, replay
from otto.web.session_resolver import iter_session_dirs, resolve_session_dir

logger = logging.getLogger("otto.web.i2p")


def install_i2p_routes(
    app: FastAPI,
    *,
    project_dir: Path | None = None,
    project_dir_provider: Callable[[], Path | None] | None = None,
) -> None:
    """Mount /api/i2p/* routes and the /i2p/ HTML view onto the given app.

    V19 fix: support launcher mode by accepting a callable that resolves
    the current project at REQUEST time. In launcher mode the active
    project changes via /api/projects/select; routes constructed with a
    fixed `project_dir` from app boot point at the wrong (or no) project.
    Pass `project_dir_provider=lambda: app.state.project_dir` so each
    request consults the currently-selected project.

    `project_dir` (legacy fixed) and `project_dir_provider` (V19 dynamic)
    are mutually exclusive. Provider takes precedence when both are set.
    """
    if project_dir_provider is None and project_dir is None:
        raise ValueError("install_i2p_routes needs project_dir or project_dir_provider")

    def _resolve_project_dir() -> Path:
        """Get the current project dir or raise 409 if none selected."""
        if project_dir_provider is not None:
            resolved = project_dir_provider()
        else:
            resolved = project_dir
        if resolved is None:
            raise HTTPException(
                status_code=409,
                detail="No project selected. Open a project from the launcher first.",
            )
        return resolved

    router = APIRouter(prefix="/api/i2p", tags=["i2p"])
    page_router = APIRouter(prefix="/i2p", tags=["i2p"])

    @page_router.get("", response_class=HTMLResponse, include_in_schema=False)
    @page_router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def i2p_page() -> HTMLResponse:
        return HTMLResponse(_I2P_INDEX_HTML)

    @page_router.get("/{session_id}", response_class=HTMLResponse, include_in_schema=False)
    def i2p_session_page(session_id: str) -> HTMLResponse:
        # The page itself is the same shell; client-side JS reads the
        # session id from the path and fetches /api/i2p/sessions/<id>.
        return HTMLResponse(_I2P_INDEX_HTML)

    @router.get("/sessions")
    def list_sessions() -> JSONResponse:
        items: list[dict[str, Any]] = []
        for resolved in sorted(
            iter_session_dirs(_resolve_project_dir()),
            key=lambda item: item.session_id,
            reverse=True,
        ):
            spec_path = resolved.path / "spec" / "spec.json"
            if not spec_path.exists():
                continue  # not an i2p session
            items.append(_session_summary(resolved.path))
        return JSONResponse({"sessions": items})

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> JSONResponse:
        session_dir = resolve_session_dir(_resolve_project_dir(), session_id)
        spec_path = session_dir / "spec" / "spec.json"
        if not spec_path.exists():
            raise HTTPException(status_code=404, detail="not an i2p session")
        try:
            spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"spec load failed: {exc}") from exc

        # v5 FlatSpec carries no `groups` field; legacy specs do. Read defensively.
        group_ids = [s["id"] for s in (spec_data.get("groups") or []) if isinstance(s, dict) and "id" in s]
        run_state = None
        if journal_path(session_dir).exists():
            try:
                state = replay(session_dir, group_ids=group_ids)
                run_state = _state_to_dict(state)
            except Exception as exc:
                logger.warning("replay failed for %s: %s", session_id, exc)

        proof_packet = None
        proof_packet_path = session_dir / PROOF_PACKET_JSON_FILENAME
        if proof_packet_path.exists():
            try:
                proof_packet = json.loads(proof_packet_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("proof packet parse failed for %s: %s", session_id, exc)

        return JSONResponse({
            "session_id": session_id,
            "spec": spec_data,
            "state": run_state,
            "proof_packet": proof_packet,
            "has_proof_packet_html": (session_dir / PROOF_PACKET_HTML_FILENAME).exists(),
        })

    @router.get("/sessions/{session_id}/proof-packet.html")
    def get_proof_packet_html(session_id: str) -> FileResponse:
        session_dir = resolve_session_dir(_resolve_project_dir(), session_id)
        path = session_dir / PROOF_PACKET_HTML_FILENAME
        if not path.exists():
            raise HTTPException(status_code=404, detail="proof-packet.html not yet produced")
        return FileResponse(path, media_type="text/html")

    @router.get("/sessions/{session_id}/proof-packet.json")
    def get_proof_packet_json(session_id: str) -> FileResponse:
        session_dir = resolve_session_dir(_resolve_project_dir(), session_id)
        path = session_dir / PROOF_PACKET_JSON_FILENAME
        if not path.exists():
            raise HTTPException(status_code=404, detail="proof-packet.json not yet produced")
        return FileResponse(path, media_type="application/json")

    @router.get("/sessions/{session_id}/evidence/{path:path}")
    def get_evidence(session_id: str, path: str) -> FileResponse:
        session_dir = resolve_session_dir(_resolve_project_dir(), session_id)
        target = (session_dir / path).resolve()
        # Path traversal guard — must remain inside session_dir.
        try:
            target.relative_to(session_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="path escapes session dir") from exc
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="evidence not found")
        return FileResponse(target)

    app.include_router(router)
    app.include_router(page_router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_summary(session_dir: Path) -> dict[str, Any]:
    """Compose a lightweight summary for the session listing."""
    summary: dict[str, Any] = {
        "session_id": session_dir.name,
        "has_spec": True,
        "has_proof_packet_html": (session_dir / PROOF_PACKET_HTML_FILENAME).exists(),
    }
    spec_path = session_dir / "spec" / "spec.json"
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        summary["intent"] = data.get("intent") or ""
        summary["project_kind"] = data.get("project_kind") or ""
        summary["group_count"] = len(data.get("groups") or [])
    except Exception:
        summary["intent"] = ""
        summary["project_kind"] = ""
        summary["group_count"] = 0

    proof_packet_path = session_dir / PROOF_PACKET_JSON_FILENAME
    if proof_packet_path.exists():
        try:
            packet = json.loads(proof_packet_path.read_text(encoding="utf-8"))
            summary["verdict"] = packet.get("verdict")
            summary["wall_s"] = packet.get("wall_s")
            summary["cost_usd"] = packet.get("cost_usd")
            summary["landed_count"] = len(packet.get("landed_group_ids") or [])
            summary["blocked_count"] = len(packet.get("blocked_group_ids") or [])
        except Exception:
            pass
    return summary


def _state_to_dict(state: Any) -> dict[str, Any]:
    """Serialise a `RunState` object to a JSON-safe dict."""
    return {
        "audit_started": getattr(state, "audit_started", False),
        "audit_finished": getattr(state, "audit_finished", False),
        "audit_verdict": getattr(state, "audit_verdict", ""),
        "run_finished": getattr(state, "run_finished", False),
        "run_verdict": getattr(state, "run_verdict", ""),
        "groups": [
            {
                "group_id": ss.group_id,
                "phase": ss.phase,
                "attempts": ss.attempts,
                "last_failure": ss.last_failure,
                "last_event_ts": ss.last_event_ts,
            }
            for ss in getattr(state, "groups", {}).values()
        ],
    }


_I2P_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Otto i2p — sessions</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 1100px; margin: 0 auto; padding: 24px;
    color: #1f2328;
  }
  header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 18px; }
  h1 { margin: 0; font-size: 1.4em; }
  .muted { color: #57606a; }
  table { width: 100%; border-collapse: collapse; margin: 14px 0; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #d0d7de; font-size: 0.92em; }
  th { background: #f6f8fa; font-weight: 600; }
  tr:hover { background: #fafbfc; cursor: pointer; }
  .verdict { display: inline-block; padding: 2px 8px; border-radius: 12px; font-weight: 600; font-size: 0.78em; }
  .verdict.passed  { background: #dafbe1; color: #1a7f37; }
  .verdict.partial { background: #fff8c5; color: #9a6700; }
  .verdict.blocked { background: #ffebe9; color: #cf222e; }
  .verdict.unknown { background: #ddf4ff; color: #0969da; }
  .actions a { margin-right: 8px; color: #0969da; text-decoration: none; }
  .detail { margin-top: 18px; }
  .detail .row { display: grid; grid-template-columns: 160px 1fr; gap: 8px 16px; align-items: baseline; padding: 4px 0; }
  .group {
    border: 1px solid #d0d7de; border-radius: 6px; padding: 10px 14px; margin: 8px 0;
    background: #f6f8fa;
  }
  .group.landed   { border-left: 4px solid #1a7f37; }
  .group.eligible { border-left: 4px solid #0969da; }
  .group.blocked  { border-left: 4px solid #cf222e; }
  .group.failed   { border-left: 4px solid #9a6700; }
  .group.building { border-left: 4px solid #6e7781; }
  .group.checking { border-left: 4px solid #0969da; }
  .group.pending  { border-left: 4px solid #d0d7de; }
  iframe { width: 100%; height: 78vh; border: 1px solid #d0d7de; border-radius: 6px; margin-top: 14px; }
  pre.code { background: #f6f8fa; border: 1px solid #d0d7de; padding: 10px; border-radius: 6px;
             overflow-x: auto; font-size: 0.85em; }
  .empty { padding: 40px; text-align: center; color: #57606a; border: 1px dashed #d0d7de; border-radius: 6px; }
</style>
</head>
<body>
<header>
  <h1>Otto · intent-to-product sessions</h1>
  <span class="muted">Phase A — read-only view</span>
</header>
<div id="app">Loading…</div>

<script>
const API = "/api/i2p";
function el(tag, attrs={}, children=[]) {
  const e = document.createElement(tag);
  for (const k in attrs) {
    if (k === 'class') e.className = attrs[k];
    else if (k === 'onclick') e.onclick = attrs[k];
    else if (k === 'html') e.innerHTML = attrs[k];
    else e.setAttribute(k, attrs[k]);
  }
  for (const c of (Array.isArray(children) ? children : [children])) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}
function escape(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function jget(url) {
  const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
  if (!r.ok) throw new Error(url + ': ' + r.status);
  return r.json();
}
function verdictBadge(v) {
  v = (v || 'unknown').toLowerCase();
  return el('span', { class: 'verdict ' + v }, v.toUpperCase());
}
function fmtNum(n, d=1) { return (n == null) ? '—' : Number(n).toFixed(d); }

async function renderList() {
  const root = document.getElementById('app');
  root.replaceChildren(el('p', {class:'muted'}, 'Loading sessions…'));
  let data;
  try { data = await jget(API + '/sessions'); }
  catch (e) {
    root.replaceChildren(el('div', {class:'empty'}, 'Failed to load sessions: ' + e.message));
    return;
  }
  if (!data.sessions.length) {
    root.replaceChildren(el('div', {class:'empty'},
      'No i2p sessions yet. Run `otto run "<intent>"` to compile a spec.'));
    return;
  }
  const head = el('thead', {}, el('tr', {}, [
    el('th',{},'session_id'), el('th',{},'verdict'), el('th',{},'intent'),
    el('th',{},'kind'), el('th',{},'groups'), el('th',{},'wall (s)'),
    el('th',{},'cost ($)'), el('th',{},'actions')
  ]));
  const rows = data.sessions.map(s => {
    const tr = el('tr', { onclick: () => location.assign('/i2p/' + s.session_id) }, [
      el('td', {}, el('code',{},s.session_id)),
      el('td', {}, verdictBadge(s.verdict)),
      el('td', {}, escape(s.intent || '').slice(0, 80)),
      el('td', {}, s.project_kind || '—'),
      el('td', {}, (s.landed_count||0) + ' / ' + (s.group_count || 0)),
      el('td', {}, fmtNum(s.wall_s, 0)),
      el('td', {}, fmtNum(s.cost_usd, 2)),
      el('td', { class:'actions' }, [
        s.has_proof_packet_html
          ? el('a', { href: '/api/i2p/sessions/' + s.session_id + '/proof-packet.html', target: '_blank' }, 'proof')
          : null,
        el('a', { href: '/i2p/' + s.session_id }, 'detail')
      ])
    ]);
    return tr;
  });
  const table = el('table', {}, [head, el('tbody', {}, rows)]);
  root.replaceChildren(table);
}

async function renderDetail(sessionId) {
  const root = document.getElementById('app');
  root.replaceChildren(el('p', {class:'muted'}, 'Loading…'));
  let data;
  try { data = await jget(API + '/sessions/' + encodeURIComponent(sessionId)); }
  catch (e) {
    root.replaceChildren(el('div', {class:'empty'}, 'Failed to load: ' + e.message));
    return;
  }
  const back = el('a', { href: '/i2p/' }, '← all sessions');
  const verdictV = (data.proof_packet && data.proof_packet.verdict) || 'unknown';
  const heading = el('h2', {}, [
    el('code', {}, sessionId), document.createTextNode(' '), verdictBadge(verdictV)
  ]);
  const intent = data.spec ? data.spec.intent : '';
  const meta = el('p', { class: 'muted' }, escape(intent));
  // Group list with state-derived phase
  const phaseBy = {};
  if (data.state && data.state.groups) {
    for (const ss of data.state.groups) phaseBy[ss.group_id] = ss.phase;
  }
  const groups = (data.spec && data.spec.groups) || [];
  const groupCards = groups.map(s => {
    const phase = phaseBy[s.id] || 'pending';
    const statusLine = el('div', { class: 'muted' },
      'phase: ' + phase + (s.dependencies && s.dependencies.length ? ' · deps: ' + s.dependencies.join(', ') : ''));
    const ownedLine = (s.owned_paths && s.owned_paths.length)
      ? el('div', { class: 'muted' }, 'owned: ' + s.owned_paths.join(', '))
      : null;
    const checksLine = (s.checks && s.checks.length)
      ? el('div', { class: 'muted' }, s.checks.length + ' check(s): ' +
          s.checks.map(c => c.kind).join(', '))
      : null;
    return el('div', { class: 'group ' + phase }, [
      el('strong', {}, s.id + ' — '), el('span', {}, escape(s.name || '')),
      statusLine, ownedLine, checksLine
    ]);
  });
  const groupSection = el('div', {}, [el('h3', {}, 'Groups'), ...groupCards]);

  // Proof packet iframe (Step 8b: embedded view of the rendered proof packet)
  let proofSection = null;
  if (data.has_proof_packet_html) {
    proofSection = el('div', {}, [
      el('h3', {}, 'Proof packet'),
      el('iframe', { src: '/api/i2p/sessions/' + sessionId + '/proof-packet.html' })
    ]);
  } else if (data.spec) {
    proofSection = el('div', {class:'muted'}, [
      el('h3', {}, 'Proof packet'),
      'Not yet produced. Build / audit / render must run before this is available.'
    ]);
  }

  // Spec JSON (collapsible)
  const specBlock = el('details', {}, [
    el('summary', {}, 'Spec JSON'),
    el('pre', { class: 'code' }, data.spec ? JSON.stringify(data.spec, null, 2) : '(none)')
  ]);

  root.replaceChildren(back, heading, meta, groupSection, proofSection, specBlock);
}

function route() {
  const m = location.pathname.match(/^\\/i2p\\/([^/]+)$/);
  if (m) renderDetail(decodeURIComponent(m[1]));
  else renderList();
}
window.addEventListener('popstate', route);
route();
</script>
</body>
</html>"""


__all__ = ["install_i2p_routes"]
