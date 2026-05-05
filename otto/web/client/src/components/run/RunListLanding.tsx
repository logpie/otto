// RunListLanding — default landing page for the post-Phase-C frontend.
//
// Phase C.4 deleted the legacy <App/> Mission Control inspector and its
// /api/runs/<id>/... backend surface. The default landing now lists
// sessions from `/api/run-view` and links each to `?view=run-view&session=<id>`,
// which mounts <RunViewPage/> via main.tsx routing.
//
// B4 (post-RUA round 1): replaced the bullet-list of session-IDs with
// per-session cards per wireframe Screen 2 (intent, status pill, count
// rollup, wall+cost, finished-relative). Backend was extended to return
// a `sessions: [obj]` array alongside the legacy `runs: [str]` array.
// Cards collapse gracefully when fields are absent — "—" placeholders
// rather than failing the whole list.

import { useEffect, useMemo, useState } from "react";

interface Props {
  // Optional override for tests. Defaults to /api/run-view.
  endpoint?: string;
}

interface SessionSummary {
  id: string;
  intent: string | null;
  status: string | null;
  verdict: string | null;
  cost_usd: number | null;
  wall_s: number | null;
  feature_total: number | null;
  feature_passed: number | null;
  critical_findings: number | null;
  quality_score: number | null;
  finished_at: string | null;
  lifecycle: string | null;
}

interface ListResponse {
  runs?: string[];
  sessions?: SessionSummary[];
}

// Status pill — local copy. The parallel agent A is also building a
// Pill component; we duplicate the pattern here intentionally to keep
// this file independent and reconcile in a future commit.
//
// R2-B3 fix: when a session has reached a terminal verdict
// (passed/partial/blocked), surface the VERDICT rather than the
// lifecycle status — users care whether the run actually delivered,
// not that it's "completed". Non-terminal runs still render status
// (running, queued, awaiting_spec_review, etc.).
function StatusPill({
  status,
  verdict,
}: {
  status: string | null;
  verdict: string | null;
}) {
  const { label, tone } = pillSourceFor(status, verdict);
  return (
    <span
      className={`landing-status-pill landing-status-pill--${tone}`}
      data-testid="landing-status-pill"
    >
      {label}
    </span>
  );
}

function pillSourceFor(
  status: string | null,
  verdict: string | null,
): { label: string; tone: string } {
  const v = (verdict || "").toLowerCase();
  if (v === "passed") return { label: "passed", tone: "success" };
  if (v === "partial") return { label: "partial", tone: "warning" };
  if (v === "blocked") return { label: "blocked", tone: "danger" };
  const s = (status || "unknown").toLowerCase();
  return { label: s, tone: pillToneFor(s) };
}

function pillToneFor(status: string): string {
  if (status === "passed" || status === "landed") return "success";
  if (status === "partial" || status === "awaiting_spec_review") return "warning";
  if (
    status === "blocked" ||
    status === "failed" ||
    status === "aborted"
  )
    return "danger";
  if (
    status === "queued" ||
    status === "compiling" ||
    status === "building" ||
    status === "auditing" ||
    status === "rendering" ||
    status === "landing"
  )
    return "info";
  return "neutral";
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1).trimEnd() + "…";
}

function formatDuration(wall_s: number | null): string {
  if (wall_s === null || wall_s === undefined) return "—";
  if (wall_s < 60) return `${Math.round(wall_s)}s`;
  const m = Math.floor(wall_s / 60);
  const s = Math.round(wall_s - m * 60);
  return `${m}m${s > 0 ? ` ${s}s` : ""}`;
}

function formatCost(cost: number | null): string {
  if (cost === null || cost === undefined) return "—";
  return `$${cost.toFixed(2)}`;
}

const RTF =
  typeof Intl !== "undefined" && Intl.RelativeTimeFormat
    ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
    : null;

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "—";
  const diffMs = ts - Date.now();
  const absSec = Math.abs(diffMs) / 1000;
  if (!RTF) {
    if (absSec < 60) return "just now";
    return iso;
  }
  if (absSec < 60) return RTF.format(Math.round(diffMs / 1000), "second");
  if (absSec < 3600) return RTF.format(Math.round(diffMs / 60000), "minute");
  if (absSec < 86400) return RTF.format(Math.round(diffMs / 3600000), "hour");
  if (absSec < 86400 * 30)
    return RTF.format(Math.round(diffMs / 86400000), "day");
  if (absSec < 86400 * 365)
    return RTF.format(Math.round(diffMs / (86400000 * 30)), "month");
  return RTF.format(Math.round(diffMs / (86400000 * 365)), "year");
}

function SessionCard({ session }: { session: SessionSummary }) {
  const href = `?view=run-view&session=${encodeURIComponent(session.id)}`;
  const intent = session.intent
    ? truncate(session.intent, 80)
    : session.id;
  const fullIntent = session.intent || session.id;
  const total = session.feature_total ?? 0;
  const passed = session.feature_passed ?? 0;
  const critical = session.critical_findings ?? 0;
  const quality = session.quality_score;
  const showCounts = total > 0 || critical > 0 || quality !== null;
  const lifecycle = session.lifecycle;
  const isAwaitingReview = session.status === "awaiting_spec_review";

  return (
    <article
      className="landing-card"
      data-testid="landing-card"
      data-session-id={session.id}
    >
      <a className="landing-card-link" href={href} aria-label={`Open run ${session.id}`}>
        <div className="landing-card-row landing-card-row-top">
          <StatusPill status={session.status} verdict={session.verdict} />
          <span
            className="landing-card-intent"
            title={fullIntent}
          >
            {intent}
          </span>
        </div>
        {showCounts ? (
          <div className="landing-card-row landing-card-counts">
            {total > 0 ? (
              <span className="landing-card-counts-item">
                {passed}/{total} features
              </span>
            ) : null}
            {critical > 0 ? (
              <span className="landing-card-counts-item landing-card-counts-critical">
                {critical} critical {critical === 1 ? "finding" : "findings"}
              </span>
            ) : null}
            {quality !== null && quality !== undefined ? (
              <span className="landing-card-counts-item">
                q {quality}/5
              </span>
            ) : null}
          </div>
        ) : null}
        <div className="landing-card-row landing-card-metrics">
          <span>wall {formatDuration(session.wall_s)}</span>
          <span>cost {formatCost(session.cost_usd)}</span>
          <span>finished {formatRelative(session.finished_at)}</span>
        </div>
      </a>
      {(isAwaitingReview || lifecycle === "draft") && (
        <div className="landing-card-actions">
          {/* R3-B7: Review-spec is a secondary action on the landing
              card. The card row already routes to the run detail; the
              landing's primary affordance lives elsewhere. Use the
              neutral `.secondary` outlined style so this link doesn't
              compete with the future primary CTA via the teal tone. */}
          <a
            className="landing-card-action secondary"
            href={`?view=spec-review&spec=${encodeURIComponent(session.id)}`}
            onClick={(e) => e.stopPropagation()}
          >
            Review spec
          </a>
        </div>
      )}
    </article>
  );
}

export function RunListLanding({ endpoint = "/api/run-view" }: Props) {
  const [payload, setPayload] = useState<ListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(endpoint)
      .then((resp) => {
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        }
        return resp.json() as Promise<ListResponse>;
      })
      .then((body) => {
        if (cancelled) return;
        setPayload(body);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setError(err.message || String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [endpoint]);

  const sessions = useMemo<SessionSummary[]>(() => {
    if (!payload) return [];
    if (payload.sessions && payload.sessions.length > 0) {
      return payload.sessions;
    }
    // Backwards compatibility: if backend only returned `runs: string[]`
    // (older fixture, pre-B4), synthesize stub summaries so the cards
    // still render with the session id as the headline.
    return (payload.runs || []).map((id) => ({
      id,
      intent: null,
      status: null,
      verdict: null,
      cost_usd: null,
      wall_s: null,
      feature_total: null,
      feature_passed: null,
      critical_findings: null,
      quality_score: null,
      finished_at: null,
      lifecycle: null,
    }));
  }, [payload]);

  if (error) {
    return (
      <div className="run-list-landing run-list-landing--error" data-testid="run-list-error">
        <h1>Runs</h1>
        <p>Failed to load sessions: {error}</p>
      </div>
    );
  }

  if (payload === null) {
    return (
      <div className="run-list-landing run-list-landing--loading" data-testid="run-list-loading">
        <h1>Runs</h1>
        <p>Loading sessions…</p>
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="run-list-landing run-list-landing--empty" data-testid="run-list-empty">
        <h1>Runs</h1>
        <p>No sessions yet. Run <code>otto build</code> to create one.</p>
      </div>
    );
  }

  return (
    <div className="run-list-landing" data-testid="run-list">
      <header className="run-list-landing-heading">
        <h1>Runs</h1>
        <span
          className="run-list-landing-count-badge"
          data-testid="run-list-landing-count"
          aria-label={`${sessions.length} session${sessions.length === 1 ? "" : "s"}`}
        >
          {sessions.length} session{sessions.length === 1 ? "" : "s"}
        </span>
      </header>
      <div className="landing-card-list" role="list">
        {sessions.map((session) => (
          <SessionCard key={session.id} session={session} />
        ))}
      </div>
    </div>
  );
}

export default RunListLanding;
