import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {api} from "../../api";
import {ConfirmDialog, type ConfirmState} from "../ConfirmDialog";
import {JobDialog, collectPriorRunOptions} from "../new-job/JobDialog";
import {TaskQueueList} from "../tasks/TaskQueueList";
import {ProjectOverview} from "../overview/Overview";
import {RunViewPage} from "../run/RunViewPage";
import {DiagnosticsSummary} from "../health/SystemHealth";
import {BulkLandingConfirmList} from "../review/ConfirmDetails";
import {defaultFilters} from "../../uiTypes";
import type {BoardTask} from "../../uiTypes";
import type {ActionResult, AutopilotDecision, AutopilotIncident, ProjectInfo, StateResponse, VerificationPolicy} from "../../types";
import {canMerge, errorMessage, landingBulkConfirmation, refreshIntervalMs} from "../../utils/missionControl";

interface Props {
  project?: ProjectInfo | null;
}

export function ProjectWorkspace({project}: Props) {
  const [workspaceView, setWorkspaceView] = useState<"tasks" | "diagnostics">(
    () => readWorkspaceView(),
  );
  const [data, setData] = useState<StateResponse | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [jobOpen, setJobOpen] = useState(false);
  const [banner, setBanner] = useState<{kind: "info" | "error"; message: string} | null>(null);
  const [recoveryPending, setRecoveryPending] = useState<string | null>(null);
  const [landPending, setLandPending] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [confirmAck, setConfirmAck] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedQueuedTask, setSelectedQueuedTask] = useState<BoardTask | null>(null);
  const verificationPolicyRef = useRef<VerificationPolicy>("smart");

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

  useEffect(() => {
    const onPop = () => setWorkspaceView(readWorkspaceView());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const changeWorkspaceView = useCallback((next: "tasks" | "diagnostics") => {
    setWorkspaceView(next);
    const url = new URL(window.location.href);
    if (next === "diagnostics") {
      url.searchParams.set("view", "diagnostics");
    } else if (url.searchParams.get("view") === "diagnostics") {
      url.searchParams.delete("view");
    }
    const target = `${url.pathname}${url.search}${url.hash}`;
    const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (target !== current) {
      window.history.pushState({...window.history.state, workspaceView: next}, "", target);
    }
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

  const approveRecovery = useCallback(async (decisionId: string) => {
    setRecoveryPending(decisionId);
    try {
      const body = await api<{message?: string; status?: string}>(
        `/api/autopilot/decisions/${encodeURIComponent(decisionId)}/approve`,
        {
          method: "POST",
          body: JSON.stringify({}),
        },
      );
      setBanner({kind: "info", message: body.message || `Recovery ${body.status || "approved"}.`});
      await refresh();
    } catch (error) {
      setBanner({kind: "error", message: `Could not approve recovery: ${errorMessage(error)}.`});
    } finally {
      setRecoveryPending(null);
    }
  }, [refresh]);

  const closeConfirm = useCallback(() => {
    setConfirm(null);
    setConfirmError(null);
    setConfirmAck(false);
  }, []);

  const runConfirm = useCallback(async () => {
    if (!confirm) return;
    setLandPending(true);
    setConfirmError(null);
    try {
      await confirm.onConfirm();
      closeConfirm();
    } catch (error) {
      setConfirmError(errorMessage(error));
    } finally {
      setLandPending(false);
    }
  }, [closeConfirm, confirm]);

  const landReady = useCallback(async () => {
    const body = await api<ActionResult>("/api/actions/merge-all", {
      method: "POST",
      body: JSON.stringify({verification_policy: verificationPolicyRef.current || "smart"}),
    });
    if (!body.ok) {
      throw new Error(body.message || "Landing did not start.");
    }
    setBanner({
      kind: body.severity === "error" ? "error" : "info",
      message: body.message || "Landing ready work.",
    });
    await refresh();
  }, [refresh]);

  const requestLandReady = useCallback(() => {
    if (!data || !canMerge(data.landing)) return;
    const ready = data.landing.items.filter((item) => item.landing_state === "ready");
    verificationPolicyRef.current = "smart";
    setConfirmError(null);
    setConfirmAck(false);
    setConfirm({
      title: "Land Ready Work",
      body: landingBulkConfirmation(data.landing),
      bodyContent: (
        <BulkLandingConfirmList
          items={ready}
          target={data.landing.target || data.project?.branch || project?.branch || "main"}
          verificationPolicyRef={verificationPolicyRef}
        />
      ),
      confirmLabel: "Land ready work",
      onConfirm: landReady,
    });
  }, [data, landReady, project?.branch]);

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

      {data ? (
        <RecoveryPrompt
          decisions={data.autopilot?.pending_decisions || []}
          incidents={data.autopilot?.incidents || []}
          pendingDecisionId={recoveryPending}
          onApprove={approveRecovery}
        />
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
          <div className="project-workspace-tabs" role="group" aria-label="Project workspace views">
            <button
              type="button"
              className={workspaceView === "tasks" ? "active" : ""}
              aria-pressed={workspaceView === "tasks"}
              data-testid="tasks-tab"
              onClick={() => changeWorkspaceView("tasks")}
            >
              Tasks
            </button>
            <button
              type="button"
              className={workspaceView === "diagnostics" ? "active" : ""}
              aria-pressed={workspaceView === "diagnostics"}
              data-testid="diagnostics-tab"
              onClick={() => changeWorkspaceView("diagnostics")}
            >
              Health
            </button>
          </div>
          {workspaceView === "diagnostics" ? (
            <section className="workspace-diagnostics" data-testid="diagnostics-view" aria-labelledby="workspaceDiagnosticsHeading">
              <div className="panel-heading">
                <div>
                  <h2 id="workspaceDiagnosticsHeading">Health</h2>
                  <p className="panel-subtitle">Project state, watcher health, recovery actions, and recent run history.</p>
                </div>
              </div>
              <ProjectOverview data={data} />
              <DiagnosticsSummary data={data} onSelect={openRun} />
            </section>
          ) : (
            <>
              <TaskQueueList
                data={data}
                filters={defaultFilters}
                selectedRunId={selectedRunId}
                selectedQueuedTaskId={selectedQueuedTask?.id ?? null}
                onSelect={openRun}
                onSelectQueued={openQueuedTask}
                onLandReady={requestLandReady}
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
          )}
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

      {confirm ? (
        <ConfirmDialog
          confirm={confirm}
          pending={landPending}
          error={confirmError}
          checkboxAck={confirmAck}
          onChangeCheckboxAck={setConfirmAck}
          onCancel={closeConfirm}
          onConfirm={runConfirm}
        />
      ) : null}
    </div>
  );
}

function RecoveryPrompt({
  decisions,
  incidents,
  pendingDecisionId,
  onApprove,
}: {
  decisions: AutopilotDecision[];
  incidents: AutopilotIncident[];
  pendingDecisionId: string | null;
  onApprove: (decisionId: string) => void;
}) {
  const decision = decisions.find((item) => item.status === "pending") || decisions[0] || null;
  const incident = incidents[0] || null;
  if (!decision && !incident) return null;
  const planSteps = decision?.plan_steps || [];
  const pending = decision ? pendingDecisionId === decision.id : false;
  return (
    <section className="workspace-recovery" data-testid="workspace-recovery" aria-labelledby="workspaceRecoveryHeading">
      <div>
        <span className="workspace-recovery-kicker">Needs action</span>
        <h2 id="workspaceRecoveryHeading">
          {decision?.title || incident?.title || "Recovery available"}
        </h2>
        <p>{decision?.rationale || decision?.reason || incident?.detail || "Mission Control found recoverable work."}</p>
        {planSteps.length ? (
          <ol>
            {planSteps.map((step, index) => (
              <li key={`${step.action}-${index}`}>
                <strong>{step.label}</strong>
                {step.detail ? <span>{step.detail}</span> : null}
              </li>
            ))}
          </ol>
        ) : null}
      </div>
      {decision?.status === "pending" ? (
        <button
          type="button"
          className="primary"
          data-testid="workspace-recovery-approve"
          onClick={() => onApprove(decision.id)}
          disabled={pending}
        >
          {pending ? "Approving..." : recoveryActionLabel(decision)}
        </button>
      ) : decision ? (
        <span className="workspace-recovery-status">{decision.status}</span>
      ) : null}
    </section>
  );
}

function recoveryActionLabel(decision: AutopilotDecision): string {
  if ((decision.plan_steps || []).length > 1) return "Approve recovery plan";
  if (decision.action === "requeue") return "Requeue task";
  if (decision.action === "start_watcher") return "Start queue runner";
  return decision.action_label || "Approve recovery";
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

function readWorkspaceView(): "tasks" | "diagnostics" {
  if (typeof window === "undefined") return "tasks";
  return new URLSearchParams(window.location.search).get("view") === "diagnostics"
    ? "diagnostics"
    : "tasks";
}
