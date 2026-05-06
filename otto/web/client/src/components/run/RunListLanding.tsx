// RunListLanding — default landing page for the post-Phase-C frontend.
//
// Phase C.4 deleted the legacy <App/> Mission Control inspector and its
// /api/runs/<id>/... backend surface. The default landing now lists
// sessions from `/api/run-view` and links each to `?view=run-view&session=<id>`,
// which mounts <RunViewPage/> via main.tsx routing.
//
// B4 (post-RUA round 1): replaced the bullet-list of session-IDs with
// per-session cards per wireframe Screen 2.
//
// Round 5 cleanup (R3-B16, R3-B9, R3-B10, R3-B8, R3-B2, R3-B4):
//   - Whole card is now a single <a> link to the run drawer; the
//     "Review spec" affordance moved inline as a small secondary link
//     so the card-as-link affordance is unambiguous.
//   - Toolbar with status filter + manual refresh button. While at
//     least one session is non-terminal, the list auto-refreshes every
//     5 seconds; the timer stops when all sessions are terminal so the
//     idle landing isn't burning network.
//   - "Built in N groups" subline per card (backend now returns
//     `group_count`).
//   - "+ New run" primary button opens the real JobDialog and enqueues
//     work through `/api/queue/{command}`. Mission Control Web is the
//     primary product surface; it must not ask users to copy CLI commands
//     for the normal build flow.
//   - "3 sessions" badge has its own row with proper spacing; intent
//     and metric rows now use distinct typographic weights.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { MouseEvent } from "react";
import { api } from "../../api";
import { JobDialog } from "../new-job/JobDialog";
import { RunViewPage } from "./RunViewPage";
import type { StateResponse } from "../../types";
import { errorMessage } from "../../utils/missionControl";

interface Props {
  // Optional override for tests. Defaults to /api/run-view.
  endpoint?: string;
  project?: StateResponse["project"] | undefined;
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
  group_count: number | null;
  finished_at: string | null;
  lifecycle: string | null;
}

interface ListResponse {
  runs?: string[];
  sessions?: SessionSummary[];
}

// R3-B9: filter set kept intentionally small — the four states a user
// actually sorts by when scanning a long list. "running" folds in
// every non-terminal status (queued, compiling, building, ...) so the
// dropdown stays a one-look UI.
type StatusFilter = "all" | "passed" | "partial" | "blocked" | "running";

const TERMINAL_VERDICTS = new Set(["passed", "partial", "blocked"]);

function isTerminal(s: SessionSummary): boolean {
  return s.verdict !== null && TERMINAL_VERDICTS.has((s.verdict || "").toLowerCase());
}

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

// R3-B16: the entire card is now an <a> to the run drawer. Review-spec
// is rendered inline as a small secondary link inside the same card,
// using stopPropagation so clicking it doesn't bubble up and pre-empt
// its own navigation. Card hover state hints clickability.
function SessionCard({
  session,
  onOpen,
}: {
  session: SessionSummary;
  onOpen: (sessionId: string) => void;
}) {
  const href = `?view=run-view&session=${encodeURIComponent(session.id)}`;
  const intent = session.intent
    ? truncate(session.intent, 80)
    : session.id;
  const fullIntent = session.intent || session.id;
  const total = session.feature_total ?? 0;
  const passed = session.feature_passed ?? 0;
  const critical = session.critical_findings ?? 0;
  const quality = session.quality_score;
  const groupCount = session.group_count;
  const showCounts = total > 0 || critical > 0 || quality !== null;
  const lifecycle = session.lifecycle;
  const isAwaitingReview = session.status === "awaiting_spec_review";
  const showReviewLink = isAwaitingReview || lifecycle === "draft";
  const specHref = `?view=spec-review&spec=${encodeURIComponent(session.id)}`;
  const openInDrawer = (event: MouseEvent<HTMLAnchorElement>) => {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    onOpen(session.id);
  };

  return (
    <a
      className="landing-card landing-card-as-link"
      data-testid="landing-card"
      data-session-id={session.id}
      href={href}
      onClick={openInDrawer}
      aria-label={`Open run ${session.id}`}
    >
      <div className="landing-card-row landing-card-row-top">
        <StatusPill status={session.status} verdict={session.verdict} />
        <span
          className="landing-card-intent"
          title={fullIntent}
        >
          {intent}
        </span>
      </div>
      {/* R3-B10: "Built in N groups" subline. Renders only when the
          backend has a concrete count to surface; legacy sessions
          without spec.json fall through to no subline. */}
      {groupCount !== null && groupCount !== undefined && groupCount > 0 ? (
        <div
          className="landing-card-row landing-card-subline"
          data-testid="landing-card-groups"
        >
          Built in {groupCount} {groupCount === 1 ? "group" : "groups"}
        </div>
      ) : null}
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
        {showReviewLink ? (
          <a
            className="landing-card-inline-action"
            href={specHref}
            onClick={(e) => e.stopPropagation()}
            data-testid="landing-card-review-spec"
          >
            Review spec ▸
          </a>
        ) : null}
      </div>
    </a>
  );
}

function RunDetailOverlay({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <div
        className="run-list-drawer-backdrop"
        data-testid="run-list-drawer-backdrop"
        onClick={onClose}
      />
      <aside
        className="run-list-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`Run ${sessionId}`}
        data-testid="run-list-detail-drawer"
      >
        <button
          type="button"
          className="run-list-detail-drawer-close"
          data-testid="run-list-detail-drawer-close"
          aria-label="Close run details"
          onClick={onClose}
        >
          ×
        </button>
        <RunViewPage sessionId={sessionId} />
      </aside>
    </>
  );
}

function matchFilter(s: SessionSummary, f: StatusFilter): boolean {
  if (f === "all") return true;
  const v = (s.verdict || "").toLowerCase();
  if (f === "passed") return v === "passed";
  if (f === "partial") return v === "partial";
  if (f === "blocked") return v === "blocked";
  if (f === "running") return !TERMINAL_VERDICTS.has(v);
  return true;
}

// R3-B9: filter is URL-persisted at `?lf=<status>` so refreshes / shares
// preserve scope. Read on mount; defaults to "all" when absent / invalid.
const FILTER_VALUES: readonly StatusFilter[] = ["all", "passed", "partial", "blocked", "running"];
function readInitialFilter(): StatusFilter {
  if (typeof window === "undefined") return "all";
  const raw = new URLSearchParams(window.location.search).get("lf");
  return (FILTER_VALUES as readonly string[]).includes(raw || "")
    ? (raw as StatusFilter)
    : "all";
}
function writeFilterParam(f: StatusFilter): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (f === "all") url.searchParams.delete("lf");
  else url.searchParams.set("lf", f);
  window.history.replaceState(window.history.state ?? {}, "", `${url.pathname}${url.search}${url.hash}`);
}

export function RunListLanding({ endpoint = "/api/run-view", project }: Props) {
  const [payload, setPayload] = useState<ListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<StatusFilter>(() => readInitialFilter());
  const [refreshing, setRefreshing] = useState(false);
  const [showNewRun, setShowNewRun] = useState(false);
  const [queueMessage, setQueueMessage] = useState<string | null>(null);
  const [queueMessageKind, setQueueMessageKind] = useState<"info" | "error">("info");
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  // R3-B9 auto-refresh: bumped each time a poll completes so dependents
  // (the auto-refresh effect) can re-trigger without re-fetching from
  // their own deps.
  const [tick, setTick] = useState(0);

  const fetchSessions = useCallback(async (signal?: AbortSignal) => {
    setRefreshing(true);
    try {
      const resp = await fetch(endpoint, signal ? { signal } : undefined);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      }
      const body = (await resp.json()) as ListResponse;
      setPayload(body);
      setError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError((err as Error).message || String(err));
    } finally {
      setRefreshing(false);
    }
  }, [endpoint]);

  // Initial fetch.
  useEffect(() => {
    const ctrl = new AbortController();
    void fetchSessions(ctrl.signal);
    return () => ctrl.abort();
  }, [fetchSessions]);

  // Persist filter to URL (mirror the existing routeState pattern).
  useEffect(() => {
    writeFilterParam(filter);
  }, [filter]);

  const sessions = useMemo<SessionSummary[]>(() => {
    if (!payload) return [];
    if (payload.sessions && payload.sessions.length > 0) {
      return payload.sessions;
    }
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
      group_count: null,
      finished_at: null,
      lifecycle: null,
    }));
  }, [payload]);

  // R3-B9: poll only while at least one session is non-terminal. Keeps
  // the idle landing page silent on the network and stops the timer
  // once everything is settled.
  const hasInflight = useMemo(
    () => sessions.some((s) => !isTerminal(s)),
    [sessions],
  );
  useEffect(() => {
    if (!hasInflight) return;
    const id = window.setInterval(() => {
      setTick((t) => t + 1);
    }, 5000);
    return () => window.clearInterval(id);
  }, [hasInflight]);
  useEffect(() => {
    if (tick === 0) return;
    void fetchSessions();
  }, [tick, fetchSessions]);

  const filteredSessions = useMemo(
    () => sessions.filter((s) => matchFilter(s, filter)),
    [sessions, filter],
  );

  const handleManualRefresh = () => {
    void fetchSessions();
  };
  const openSession = useCallback((sessionId: string) => {
    window.history.pushState({...window.history.state, runDrawer: sessionId}, "", window.location.href);
    setSelectedSessionId(sessionId);
  }, []);
  const closeSession = useCallback(() => {
    if (window.history.state?.runDrawer) {
      window.history.back();
      return;
    }
    setSelectedSessionId(null);
  }, []);
  useEffect(() => {
    const onPop = () => setSelectedSessionId(null);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const handleQueued = useCallback(async (message?: string) => {
    setShowNewRun(false);
    const queuedMessage = message || "Job queued.";
    setQueueMessageKind("info");
    setQueueMessage(queuedMessage);
    try {
      const started = await api<{message?: string}>("/api/watcher/start", {
        method: "POST",
        body: JSON.stringify({}),
      });
      setQueueMessage(`${queuedMessage} ${started.message || "Queue runner started."}`);
    } catch (error) {
      setQueueMessageKind("error");
      setQueueMessage(`${queuedMessage} Could not start queue runner: ${errorMessage(error)}.`);
    }
    await fetchSessions();
  }, [fetchSessions]);

  const handleQueueError = useCallback((message: string) => {
    setQueueMessageKind("error");
    setQueueMessage(message);
  }, []);

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

  return (
    <div className="run-list-landing" data-testid="run-list">
      <header className="run-list-landing-heading">
        {/* R3-B2: count badge has its own row so it doesn't crowd the h1. */}
        <div className="run-list-landing-title-row">
          <h1>Runs</h1>
          <button
            type="button"
            className="run-list-landing-new-run"
            onClick={() => setShowNewRun(true)}
            data-testid="landing-new-run-button"
          >
            + New run
          </button>
        </div>
        <div className="run-list-landing-meta-row">
          <span
            className="run-list-landing-count-badge"
            data-testid="run-list-landing-count"
            aria-label={`${sessions.length} session${sessions.length === 1 ? "" : "s"}`}
          >
            {sessions.length} session{sessions.length === 1 ? "" : "s"}
            {filter !== "all" && filteredSessions.length !== sessions.length ? (
              <>
                {" "}· {filteredSessions.length} after filter
              </>
            ) : null}
          </span>
          <div className="run-list-landing-toolbar">
            <label className="run-list-landing-filter">
              <span className="run-list-landing-filter-label">Filter:</span>
              <select
                value={filter}
                onChange={(e) => setFilter(e.target.value as StatusFilter)}
                data-testid="landing-filter"
                aria-label="Filter sessions by verdict"
              >
                <option value="all">All</option>
                <option value="passed">Passed</option>
                <option value="partial">Partial</option>
                <option value="blocked">Blocked</option>
                <option value="running">Running</option>
              </select>
            </label>
            <button
              type="button"
              className="run-list-landing-refresh"
              onClick={handleManualRefresh}
              disabled={refreshing}
              data-testid="landing-refresh"
              aria-label="Refresh sessions"
              title={hasInflight ? "Auto-refreshing every 5s while runs are in flight" : "Refresh"}
            >
              {refreshing ? "Refreshing…" : "Refresh"}
              {hasInflight && !refreshing ? (
                <span className="run-list-landing-live-dot" aria-hidden>
                  ●
                </span>
              ) : null}
            </button>
          </div>
        </div>
      </header>
      {queueMessage ? (
        <div
          className={`run-list-queue-banner run-list-queue-banner--${queueMessageKind}`}
          data-testid="run-list-queue-banner"
          role={queueMessageKind === "error" ? "alert" : "status"}
        >
          {queueMessage}
        </div>
      ) : null}
      {sessions.length === 0 ? (
        <div className="run-list-landing-empty-body" data-testid="run-list-empty">
          <p>
            No sessions yet. Click <strong>+ New run</strong> above to queue
            work from Mission Control.
          </p>
        </div>
      ) : filteredSessions.length === 0 ? (
        <div className="run-list-landing-empty-body" data-testid="run-list-filter-empty">
          <p>
            No sessions match the <strong>{filter}</strong> filter.{" "}
            <button
              type="button"
              className="run-list-landing-empty-clear"
              onClick={() => setFilter("all")}
            >
              Clear filter
            </button>
          </p>
        </div>
      ) : (
        <div className="landing-card-list" role="list">
          {filteredSessions.map((session) => (
            <SessionCard key={session.id} session={session} onOpen={openSession} />
          ))}
        </div>
      )}
      {showNewRun && (
        <JobDialog
          project={project}
          dirtyFiles={[]}
          priorRunOptions={[]}
          onClose={() => setShowNewRun(false)}
          onQueued={handleQueued}
          onError={handleQueueError}
        />
      )}
      {selectedSessionId ? (
        <RunDetailOverlay sessionId={selectedSessionId} onClose={closeSession} />
      ) : null}
    </div>
  );
}

export default RunListLanding;
