import {FormEvent, useCallback, useEffect, useRef, useState} from "react";
import {ApiError, api, buildQueuePayload, stateQueryParams} from "./api";
import type {
  ActionResult,
  ActionState,
  ArtifactContentResponse,
  ArtifactRef,
  HistoryItem,
  ImproveSubcommand,
  JobCommand,
  LandingItem,
  LandingState,
  LiveRunItem,
  LogsResponse,
  MissionEvent,
  OutcomeFilter,
  QueueResult,
  RunDetail,
  RunTypeFilter,
  StateResponse,
  WatcherInfo,
} from "./types";

interface ToastState {
  message: string;
  severity: "information" | "warning" | "error";
}

interface ResultBannerState {
  title: string;
  body: string;
  severity: ToastState["severity"];
}

interface ConfirmState {
  title: string;
  body: string;
  confirmLabel: string;
  tone?: "primary" | "danger";
  onConfirm: () => Promise<void>;
}

interface Filters {
  type: RunTypeFilter;
  outcome: OutcomeFilter;
  query: string;
  activeOnly: boolean;
}

const defaultFilters: Filters = {
  type: "all",
  outcome: "all",
  query: "",
  activeOnly: false,
};

export function App() {
  const [filters, setFilters] = useState<Filters>(defaultFilters);
  const [data, setData] = useState<StateResponse | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logText, setLogText] = useState("");
  const [showingArtifacts, setShowingArtifacts] = useState(false);
  const [selectedArtifactIndex, setSelectedArtifactIndex] = useState<number | null>(null);
  const [artifactContent, setArtifactContent] = useState<ArtifactContentResponse | null>(null);
  const [refreshStatus, setRefreshStatus] = useState("idle");
  const [jobOpen, setJobOpen] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [resultBanner, setResultBanner] = useState<ResultBannerState | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  const logOffsetRef = useRef(0);
  const selectedRunIdRef = useRef<string | null>(null);

  useEffect(() => {
    selectedRunIdRef.current = selectedRunId;
  }, [selectedRunId]);

  const showToast = useCallback((message: string, severity: ToastState["severity"] = "information") => {
    if (severity === "error") setLastError(message);
    setToast({message, severity});
    window.setTimeout(() => setToast(null), 3200);
  }, []);

  const requestConfirm = useCallback((next: ConfirmState) => {
    setConfirm(next);
  }, []);

  const selectRun = useCallback((runId: string) => {
    if (runId !== selectedRunIdRef.current) {
      setDetail(null);
      setLogText("");
      setArtifactContent(null);
      setSelectedArtifactIndex(null);
    }
    setSelectedRunId(runId);
  }, []);

  const executeConfirmedAction = useCallback(async () => {
    if (!confirm || confirmPending) return;
    setConfirmPending(true);
    try {
      await confirm.onConfirm();
      setConfirm(null);
    } catch (error) {
      showToast(errorMessage(error), "error");
    } finally {
      setConfirmPending(false);
    }
  }, [confirm, confirmPending, showToast]);

  const loadLogs = useCallback(async (runId: string, reset = false) => {
    if (showingArtifacts && !reset) return;
    const offset = reset ? 0 : logOffsetRef.current;
    try {
      const logs = await api<LogsResponse>(`/api/runs/${encodeURIComponent(runId)}/logs?offset=${offset}`);
      if (selectedRunIdRef.current !== runId) return;
      if (reset) setLogText("");
      if (logs.text) {
        setLogText((current) => `${reset ? "" : current}${logs.text}`);
      }
      logOffsetRef.current = logs.next_offset || offset;
    } catch (error) {
      if (detailWasRemoved(error)) return;
      showToast(errorMessage(error), "error");
    }
  }, [showingArtifacts, showToast]);

  const refreshDetail = useCallback(async (runId: string, resetLogs = false) => {
    const params = stateQueryParams(filters).toString();
    const nextDetail = await api<RunDetail>(`/api/runs/${encodeURIComponent(runId)}?${params}`);
    if (selectedRunIdRef.current !== runId) return;
    setDetail(nextDetail);
    if (resetLogs) {
      await loadLogs(runId, true);
    }
  }, [filters, loadLogs]);

  const refresh = useCallback(async () => {
    setRefreshStatus("refreshing");
    try {
      const next = await api<StateResponse>(`/api/state?${stateQueryParams(filters).toString()}`);
      setData(next);
      setLastError(null);
      const visible = visibleRunIds(next);
      if (selectedRunId && visible.has(selectedRunId)) {
        void refreshDetail(selectedRunId).catch((error) => {
          if (!detailWasRemoved(error)) showToast(errorMessage(error), "error");
        });
      }
      setSelectedRunId((current) => {
        if (current && visible.has(current)) return current;
        return next.live.items[0]?.run_id || next.landing.items.find((item) => item.run_id)?.run_id || next.history.items[0]?.run_id || null;
      });
      setRefreshStatus("idle");
    } catch (error) {
      setRefreshStatus("error");
      showToast(errorMessage(error), "error");
    }
  }, [filters, refreshDetail, selectedRunId, showToast]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), refreshIntervalMs(data));
    return () => window.clearInterval(interval);
  }, [refresh, data?.live.refresh_interval_s]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      setLogText("");
      setArtifactContent(null);
      return;
    }
    setDetail(null);
    logOffsetRef.current = 0;
    setLogText("");
    setArtifactContent(null);
    setSelectedArtifactIndex(null);
    refreshDetail(selectedRunId, true).catch((error) => {
      if (detailWasRemoved(error)) {
        setSelectedRunId(null);
        setDetail(null);
        setLogText("");
        setArtifactContent(null);
        return;
      }
      showToast(errorMessage(error), "error");
    });
  }, [refreshDetail, selectedRunId, showToast]);

  useEffect(() => {
    if (!selectedRunId || showingArtifacts) return;
    const interval = window.setInterval(() => void loadLogs(selectedRunId), 1200);
    return () => window.clearInterval(interval);
  }, [loadLogs, selectedRunId, showingArtifacts]);

  const runActionForRun = useCallback(async (runId: string, action: string, message: string, label?: string) => {
    if (action === "merge" && data?.landing.merge_blocked) {
      showToast(mergeBlockedText(data.landing), "error");
      return;
    }
    const actionLabel = capitalize(label || action);
    requestConfirm({
      title: action === "merge" ? "Land task" : `${actionLabel} run`,
      body: message,
      confirmLabel: action === "merge" ? "Land task" : actionLabel,
      tone: ["cancel", "cleanup"].includes(action) ? "danger" : "primary",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>(`/api/runs/${encodeURIComponent(runId)}/actions/${action}`, {
            method: "POST",
            body: JSON.stringify({}),
          });
          handleActionResult(result, `${action} requested`, showToast, setResultBanner);
          if (result.refresh !== false) await refresh();
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [data?.landing, refresh, requestConfirm, showToast]);

  const mergeReadyTasks = useCallback(async () => {
    const landing = data?.landing;
    const ready = landing?.counts.ready || 0;
    if (landing?.merge_blocked) {
      showToast(mergeBlockedText(landing), "error");
      return;
    }
    if (!ready) {
      showToast("No land-ready tasks", "warning");
      return;
    }
    requestConfirm({
      title: "Land ready tasks",
      body: landingBulkConfirmation(landing),
      confirmLabel: ready === 1 ? "Land 1 task" : `Land ${ready} tasks`,
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>("/api/actions/merge-all", {method: "POST", body: "{}"});
          handleActionResult(result, "merge all requested", showToast, setResultBanner);
          if (result.refresh !== false) await refresh();
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [data?.landing, refresh, requestConfirm, showToast]);

  const runWatcherAction = useCallback(async (action: "start" | "stop") => {
    const execute = async () => {
      try {
        const result = await api<ActionResult | {message?: string}>(`/api/watcher/${action}`, {
          method: "POST",
          body: action === "start" ? JSON.stringify({concurrent: 2}) : "{}",
        });
        showToast(result.message || `watcher ${action} requested`);
        await refresh();
      } catch (error) {
        showToast(errorMessage(error), "error");
      }
    };
    if (action === "stop") {
      requestConfirm({
        title: "Stop watcher",
        body: "Stop the queue watcher? Running tasks will be interrupted.",
        confirmLabel: "Stop watcher",
        tone: "danger",
        onConfirm: execute,
      });
      return;
    }
    await execute();
  }, [refresh, requestConfirm, showToast]);

  const loadArtifact = useCallback(async (index: number) => {
    if (!selectedRunId) return;
    setSelectedArtifactIndex(index);
    setShowingArtifacts(true);
    setArtifactContent(null);
    try {
      const content = await api<ArtifactContentResponse>(`/api/runs/${encodeURIComponent(selectedRunId)}/artifacts/${index}/content`);
      setArtifactContent(content);
    } catch (error) {
      showToast(errorMessage(error), "error");
    }
  }, [selectedRunId, showToast]);

  const project = data?.project;
  const watcher = data?.watcher;
  const landing = data?.landing;
  const active = activeCount(watcher);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">O</div>
          <div>
            <h1>Otto</h1>
            <p>Mission Control</p>
          </div>
        </div>
        <ProjectMeta project={project} watcher={watcher} active={active} />
        <button className="primary" type="button" onClick={() => setJobOpen(true)}>New job</button>
        <button type="button" disabled={!canStartWatcher(data)} title={data?.runtime.supervisor.start_blocked_reason || watcher?.health.next_action || ""} onClick={() => void runWatcherAction("start")}>Start watcher</button>
        <button type="button" disabled={!canStopWatcher(data)} title={watcher?.health.next_action || ""} onClick={() => void runWatcherAction("stop")}>Stop watcher</button>
        <button type="button" disabled={!canMerge(landing)} title={mergeButtonTitle(landing)} onClick={() => void mergeReadyTasks()}>
          {landing?.counts.ready ? `Land ${landing.counts.ready} ready` : "Land ready"}
        </button>
      </aside>

      <main className="workspace">
        <Toolbar filters={filters} refreshStatus={refreshStatus} onChange={setFilters} onRefresh={() => void refresh()} />
        <OperationalOverview
          data={data}
          lastError={lastError}
          resultBanner={resultBanner}
          onDismissError={() => setLastError(null)}
          onDismissResult={() => setResultBanner(null)}
        />
        <section className="grid">
          <div className="tables">
            <LandingQueue
              landing={landing}
              selectedRunId={selectedRunId}
              onSelect={selectRun}
              onMergeReady={() => void mergeReadyTasks()}
              onMergeRun={(runId) => void runActionForRun(runId, "merge", "Land this task into the target branch?")}
            />
            <LiveRuns items={data?.live.items || []} selectedRunId={selectedRunId} onSelect={selectRun} />
            <EventTimeline events={data?.events} />
            <History items={data?.history.items || []} totalRows={data?.history.total_rows || 0} selectedRunId={selectedRunId} onSelect={selectRun} />
          </div>
          <RunDetailPanel
            detail={detail}
            landing={landing}
            logText={logText}
            showingArtifacts={showingArtifacts}
            selectedArtifactIndex={selectedArtifactIndex}
            artifactContent={artifactContent}
            onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
            onShowLogs={() => {
              setShowingArtifacts(false);
              setArtifactContent(null);
              if (selectedRunId) void loadLogs(selectedRunId, true);
            }}
            onShowArtifacts={() => setShowingArtifacts(true)}
            onLoadArtifact={(index) => void loadArtifact(index)}
            onBackToArtifacts={() => {
              setSelectedArtifactIndex(null);
              setArtifactContent(null);
            }}
          />
        </section>
      </main>

      {jobOpen && (
        <JobDialog
          onClose={() => setJobOpen(false)}
          onQueued={async (message) => {
            setJobOpen(false);
            showToast(message || "queued");
            await refresh();
          }}
          onError={(message) => showToast(message, "error")}
        />
      )}
      {confirm && (
        <ConfirmDialog
          confirm={confirm}
          pending={confirmPending}
          onCancel={() => {
            if (!confirmPending) setConfirm(null);
          }}
          onConfirm={() => void executeConfirmedAction()}
        />
      )}
      {toast && <div id="toast" className={`visible toast-${toast.severity}`} role="status" aria-live="polite">{toast.message}</div>}
    </div>
  );
}

function ProjectMeta({project, watcher, active}: {project: StateResponse["project"] | undefined; watcher: WatcherInfo | undefined; active: number}) {
  const counts = watcher?.counts || {};
  const health = watcher?.health;
  return (
    <dl className="project-meta" aria-label="Project metadata">
      <MetaItem label="Project" value={project?.name || "-"} />
      <MetaItem label="Branch" value={project?.branch || "-"} />
      <MetaItem label="State" value={!project ? "unknown" : project.dirty ? "dirty" : "clean"} />
      <MetaItem label="Watcher" value={watcherSummary(watcher)} />
      <MetaItem label="Heartbeat" value={health?.heartbeat_age_s === null || health?.heartbeat_age_s === undefined ? "-" : `${Math.round(health.heartbeat_age_s)}s ago`} />
      <MetaItem label="Active" value={String(active)} />
      <MetaItem label="Queue" value={`queued ${counts.queued || 0} / active ${active} / done ${counts.done || 0}`} />
    </dl>
  );
}

function MetaItem({label, value}: {label: string; value: string}) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function Toolbar({filters, refreshStatus, onChange, onRefresh}: {
  filters: Filters;
  refreshStatus: string;
  onChange: (filters: Filters) => void;
  onRefresh: () => void;
}) {
  return (
    <header className="toolbar">
      <div className="filters" aria-label="Run filters">
        <label>Type
          <select value={filters.type} onChange={(event) => onChange({...filters, type: event.target.value as RunTypeFilter})}>
            <option value="all">All</option>
            <option value="build">Build</option>
            <option value="improve">Improve</option>
            <option value="certify">Certify</option>
            <option value="merge">Merge</option>
            <option value="queue">Queue</option>
          </select>
        </label>
        <label>Outcome
          <select value={filters.outcome} onChange={(event) => onChange({...filters, outcome: event.target.value as OutcomeFilter})}>
            <option value="all">All</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
            <option value="interrupted">Interrupted</option>
            <option value="cancelled">Cancelled</option>
            <option value="removed">Removed</option>
            <option value="other">Other</option>
          </select>
        </label>
        <label className="search-label">Search
          <input
            value={filters.query}
            type="search"
            placeholder="run, task, branch"
            onChange={(event) => onChange({...filters, query: event.target.value})}
          />
        </label>
        <label className="check-label">
          <input
            checked={filters.activeOnly}
            type="checkbox"
            onChange={(event) => onChange({...filters, activeOnly: event.target.checked})}
          />
          Active
        </label>
        <button type="button" onClick={() => onChange(defaultFilters)}>Clear filters</button>
      </div>
      <div className="toolbar-actions">
        <span className="muted">{refreshStatus}</span>
        <button type="button" onClick={onRefresh}>Refresh</button>
      </div>
    </header>
  );
}

function OperationalOverview({data, lastError, resultBanner, onDismissError, onDismissResult}: {
  data: StateResponse | null;
  lastError: string | null;
  resultBanner: ResultBannerState | null;
  onDismissError: () => void;
  onDismissResult: () => void;
}) {
  const health = workflowHealth(data);
  return (
    <section className="overview" aria-label="Mission overview">
      <div className="overview-strip">
        <OverviewMetric label="Active" value={String(health.active)} tone={health.active ? "info" : "neutral"} />
        <OverviewMetric label="Needs attention" value={String(health.needsAttention)} tone={health.needsAttention ? "danger" : "neutral"} />
        <OverviewMetric label="Ready" value={String(health.ready)} tone={health.ready ? "success" : "neutral"} />
        <OverviewMetric label="Repository" value={health.repositoryLabel} tone={health.repositoryTone} />
        <OverviewMetric label="Watcher" value={health.watcherLabel} tone={health.watcherTone} />
        <OverviewMetric label="Runtime" value={health.runtimeLabel} tone={health.runtimeTone} />
      </div>
      {lastError && (
        <div className="status-banner error">
          <strong>Last error</strong>
          <span>{lastError}</span>
          <button type="button" onClick={onDismissError}>Dismiss</button>
        </div>
      )}
      {resultBanner && (
        <div className={`status-banner ${resultBanner.severity === "error" ? "error" : "warning"}`}>
          <strong>{resultBanner.title}</strong>
          <span>{resultBanner.body}</span>
          <button type="button" onClick={onDismissResult}>Dismiss</button>
        </div>
      )}
      {data?.runtime.issues.length ? <RuntimeWarnings data={data} /> : null}
    </section>
  );
}

function OverviewMetric({label, value, tone}: {label: string; value: string; tone: "neutral" | "info" | "success" | "warning" | "danger"}) {
  return (
    <div className={`overview-metric tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function RuntimeWarnings({data}: {data: StateResponse}) {
  const top = data.runtime.issues.slice(0, 3);
  const bannerTone = top.some((issue) => issue.severity === "error") ? "error" : "warning";
  const backlog = data.runtime.command_backlog;
  const suffix = [
    backlog.pending ? `${backlog.pending} pending` : "",
    backlog.processing ? `${backlog.processing} processing` : "",
    backlog.malformed ? `${backlog.malformed} malformed` : "",
  ].filter(Boolean).join(" / ");
  return (
    <div className={`status-banner ${bannerTone} runtime-banner`}>
      <strong>Runtime</strong>
      <span title={top.map((issue) => `${issue.label}: ${issue.detail}`).join("\n")}>
        {top.map((issue) => `${issue.label}: ${issue.next_action}`).join(" | ")}
      </span>
      <span className="runtime-backlog">{suffix || data.runtime.status}</span>
    </div>
  );
}

function LandingQueue({landing, selectedRunId, onSelect, onMergeReady, onMergeRun}: {
  landing: LandingState | undefined;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  onMergeReady: () => void;
  onMergeRun: (runId: string) => void;
}) {
  const items = landing?.items || [];
  return (
    <section className="panel landing-panel" aria-labelledby="landingHeading">
      <div className="panel-heading">
        <div>
          <h2 id="landingHeading">Review &amp; Land</h2>
          <p className="panel-subtitle">{landingSummaryText(landing)}</p>
        </div>
        <button className="primary" type="button" disabled={!canMerge(landing)} title={mergeButtonTitle(landing)} onClick={onMergeReady}>
          {landing?.counts.ready ? `Land ${landing.counts.ready} ready` : "Land ready"}
        </button>
      </div>
      <LandingWarnings landing={landing} />
      <LandingPlan landing={landing} onMergeReady={onMergeReady} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Review</th>
              <th>Task</th>
              <th>Branch</th>
              <th>Changes</th>
              <th>Proof</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {items.length ? items.map((item) => (
              <LandingRow
                key={item.task_id}
                item={item}
                selected={item.run_id === selectedRunId}
                mergeBlocked={Boolean(landing?.merge_blocked)}
                onSelect={onSelect}
                onMergeRun={onMergeRun}
              />
            )) : (
              <tr><td colSpan={6} className="empty-cell">No queued work yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function LandingPlan({landing, onMergeReady}: {landing: LandingState | undefined; onMergeReady: () => void}) {
  const items = landing?.items || [];
  const ready = items.filter((item) => item.landing_state === "ready");
  const needsAction = items.filter((item) => item.landing_state === "blocked");
  const merged = items.filter((item) => item.landing_state === "merged");
  const firstReady = ready.slice(0, 3);
  return (
    <div className="landing-plan" aria-label="Landing plan">
      <div className="plan-summary">
        <PlanMetric label="Ready to land" value={String(ready.length)} tone={ready.length ? "success" : "neutral"} />
        <PlanMetric label="Needs action" value={String(needsAction.length)} tone={needsAction.length ? "warning" : "neutral"} />
        <PlanMetric label="Already landed" value={String(merged.length)} tone={merged.length ? "info" : "neutral"} />
      </div>
      <div className="plan-copy">
        <strong>{landingPlanHeadline(landing)}</strong>
        <span>{landingPlanBody(landing, ready, needsAction)}</span>
      </div>
      {firstReady.length > 0 && (
        <ul className="plan-ready-list">
          {firstReady.map((item) => (
            <li key={item.task_id}>
              <span>{item.task_id}</span>
              <strong>{changeLine(item)} / {proofLine(item)}</strong>
            </li>
          ))}
          {ready.length > firstReady.length && <li>+{ready.length - firstReady.length} more ready task{ready.length - firstReady.length === 1 ? "" : "s"}</li>}
        </ul>
      )}
      <button type="button" disabled={!canMerge(landing)} title={mergeButtonTitle(landing)} onClick={onMergeReady}>
        Review complete: land ready work
      </button>
    </div>
  );
}

function PlanMetric({label, value, tone}: {label: string; value: string; tone: "neutral" | "info" | "success" | "warning"}) {
  return (
    <div className={`plan-metric plan-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function LandingWarnings({landing}: {landing: LandingState | undefined}) {
  const blockers = landing?.merge_blockers || [];
  const dirtyFiles = landing?.dirty_files || [];
  const collisions = landing?.collisions || [];
  if (!blockers.length && !collisions.length) return null;
  return (
    <div className="landing-warnings">
      {blockers.length > 0 && (
        <div>
          <strong>Merge blocked by local repository state</strong>
          <p>{blockers.join("; ")}. Commit, stash, or revert these project changes before merging.</p>
          {dirtyFiles.length > 0 && (
            <ul>
              {dirtyFiles.slice(0, 6).map((path) => <li key={path}>{path}</li>)}
              {dirtyFiles.length > 6 && <li>+{dirtyFiles.length - 6} more</li>}
            </ul>
          )}
        </div>
      )}
      {collisions.length > 0 && (
        <div>
          <strong>{collisions.length} overlap{collisions.length === 1 ? "" : "s"} before merging into {landing?.target || "main"}</strong>
          <ul>
            {collisions.slice(0, 4).map((collision) => (
              <li key={`${collision.left}-${collision.right}`}>
                {collision.left} vs {collision.right}: {collision.files.join(", ")}
                {collision.file_count > collision.files.length ? ` (+${collision.file_count - collision.files.length} more)` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function LandingRow({item, selected, mergeBlocked, onSelect, onMergeRun}: {
  item: LandingItem;
  selected: boolean;
  mergeBlocked: boolean;
  onSelect: (runId: string) => void;
  onMergeRun: (runId: string) => void;
}) {
  const canMergeRow = item.landing_state === "ready" && Boolean(item.run_id) && !mergeBlocked;
  const canOpenRow = Boolean(item.run_id);
  return (
    <tr
      className={selected ? "selected" : ""}
      role="button"
      tabIndex={item.run_id ? 0 : -1}
      aria-selected={selected}
      onClick={() => item.run_id && onSelect(item.run_id)}
      onKeyDown={(event) => item.run_id && selectOnKeyboard(event, () => onSelect(item.run_id as string))}
    >
      <td><span className={`landing-chip landing-${item.landing_state || "blocked"}`}>{item.label || item.landing_state}</span></td>
      <td title={item.summary || ""}>
        <strong>{item.task_id || "-"}</strong>
        <span className="landing-subtext">{shortText(item.summary || "", 96)}</span>
      </td>
      <td title={item.branch || ""}>{item.branch || "-"}</td>
      <td title={changeListTitle(item)}>{changeLine(item)}</td>
      <td title={proofLine(item)}>{proofLine(item)}</td>
      <td>
        <button type="button" disabled={!canOpenRow || (item.landing_state === "ready" && !canMergeRow)} title={mergeBlocked ? mergeButtonTitle({merge_blocked: true} as LandingState) : ""} onClick={(event) => {
          event.stopPropagation();
          if (!item.run_id) return;
          if (canMergeRow) {
            onMergeRun(item.run_id);
            return;
          }
          onSelect(item.run_id);
        }}>
          {actionLabelForLanding(item)}
        </button>
      </td>
    </tr>
  );
}

function LiveRuns({items, selectedRunId, onSelect}: {items: LiveRunItem[]; selectedRunId: string | null; onSelect: (runId: string) => void}) {
  return (
    <section className="panel" aria-labelledby="liveHeading">
      <div className="panel-heading">
        <h2 id="liveHeading">Live Runs</h2>
        <span className="pill">{items.length}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Run</th>
              <th>Branch / Task</th>
              <th>Elapsed</th>
              <th>Usage</th>
              <th>Event</th>
            </tr>
          </thead>
          <tbody>
            {items.length ? items.map((item) => (
              <tr
                key={item.run_id}
                className={item.run_id === selectedRunId ? "selected" : ""}
                role="button"
                tabIndex={0}
                aria-selected={item.run_id === selectedRunId}
                onClick={() => onSelect(item.run_id)}
                onKeyDown={(event) => selectOnKeyboard(event, () => onSelect(item.run_id))}
              >
                <td className={`status-${item.display_status}`} title={item.overlay?.reason || item.display_status}>{item.display_status.toUpperCase()}</td>
                <td title={item.run_id}>{item.display_id || item.run_id}</td>
                <td title={item.branch_task || ""}>{item.branch_task || "-"}</td>
                <td>{item.elapsed_display || "-"}</td>
                <td>{item.cost_display || "-"}</td>
                <td title={item.last_event || ""}>{item.last_event || "-"}</td>
              </tr>
            )) : (
              <tr><td colSpan={6} className="empty-cell">No live runs.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function History({items, totalRows, selectedRunId, onSelect}: {items: HistoryItem[]; totalRows: number; selectedRunId: string | null; onSelect: (runId: string) => void}) {
  return (
    <section className="panel" aria-labelledby="historyHeading">
      <div className="panel-heading">
        <h2 id="historyHeading">History</h2>
        <span className="pill">{totalRows}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Outcome</th>
              <th>Run</th>
              <th>Summary</th>
              <th>Duration</th>
              <th>Usage</th>
            </tr>
          </thead>
          <tbody>
            {items.length ? items.map((item) => (
              <tr
                key={item.run_id}
                className={item.run_id === selectedRunId ? "selected" : ""}
                role="button"
                tabIndex={0}
                aria-selected={item.run_id === selectedRunId}
                onClick={() => onSelect(item.run_id)}
                onKeyDown={(event) => selectOnKeyboard(event, () => onSelect(item.run_id))}
              >
                <td className={`status-${(item.terminal_outcome || item.status || "").toLowerCase()}`}>{item.outcome_display || "-"}</td>
                <td title={item.run_id}>{item.queue_task_id || item.run_id}</td>
                <td title={item.summary || ""}>{item.summary || "-"}</td>
                <td>{item.duration_display || "-"}</td>
                <td>{item.cost_display || "-"}</td>
              </tr>
            )) : (
              <tr><td colSpan={5} className="empty-cell">No matching history.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function EventTimeline({events}: {events: StateResponse["events"] | undefined}) {
  const items = events?.items || [];
  const malformed = events?.malformed_count || 0;
  return (
    <section className="panel timeline-panel" aria-labelledby="timelineHeading">
      <div className="panel-heading">
        <div>
          <h2 id="timelineHeading">Operator Timeline</h2>
          <p className="panel-subtitle">{timelineSubtitle(events)}</p>
        </div>
        <span className="pill">{events?.total_count || 0}</span>
      </div>
      <div className="timeline-list" role="list">
        {malformed > 0 && (
          <div className="timeline-warning">Ignored {malformed} malformed event row{malformed === 1 ? "" : "s"}.</div>
        )}
        {items.length ? items.map((event) => (
          <div className={`timeline-item event-${event.severity}`} key={event.event_id || `${event.created_at}-${event.message}`} role="listitem">
            <span className="timeline-severity">{event.severity}</span>
            <div>
              <strong title={event.message}>{event.message}</strong>
              <span>{eventTargetLine(event)}</span>
            </div>
            <time dateTime={event.created_at}>{formatEventTime(event.created_at)}</time>
          </div>
        )) : (
          <div className="timeline-empty">No operator events yet.</div>
        )}
      </div>
    </section>
  );
}

function RunDetailPanel({detail, landing, logText, showingArtifacts, selectedArtifactIndex, artifactContent, onRunAction, onShowLogs, onShowArtifacts, onLoadArtifact, onBackToArtifacts}: {
  detail: RunDetail | null;
  landing: LandingState | undefined;
  logText: string;
  showingArtifacts: boolean;
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  onRunAction: (action: string, label?: string) => void;
  onShowLogs: () => void;
  onShowArtifacts: () => void;
  onLoadArtifact: (index: number) => void;
  onBackToArtifacts: () => void;
}) {
  return (
    <aside className="detail" aria-labelledby="detailHeading">
      <div className="panel-heading">
        <h2 id="detailHeading">{detail ? "Review Packet" : "Run Detail"}</h2>
        <span className="pill">{detail?.display_status || "-"}</span>
      </div>
      {detail ? (
        <>
          <ReviewPacket packet={detail.review_packet} onRunAction={onRunAction} />
          <div className="detail-body">
            <h3>{detail.title || detail.run_id}</h3>
            <dl>
              <dt>Run</dt><dd>{detail.run_id}</dd>
              <dt>Type</dt><dd>{detail.domain} / {detail.run_type}</dd>
              <dt>Branch</dt><dd>{detail.branch || "-"}</dd>
              <dt>Worktree</dt><dd>{detail.worktree || detail.cwd || "-"}</dd>
              <dt>Provider</dt><dd>{providerLine(detail)}</dd>
              <dt>Artifacts</dt><dd>{detail.artifacts.length}</dd>
              {detail.overlay && <><dt>Overlay</dt><dd>{detail.overlay.reason}</dd></>}
              {detail.summary_lines.map((line, index) => <DetailLine key={`${line}-${index}`} line={line} />)}
            </dl>
          </div>
          <ActionBar actions={detail.legal_actions || []} mergeBlocked={Boolean(landing?.merge_blocked)} onRunAction={onRunAction} />
          <div className="detail-tabs">
            <button className={`tab ${!showingArtifacts ? "active" : ""}`} type="button" onClick={onShowLogs}>Logs</button>
            <button className={`tab ${showingArtifacts ? "active" : ""}`} type="button" onClick={onShowArtifacts}>Artifacts</button>
          </div>
          {!showingArtifacts ? (
            <pre className="log-pane">{logText}</pre>
          ) : (
            <ArtifactPane
              artifacts={detail.artifacts || []}
              selectedArtifactIndex={selectedArtifactIndex}
              artifactContent={artifactContent}
              onLoadArtifact={onLoadArtifact}
              onBack={onBackToArtifacts}
            />
          )}
        </>
      ) : (
        <div className="detail-body empty">Select a run.</div>
      )}
    </aside>
  );
}

function ReviewPacket({packet, onRunAction}: {packet: RunDetail["review_packet"]; onRunAction: (action: string, label?: string) => void}) {
  const action = packet.next_action;
  const blockers = packet.readiness.blockers || [];
  const evidence = packet.evidence.slice(0, 4);
  return (
    <section className={`review-packet review-${packet.readiness.tone || "info"}`} aria-label="Review packet">
      <div className="review-head">
        <div>
          <span className="review-kicker">{packet.readiness.label}</span>
          <strong>{packet.headline}</strong>
          <span title={packet.summary}>{packet.summary}</span>
        </div>
        <button
          className={action.enabled ? "primary" : ""}
          type="button"
          disabled={!action.enabled || !action.action_key}
          title={action.reason || ""}
          onClick={() => action.action_key && onRunAction(actionName(action.action_key), action.label)}
        >
          {reviewActionLabel(action.label)}
        </button>
      </div>
      <div className="review-next-step">
        <strong>Next</strong>
        <span>{packet.readiness.next_step}</span>
      </div>
      {blockers.length > 0 && (
        <ul className="review-blockers" aria-label="Review blockers">
          {blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
        </ul>
      )}
      <div className="review-grid">
        <ReviewMetric label="Stories" value={storiesLine(packet)} />
        <ReviewMetric label="Changes" value={packet.changes.file_count ? `${packet.changes.file_count} file${packet.changes.file_count === 1 ? "" : "s"}` : "-"} />
        <ReviewMetric label="Evidence" value={`${packet.evidence.filter((item) => item.exists).length}/${packet.evidence.length}`} />
      </div>
      <div className="review-checklist" aria-label="Readiness checklist">
        {packet.checks.map((check) => (
          <div className={`review-check check-${check.status}`} key={check.key}>
            <span>{checkStatusLabel(check.status)}</span>
            <div>
              <strong>{check.label}</strong>
              <p>{check.detail}</p>
            </div>
          </div>
        ))}
      </div>
      {packet.failure && <div className="review-note danger">{packet.failure.reason || "failure recorded"}</div>}
      {packet.changes.diff_error && <div className="review-note danger">{packet.changes.diff_error}</div>}
      {packet.changes.files.length > 0 && (
        <ul className="review-files" aria-label="Changed files">
          {packet.changes.files.map((path) => <li key={path}>{path}</li>)}
          {packet.changes.truncated && <li>more files not shown</li>}
        </ul>
      )}
      {evidence.length > 0 && (
        <div className="review-evidence" aria-label="Evidence artifacts">
          {evidence.map((artifact) => (
            <span className={artifact.exists ? "" : "missing"} key={`${artifact.index}-${artifact.path}`}>
              {artifact.label}{artifact.exists ? "" : " missing"}
            </span>
          ))}
          {packet.evidence.length > evidence.length && <span>+{packet.evidence.length - evidence.length} more</span>}
        </div>
      )}
      {packet.changes.diff_command && <code title={packet.changes.diff_command}>{packet.changes.diff_command}</code>}
    </section>
  );
}

function ReviewMetric({label, value}: {label: string; value: string}) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function DetailLine({line}: {line: string}) {
  const split = line.indexOf(":");
  if (split > 0 && split < 24) {
    return (
      <>
        <dt>{line.slice(0, split)}</dt>
        <dd>{line.slice(split + 1).trim() || "-"}</dd>
      </>
    );
  }
  return (
    <>
      <dt>Info</dt>
      <dd>{line}</dd>
    </>
  );
}

function ActionBar({actions, mergeBlocked, onRunAction}: {actions: ActionState[]; mergeBlocked: boolean; onRunAction: (action: string, label?: string) => void}) {
  const visible = actions.filter((action) => !["o", "e", "M"].includes(action.key));
  return (
    <div className="action-bar">
      {visible.map((action) => {
        const name = actionName(action.key);
        const disabled = !action.enabled || (action.key === "m" && mergeBlocked);
        const title = action.key === "m" && mergeBlocked ? "Commit, stash, or revert local project changes before merging." : action.reason || action.preview || "";
        return (
          <button key={action.key} type="button" disabled={disabled} title={title} onClick={() => onRunAction(name, action.label)}>
            {reviewActionLabel(action.label)}
          </button>
        );
      })}
    </div>
  );
}

function ArtifactPane({artifacts, selectedArtifactIndex, artifactContent, onLoadArtifact, onBack}: {
  artifacts: ArtifactRef[];
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  onLoadArtifact: (index: number) => void;
  onBack: () => void;
}) {
  if (selectedArtifactIndex !== null) {
    return (
      <div className="artifact-pane">
        <button type="button" onClick={onBack}>Back to artifacts</button>
        <div className="artifact-meta">{artifactContent?.artifact.label || "artifact"} {artifactContent?.truncated ? "(truncated)" : ""}</div>
        <pre>{artifactContent?.content || ""}</pre>
      </div>
    );
  }
  if (!artifacts.length) return <div className="artifact-pane">No artifacts.</div>;
  return (
    <div className="artifact-pane artifact-list">
      {artifacts.map((artifact) => (
        <button key={artifact.index} type="button" disabled={!artifact.exists} onClick={() => onLoadArtifact(artifact.index)}>
          <strong>{artifact.label}</strong>
          <span>{artifact.kind} {artifact.exists ? "" : "(missing)"}</span>
        </button>
      ))}
    </div>
  );
}

function JobDialog({onClose, onQueued, onError}: {onClose: () => void; onQueued: (message?: string) => Promise<void>; onError: (message: string) => void}) {
  const [command, setCommand] = useState<JobCommand>("build");
  const [subcommand, setSubcommand] = useState<"bugs" | "feature" | "target">("bugs");
  const [intent, setIntent] = useState("");
  const [taskId, setTaskId] = useState("");
  const [after, setAfter] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [fast, setFast] = useState(true);
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (command === "build" && !intent.trim()) {
      setStatus("Build intent is required.");
      return;
    }
    setStatus("queueing");
    setSubmitting(true);
    try {
      const payload = buildQueuePayload({command, subcommand, intent: intent.trim(), taskId: taskId.trim(), after, provider, model, effort, fast});
      const result = await api<QueueResult>(`/api/queue/${command}`, {method: "POST", body: JSON.stringify(payload)});
      await onQueued(result.message);
    } catch (error) {
      const message = errorMessage(error);
      setStatus(message);
      onError(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <form className="job-dialog" onSubmit={(event) => void submit(event)}>
        <header>
          <h2>New queue job</h2>
          <button type="button" aria-label="Close" onClick={onClose}>x</button>
        </header>
        <label>Command
          <select value={command} onChange={(event) => setCommand(event.target.value as JobCommand)}>
            <option value="build">Build</option>
            <option value="improve">Improve</option>
            <option value="certify">Certify</option>
          </select>
        </label>
        {command === "improve" && (
          <label>Improve mode
            <select value={subcommand} onChange={(event) => setSubcommand(event.target.value as ImproveSubcommand)}>
              <option value="bugs">Bugs</option>
              <option value="feature">Feature</option>
              <option value="target">Target</option>
            </select>
          </label>
        )}
        <label>Intent / focus
          <textarea value={intent} rows={5} placeholder="Describe the requested outcome" onChange={(event) => setIntent(event.target.value)} />
        </label>
        <div className="field-grid">
          <label>Task id
            <input value={taskId} type="text" placeholder="optional" onChange={(event) => setTaskId(event.target.value)} />
          </label>
          <label>After
            <input value={after} type="text" placeholder="task-a, task-b" onChange={(event) => setAfter(event.target.value)} />
          </label>
        </div>
        <div className="field-grid">
          <label>Provider
            <select value={provider} onChange={(event) => setProvider(event.target.value)}>
              <option value="">Inherit</option>
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
            </select>
          </label>
          <label>Reasoning effort
            <select value={effort} onChange={(event) => setEffort(event.target.value)}>
              <option value="">Inherit</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="max">Max</option>
            </select>
          </label>
        </div>
        <label>Model
          <input value={model} type="text" placeholder="provider default" onChange={(event) => setModel(event.target.value)} />
        </label>
        <label className="check-label">
          <input checked={fast} type="checkbox" onChange={(event) => setFast(event.target.checked)} />
          Fast mode
        </label>
        <footer>
          <span className="muted">{status}</span>
          <button className="primary" type="submit" disabled={submitting}>{submitting ? "Queueing" : "Queue job"}</button>
        </footer>
      </form>
    </div>
  );
}

function ConfirmDialog({confirm, pending, onCancel, onConfirm}: {
  confirm: ConfirmState;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const confirmClass = confirm.tone === "danger" ? "danger-button" : "primary";
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirmHeading">
        <header>
          <h2 id="confirmHeading">{confirm.title}</h2>
          <button type="button" aria-label="Close" disabled={pending} onClick={onCancel}>x</button>
        </header>
        <p>{confirm.body}</p>
        <footer>
          <button type="button" disabled={pending} onClick={onCancel}>Cancel</button>
          <button className={confirmClass} type="button" disabled={pending} onClick={onConfirm}>
            {pending ? "Working" : confirm.confirmLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}

function visibleRunIds(data: StateResponse): Set<string> {
  return new Set([
    ...data.live.items.map((item) => item.run_id),
    ...data.landing.items.map((item) => item.run_id).filter((value): value is string => Boolean(value)),
    ...data.history.items.map((item) => item.run_id),
  ]);
}

function refreshIntervalMs(data: StateResponse | null): number {
  return Math.max(700, Math.min(5000, Number(data?.live.refresh_interval_s || 1.5) * 1000));
}

function activeCount(watcher?: WatcherInfo): number {
  const counts = watcher?.counts || {};
  return Number(counts.running || 0) + Number(counts.starting || 0) + Number(counts.terminating || 0);
}

function canStartWatcher(data?: StateResponse | null): boolean {
  return Boolean(data?.runtime.supervisor.can_start);
}

function canStopWatcher(data?: StateResponse | null): boolean {
  return Boolean(data?.runtime.supervisor.can_stop);
}

function watcherSummary(watcher?: WatcherInfo): string {
  const health = watcher?.health;
  if (!health) return "stopped";
  if (health.state === "running") return `running pid ${health.blocking_pid || "-"}`;
  if (health.state === "stale") return `stale pid ${health.blocking_pid || "-"}`;
  return "stopped";
}

function workflowHealth(data: StateResponse | null): {
  active: number;
  needsAttention: number;
  ready: number;
  repositoryLabel: string;
  repositoryTone: "neutral" | "warning" | "danger";
  watcherLabel: string;
  watcherTone: "neutral" | "success" | "warning";
  runtimeLabel: string;
  runtimeTone: "neutral" | "warning" | "danger";
} {
  if (!data) {
    return {
      active: 0,
      needsAttention: 0,
      ready: 0,
      repositoryLabel: "unknown",
      repositoryTone: "warning",
      watcherLabel: "unknown",
      watcherTone: "warning",
      runtimeLabel: "loading",
      runtimeTone: "warning",
    };
  }
  const active = activeCount(data?.watcher);
  const attentionKeys = new Set<string>();
  for (const item of data?.history.items || []) {
    if (isAttentionStatus(item.status)) attentionKeys.add(item.queue_task_id || item.run_id);
  }
  for (const item of data?.live.items || []) {
    if (isAttentionStatus(item.display_status)) attentionKeys.add(item.queue_task_id || item.run_id);
  }
  for (const item of data?.landing.items || []) {
    if (isAttentionStatus(item.queue_status)) attentionKeys.add(item.task_id);
  }
  const needsAttention = attentionKeys.size;
  const ready = data?.landing.counts.ready || 0;
  const repositoryLabel = data?.landing.merge_blocked
    ? "blocked"
    : data?.project.dirty
    ? "dirty"
    : "clean";
  const repositoryTone = data?.landing.merge_blocked ? "danger" : data?.project.dirty ? "warning" : "neutral";
  const watcherLabel = data?.watcher.health.state || "stopped";
  const watcherTone = data?.watcher.health.state === "running" ? "success" : ready || active || data?.watcher.health.state === "stale" ? "warning" : "neutral";
  const runtimeIssues = data?.runtime.issues.length || 0;
  const runtimeHasError = Boolean(data?.runtime.issues.some((issue) => issue.severity === "error"));
  const runtimeLabel = runtimeIssues ? `${runtimeIssues} issue${runtimeIssues === 1 ? "" : "s"}` : "healthy";
  const runtimeTone = runtimeHasError ? "danger" : runtimeIssues ? "warning" : "neutral";
  return {active, needsAttention, ready, repositoryLabel, repositoryTone, watcherLabel, watcherTone, runtimeLabel, runtimeTone};
}

function selectOnKeyboard(event: {key: string; preventDefault: () => void}, onSelect: () => void) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  onSelect();
}

function handleActionResult(
  result: ActionResult,
  fallback: string,
  showToast: (message: string, severity?: ToastState["severity"]) => void,
  setResultBanner: (banner: ResultBannerState | null) => void,
) {
  const severity = actionToastSeverity(result);
  const message = result.message || fallback;
  if (result.clear_banner) {
    setResultBanner(null);
  }
  if (result.ok && !result.modal_title && !result.modal_message) {
    setResultBanner(null);
  }
  if (result.modal_title || result.modal_message) {
    setResultBanner({
      title: result.modal_title || (severity === "error" ? "Action failed" : "Action result"),
      body: result.modal_message || message,
      severity,
    });
  }
  showToast(message, severity);
}

function actionToastSeverity(result: ActionResult): ToastState["severity"] {
  const severity = String(result.severity || "").toLowerCase();
  if (severity === "error") return "error";
  if (severity === "warning") return "warning";
  return result.ok ? "information" : "warning";
}

function isAttentionStatus(status: string | null | undefined): boolean {
  return ["failed", "cancelled", "interrupted", "stale"].includes(String(status || "").toLowerCase());
}

function canMerge(landing?: LandingState): boolean {
  return Boolean(landing && landing.counts.ready > 0 && !landing.merge_blocked);
}

function mergeButtonTitle(landing?: LandingState): string {
  return landing?.merge_blocked ? "Commit, stash, or revert local project changes before merging." : "";
}

function mergeBlockedText(landing: LandingState): string {
  const suffix = landing.dirty_files.length ? `: ${landing.dirty_files.slice(0, 3).join(", ")}` : "";
  return `Merge blocked by local changes${suffix}`;
}

function landingBulkConfirmation(landing?: LandingState): string {
  const ready = (landing?.items || []).filter((item) => item.landing_state === "ready");
  const target = landing?.target || "main";
  const taskList = ready.slice(0, 5).map((item) => item.task_id).join(", ");
  const suffix = ready.length > 5 ? `, +${ready.length - 5} more` : "";
  const changed = ready.reduce((sum, item) => sum + Number(item.changed_file_count || 0), 0);
  return `Land ${ready.length} ready task${ready.length === 1 ? "" : "s"} into ${target}: ${taskList}${suffix}. This will land ${changed} changed file${changed === 1 ? "" : "s"} across the ready work.`;
}

function landingSummaryText(landing?: LandingState): string {
  if (!landing || (!landing.counts.ready && !landing.counts.merged && !landing.counts.blocked)) {
    return "Finished tasks appear here for review, evidence checks, and landing.";
  }
  const parts: string[] = [];
  if (landing.counts.ready) parts.push(`${landing.counts.ready} ready to land`);
  if (landing.counts.merged) parts.push(`${landing.counts.merged} already landed`);
  if (landing.counts.blocked) parts.push(`${landing.counts.blocked} not ready`);
  const summary = `${parts.join(" / ")} into ${landing.target}.`;
  return landing.merge_blocked ? `${summary} Merge blocked by local changes.` : summary;
}

function landingPlanHeadline(landing?: LandingState): string {
  if (!landing || !landing.counts.total) return "No work is waiting for review.";
  if (landing.merge_blocked) return "Repository cleanup is required before landing.";
  if (landing.counts.ready) return `${landing.counts.ready} task${landing.counts.ready === 1 ? "" : "s"} can land now.`;
  if (landing.counts.blocked) return "Work exists, but nothing is ready to land.";
  return "All visible work has already landed.";
}

function landingPlanBody(landing: LandingState | undefined, ready: LandingItem[], needsAction: LandingItem[]): string {
  if (!landing || !landing.counts.total) return "Queue a build, improve, or certify job to start the product loop.";
  if (landing.merge_blocked) return "Commit, stash, or revert local project changes, then refresh Mission Control.";
  if (ready.length) {
    const fileCount = ready.reduce((sum, item) => sum + Number(item.changed_file_count || 0), 0);
    return `Review the checklist for each task, then land the ready branches into ${landing.target}. ${fileCount} changed file${fileCount === 1 ? "" : "s"} will be considered.`;
  }
  if (needsAction.length) return "Open each blocked task to inspect its failure, stale state, missing branch, or unfinished run.";
  return "Use history and artifacts to audit what landed, or queue the next product task.";
}

function providerLine(detail: RunDetail): string {
  return [detail.provider, detail.model, detail.reasoning_effort].filter(Boolean).join(" / ") || "-";
}

function actionName(key: string): string {
  return {c: "cancel", r: "resume", R: "retry", x: "cleanup", m: "merge", M: "merge-all"}[key] || key;
}

function actionConfirmationBody(action: string, label?: string): string {
  const normalized = (label || action).toLowerCase();
  if (action === "cancel") return "Cancel this run?";
  if (action === "merge") return "Land this task into the target branch?";
  if (normalized === "remove") return "Remove this queue task?";
  if (normalized === "cleanup") return "Clean up this run?";
  if (normalized === "requeue") return "Requeue this task?";
  if (normalized === "resume") return "Resume this run?";
  return `${capitalize(normalized)} this run?`;
}

function proofLine(item: LandingItem): string {
  const passed = Number(item.stories_passed || 0);
  const tested = Number(item.stories_tested || 0);
  if (tested) return `${passed}/${tested} stories`;
  if (item.queue_status) return item.queue_status;
  return "-";
}

function changeLine(item: LandingItem): string {
  if (item.diff_error) return "diff error";
  const count = Number(item.changed_file_count || 0);
  if (!count) return "-";
  return `${count} file${count === 1 ? "" : "s"}`;
}

function changeListTitle(item: LandingItem): string {
  if (item.diff_error) return item.diff_error;
  if (!item.changed_files.length) return "";
  const suffix = item.changed_file_count > item.changed_files.length ? `\n+${item.changed_file_count - item.changed_files.length} more` : "";
  return `${item.changed_files.join("\n")}${suffix}`;
}

function timelineSubtitle(events?: StateResponse["events"]): string {
  if (!events || !events.total_count) return "Queue, watcher, merge, and recovery actions appear here.";
  const malformed = events.malformed_count ? ` / ${events.malformed_count} malformed` : "";
  const scope = events.truncated ? "scanned recent log" : String(events.total_count);
  return `Recent ${Math.min(events.items.length, events.limit)} of ${scope}${malformed}.`;
}

function eventTargetLine(event: MissionEvent): string {
  const target = [event.task_id ? `task ${event.task_id}` : "", event.run_id ? `run ${event.run_id}` : ""]
    .filter(Boolean)
    .join(" / ");
  return [event.kind, target].filter(Boolean).join(" - ") || "-";
}

function formatEventTime(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function actionLabelForLanding(item: LandingItem): string {
  if (item.landing_state === "merged") return "Audit";
  if (item.landing_state === "ready") return "Land";
  return item.run_id ? "Review" : item.queue_status || "Blocked";
}

function storiesLine(packet: RunDetail["review_packet"]): string {
  const tested = Number(packet.certification.stories_tested || 0);
  const passed = Number(packet.certification.stories_passed || 0);
  return tested ? `${passed}/${tested}` : "-";
}

function reviewActionLabel(label: string): string {
  const normalized = label.toLowerCase();
  if (normalized === "merge selected") return "Land selected";
  return normalized.includes("merge") ? "Land task" : label;
}

function checkStatusLabel(status: string): string {
  return {
    pass: "Pass",
    warn: "Warn",
    fail: "Fail",
    pending: "Wait",
    info: "Info",
  }[status] || capitalize(status || "info");
}

function shortText(value: string, maxLength: number): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3))}...`;
}

function capitalize(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : value;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "Unknown error");
}

function detailWasRemoved(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}
