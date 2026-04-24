import {FormEvent, useCallback, useEffect, useRef, useState} from "react";
import {api, buildQueuePayload, stateQueryParams} from "./api";
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
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  const logOffsetRef = useRef(0);

  const showToast = useCallback((message: string, severity: ToastState["severity"] = "information") => {
    if (severity === "error") setLastError(message);
    setToast({message, severity});
    window.setTimeout(() => setToast(null), 3200);
  }, []);

  const requestConfirm = useCallback((next: ConfirmState) => {
    setConfirm(next);
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

  const refresh = useCallback(async () => {
    setRefreshStatus("refreshing");
    try {
      const next = await api<StateResponse>(`/api/state?${stateQueryParams(filters).toString()}`);
      setData(next);
      setLastError(null);
      setSelectedRunId((current) => {
        const visible = visibleRunIds(next);
        if (current && visible.has(current)) return current;
        return next.live.items[0]?.run_id || next.landing.items.find((item) => item.run_id)?.run_id || next.history.items[0]?.run_id || null;
      });
      setRefreshStatus("idle");
    } catch (error) {
      setRefreshStatus("error");
      showToast(errorMessage(error), "error");
    }
  }, [filters, showToast]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), refreshIntervalMs(data));
    return () => window.clearInterval(interval);
  }, [refresh, data?.live.refresh_interval_s]);

  const loadLogs = useCallback(async (runId: string, reset = false) => {
    if (showingArtifacts) return;
    const offset = reset ? 0 : logOffsetRef.current;
    try {
      const logs = await api<LogsResponse>(`/api/runs/${encodeURIComponent(runId)}/logs?offset=${offset}`);
      if (reset) setLogText("");
      if (logs.text) {
        setLogText((current) => `${reset ? "" : current}${logs.text}`);
      }
      logOffsetRef.current = logs.next_offset || offset;
    } catch (error) {
      showToast(errorMessage(error), "error");
    }
  }, [showingArtifacts, showToast]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      setLogText("");
      setArtifactContent(null);
      return;
    }
    logOffsetRef.current = 0;
    setLogText("");
    setArtifactContent(null);
    setSelectedArtifactIndex(null);
    const params = stateQueryParams(filters).toString();
    api<RunDetail>(`/api/runs/${encodeURIComponent(selectedRunId)}?${params}`)
      .then(setDetail)
      .then(() => loadLogs(selectedRunId, true))
      .catch((error) => showToast(errorMessage(error), "error"));
  }, [filters, loadLogs, selectedRunId, showToast]);

  useEffect(() => {
    if (!selectedRunId || showingArtifacts) return;
    const interval = window.setInterval(() => void loadLogs(selectedRunId), 1200);
    return () => window.clearInterval(interval);
  }, [loadLogs, selectedRunId, showingArtifacts]);

  const runActionForRun = useCallback(async (runId: string, action: string, message: string) => {
    if (action === "merge" && data?.landing.merge_blocked) {
      showToast(mergeBlockedText(data.landing), "error");
      return;
    }
    requestConfirm({
      title: action === "merge" ? "Merge task" : `${capitalize(action)} run`,
      body: message,
      confirmLabel: action === "merge" ? "Merge task" : capitalize(action),
      tone: ["cancel", "cleanup"].includes(action) ? "danger" : "primary",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>(`/api/runs/${encodeURIComponent(runId)}/actions/${action}`, {
            method: "POST",
            body: JSON.stringify({}),
          });
          showToast(result.message || `${action} requested`, result.ok ? "information" : "warning");
          await refresh();
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
      showToast("No merge-ready tasks", "warning");
      return;
    }
    requestConfirm({
      title: "Merge ready tasks",
      body: `Merge ${ready} ready task${ready === 1 ? "" : "s"} into ${landing?.target || "main"}?`,
      confirmLabel: ready === 1 ? "Merge 1 task" : `Merge ${ready} tasks`,
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>("/api/actions/merge-all", {method: "POST", body: "{}"});
          showToast(result.message || "merge all requested", result.ok ? "information" : "warning");
          await refresh();
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
        <button type="button" disabled={Boolean(watcher?.alive)} onClick={() => void runWatcherAction("start")}>Start watcher</button>
        <button type="button" disabled={!watcher?.alive} onClick={() => void runWatcherAction("stop")}>Stop watcher</button>
        <button type="button" disabled={!canMerge(landing)} title={mergeButtonTitle(landing)} onClick={() => void mergeReadyTasks()}>
          {landing?.counts.ready ? `Merge ${landing.counts.ready} ready` : "Merge ready"}
        </button>
      </aside>

      <main className="workspace">
        <Toolbar filters={filters} refreshStatus={refreshStatus} onChange={setFilters} onRefresh={() => void refresh()} />
        <OperationalOverview data={data} lastError={lastError} onDismissError={() => setLastError(null)} />
        <section className="grid">
          <div className="tables">
            <LandingQueue
              landing={landing}
              selectedRunId={selectedRunId}
              onSelect={setSelectedRunId}
              onMergeReady={() => void mergeReadyTasks()}
              onMergeRun={(runId) => void runActionForRun(runId, "merge", "Merge this task?")}
            />
            <LiveRuns items={data?.live.items || []} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
            <History items={data?.history.items || []} totalRows={data?.history.total_rows || 0} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
          </div>
          <RunDetailPanel
            detail={detail}
            landing={landing}
            logText={logText}
            showingArtifacts={showingArtifacts}
            selectedArtifactIndex={selectedArtifactIndex}
            artifactContent={artifactContent}
            onRunAction={(action) => selectedRunId && void runActionForRun(selectedRunId, action, `Run ${action}?`)}
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
  return (
    <dl className="project-meta" aria-label="Project metadata">
      <MetaItem label="Project" value={project?.name || "-"} />
      <MetaItem label="Branch" value={project?.branch || "-"} />
      <MetaItem label="State" value={project?.dirty ? "dirty" : "clean"} />
      <MetaItem label="Watcher" value={watcher?.alive ? `running pid ${watcher.watcher?.pid || "-"}` : "stopped"} />
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

function OperationalOverview({data, lastError, onDismissError}: {data: StateResponse | null; lastError: string | null; onDismissError: () => void}) {
  const health = workflowHealth(data);
  return (
    <section className="overview" aria-label="Mission overview">
      <div className="overview-strip">
        <OverviewMetric label="Active" value={String(health.active)} tone={health.active ? "info" : "neutral"} />
        <OverviewMetric label="Needs attention" value={String(health.needsAttention)} tone={health.needsAttention ? "danger" : "neutral"} />
        <OverviewMetric label="Ready" value={String(health.ready)} tone={health.ready ? "success" : "neutral"} />
        <OverviewMetric label="Repository" value={health.repositoryLabel} tone={health.repositoryTone} />
        <OverviewMetric label="Watcher" value={health.watcherLabel} tone={health.watcherTone} />
      </div>
      {lastError && (
        <div className="status-banner error">
          <strong>Last error</strong>
          <span>{lastError}</span>
          <button type="button" onClick={onDismissError}>Dismiss</button>
        </div>
      )}
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
          <h2 id="landingHeading">Landing Queue</h2>
          <p className="panel-subtitle">{landingSummaryText(landing)}</p>
        </div>
        <button className="primary" type="button" disabled={!canMerge(landing)} title={mergeButtonTitle(landing)} onClick={onMergeReady}>
          {landing?.counts.ready ? `Merge ${landing.counts.ready} ready` : "Merge ready"}
        </button>
      </div>
      <LandingWarnings landing={landing} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Landing</th>
              <th>Task</th>
              <th>Branch</th>
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
              <tr><td colSpan={5} className="empty-cell">No queued work yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
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
  return (
    <tr className={selected ? "selected" : ""} onClick={() => item.run_id && onSelect(item.run_id)}>
      <td><span className={`landing-chip landing-${item.landing_state || "blocked"}`}>{item.label || item.landing_state}</span></td>
      <td title={item.summary || ""}>
        <strong>{item.task_id || "-"}</strong>
        <span className="landing-subtext">{shortText(item.summary || "", 96)}</span>
      </td>
      <td title={item.branch || ""}>{item.branch || "-"}</td>
      <td title={proofLine(item)}>{proofLine(item)}</td>
      <td>
        <button type="button" disabled={!canMergeRow} title={mergeBlocked ? mergeButtonTitle({merge_blocked: true} as LandingState) : ""} onClick={(event) => {
          event.stopPropagation();
          if (item.run_id) onMergeRun(item.run_id);
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
              <tr key={item.run_id} className={item.run_id === selectedRunId ? "selected" : ""} onClick={() => onSelect(item.run_id)}>
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
              <tr key={item.run_id} className={item.run_id === selectedRunId ? "selected" : ""} onClick={() => onSelect(item.run_id)}>
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

function RunDetailPanel({detail, landing, logText, showingArtifacts, selectedArtifactIndex, artifactContent, onRunAction, onShowLogs, onShowArtifacts, onLoadArtifact, onBackToArtifacts}: {
  detail: RunDetail | null;
  landing: LandingState | undefined;
  logText: string;
  showingArtifacts: boolean;
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  onRunAction: (action: string) => void;
  onShowLogs: () => void;
  onShowArtifacts: () => void;
  onLoadArtifact: (index: number) => void;
  onBackToArtifacts: () => void;
}) {
  return (
    <aside className="detail" aria-labelledby="detailHeading">
      <div className="panel-heading">
        <h2 id="detailHeading">Run Detail</h2>
        <span className="pill">{detail?.display_status || "-"}</span>
      </div>
      {detail ? (
        <>
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

function ActionBar({actions, mergeBlocked, onRunAction}: {actions: ActionState[]; mergeBlocked: boolean; onRunAction: (action: string) => void}) {
  const visible = actions.filter((action) => !["o", "e", "M"].includes(action.key));
  return (
    <div className="action-bar">
      {visible.map((action) => {
        const name = actionName(action.key);
        const disabled = !action.enabled || (action.key === "m" && mergeBlocked);
        const title = action.key === "m" && mergeBlocked ? "Commit, stash, or revert local project changes before merging." : action.reason || action.preview || "";
        return (
          <button key={action.key} type="button" disabled={disabled} title={title} onClick={() => onRunAction(name)}>
            {action.label}
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

function workflowHealth(data: StateResponse | null): {
  active: number;
  needsAttention: number;
  ready: number;
  repositoryLabel: string;
  repositoryTone: "neutral" | "warning" | "danger";
  watcherLabel: string;
  watcherTone: "neutral" | "success" | "warning";
} {
  const active = activeCount(data?.watcher);
  const failedHistory = (data?.history.items || []).filter((item) => ["failed", "cancelled", "interrupted"].includes(item.status)).length;
  const staleLive = (data?.live.items || []).filter((item) => item.display_status === "stale").length;
  const needsAttention = failedHistory + staleLive;
  const ready = data?.landing.counts.ready || 0;
  const repositoryLabel = data?.landing.merge_blocked
    ? "blocked"
    : data?.project.dirty
    ? "dirty"
    : "clean";
  const repositoryTone = data?.landing.merge_blocked ? "danger" : data?.project.dirty ? "warning" : "neutral";
  const watcherLabel = data?.watcher.alive ? "running" : "stopped";
  const watcherTone = data?.watcher.alive ? "success" : ready || active ? "warning" : "neutral";
  return {active, needsAttention, ready, repositoryLabel, repositoryTone, watcherLabel, watcherTone};
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

function landingSummaryText(landing?: LandingState): string {
  if (!landing || (!landing.counts.ready && !landing.counts.merged && !landing.counts.blocked)) {
    return "Queue work appears here when tasks start or finish.";
  }
  const parts: string[] = [];
  if (landing.counts.ready) parts.push(`${landing.counts.ready} ready to merge`);
  if (landing.counts.merged) parts.push(`${landing.counts.merged} already merged`);
  if (landing.counts.blocked) parts.push(`${landing.counts.blocked} not ready`);
  const summary = `${parts.join(" / ")} into ${landing.target}.`;
  return landing.merge_blocked ? `${summary} Merge blocked by local changes.` : summary;
}

function providerLine(detail: RunDetail): string {
  return [detail.provider, detail.model, detail.reasoning_effort].filter(Boolean).join(" / ") || "-";
}

function actionName(key: string): string {
  return {c: "cancel", r: "resume", R: "retry", x: "cleanup", m: "merge", M: "merge-all"}[key] || key;
}

function proofLine(item: LandingItem): string {
  const passed = Number(item.stories_passed || 0);
  const tested = Number(item.stories_tested || 0);
  if (tested) return `${passed}/${tested} stories`;
  if (item.queue_status) return item.queue_status;
  return "-";
}

function actionLabelForLanding(item: LandingItem): string {
  if (item.landing_state === "merged") return "Merged";
  if (item.landing_state === "ready") return "Merge";
  return item.queue_status || "Blocked";
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
