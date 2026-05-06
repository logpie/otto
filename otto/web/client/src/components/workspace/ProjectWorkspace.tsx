import {useCallback, useEffect, useMemo, useState} from "react";
import {api} from "../../api";
import {JobDialog, collectPriorRunOptions} from "../new-job/JobDialog";
import {TaskQueueList} from "../tasks/TaskQueueList";
import {ProjectOverview} from "../overview/Overview";
import {RunViewPage} from "../run/RunViewPage";
import {defaultFilters} from "../../uiTypes";
import type {BoardTask} from "../../uiTypes";
import type {ProjectInfo, StateResponse} from "../../types";
import {errorMessage, refreshIntervalMs} from "../../utils/missionControl";

interface Props {
  project?: ProjectInfo | null;
}

export function ProjectWorkspace({project}: Props) {
  const [data, setData] = useState<StateResponse | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [jobOpen, setJobOpen] = useState(false);
  const [banner, setBanner] = useState<{kind: "info" | "error"; message: string} | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedQueuedTask, setSelectedQueuedTask] = useState<BoardTask | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const body = await api<StateResponse>("/api/state");
      setData(body);
      setStateError(null);
    } catch (error) {
      setStateError(errorMessage(error));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!data) return;
    const interval = Math.max(750, refreshIntervalMs(data));
    const id = window.setInterval(() => void refresh(), interval);
    return () => window.clearInterval(id);
  }, [data, refresh]);

  const priorRunOptions = useMemo(
    () => collectPriorRunOptions(data?.landing.items || [], data?.history.items || []),
    [data?.landing.items, data?.history.items],
  );

  const openRun = useCallback((runId: string) => {
    setSelectedQueuedTask(null);
    window.history.pushState({...window.history.state, runDrawer: runId}, "", window.location.href);
    setSelectedRunId(runId);
  }, []);

  const closeRun = useCallback(() => {
    if (window.history.state?.runDrawer) {
      window.history.back();
      return;
    }
    setSelectedRunId(null);
  }, []);

  useEffect(() => {
    const onPop = () => setSelectedRunId(null);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const startWatcher = useCallback(async () => {
    try {
      const body = await api<{message?: string}>("/api/watcher/start", {
        method: "POST",
        body: JSON.stringify({}),
      });
      setBanner({kind: "info", message: body.message || "Queue runner started."});
      await refresh();
    } catch (error) {
      setBanner({kind: "error", message: `Could not start queue runner: ${errorMessage(error)}.`});
    }
  }, [refresh]);

  const openQueuedTask = useCallback((task: BoardTask) => {
    setSelectedQueuedTask(task);
    setBanner({
      kind: "info",
      message: `${task.title} is queued but has no Otto session yet. Start the queue runner to create the run detail.`,
    });
  }, []);

  const currentProject = data?.project || project || null;
  const projectName = currentProject?.name || currentProject?.path || "Project";
  const branch = currentProject?.branch || "main";
  const hasWork = Boolean(
    (data?.landing.items.length || 0) > 0
      || (data?.live.items.length || 0) > 0
      || (data?.history.total_rows || 0) > 0,
  );

  return (
    <div
      className="project-workspace"
      data-testid="project-workspace"
      data-mc-shell={data || stateError ? "ready" : "loading"}
      data-drawer-open={selectedRunId ? "true" : "false"}
    >
      <header className="project-workspace-head">
        <div className="project-workspace-title">
          <span className="project-workspace-kicker">Project workspace</span>
          <h1>{projectName}</h1>
          <p>
            Build from intent, watch spec/group/feature progress, review proof,
            and land completed work on <strong>{branch}</strong>.
          </p>
        </div>
        <div className="project-workspace-actions">
          <button
            type="button"
            className="primary project-workspace-build"
            data-testid="new-job-button"
            onClick={() => setJobOpen(true)}
          >
            Build from intent
          </button>
          <button
            type="button"
            className="project-workspace-refresh"
            onClick={() => void refresh()}
            disabled={refreshing}
            data-testid="project-workspace-refresh"
          >
            {refreshing ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </header>

      {banner ? (
        <div
          className={`run-list-queue-banner run-list-queue-banner--${banner.kind}`}
          data-testid="run-list-queue-banner"
          role={banner.kind === "error" ? "alert" : "status"}
        >
          {banner.message}
        </div>
      ) : null}

      {stateError && !data ? (
        <div className="project-workspace-state-error" data-testid="project-workspace-error" role="alert">
          <strong>Mission state failed to load.</strong>
          <span>{stateError}</span>
          <button type="button" onClick={() => void refresh()}>Retry</button>
        </div>
      ) : null}

      {!data && !stateError ? (
        <div className="project-workspace-loading" data-testid="project-workspace-loading">
          Loading project workflow...
        </div>
      ) : null}

      {data ? (
        <>
          <TaskQueueList
            data={data}
            filters={defaultFilters}
            selectedRunId={selectedRunId}
            selectedQueuedTaskId={selectedQueuedTask?.id ?? null}
            onSelect={openRun}
            onSelectQueued={openQueuedTask}
            onNewJob={() => setJobOpen(true)}
            onStartWatcher={() => void startWatcher()}
          />
          <details className="tasks-supplementary" data-testid="tasks-supplementary" open={!hasWork}>
            <summary><span>Project info & activity</span></summary>
            <div className="tasks-supplementary-body">
              <ProjectOverview data={data} />
            </div>
          </details>
        </>
      ) : null}

      {jobOpen ? (
        <JobDialog
          project={currentProject || undefined}
          dirtyFiles={data?.landing.dirty_files || []}
          priorRunOptions={priorRunOptions}
          onClose={() => setJobOpen(false)}
          onQueued={async (message) => {
            setJobOpen(false);
            const queuedMessage = message || "Job queued.";
            setBanner({kind: "info", message: `${queuedMessage} Starting queue runner...`});
            await refresh();
            await startWatcher();
          }}
          onError={(message) => setBanner({kind: "error", message})}
        />
      ) : null}

      {selectedRunId ? (
        <RunDetailOverlay sessionId={selectedRunId} onClose={closeRun} />
      ) : null}
    </div>
  );
}

function RunDetailOverlay({sessionId, onClose}: {sessionId: string; onClose: () => void}) {
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

export default ProjectWorkspace;
