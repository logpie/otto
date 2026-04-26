import {FormEvent, useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {ReactNode} from "react";
import {ApiError, api, buildQueuePayload, stateQueryParams} from "./api";
import type {
  ActionResult,
  ActionState,
  AgentBuildConfig,
  ArtifactContentResponse,
  ArtifactRef,
  CertificationPolicy,
  CommandBacklogItem,
  DiffResponse,
  ExecutionMode,
  HistoryItem,
  ImproveSubcommand,
  JobCommand,
  LandingItem,
  LandingState,
  LiveRunItem,
  LogsResponse,
  ManagedProjectInfo,
  MissionEvent,
  OutcomeFilter,
  PlanningMode,
  ProjectMutationResponse,
  ProjectsResponse,
  QueueResult,
  RunBuildConfig,
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

type ViewMode = "tasks" | "diagnostics";

type BoardStage = "attention" | "working" | "ready" | "landed";
type InspectorMode = "try" | "proof" | "logs" | "artifacts" | "diff";

interface RouteState {
  viewMode: ViewMode;
  selectedRunId: string | null;
}

interface BoardTask {
  id: string;
  runId: string | null;
  title: string;
  summary: string;
  stage: BoardStage;
  status: string;
  branch: string | null;
  changedFileCount: number | null;
  proof: string;
  reason: string;
  active: boolean;
  elapsedDisplay: string | null;
  lastEvent: string | null;
  progress: string | null;
  buildConfig: RunBuildConfig | null;
  source: "landing" | "live" | "history";
}

const defaultFilters: Filters = {
  type: "all",
  outcome: "all",
  query: "",
  activeOnly: false,
};

function readRouteState(): RouteState {
  if (typeof window === "undefined") return {viewMode: "tasks", selectedRunId: null};
  const params = new URLSearchParams(window.location.search);
  return {
    viewMode: params.get("view") === "diagnostics" ? "diagnostics" : "tasks",
    selectedRunId: params.get("run") || null,
  };
}

function writeRouteState(route: RouteState, mode: "push" | "replace"): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set("view", route.viewMode);
  if (route.selectedRunId) {
    url.searchParams.set("run", route.selectedRunId);
  } else {
    url.searchParams.delete("run");
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next === current) return;
  const method = mode === "replace" ? "replaceState" : "pushState";
  window.history[method]({otto: true}, "", next);
}

export function App() {
  const initialRoute = useMemo(() => readRouteState(), []);
  const [filters, setFilters] = useState<Filters>(defaultFilters);
  const [data, setData] = useState<StateResponse | null>(null);
  const [projectsState, setProjectsState] = useState<ProjectsResponse | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRoute.selectedRunId);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logText, setLogText] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorMode, setInspectorMode] = useState<InspectorMode>("proof");
  const [selectedArtifactIndex, setSelectedArtifactIndex] = useState<number | null>(null);
  const [artifactContent, setArtifactContent] = useState<ArtifactContentResponse | null>(null);
  const [proofArtifactIndex, setProofArtifactIndex] = useState<number | null>(null);
  const [proofContent, setProofContent] = useState<ArtifactContentResponse | null>(null);
  const [diffContent, setDiffContent] = useState<DiffResponse | null>(null);
  const [refreshStatus, setRefreshStatus] = useState("idle");
  const [jobOpen, setJobOpen] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [resultBanner, setResultBanner] = useState<ResultBannerState | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>(initialRoute.viewMode);
  const logOffsetRef = useRef(0);
  const selectedRunIdRef = useRef<string | null>(initialRoute.selectedRunId);
  const viewModeRef = useRef<ViewMode>(initialRoute.viewMode);

  useEffect(() => {
    selectedRunIdRef.current = selectedRunId;
  }, [selectedRunId]);

  useEffect(() => {
    viewModeRef.current = viewMode;
  }, [viewMode]);

  useEffect(() => {
    writeRouteState({viewMode: viewModeRef.current, selectedRunId: selectedRunIdRef.current}, "replace");
    const onPopState = () => {
      const next = readRouteState();
      viewModeRef.current = next.viewMode;
      selectedRunIdRef.current = next.selectedRunId;
      setViewMode(next.viewMode);
      setSelectedRunId(next.selectedRunId);
      setInspectorOpen(false);
      setJobOpen(false);
      setConfirm(null);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigateView = useCallback((nextView: ViewMode) => {
    if (nextView === viewModeRef.current) return;
    viewModeRef.current = nextView;
    setViewMode(nextView);
    setInspectorOpen(false);
    writeRouteState({viewMode: nextView, selectedRunId: selectedRunIdRef.current}, "push");
  }, []);

  const showToast = useCallback((message: string, severity: ToastState["severity"] = "information") => {
    if (severity === "error") setLastError(message);
    setToast({message, severity});
    window.setTimeout(() => setToast(null), 3200);
  }, []);

  const requestConfirm = useCallback((next: ConfirmState) => {
    setConfirm(next);
  }, []);

  const openJobDialog = useCallback(() => {
    setInspectorOpen(false);
    setJobOpen(true);
  }, []);

  const selectRun = useCallback((runId: string) => {
    setInspectorOpen(false);
    if (runId !== selectedRunIdRef.current) {
      setDetail(null);
      setLogText("");
      setArtifactContent(null);
      setProofContent(null);
      setDiffContent(null);
      setProofArtifactIndex(null);
      setSelectedArtifactIndex(null);
      setInspectorMode("proof");
    }
    selectedRunIdRef.current = runId;
    setSelectedRunId(runId);
    writeRouteState({viewMode: viewModeRef.current, selectedRunId: runId}, "push");
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
    if ((inspectorMode !== "logs" || !inspectorOpen) && !reset) return;
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
  }, [inspectorMode, inspectorOpen, showToast]);

  const refreshDetail = useCallback(async (runId: string) => {
    const params = stateQueryParams(filters).toString();
    const nextDetail = await api<RunDetail>(`/api/runs/${encodeURIComponent(runId)}?${params}`);
    if (selectedRunIdRef.current !== runId) return;
    setDetail(nextDetail);
  }, [filters]);

  const loadProjects = useCallback(async () => {
    const next = await api<ProjectsResponse>("/api/projects");
    setProjectsState(next);
    return next;
  }, []);

  const refresh = useCallback(async (showStatus = false) => {
    if (showStatus) setRefreshStatus("refreshing");
    try {
      const projectStatus = await loadProjects();
      if (projectStatus.launcher_enabled && !projectStatus.current) {
        setData(null);
        setSelectedRunId(null);
        setDetail(null);
        setLogText("");
        setArtifactContent(null);
        setProofContent(null);
        setDiffContent(null);
        setProofArtifactIndex(null);
        setInspectorOpen(false);
        setLastError(null);
        setRefreshStatus((current) => showStatus || current === "error" ? "idle" : current);
        return;
      }
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
        const nextRunId = next.live.items[0]?.run_id || next.landing.items.find((item) => item.run_id)?.run_id || next.history.items[0]?.run_id || null;
        selectedRunIdRef.current = nextRunId;
        writeRouteState({viewMode: viewModeRef.current, selectedRunId: nextRunId}, "replace");
        return nextRunId;
      });
      setRefreshStatus((current) => showStatus || current === "error" ? "idle" : current);
    } catch (error) {
      setRefreshStatus("error");
      showToast(errorMessage(error), "error");
    }
  }, [filters, loadProjects, refreshDetail, selectedRunId, showToast]);

  useEffect(() => {
    void refresh(false);
    const interval = window.setInterval(() => void refresh(false), refreshIntervalMs(data));
    return () => window.clearInterval(interval);
  }, [refresh, data?.live.refresh_interval_s]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      setLogText("");
      setArtifactContent(null);
      setProofContent(null);
      setDiffContent(null);
      setProofArtifactIndex(null);
      setInspectorOpen(false);
      return;
    }
    setInspectorMode("proof");
    setDetail(null);
    logOffsetRef.current = 0;
    setLogText("");
    setArtifactContent(null);
    setProofContent(null);
    setDiffContent(null);
    setProofArtifactIndex(null);
    setSelectedArtifactIndex(null);
    refreshDetail(selectedRunId).catch((error) => {
      if (detailWasRemoved(error)) {
        selectedRunIdRef.current = null;
        setSelectedRunId(null);
        setDetail(null);
        setLogText("");
        setArtifactContent(null);
        setProofContent(null);
        setDiffContent(null);
        setProofArtifactIndex(null);
        setInspectorOpen(false);
        writeRouteState({viewMode: viewModeRef.current, selectedRunId: null}, "replace");
        return;
      }
      showToast(errorMessage(error), "error");
    });
  }, [refreshDetail, selectedRunId, showToast]);

  useEffect(() => {
    if (!selectedRunId || inspectorMode !== "logs" || !inspectorOpen) return;
    const interval = window.setInterval(() => void loadLogs(selectedRunId), 1200);
    return () => window.clearInterval(interval);
  }, [inspectorMode, inspectorOpen, loadLogs, selectedRunId]);

  const runActionForRun = useCallback(async (runId: string, action: string, message: string, label?: string) => {
    if (action === "merge" && data?.landing.merge_blocked) {
      showToast(mergeBlockedText(data.landing), "error");
      return;
    }
    const actionPayload: Record<string, string> = {};
    if (action === "regenerate-spec") {
      const note = window.prompt("What should change in the spec?");
      if (note === null) return;
      if (!note.trim()) {
        showToast("Add a short spec change note.", "warning");
        return;
      }
      actionPayload.note = note.trim();
    }
    const actionLabel = capitalize(label || action);
    const specAction = action === "approve-spec" || action === "regenerate-spec";
    requestConfirm({
      title: action === "merge" ? "Land task" : specAction ? actionLabel : `${actionLabel} run`,
      body: message,
      confirmLabel: action === "merge" ? "Land task" : actionLabel,
      tone: ["cancel", "cleanup"].includes(action) ? "danger" : "primary",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>(`/api/runs/${encodeURIComponent(runId)}/actions/${action}`, {
            method: "POST",
            body: JSON.stringify(actionPayload),
          });
          handleActionResult(result, `${action} requested`, showToast, setResultBanner);
          if (result.refresh !== false) await refresh(true);
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
          if (result.refresh !== false) await refresh(true);
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [data?.landing, refresh, requestConfirm, showToast]);

  const recoverLanding = useCallback(async () => {
    requestConfirm({
      title: "Recover landing",
      body: "Abort the interrupted git merge, then run Otto's conflict-resolving merge for the remaining ready work. This may invoke the configured merge provider.",
      confirmLabel: "Recover landing",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>("/api/actions/merge-recover", {method: "POST", body: "{}"});
          handleActionResult(result, "landing recovery requested", showToast, setResultBanner);
          if (result.refresh !== false) await refresh(true);
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [refresh, requestConfirm, showToast]);

  const abortMerge = useCallback(async () => {
    requestConfirm({
      title: "Abort merge",
      body: "Clean up the in-progress git merge without landing the remaining ready tasks. Use this when you want the repository back in a safe state before deciding what to do next.",
      confirmLabel: "Abort merge",
      tone: "danger",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>("/api/actions/merge-abort", {method: "POST", body: "{}"});
          handleActionResult(result, "merge abort requested", showToast, setResultBanner);
          if (result.refresh !== false) await refresh(true);
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [refresh, requestConfirm, showToast]);

  const resolveReleaseIssues = useCallback(async () => {
    requestConfirm({
      title: "Resolve release issues",
      body: releaseResolutionConfirmation(data),
      confirmLabel: "Resolve release",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>("/api/actions/resolve-release", {method: "POST", body: "{}"});
          handleActionResult(result, "release issue resolution requested", showToast, setResultBanner);
          if (result.refresh !== false) await refresh(true);
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [data, refresh, requestConfirm, showToast]);

  const runWatcherAction = useCallback(async (action: "start" | "stop") => {
    const execute = async () => {
      try {
        const result = await api<ActionResult | {message?: string}>(`/api/watcher/${action}`, {
          method: "POST",
          body: action === "start" ? JSON.stringify({concurrent: 2}) : "{}",
        });
        showToast(result.message || `watcher ${action} requested`);
        await refresh(true);
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
    const runId = selectedRunIdRef.current;
    if (!runId) return;
    setSelectedArtifactIndex(index);
    setInspectorMode("artifacts");
    setInspectorOpen(true);
    setArtifactContent(null);
    try {
      const content = await api<ArtifactContentResponse>(`/api/runs/${encodeURIComponent(runId)}/artifacts/${index}/content`);
      if (selectedRunIdRef.current !== runId) return;
      setArtifactContent(content);
    } catch (error) {
      if (detailWasRemoved(error) || selectedRunIdRef.current !== runId) return;
      showToast(errorMessage(error), "error");
    }
  }, [showToast]);

  const loadProofArtifact = useCallback(async (index: number) => {
    const runId = selectedRunIdRef.current;
    if (!runId) return;
    setProofArtifactIndex(index);
    setProofContent(null);
    try {
      const content = await api<ArtifactContentResponse>(`/api/runs/${encodeURIComponent(runId)}/artifacts/${index}/content`);
      if (selectedRunIdRef.current !== runId) return;
      setProofContent(content);
    } catch (error) {
      if (detailWasRemoved(error) || selectedRunIdRef.current !== runId) return;
      showToast(errorMessage(error), "error");
    }
  }, [showToast]);

  const loadDiff = useCallback(async () => {
    const runId = selectedRunIdRef.current;
    if (!runId) return;
    setDiffContent(null);
    try {
      const content = await api<DiffResponse>(`/api/runs/${encodeURIComponent(runId)}/diff`);
      if (selectedRunIdRef.current !== runId) return;
      setDiffContent(content);
    } catch (error) {
      if (detailWasRemoved(error) || selectedRunIdRef.current !== runId) return;
      showToast(errorMessage(error), "error");
    }
  }, [showToast]);

  const showLogs = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("logs");
    setArtifactContent(null);
    const runId = selectedRunIdRef.current;
    if (runId) void loadLogs(runId, true);
  }, [loadLogs]);

  const showArtifacts = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("artifacts");
    setSelectedArtifactIndex(null);
    setArtifactContent(null);
  }, []);

  const showDiff = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("diff");
    setArtifactContent(null);
    void loadDiff();
  }, [loadDiff]);

  const showProof = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("proof");
  }, []);

  const showTryProduct = useCallback(() => {
    setInspectorOpen(true);
    setInspectorMode("try");
  }, []);

  useEffect(() => {
    if (!detail || !inspectorOpen || inspectorMode !== "proof") return;
    const artifact = preferredProofArtifact(detail.artifacts);
    if (!artifact) return;
    if (proofArtifactIndex === artifact.index && proofContent) return;
    void loadProofArtifact(artifact.index);
  }, [detail, inspectorMode, inspectorOpen, loadProofArtifact, proofArtifactIndex, proofContent]);

  const project = data?.project;
  const watcher = data?.watcher;
  const landing = data?.landing;
  const active = activeCount(watcher);
  const watcherHint = watcherControlHint(data);
  const modalOpen = jobOpen || Boolean(confirm);

  const createManagedProject = useCallback(async (name: string) => {
    const result = await api<ProjectMutationResponse>("/api/projects/create", {
      method: "POST",
      body: JSON.stringify({name}),
    });
    setProjectsState((current) => ({
      launcher_enabled: current?.launcher_enabled ?? true,
      projects_root: current?.projects_root || "",
      current: result.project || null,
      projects: result.projects,
    }));
    viewModeRef.current = "tasks";
    selectedRunIdRef.current = null;
    setViewMode("tasks");
    setSelectedRunId(null);
    writeRouteState({viewMode: "tasks", selectedRunId: null}, "replace");
    showToast(`Created ${result.project?.name || "project"}`);
    await refresh(true);
  }, [refresh, showToast]);

  const selectManagedProject = useCallback(async (path: string) => {
    const result = await api<ProjectMutationResponse>("/api/projects/select", {
      method: "POST",
      body: JSON.stringify({path}),
    });
    setProjectsState((current) => ({
      launcher_enabled: current?.launcher_enabled ?? true,
      projects_root: current?.projects_root || "",
      current: result.project || null,
      projects: result.projects,
    }));
    viewModeRef.current = "tasks";
    selectedRunIdRef.current = null;
    setViewMode("tasks");
    setSelectedRunId(null);
    writeRouteState({viewMode: "tasks", selectedRunId: null}, "replace");
    showToast(`Opened ${result.project?.name || "project"}`);
    await refresh(true);
  }, [refresh, showToast]);

  const switchProject = useCallback(async () => {
    const result = await api<ProjectMutationResponse>("/api/projects/clear", {
      method: "POST",
      body: "{}",
    });
    setProjectsState((current) => ({
      launcher_enabled: current?.launcher_enabled ?? true,
      projects_root: current?.projects_root || "",
      current: result.current || null,
      projects: result.projects,
    }));
    setData(null);
    setSelectedRunId(null);
    setDetail(null);
    setLogText("");
    setArtifactContent(null);
    setProofContent(null);
    setDiffContent(null);
    setProofArtifactIndex(null);
    setSelectedArtifactIndex(null);
    setInspectorOpen(false);
    setJobOpen(false);
    viewModeRef.current = "tasks";
    selectedRunIdRef.current = null;
    setViewMode("tasks");
    writeRouteState({viewMode: "tasks", selectedRunId: null}, "replace");
    showToast("Choose a project");
  }, [showToast]);

  if (projectsState?.launcher_enabled && !projectsState.current && !data) {
    return (
      <div className="app-shell launcher-shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">O</div>
            <div>
              <h1>Otto</h1>
              <p>Project Launcher</p>
            </div>
          </div>
          <p className="sidebar-hint">Choose a managed project before queueing work.</p>
        </aside>
        <main className="workspace launcher-workspace">
          <ProjectLauncher
            projectsState={projectsState}
            refreshStatus={refreshStatus}
            onCreate={createManagedProject}
            onSelect={selectManagedProject}
            onRefresh={() => void refresh(true)}
          />
        </main>
        {toast && <div id="toast" className={`visible toast-${toast.severity}`} role="status" aria-live="polite">{toast.message}</div>}
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-hidden={modalOpen ? true : undefined}>
        <div className="brand">
          <div className="brand-mark">O</div>
          <div>
            <h1>Otto</h1>
            <p>Mission Control</p>
          </div>
        </div>
        <ProjectMeta project={project} watcher={watcher} landing={landing} active={active} />
        {projectsState?.launcher_enabled && (
          <button type="button" data-testid="switch-project-button" onClick={() => void switchProject()}>Switch project</button>
        )}
        <button className="primary" type="button" data-testid="new-job-button" onClick={openJobDialog}>New job</button>
        <button type="button" data-testid="start-watcher-button" disabled={!canStartWatcher(data)} aria-describedby="watcher-action-hint" title={data?.runtime.supervisor.start_blocked_reason || watcher?.health.next_action || ""} onClick={() => void runWatcherAction("start")}>Start watcher</button>
        <button type="button" data-testid="stop-watcher-button" disabled={!canStopWatcher(data)} aria-describedby="watcher-action-hint" title={watcher?.health.next_action || ""} onClick={() => void runWatcherAction("stop")}>Stop watcher</button>
        <p id="watcher-action-hint" className="sidebar-hint">{watcherHint}</p>
      </aside>

      <main className="workspace" aria-hidden={modalOpen ? true : undefined}>
        <Toolbar
          filters={filters}
          refreshStatus={refreshStatus}
          viewMode={viewMode}
          onChange={setFilters}
          onRefresh={() => void refresh(true)}
          onViewChange={navigateView}
        />
        {viewMode === "tasks" ? (
          <section className="mission-layout" aria-label="Mission Control task workflow">
            <div className="main-stack">
              <MissionFocus
                data={data}
                lastError={lastError}
                resultBanner={resultBanner}
                onNewJob={openJobDialog}
                onStartWatcher={() => void runWatcherAction("start")}
                onLandReady={() => void mergeReadyTasks()}
                onRecoverLanding={() => void recoverLanding()}
                onAbortMerge={() => void abortMerge()}
                onResolveRelease={() => void resolveReleaseIssues()}
                onOpenDiagnostics={() => navigateView("diagnostics")}
                onDismissError={() => setLastError(null)}
                onDismissResult={() => setResultBanner(null)}
              />
              <div className="task-workbench">
                <div className="workbench-primary">
                  <ProjectOverview data={data} />
                  <TaskBoard
                    data={data}
                    filters={filters}
                    selectedRunId={selectedRunId}
                    onSelect={selectRun}
                    onLandReady={() => void mergeReadyTasks()}
                  />
                </div>
                <RunDetailPanel
                  detail={detail}
                  landing={landing}
                  onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
                  onShowTryProduct={showTryProduct}
                  onShowProof={showProof}
                  onShowLogs={showLogs}
                  onShowDiff={showDiff}
                  onShowArtifacts={showArtifacts}
                  onLoadArtifact={(index) => void loadArtifact(index)}
                />
              </div>
            </div>
            {inspectorOpen && detail && (
              <RunInspector
                detail={detail}
                mode={inspectorMode}
                logText={logText}
                selectedArtifactIndex={selectedArtifactIndex}
                artifactContent={artifactContent}
                proofArtifactIndex={proofArtifactIndex}
                proofContent={proofContent}
                diffContent={diffContent}
                onShowLogs={showLogs}
                onShowTryProduct={showTryProduct}
                onShowProof={showProof}
                onShowDiff={showDiff}
                onShowArtifacts={showArtifacts}
                onLoadProofArtifact={(index) => void loadProofArtifact(index)}
                onLoadArtifact={(index) => void loadArtifact(index)}
                onBackToArtifacts={() => {
                  setSelectedArtifactIndex(null);
                  setArtifactContent(null);
                }}
                onClose={() => setInspectorOpen(false)}
              />
            )}
          </section>
        ) : (
          <section className="diagnostics-layout" aria-label="Mission Control diagnostics">
            <OperationalOverview
              data={data}
              lastError={lastError}
              resultBanner={resultBanner}
              onDismissError={() => setLastError(null)}
              onDismissResult={() => setResultBanner(null)}
            />
            <div className="diagnostics-workspace">
              <div className="diagnostics-grid health-grid">
                <SystemHealth data={data} />
                <RecentRunsPanel
                  items={(data?.history.items || []).slice(0, 10)}
                  totalRows={data?.history.total_rows || 0}
                  selectedRunId={selectedRunId}
                  onSelect={(runId) => {
                    selectRun(runId);
                    navigateView("tasks");
                  }}
                />
                <EventTimeline events={data?.events} />
              </div>
            </div>
          </section>
        )}
      </main>

      {jobOpen && (
        <JobDialog
          project={project}
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

function ProjectMeta({project, watcher, landing, active}: {
  project: StateResponse["project"] | undefined;
  watcher: WatcherInfo | undefined;
  landing: LandingState | undefined;
  active: number;
}) {
  const counts = watcher?.counts || {};
  const health = watcher?.health;
  return (
    <dl className="project-meta" aria-label="Project metadata">
      <MetaItem label="Project" value={project?.name || "-"} />
      <MetaItem label="Branch" value={project?.branch || "-"} />
      <MetaItem label="State" value={!project ? "unknown" : project.dirty ? "dirty" : "clean"} />
      <MetaItem label="Watcher" value={watcherSummary(watcher)} />
      <MetaItem label="Heartbeat" value={health?.heartbeat_age_s === null || health?.heartbeat_age_s === undefined ? "-" : `${Math.round(health.heartbeat_age_s)}s ago`} />
      <MetaItem label="In flight" value={String(active)} />
      <MetaItem label="Tasks" value={`queued ${counts.queued || 0} / ready ${landing?.counts.ready || 0} / landed ${landing?.counts.merged || 0}`} />
    </dl>
  );
}

function MetaItem({label, value}: {label: string; value: string}) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function ProjectLauncher({projectsState, refreshStatus, onCreate, onSelect, onRefresh}: {
  projectsState: ProjectsResponse;
  refreshStatus: string;
  onCreate: (name: string) => Promise<void>;
  onSelect: (path: string) => Promise<void>;
  onRefresh: () => void;
}) {
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");
  const [pending, setPending] = useState(false);
  const projects = projectsState.projects || [];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setStatus("Project name is required.");
      return;
    }
    setPending(true);
    setStatus("Creating project");
    try {
      await onCreate(trimmed);
      setName("");
      setStatus("");
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  async function openProject(project: ManagedProjectInfo) {
    if (!project.path || pending) return;
    setPending(true);
    setStatus(`Opening ${project.name}`);
    try {
      await onSelect(project.path);
      setStatus("");
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="project-launcher" aria-labelledby="projectLauncherHeading">
      <div className="launcher-head">
        <div>
          <span>Managed workspace</span>
          <h2 id="projectLauncherHeading">Project Launcher</h2>
          <p>Create or open a managed git project before queueing work.</p>
        </div>
        <div className="launcher-actions">
          {refreshLabel(refreshStatus) && <span className="muted">{refreshLabel(refreshStatus)}</span>}
          <button type="button" onClick={onRefresh}>Refresh</button>
        </div>
      </div>

      <div className="launcher-grid">
        <form className="launcher-panel launcher-form" onSubmit={(event) => void submit(event)}>
          <div>
            <h3>Create project</h3>
            <p>Otto will create a normal folder and initialize a git repo in the managed projects root.</p>
          </div>
          <label>Project name
            <input
              value={name}
              autoFocus
              type="text"
              placeholder="Expense approval portal"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <button className="primary" type="submit" disabled={pending}>{pending ? "Working" : "Create project"}</button>
          <p className="launcher-status" aria-live="polite">{status}</p>
        </form>

        <div className="launcher-panel managed-root">
          <h3>Managed root</h3>
          <code title={projectsState.projects_root}>{projectsState.projects_root}</code>
          <p>Managed projects isolate Otto work from the repo that launched the web server. They are not a filesystem security sandbox.</p>
        </div>
      </div>

      <div className="launcher-panel project-list-panel">
        <div className="panel-heading">
          <div>
            <h3>Open project</h3>
            <p className="panel-subtitle">Existing managed git repos under the projects root.</p>
          </div>
          <span className="pill">{projects.length}</span>
        </div>
        <div className="project-list">
          {projects.length ? projects.map((project) => (
            <button className="project-row" type="button" key={project.path} disabled={pending} onClick={() => void openProject(project)}>
              <span>
                <strong>{project.name}</strong>
                <code title={project.path}>{project.path}</code>
              </span>
              <span className="project-row-meta">
                <span>{project.branch || "-"}</span>
                <span>{project.dirty ? "dirty" : "clean"}</span>
                <span>{project.head_sha ? project.head_sha.slice(0, 7) : "-"}</span>
              </span>
            </button>
          )) : (
            <div className="launcher-empty">No managed projects yet.</div>
          )}
        </div>
      </div>
    </section>
  );
}

function Toolbar({filters, refreshStatus, viewMode, onChange, onRefresh, onViewChange}: {
  filters: Filters;
  refreshStatus: string;
  viewMode: ViewMode;
  onChange: (filters: Filters) => void;
  onRefresh: () => void;
  onViewChange: (viewMode: ViewMode) => void;
}) {
  return (
    <header className="toolbar">
      <div className="view-tabs" aria-label="Mission Control views">
        <button
          className={viewMode === "tasks" ? "active" : ""}
          type="button"
          aria-pressed={viewMode === "tasks"}
          data-testid="tasks-tab"
          onClick={() => onViewChange("tasks")}
        >
          Tasks
        </button>
        <button
          className={viewMode === "diagnostics" ? "active" : ""}
          type="button"
          aria-pressed={viewMode === "diagnostics"}
          data-testid="diagnostics-tab"
          onClick={() => onViewChange("diagnostics")}
        >
          Health
        </button>
      </div>
      <div className="filters" aria-label="Run filters">
        <label>Type
          <select data-testid="filter-type-select" value={filters.type} onChange={(event) => onChange({...filters, type: event.target.value as RunTypeFilter})}>
            <option value="all">All</option>
            <option value="build">Build</option>
            <option value="improve">Improve</option>
            <option value="certify">Certify</option>
            <option value="merge">Merge</option>
            <option value="queue">Queue</option>
          </select>
        </label>
        <label>Outcome
          <select data-testid="filter-outcome-select" value={filters.outcome} onChange={(event) => onChange({...filters, outcome: event.target.value as OutcomeFilter})}>
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
        {refreshLabel(refreshStatus) && <span className="muted">{refreshLabel(refreshStatus)}</span>}
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

function ProjectOverview({data}: {data: StateResponse | null}) {
  const stats = data?.project_stats;
  const health = workflowHealth(data);
  const landed = data?.landing.counts.merged || 0;
  const totalTasks = data?.landing.counts.total || 0;
  const openTasks = Math.max(totalTasks - landed, 0);
  const landingStories = storyTotalsFromLanding(data?.landing.items || []);
  const storiesPassed = stats?.stories_tested ? stats.stories_passed : landingStories.passed;
  const storiesTested = stats?.stories_tested ? stats.stories_tested : landingStories.tested;
  const storyValue = storiesTested ? `${storiesPassed}/${storiesTested}` : "-";
  return (
    <section className="panel project-overview" aria-labelledby="projectOverviewHeading">
      <div className="panel-heading">
        <div>
          <h2 id="projectOverviewHeading">Project Overview</h2>
          <p className="panel-subtitle">Work, review readiness, and provider usage for this project.</p>
        </div>
      </div>
      <div className="project-stat-grid">
        <ProjectStatCard
          label="Current work"
          value={`${openTasks} open`}
          detail={`${health.ready} ready · ${health.needsAttention} attention · ${landed} landed`}
          tone={health.needsAttention ? "warning" : health.ready ? "success" : health.active ? "info" : "neutral"}
        />
        <ProjectStatCard
          label="Run history"
          value={`${stats?.history_count || 0} runs`}
          detail={`${totalTasks} tracked tasks · ${stats?.success_count || 0} success · ${stats?.failed_count || 0} failed`}
          tone={stats?.failed_count ? "warning" : "neutral"}
        />
        <ProjectStatCard
          label="Total tokens"
          value={stats?.token_display || "-"}
          detail={tokenBreakdownLine(stats?.token_usage)}
          tone={stats?.total_tokens ? "info" : "neutral"}
        />
        <ProjectStatCard
          label="Runtime"
          value={stats?.duration_display || "-"}
          detail="Completed plus active run time"
          tone={stats?.total_duration_s ? "info" : "neutral"}
        />
        <ProjectStatCard
          label="Stories"
          value={storyValue}
          detail={storiesTested ? "Certified product stories" : "No story evidence yet"}
          tone={storiesTested ? (storiesPassed === storiesTested ? "success" : "warning") : "neutral"}
        />
      </div>
    </section>
  );
}

function ProjectStatCard({label, value, detail, tone}: {
  label: string;
  value: string;
  detail: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}) {
  return (
    <div className={`project-stat-card tone-${tone}`}>
      <span>{label}</span>
      <strong title={value}>{value}</strong>
      <p title={detail}>{detail}</p>
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

function SystemHealth({data}: {data: StateResponse | null}) {
  const runtime = data?.runtime;
  const watcher = data?.watcher.health;
  const backlog = runtime?.command_backlog;
  const queueFile = runtime?.files.queue;
  const stateFile = runtime?.files.state;
  const dirty = data?.landing.dirty_files || [];
  return (
    <section className="panel system-health" aria-labelledby="systemHealthHeading">
      <div className="panel-heading">
        <div>
          <h2 id="systemHealthHeading">System Health</h2>
          <p className="panel-subtitle">Use this when the queue, watcher, or repository needs recovery.</p>
        </div>
        <span className={`pill health-${runtime?.status || "loading"}`}>{runtime?.status || "loading"}</span>
      </div>
      <div className="system-health-grid">
        <HealthCard
          title="Watcher"
          status={watcher?.state || "unknown"}
          detail={watcher ? `${watcher.blocking_pid ? `pid ${watcher.blocking_pid}` : "no pid"} · ${watcher.heartbeat_age_s === null || watcher.heartbeat_age_s === undefined ? "no heartbeat" : `${formatDuration(watcher.heartbeat_age_s)} heartbeat`}` : "No watcher data yet"}
          next={watcher?.next_action || "Refresh state"}
          tone={watcher?.state === "running" ? "success" : watcher?.state === "stale" ? "warning" : "neutral"}
        />
        <HealthCard
          title="Queue files"
          status={`${backlog?.pending || 0} pending`}
          detail={`${backlog?.processing || 0} processing · ${backlog?.malformed || 0} malformed`}
          next={queueFile?.error || stateFile?.error || "Queue and state files are readable"}
          tone={backlog?.malformed ? "danger" : backlog?.pending || backlog?.processing ? "info" : "neutral"}
        />
        <HealthCard
          title="Repository"
          status={data?.landing.merge_blocked ? "blocked" : data?.project.dirty ? "dirty" : "clean"}
          detail={dirty.length ? dirty.slice(0, 3).join(", ") : `branch ${data?.project.branch || "-"}`}
          next={data?.landing.merge_blocked ? "Clean or commit local changes before landing" : "No landing blocker detected"}
          tone={data?.landing.merge_blocked ? "danger" : data?.project.dirty ? "warning" : "success"}
        />
        <HealthCard
          title="Runtime owner"
          status={runtime?.supervisor.mode || "unknown"}
          detail={runtime?.supervisor.stop_target_pid ? `stop target pid ${runtime.supervisor.stop_target_pid}` : runtime?.supervisor.start_blocked_reason || "No active stop target"}
          next={runtime?.supervisor.can_start ? "Start watcher is safe" : runtime?.supervisor.can_stop ? "Stop watcher is available" : runtime?.supervisor.start_blocked_reason || "No runtime action available"}
          tone={runtime?.issues.some((issue) => issue.severity === "error") ? "danger" : runtime?.issues.length ? "warning" : "neutral"}
        />
      </div>
      <div className="system-issues" role="list" aria-label="Runtime issues">
        {runtime?.issues.length ? runtime.issues.map((issue, index) => (
          <div className={`system-issue severity-${issue.severity}`} role="listitem" key={`${issue.label}-${index}`}>
            <span>{issue.severity}</span>
            <strong>{issue.label}</strong>
            <p>{issue.detail}</p>
            <em>{issue.next_action}</em>
          </div>
        )) : <div className="diagnostic-empty">No runtime issues.</div>}
      </div>
    </section>
  );
}

function HealthCard({title, status, detail, next, tone}: {
  title: string;
  status: string;
  detail: string;
  next: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}) {
  return (
    <article className={`health-card tone-${tone}`}>
      <span>{title}</span>
      <strong>{status}</strong>
      <p>{detail}</p>
      <em>{next}</em>
    </article>
  );
}

function DiagnosticsSummary({data, onSelect}: {data: StateResponse | null; onSelect: (runId: string) => void}) {
  const issues = data?.runtime.issues || [];
  const landingItems = data?.landing.items || [];
  const commands = data?.runtime.command_backlog.items || [];
  const visibleLanding = [
    ...landingItems.filter((item) => item.landing_state === "ready"),
    ...landingItems.filter((item) => item.landing_state === "blocked"),
    ...landingItems.filter((item) => item.landing_state === "merged"),
  ].slice(0, 8);
  const diagnosticCount = issues.length + commands.length + visibleLanding.length;
  return (
    <section className="panel diagnostics-summary" aria-labelledby="diagnosticsSummaryHeading">
      <div className="panel-heading">
        <div>
          <h2 id="diagnosticsSummaryHeading">Diagnostics Summary</h2>
          <p className="panel-subtitle">Runtime issues and landing state, translated into operator actions.</p>
        </div>
        <span className="pill" title="Runtime issues, command backlog items, and review items." aria-label={`${diagnosticCount} diagnostic items`}>{diagnosticCount}</span>
      </div>
      <div className="diagnostics-summary-body">
        <section aria-label="Command backlog">
          <h3>Command Backlog</h3>
          {commands.length ? commands.map((command, index) => (
            <details className={`diagnostic-card command-${command.state}`} key={`${command.command_id || command.run_id || "command"}-${index}`}>
              <summary>
                <span>{command.state}</span>
                <strong>{command.kind || "queued action"}</strong>
                <small>{commandBacklogLine(command)}</small>
              </summary>
              <p>{command.run_id || command.task_id || command.command_id || "target unknown"}</p>
              <em>{commandBacklogLine(command)}</em>
            </details>
          )) : <div className="diagnostic-empty">No pending commands.</div>}
        </section>
        <section aria-label="Runtime issues">
          <h3>Runtime Issues</h3>
          {issues.length ? issues.slice(0, 4).map((issue, index) => (
            <details className={`diagnostic-card severity-${issue.severity}`} key={`${issue.label}-${index}`} open={issue.severity === "error"}>
              <summary>
                <span>{issue.severity}</span>
                <strong>{issue.label}</strong>
                <small>{issue.next_action}</small>
              </summary>
              <p>{issue.detail}</p>
              <em>{issue.next_action}</em>
            </details>
          )) : <div className="diagnostic-empty">No runtime issues.</div>}
        </section>
        <section aria-label="Landing states" className="wide-diagnostics-section">
          <h3>Review And Landing</h3>
          {visibleLanding.length ? visibleLanding.map((item) => (
            <button
              className={`diagnostic-card landing-state-${item.landing_state}`}
              type="button"
              key={item.task_id}
              disabled={!item.run_id}
              onClick={() => item.run_id && onSelect(item.run_id)}
            >
              <span>{landingStateText(item)}</span>
              <strong>{item.task_id}</strong>
              <p>{item.summary || item.branch || "-"}</p>
              <em>{diagnosticLandingAction(item)}</em>
            </button>
          )) : <div className="diagnostic-empty">No queued work.</div>}
        </section>
      </div>
    </section>
  );
}

function MissionFocus({data, lastError, resultBanner, onNewJob, onStartWatcher, onLandReady, onRecoverLanding, onAbortMerge, onResolveRelease, onOpenDiagnostics, onDismissError, onDismissResult}: {
  data: StateResponse | null;
  lastError: string | null;
  resultBanner: ResultBannerState | null;
  onNewJob: () => void;
  onStartWatcher: () => void;
  onLandReady: () => void;
  onRecoverLanding: () => void;
  onAbortMerge: () => void;
  onResolveRelease: () => void;
  onOpenDiagnostics: () => void;
  onDismissError: () => void;
  onDismissResult: () => void;
}) {
  const focus = missionFocus(data);
  const activeSummary = activeRunSummary(data);
  return (
    <section className={`mission-focus focus-${focus.tone}`} data-testid="mission-focus" aria-label="Mission focus">
      <div className="focus-copy">
        <span>{focus.kicker}</span>
        <h2>{focus.title}</h2>
        <p>{focus.body}</p>
        {activeSummary ? (
          <div className="focus-live-summary" aria-label="Active worker summary">
            <span className="task-live-dot" aria-hidden="true" />
            <strong>{activeSummary.label}</strong>
            <span>{activeSummary.detail}</span>
          </div>
        ) : null}
      </div>
      <div className="focus-actions">
        {focus.primary === "land" && (
          <>
            <button className="primary" type="button" disabled={!canResolveRelease(data)} onClick={onResolveRelease}>Resolve release issues</button>
            <button type="button" disabled={!canMerge(data?.landing)} onClick={onLandReady}>Land all ready</button>
          </>
        )}
        {focus.primary === "start" && (
          <button className="primary" type="button" disabled={!canStartWatcher(data)} onClick={onStartWatcher}>Start watcher</button>
        )}
        {focus.primary === "diagnostics" && (
          <button className="primary" type="button" onClick={onOpenDiagnostics}>Open health</button>
        )}
        {focus.primary === "recover" && (
          <>
            <button className="primary" type="button" onClick={onResolveRelease}>Resolve release issues</button>
            <button type="button" onClick={onRecoverLanding}>Recover landing</button>
            <button type="button" onClick={onAbortMerge}>Abort merge</button>
          </>
        )}
        {focus.primary === "resolve" && (
          <button className="primary" type="button" disabled={!canResolveRelease(data)} onClick={onResolveRelease}>Resolve release issues</button>
        )}
        {focus.primary === "new" && (
          <button className="primary" type="button" onClick={onNewJob}>New job</button>
        )}
        {focus.primary !== "new" && <button type="button" onClick={onNewJob}>New job</button>}
      </div>
      <div className="focus-metrics">
        <FocusMetric label="Queued/running" value={String(focus.working)} />
        <FocusMetric label="Needs action" value={String(focus.needsAction)} />
        <FocusMetric label="Ready" value={String(focus.ready)} />
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

function FocusMetric({label, value}: {label: string; value: string}) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TaskBoard({data, filters, selectedRunId, onSelect, onLandReady}: {
  data: StateResponse | null;
  filters: Filters;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  onLandReady: () => void;
}) {
  const columns = taskBoardColumns(data, filters);
  const readyCount = data?.landing.counts.ready || 0;
  return (
    <section className="panel task-board-panel" data-testid="task-board" aria-labelledby="taskBoardHeading">
      <div className="panel-heading">
        <div>
          <h2 id="taskBoardHeading">Task Board</h2>
          <p className="panel-subtitle">{taskBoardSubtitle(data, filters)}</p>
        </div>
        <div className="panel-actions">
          <button
            className="primary"
            type="button"
            disabled={!canMerge(data?.landing)}
            title={mergeButtonTitle(data?.landing)}
            onClick={onLandReady}
          >
            {readyCount === 0 ? "No tasks ready" : readyCount === 1 ? "Land 1 ready task" : `Land ${readyCount} ready tasks`}
          </button>
        </div>
      </div>
      <div className="task-board">
        {columns.map((column) => (
          <section className="task-column" key={column.stage} aria-label={column.title}>
            <header>
              <span>{column.title}</span>
              <strong>{column.items.length}</strong>
            </header>
            <div className="task-list">
              {column.items.length ? column.items.map((task) => (
                <TaskCard
                  key={`${task.source}-${task.id}`}
                  task={task}
                  selected={Boolean(task.runId && task.runId === selectedRunId)}
                  onSelect={onSelect}
                />
              )) : (
                <div className="task-empty">{column.empty}</div>
              )}
            </div>
          </section>
        ))}
      </div>
    </section>
  );
}

function TaskCard({task, selected, onSelect}: {
  task: BoardTask;
  selected: boolean;
  onSelect: (runId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const selectTask = () => task.runId && onSelect(task.runId);
  const meta = [taskChangeLine(task), taskProofMeta(task), taskConfigChip(task)].filter(Boolean);
  const liveEvent = liveEventLabel(task);
  const progress = progressLabel(task);
  return (
    <article className={`task-card task-${task.stage} ${selected ? "selected" : ""}`}>
      <button
        className="task-card-main"
        type="button"
        disabled={!task.runId}
        data-testid={testIdForTask(task.id)}
        aria-pressed={selected}
        aria-label={`${task.title}: ${task.status}`}
        onClick={selectTask}
      >
        <span className="task-card-top">
          <span className="task-status">{task.status}</span>
          <span className="task-card-cta">{task.stage === "ready" ? "Review" : "Details"}</span>
        </span>
        <strong className="task-title" title={task.title}>{task.title}</strong>
        {(task.active || task.elapsedDisplay) ? (
          <span className={`task-card-live ${task.active ? "is-active" : ""}`}>
            {task.active ? <span className="task-live-dot" aria-hidden="true" /> : null}
            <span>{task.active ? "Running" : "Elapsed"}</span>
            {task.elapsedDisplay ? <strong>{task.elapsedDisplay}</strong> : null}
            {liveEvent ? <em title={task.lastEvent || ""}>{liveEvent}</em> : null}
          </span>
        ) : null}
        <span className="task-card-meta">
          {meta.map((item) => <span key={item}>{item}</span>)}
        </span>
        {progress ? <span className="task-card-progress" title={task.progress || ""}>{progress}</span> : null}
      </button>
      <button
        className="task-card-toggle"
        type="button"
        aria-expanded={expanded}
        aria-controls={`${testIdForTask(task.id)}-drawer`}
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "Less" : "More"}
      </button>
      {expanded ? (
        <div className="task-card-drawer" id={`${testIdForTask(task.id)}-drawer`}>
          <p title={task.summary}>{shortText(task.summary, 220)}</p>
          <dl>
            <dt>Branch</dt><dd title={task.branch || ""}>{task.branch || "no branch"}</dd>
            <dt>Config</dt><dd>{taskConfigSummary(task.buildConfig)}</dd>
            <dt>Status note</dt><dd>{task.reason}</dd>
          </dl>
        </div>
      ) : null}
    </article>
  );
}

function RecentRunsPanel({items, totalRows, selectedRunId, onSelect}: {
  items: HistoryItem[];
  totalRows: number;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  return (
    <section className="panel recent-runs-panel" aria-labelledby="recentRunsHeading">
      <div className="panel-heading">
        <div>
          <h2 id="recentRunsHeading">Recent Runs</h2>
          <p className="panel-subtitle">Outcome, duration, and provider usage.</p>
        </div>
        <span className="pill">{totalRows}</span>
      </div>
      <div className="recent-run-list" role="list">
        {items.length ? items.map((item) => (
          <button
            className={`recent-run-row ${item.run_id === selectedRunId ? "selected" : ""}`}
            type="button"
            key={item.run_id}
            role="listitem"
            onClick={() => onSelect(item.run_id)}
          >
            <span className={`recent-run-outcome status-${(item.terminal_outcome || item.status || "").toLowerCase()}`}>
              {item.outcome_display || item.status}
            </span>
            <span className="recent-run-main">
              <strong title={item.queue_task_id || item.run_id}>{item.queue_task_id || item.run_id}</strong>
              <em title={item.summary || ""}>{item.summary || "-"}</em>
            </span>
            <span className="recent-run-usage">
              <strong>{item.duration_display || "-"}</strong>
              <em>{usageLine(item)}</em>
            </span>
          </button>
        )) : <div className="timeline-empty">No matching runs yet.</div>}
      </div>
    </section>
  );
}

function LiveRuns({items, landing, selectedRunId, onSelect}: {
  items: LiveRunItem[];
  landing: LandingState | undefined;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  const landingByTask = new Map((landing?.items || []).map((item) => [item.task_id, item]));
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
                aria-label={`Open live run ${item.display_id || item.run_id}`}
                onClick={() => onSelect(item.run_id)}
                onKeyDown={(event) => selectOnKeyboard(event, () => onSelect(item.run_id))}
              >
                <td className={`status-${item.display_status}`} title={item.overlay?.reason || item.display_status}>{item.display_status.toUpperCase()}</td>
                <td title={item.run_id}>{item.display_id || item.run_id}</td>
                <td title={item.branch_task || ""}>{item.branch_task || "-"}</td>
                <td>{item.elapsed_display || "-"}</td>
                <td>{item.cost_display || "-"}</td>
                <td title={runEventText(item, landingByTask)}>{runEventText(item, landingByTask)}</td>
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
        <h2 id="historyHeading">Run History</h2>
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
                aria-label={`Open history run ${item.queue_task_id || item.run_id}`}
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

function EventTimeline({events, compact = false}: {events: StateResponse["events"] | undefined; compact?: boolean}) {
  const items = events?.items || [];
  const malformed = events?.malformed_count || 0;
  const visibleItems = compact ? items.slice(0, 6) : items;
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
        {visibleItems.length ? visibleItems.map((event) => (
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

function RunDetailPanel({detail, landing, onRunAction, onShowTryProduct, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadArtifact}: {
  detail: RunDetail | null;
  landing: LandingState | undefined;
  onRunAction: (action: string, label?: string) => void;
  onShowTryProduct: () => void;
  onShowProof: () => void;
  onShowLogs: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
  onLoadArtifact: (index: number) => void;
}) {
  return (
    <aside className="detail" aria-labelledby="detailHeading" data-testid="run-detail-panel">
      <div className="panel-heading">
        <h2 id="detailHeading">{detail ? "Review Packet" : "Run Detail"}</h2>
        <span className="pill">{detail ? detailStatusLabel(detail) : "-"}</span>
      </div>
      {detail ? (
        <>
          <div className="detail-scroll">
            <ReviewPacket packet={detail.review_packet} onRunAction={onRunAction} onLoadArtifact={onLoadArtifact} onShowArtifacts={onShowArtifacts} />
            <PhaseTimeline phases={detail.phase_timeline || []} />
            <details className="detail-body detail-metadata">
              <summary>
                <span>Run metadata</span>
                <strong title={detail.run_id}>{detail.run_id}</strong>
              </summary>
              <div className="detail-metadata-content">
                <h3>{detail.title || detail.run_id}</h3>
                <dl>
                  <dt>Run</dt><dd>{detail.run_id}</dd>
                  <dt>Type</dt><dd>{detail.domain} / {detail.run_type}</dd>
                  <dt>Branch</dt><dd>{detail.branch || "-"}</dd>
                  <dt>Worktree</dt><dd>{detail.worktree || detail.cwd || "-"}</dd>
                  <dt>Provider</dt><dd>{providerLine(detail)}</dd>
                  <dt>Certification</dt><dd>{certificationLine(detail.build_config)}</dd>
                  <dt>Timeouts</dt><dd>{timeoutLine(detail.build_config)}</dd>
                  <dt>Limits</dt><dd>{limitLine(detail.build_config)}</dd>
                  <dt>Run flags</dt><dd>{flagsLine(detail.build_config)}</dd>
                  <dt>Agents</dt><dd>{agentsLine(detail.build_config)}</dd>
                  <dt>Project</dt><dd>{projectConfigLine(detail.build_config)}</dd>
                  <dt>Artifacts</dt><dd>{detail.artifacts.length}</dd>
                  {detail.overlay && <><dt>Overlay</dt><dd>{detail.overlay.reason}</dd></>}
                  {detail.summary_lines.map((line, index) => <DetailLine key={`${line}-${index}`} line={line} />)}
                </dl>
              </div>
            </details>
            <ActionBar actions={detail.legal_actions || []} mergeBlocked={Boolean(landing?.merge_blocked)} onRunAction={onRunAction} />
          </div>
          <div className="detail-inspector-actions" aria-label="Evidence shortcuts">
            <button className="primary" type="button" data-testid="open-try-product-button" onClick={onShowTryProduct}>Try product</button>
            <button type="button" data-testid="open-proof-button" onClick={onShowProof}>Proof</button>
            <button type="button" data-testid="open-diff-button" disabled={!canShowDiff(detail)} onClick={onShowDiff}>Diff</button>
            <button type="button" data-testid="open-logs-button" onClick={onShowLogs}>Logs</button>
            <button type="button" data-testid="open-artifacts-button" onClick={onShowArtifacts}>Artifacts</button>
          </div>
        </>
      ) : (
        <div className="detail-body empty">Select a run.</div>
      )}
    </aside>
  );
}

function PhaseTimeline({phases}: {phases: RunDetail["phase_timeline"]}) {
  if (!phases.length) return null;
  return (
    <section className="detail-body phase-timeline" aria-label="Execution phases">
      <div className="phase-timeline-heading">
        <h3>Execution</h3>
        <span>{phases.length} phase{phases.length === 1 ? "" : "s"}</span>
      </div>
      <div className="phase-timeline-list">
        {phases.map((phase) => (
          <article key={phase.phase} className={`phase-item phase-${phase.status}`}>
            <span className="phase-status">{phase.status}</span>
            <strong>{phase.label}</strong>
            <p>{phaseProviderLine(phase)}</p>
            <em>{phaseUsageLine(phase)}</em>
          </article>
        ))}
      </div>
    </section>
  );
}

function phaseProviderLine(phase: RunDetail["phase_timeline"][number]): string {
  return [
    phase.provider || "provider default",
    phase.model || "model default",
    phase.reasoning_effort || "reasoning default",
  ].join(" / ");
}

function phaseUsageLine(phase: RunDetail["phase_timeline"][number]): string {
  const parts = [
    typeof phase.duration_s === "number" ? formatDuration(phase.duration_s) : "",
    phase.rounds ? `${phase.rounds} round${phase.rounds === 1 ? "" : "s"}` : "",
    tokenTotal(phase.token_usage) ? `${formatCompactNumber(tokenTotal(phase.token_usage))} tokens` : "",
    phase.cost_usd && phase.cost_usd > 0 ? `$${phase.cost_usd.toFixed(2)}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "No usage recorded";
}

function RunInspector({detail, mode, logText, selectedArtifactIndex, artifactContent, proofArtifactIndex, proofContent, diffContent, onShowTryProduct, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadProofArtifact, onLoadArtifact, onBackToArtifacts, onClose}: {
  detail: RunDetail;
  mode: InspectorMode;
  logText: string;
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  proofArtifactIndex: number | null;
  proofContent: ArtifactContentResponse | null;
  diffContent: DiffResponse | null;
  onShowTryProduct: () => void;
  onShowProof: () => void;
  onShowLogs: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
  onLoadProofArtifact: (index: number) => void;
  onLoadArtifact: (index: number) => void;
  onBackToArtifacts: () => void;
  onClose: () => void;
}) {
  const inspectorRef = useDialogFocus<HTMLElement>(onClose, false);
  return (
    <section
      ref={inspectorRef}
      className="run-inspector"
      role="dialog"
      aria-modal="true"
      aria-labelledby="runInspectorHeading"
      data-testid="run-inspector"
      tabIndex={-1}
    >
      <div className="run-inspector-heading">
        <div>
          <h2 id="runInspectorHeading">{detail.title || detail.run_id}</h2>
          <p>{detailStatusLabel(detail)} evidence packet</p>
        </div>
        <div className="detail-tabs" role="tablist" aria-label="Evidence view">
          <button className={`tab ${mode === "try" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "try"} onClick={onShowTryProduct}>Try Product</button>
          <button className={`tab ${mode === "proof" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "proof"} onClick={onShowProof}>Proof</button>
          <button className={`tab ${mode === "diff" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "diff"} disabled={!canShowDiff(detail)} onClick={onShowDiff}>Diff</button>
          <button className={`tab ${mode === "logs" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "logs"} onClick={onShowLogs}>Logs</button>
          <button className={`tab ${mode === "artifacts" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "artifacts"} onClick={onShowArtifacts}>Artifacts</button>
        </div>
        <button type="button" data-testid="close-inspector-button" onClick={onClose}>Close inspector</button>
      </div>
      <div className="run-inspector-body">
        {mode === "try" ? (
          <ProductHandoffPane detail={detail} />
        ) : mode === "proof" ? (
          <ProofPane detail={detail} proofArtifactIndex={proofArtifactIndex} proofContent={proofContent} onShowDiff={onShowDiff} onLoadProofArtifact={onLoadProofArtifact} />
        ) : mode === "diff" ? (
          <DiffPane diff={diffContent} />
        ) : mode === "logs" ? (
          <LogPane text={logText} />
        ) : (
          <ArtifactPane
            artifacts={detail.artifacts || []}
            selectedArtifactIndex={selectedArtifactIndex}
            artifactContent={artifactContent}
            onLoadArtifact={onLoadArtifact}
            onBack={onBackToArtifacts}
          />
        )}
      </div>
    </section>
  );
}

function ProductHandoffPane({detail}: {detail: RunDetail}) {
  const handoff = detail.review_packet.product_handoff;
  const hasLaunch = handoff.launch.length > 0;
  const hasReset = handoff.reset.length > 0;
  const hasSamples = handoff.sample_data.length > 0;
  const hasUrls = handoff.urls.length > 0;
  const hasTaskContext = Boolean(handoff.task_summary || handoff.task_flows.length || handoff.task_changed_files.length);
  return (
    <div className="product-handoff-pane" data-testid="product-handoff-pane">
      <section className="product-handoff-hero" aria-labelledby="productHandoffHeading">
        <div>
          <span>{handoff.label}</span>
          <h3 id="productHandoffHeading">Try product</h3>
          <p>{handoff.summary || productKindHint(handoff.kind)}</p>
        </div>
        <dl>
          <dt>Root</dt>
          <dd title={handoff.root}>{shortPath(handoff.root)}</dd>
          <dt>Source</dt>
          <dd>{handoff.source_path ? `${handoff.source} · ${shortPath(handoff.source_path)}` : handoff.source}</dd>
        </dl>
      </section>

      {hasTaskContext && (
        <section className="product-handoff-section handoff-task-section" aria-labelledby="productTaskHeading">
          <div className="handoff-section-heading">
            <h3 id="productTaskHeading">This task</h3>
            <span>{[handoff.task_status, handoff.task_branch].filter(Boolean).join(" · ") || "task-specific"}</span>
          </div>
          {handoff.task_summary ? <p className="handoff-task-summary">{handoff.task_summary}</p> : null}
          {handoff.task_flows.length ? (
            <div className="handoff-flow-list">
              {handoff.task_flows.map((flow, index) => (
                <article className="handoff-flow" key={`${flow.title}-${index}`}>
                  <strong>{flow.title}</strong>
                  {flow.steps.length ? (
                    <ol>
                      {flow.steps.map((step) => <li key={step}>{step}</li>)}
                    </ol>
                  ) : null}
                </article>
              ))}
            </div>
          ) : null}
          {handoff.task_changed_files.length ? (
            <details className="handoff-files">
              <summary>Changed files <strong>{handoff.task_changed_files.length}</strong></summary>
              <ul>
                {handoff.task_changed_files.map((path) => <li key={path}>{path}</li>)}
              </ul>
            </details>
          ) : null}
        </section>
      )}

      <section className="product-handoff-section" aria-labelledby="productLaunchHeading">
        <div className="handoff-section-heading">
          <h3 id="productLaunchHeading">Launch</h3>
          <span>{hasLaunch ? `${handoff.launch.length} command${handoff.launch.length === 1 ? "" : "s"}` : "not declared"}</span>
        </div>
        {hasLaunch ? (
          <CommandList commands={handoff.launch} />
        ) : (
          <p>{productKindHint(handoff.kind)}</p>
        )}
        {hasUrls && (
          <div className="handoff-links" aria-label="Product URLs">
            {handoff.urls.map((url) => (
              <a href={url} target="_blank" rel="noreferrer" key={url}>{url}</a>
            ))}
          </div>
        )}
      </section>

      <section className="product-handoff-section" aria-labelledby="productFlowsHeading">
        <div className="handoff-section-heading">
          <h3 id="productFlowsHeading">General journeys</h3>
          <span>{handoff.try_flows.length} flow{handoff.try_flows.length === 1 ? "" : "s"}</span>
        </div>
        <div className="handoff-flow-list">
          {handoff.try_flows.map((flow, index) => (
            <article className="handoff-flow" key={`${flow.title}-${index}`}>
              <strong>{flow.title}</strong>
              {flow.steps.length ? (
                <ol>
                  {flow.steps.map((step) => <li key={step}>{step}</li>)}
                </ol>
              ) : null}
            </article>
          ))}
        </div>
      </section>

      {hasSamples && (
        <section className="product-handoff-section" aria-labelledby="productSampleHeading">
          <div className="handoff-section-heading">
            <h3 id="productSampleHeading">Sample data</h3>
            <span>{handoff.sample_data.length} item{handoff.sample_data.length === 1 ? "" : "s"}</span>
          </div>
          <div className="handoff-samples">
            {handoff.sample_data.map((sample, index) => (
              <div key={`${sample.label}-${sample.value}-${index}`}>
                <span>{sample.label}</span>
                <strong>{sample.value}</strong>
                {sample.detail ? <p>{sample.detail}</p> : null}
              </div>
            ))}
          </div>
        </section>
      )}

      {(hasReset || handoff.notes.length > 0) && (
        <section className="product-handoff-section" aria-labelledby="productOpsHeading">
          <div className="handoff-section-heading">
            <h3 id="productOpsHeading">Reset and notes</h3>
            <span>{hasReset ? `${handoff.reset.length} reset command${handoff.reset.length === 1 ? "" : "s"}` : "notes"}</span>
          </div>
          {hasReset ? <CommandList commands={handoff.reset} /> : null}
          {handoff.notes.length ? (
            <ul className="handoff-notes">
              {handoff.notes.map((note) => <li key={note}>{note}</li>)}
            </ul>
          ) : null}
        </section>
      )}
    </div>
  );
}

function CommandList({commands}: {commands: Array<{label: string; command: string}>}) {
  return (
    <div className="handoff-command-list">
      {commands.map((command, index) => (
        <div className="handoff-command" key={`${command.command}-${index}`}>
          <span>{command.label}</span>
          <code>{command.command}</code>
        </div>
      ))}
    </div>
  );
}

function LogPane({text}: {text: string}) {
  const compact = compactLongText(text || "No logs yet.", 14000);
  const lineCount = text ? text.split(/\n/).length : 0;
  return (
    <div className="log-viewer">
      <div className="log-toolbar">
        <strong>Run logs</strong>
        <span>{lineCount ? `${lineCount} line${lineCount === 1 ? "" : "s"}` : "waiting for output"}{compact.truncated ? " · showing latest output" : ""}</span>
      </div>
      <pre className="log-pane log-content" tabIndex={0} aria-label="Run log output" data-testid="run-log-pane">{renderLogText(compact.text)}</pre>
    </div>
  );
}

function ProofPane({detail, proofArtifactIndex, proofContent, onShowDiff, onLoadProofArtifact}: {
  detail: RunDetail;
  proofArtifactIndex: number | null;
  proofContent: ArtifactContentResponse | null;
  onShowDiff: () => void;
  onLoadProofArtifact: (index: number) => void;
}) {
  const packet = detail.review_packet;
  const changedFiles = packet.changes.files.slice(0, 10);
  const evidence = packet.evidence.filter(isReadableArtifact);
  const stories = packet.certification.stories || [];
  const proofReport = packet.certification.proof_report;
  const proofContentIsLog = isLogArtifact(proofContent?.artifact || null);
  const proofContentText = proofContent?.content || "";
  const compact = compactLongText(proofContentIsLog ? proofContentText : formatArtifactContent(proofContentText), 20000);
  const proofChecks = packet.failure ? packet.checks.filter((check) => check.key !== "run" && check.key !== "landing") : packet.checks;
  return (
    <div className="proof-pane" data-testid="proof-pane">
      <section className="proof-summary" aria-labelledby="proofHeading">
        <div>
          <span>{packet.readiness.label}</span>
          <h3 id="proofHeading">Proof of work</h3>
          <p>{packet.headline}</p>
        </div>
        <div className="proof-metrics">
          <ReviewMetric label="Stories" value={storiesLine(packet)} />
          <ReviewMetric label="Changes" value={packet.changes.file_count ? `${packet.changes.file_count} file${packet.changes.file_count === 1 ? "" : "s"}` : "-"} />
          <ReviewMetric label="Evidence" value={evidenceLine(packet)} />
        </div>
      </section>
      <section className="proof-section" aria-labelledby="proofNextHeading">
        <h3 id="proofNextHeading">Next action</h3>
        <p>{packet.readiness.next_step}</p>
        <div className="proof-report-actions">
          {proofReport?.html_url ? (
            <a href={proofReport.html_url} target="_blank" rel="noreferrer" data-testid="proof-report-link">Open HTML proof report</a>
          ) : (
            <span>No HTML proof report is linked for this run.</span>
          )}
        </div>
      </section>
      {packet.failure && (
        <section className="proof-section proof-failure" aria-labelledby="proofFailureHeading">
          <h3 id="proofFailureHeading">What failed</h3>
          <FailureSummary failure={packet.failure} showExcerpt />
        </section>
      )}
      <section className="proof-section" aria-labelledby="proofChecksHeading">
        <h3 id="proofChecksHeading">Certification checks</h3>
        {proofChecks.length ? (
          <div className="proof-checks">
            {proofChecks.map((check) => (
              <div className={`review-check check-${check.status}`} key={check.key}>
                <span>{checkStatusLabel(check.status)}</span>
                <div>
                  <strong>{check.label}</strong>
                  <p>{formatReviewText(check.detail)}</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p>No additional checks were recorded before the task failed.</p>
        )}
      </section>
      <section className="proof-section" aria-labelledby="proofStoriesHeading">
        <h3 id="proofStoriesHeading">Stories tested</h3>
        {stories.length ? (
          <div className="proof-stories" data-testid="proof-story-list">
            {stories.map((story) => (
              <article className={`proof-story story-${storyStatusClass(story.status)}`} key={story.id || story.title}>
                <span>{storyStatusLabel(story.status)}</span>
                <div>
                  <strong>{story.title || story.id}</strong>
                  {story.detail ? <p>{formatReviewText(story.detail)}</p> : null}
                  <small>{[story.id, story.methodology, story.surface].filter(Boolean).join(" · ")}</small>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p>No per-story certification details were recorded. Open the HTML report or summary artifact if available.</p>
        )}
      </section>
      <section className="proof-section" aria-labelledby="proofFilesHeading">
        <h3 id="proofFilesHeading">Changed files</h3>
        {changedFiles.length ? (
          <ul className="proof-files">
            {changedFiles.map((path) => <li key={path}>{path}</li>)}
            {packet.changes.truncated && <li>more files not shown</li>}
          </ul>
        ) : (
          <p>No changed files reported yet.</p>
        )}
      </section>
      <section className="proof-section" aria-labelledby="proofDiffHeading">
        <h3 id="proofDiffHeading">Code diff</h3>
        {packet.changes.diff_error ? (
          <p>{formatTechnicalIssue(packet.changes.diff_error)}</p>
        ) : canShowDiff(detail) ? (
          <>
            <p>{packet.changes.diff_command || `Review ${packet.changes.file_count} changed file${packet.changes.file_count === 1 ? "" : "s"}.`}</p>
            <button type="button" data-testid="proof-open-diff-button" onClick={onShowDiff}>Open code diff</button>
          </>
        ) : (
          <p>No code diff is available for this run yet.</p>
        )}
      </section>
      <section className="proof-section" aria-labelledby="proofArtifactsHeading">
        <h3 id="proofArtifactsHeading">Evidence artifacts</h3>
        {evidence.length ? (
          <div className="proof-artifacts">
            {evidence.map((artifact) => (
              <button className={proofArtifactIndex === artifact.index ? "selected" : ""} key={artifact.index} type="button" onClick={() => onLoadProofArtifact(artifact.index)}>
                <strong>{artifact.label}</strong>
                <span>{artifact.kind}</span>
              </button>
            ))}
          </div>
        ) : (
          <p>No readable evidence artifacts are attached.</p>
        )}
      </section>
      <section className="proof-section proof-content" aria-labelledby="proofContentHeading">
        <div className="proof-content-heading">
          <div>
            <h3 id="proofContentHeading">Evidence content</h3>
            <p>{proofContent?.artifact.label || "Loading selected evidence artifact"}</p>
          </div>
          {proofContent?.truncated || compact.truncated ? <span>truncated</span> : null}
        </div>
        <pre className={proofContentIsLog ? "log-content" : ""} tabIndex={0} aria-label="Selected evidence content">
          {compact.text ? (proofContentIsLog ? renderLogText(compact.text) : compact.text) : "Loading evidence content..."}
        </pre>
      </section>
    </div>
  );
}

function DiffPane({diff}: {diff: DiffResponse | null}) {
  const sections = useMemo(() => splitDiffIntoFiles(diff?.text || "", diff?.files || []), [diff?.text, diff?.files]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  useEffect(() => {
    setSelectedPath(sections[0]?.path || null);
  }, [diff?.run_id, diff?.text, sections]);
  const selected = sections.find((section) => section.path === selectedPath) || sections[0] || null;
  if (!diff) {
    return <div className="diff-viewer"><div className="diff-toolbar"><strong>Code diff</strong><span>loading</span></div><pre className="diff-pane">Loading diff...</pre></div>;
  }
  return (
    <div className="diff-viewer" data-testid="diff-pane">
      <div className="diff-toolbar">
        <strong>Code diff</strong>
        <span>{diff.branch || "-"} → {diff.target}{diff.truncated ? " · truncated" : ""}</span>
      </div>
      {diff.error ? <div className="diff-error">{formatTechnicalIssue(diff.error)}</div> : null}
      <div className="diff-layout">
        {sections.length ? (
          <nav className="diff-file-list" aria-label="Changed files in diff" data-testid="diff-file-list">
            {sections.map((section) => (
              <button
                className={section.path === selected?.path ? "selected" : ""}
                type="button"
                key={section.path}
                onClick={() => setSelectedPath(section.path)}
              >
                {section.path}
              </button>
            ))}
          </nav>
        ) : null}
        <div className="diff-file-view">
          <div className="diff-file-heading" data-testid="diff-selected-file">
            <strong>{selected?.path || "No changed file selected"}</strong>
            <span>{sections.length ? `${sections.length} file${sections.length === 1 ? "" : "s"}` : "empty diff"}</span>
          </div>
          <pre className="diff-pane" tabIndex={0} aria-label="Code diff output">{selected?.text ? renderDiffText(selected.text) : "No diff content."}</pre>
        </div>
      </div>
    </div>
  );
}

function ReviewPacket({packet, onRunAction, onLoadArtifact, onShowArtifacts}: {
  packet: RunDetail["review_packet"];
  onRunAction: (action: string, label?: string) => void;
  onLoadArtifact: (index: number) => void;
  onShowArtifacts: () => void;
}) {
  const action = packet.next_action;
  const blockers = packet.readiness.blockers || [];
  const inProgress = packet.readiness.state === "in_progress";
  const reviewEvidence = packet.evidence.filter(isReviewEvidenceArtifact);
  const artifactCount = reviewEvidence.length;
  const readableEvidence = reviewEvidence.filter(isReadableArtifact);
  const evidence = readableEvidence.slice(0, 4);
  const showActionButton = Boolean(action.action_key);
  const hasFailure = Boolean(packet.failure);
  const attentionChecks = packet.checks.filter((check) => !["pass", "info"].includes(check.status));
  const drawerChecks = attentionChecks.length ? attentionChecks : packet.checks;
  const checksDefaultOpen = hasFailure || attentionChecks.length > 0;
  const checkSummary = attentionChecks.length
    ? `${attentionChecks.length} need review`
    : `${packet.checks.length} recorded`;
  const filesSummary = packet.changes.file_count
    ? `${packet.changes.file_count} file${packet.changes.file_count === 1 ? "" : "s"}`
    : `${packet.changes.files.length} file${packet.changes.files.length === 1 ? "" : "s"}`;
  const evidenceSummary = readableEvidence.length
    ? `${readableEvidence.length}/${reviewEvidence.length}`
    : `${reviewEvidence.length}`;
  return (
    <section className={`review-packet review-${packet.readiness.tone || "info"}`} aria-label="Review packet">
      <div className="review-head">
        <div>
          <span className="review-kicker">{packet.readiness.label}</span>
          <strong>{packet.headline}</strong>
          <span title={packet.summary}>{packet.summary}</span>
        </div>
        {showActionButton && (
          <button
            className={action.enabled ? "primary" : ""}
            type="button"
            data-testid="review-next-action-button"
            disabled={!action.enabled || !action.action_key}
            title={action.reason || ""}
            onClick={() => action.action_key && onRunAction(actionName(action.action_key), action.label)}
          >
            {reviewActionLabel(action.label)}
          </button>
        )}
      </div>
      {packet.failure && <FailureSummary failure={packet.failure} />}
      <div className="review-next-step">
        <strong>Next</strong>
        <span>{packet.readiness.next_step}</span>
      </div>
      {!hasFailure && blockers.length > 0 && (
        <ul className="review-blockers" aria-label="Review blockers">
          {blockers.map((blocker) => <li key={blocker}>{formatReviewText(blocker)}</li>)}
        </ul>
      )}
      <div className={`review-grid ${packet.readiness.state === "merged" || inProgress ? "review-grid-wide" : ""}`}>
        <ReviewMetric label="Stories" value={storiesLine(packet)} />
        <ReviewMetric label="Changes" value={packet.changes.file_count ? `${packet.changes.file_count} file${packet.changes.file_count === 1 ? "" : "s"}` : "-"} />
        <ReviewMetric label="Evidence" value={evidenceLine(packet)} />
        {(packet.readiness.state === "merged" || inProgress) && <ReviewMetric label="Artifacts" value={artifactCount ? `${artifactCount} file${artifactCount === 1 ? "" : "s"}` : "-"} />}
      </div>
      {drawerChecks.length > 0 && (
        <ReviewDrawer title="Checks" meta={checkSummary} defaultOpen={checksDefaultOpen}>
          <div className="review-checklist" aria-label="Readiness checklist">
            {drawerChecks.map((check) => (
              <div className={`review-check check-${check.status}`} key={check.key}>
                <span>{checkStatusLabel(check.status)}</span>
                <div>
                  <strong>{check.label}</strong>
                  <p>{formatReviewText(check.detail)}</p>
                </div>
              </div>
            ))}
          </div>
        </ReviewDrawer>
      )}
      {packet.changes.diff_error && <div className="review-note danger">{formatTechnicalIssue(packet.changes.diff_error)}</div>}
      {isRepositoryBlockedPacket(packet) && (
        <div className="review-note recovery-note">
          <strong>Recovery</strong>
          <span>Run git status --short, then commit, stash, or revert local project changes before landing.</span>
        </div>
      )}
      {packet.changes.files.length > 0 && (
        <ReviewDrawer title="Changed files" meta={filesSummary}>
          <ul className="review-files" aria-label="Changed files">
            {packet.changes.files.map((path) => <li key={path}>{path}</li>)}
            {packet.changes.truncated && <li>more files not shown</li>}
          </ul>
          {packet.changes.diff_command && packet.readiness.state === "ready" && <code title={packet.changes.diff_command}>{packet.changes.diff_command}</code>}
        </ReviewDrawer>
      )}
      {evidence.length > 0 && (
        <ReviewDrawer title="Evidence" meta={evidenceSummary}>
          <div className="review-evidence" aria-label="Evidence artifacts">
            {evidence.map((artifact) => (
              <button className={isReadableArtifact(artifact) ? "" : "missing"} key={`${artifact.index}-${artifact.path}`} type="button" disabled={!isReadableArtifact(artifact)} onClick={() => onLoadArtifact(artifact.index)}>
                {artifact.label}{artifact.exists ? "" : " missing"}
              </button>
            ))}
          </div>
        </ReviewDrawer>
      )}
      {packet.evidence.length > evidence.length && !inProgress && (
        <button className="review-inline-action" type="button" data-testid="review-more-artifacts-button" onClick={onShowArtifacts}>
          View all evidence
        </button>
      )}
    </section>
  );
}

function ReviewMetric({label, value}: {label: string; value: string}) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function ReviewDrawer({title, meta, defaultOpen = false, children}: {
  title: string;
  meta: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="review-drawer" open={defaultOpen}>
      <summary>
        <span>{title}</span>
        <strong>{meta}</strong>
      </summary>
      <div className="review-drawer-body">{children}</div>
    </details>
  );
}

function FailureSummary({failure, showExcerpt = false}: {
  failure: NonNullable<RunDetail["review_packet"]["failure"]>;
  showExcerpt?: boolean;
}) {
  return (
    <div className="review-note danger failure-summary">
      <strong>Failure</strong>
      <span>{failure.reason || "Failure recorded."}</span>
      {showExcerpt && failure.excerpt ? (
        <pre className="log-content" tabIndex={0} aria-label="Failure log excerpt">{renderLogText(failure.excerpt)}</pre>
      ) : null}
    </div>
  );
}

function DetailLine({line}: {line: string}) {
  const visibleLine = userVisibleDetailLine(line);
  if (!visibleLine) return null;
  const visibleSplit = visibleLine.indexOf(":");
  if (visibleSplit > 0 && visibleSplit < 24) {
    return (
      <>
        <dt>{visibleLine.slice(0, visibleSplit)}</dt>
        <dd>{visibleLine.slice(visibleSplit + 1).trim() || "-"}</dd>
      </>
    );
  }
  return (
    <>
      <dt>Info</dt>
      <dd>{visibleLine}</dd>
    </>
  );
}

function ActionBar({actions, mergeBlocked, onRunAction}: {actions: ActionState[]; mergeBlocked: boolean; onRunAction: (action: string, label?: string) => void}) {
  const visible = actions.filter((action) => !["o", "e", "m", "M"].includes(action.key));
  if (!visible.length) return <div className="advanced-actions empty" aria-hidden="true" />;
  return (
    <details className="advanced-actions">
      <summary>Advanced run actions</summary>
      <div className="action-bar" aria-label="Advanced run actions">
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
    </details>
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
    const artifactIsLog = isLogArtifact(artifactContent?.artifact || null);
    const rawContent = artifactContent?.content || "No content.";
    const compact = compactLongText(artifactIsLog ? rawContent : formatArtifactContent(rawContent), 20000);
    return (
      <div className="artifact-pane">
        <button type="button" onClick={onBack}>Back to artifacts</button>
        <div className="artifact-meta">
          {artifactContent?.artifact.label || "artifact"} {artifactContent?.truncated || compact.truncated ? "(truncated)" : ""}
        </div>
        <pre className={artifactIsLog ? "log-content" : ""} tabIndex={0} aria-label="Artifact content">
          {artifactIsLog ? renderLogText(compact.text) : compact.text}
        </pre>
      </div>
    );
  }
  if (!artifacts.length) return <div className="artifact-pane">No artifacts.</div>;
  return (
    <div className="artifact-pane artifact-list">
      {artifacts.map((artifact) => (
        <button key={artifact.index} type="button" disabled={!isReadableArtifact(artifact)} onClick={() => onLoadArtifact(artifact.index)}>
          <strong>{artifact.label}</strong>
          <span>{artifactKindLabel(artifact)}</span>
        </button>
      ))}
    </div>
  );
}

function JobDialog({project, onClose, onQueued, onError}: {
  project: StateResponse["project"] | undefined;
  onClose: () => void;
  onQueued: (message?: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [command, setCommand] = useState<JobCommand>("build");
  const [subcommand, setSubcommand] = useState<"bugs" | "feature" | "target">("bugs");
  const [intent, setIntent] = useState("");
  const [taskId, setTaskId] = useState("");
  const [after, setAfter] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("split");
  const [planning, setPlanning] = useState<PlanningMode>("direct");
  const [specFilePath, setSpecFilePath] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [buildProvider, setBuildProvider] = useState("");
  const [buildModel, setBuildModel] = useState("");
  const [buildEffort, setBuildEffort] = useState("");
  const [certifierProvider, setCertifierProvider] = useState("");
  const [certifierModel, setCertifierModel] = useState("");
  const [certifierEffort, setCertifierEffort] = useState("");
  const [fixProvider, setFixProvider] = useState("");
  const [fixModel, setFixModel] = useState("");
  const [fixEffort, setFixEffort] = useState("");
  const [certification, setCertification] = useState<CertificationPolicy>("");
  const [targetConfirmed, setTargetConfirmed] = useState(false);
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const dialogRef = useDialogFocus<HTMLFormElement>(onClose, submitting);
  const targetNeedsConfirmation = Boolean(project?.dirty);
  const submitDisabled = submitting
    || (command === "build" && !intent.trim())
    || (command === "build" && planning === "spec-file" && !specFilePath.trim())
    || (targetNeedsConfirmation && !targetConfirmed);

  useEffect(() => {
    setTargetConfirmed(false);
  }, [project?.path]);

  useEffect(() => {
    if (!certificationPolicyAllowed(command, subcommand, certification)) {
      setCertification("");
    }
    if (command !== "build") {
      setPlanning("direct");
      setSpecFilePath("");
    }
  }, [certification, command, subcommand]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (command === "build" && !intent.trim()) {
      setStatus("Build intent is required.");
      return;
    }
    if (targetNeedsConfirmation && !targetConfirmed) {
      setStatus("Confirm the dirty target project before queueing.");
      return;
    }
    setStatus("queueing");
    setSubmitting(true);
    try {
      const payload = buildQueuePayload({
        command,
        subcommand,
        intent: intent.trim(),
        taskId: taskId.trim(),
        after,
        executionMode,
        provider,
        model,
        effort,
        buildProvider,
        buildModel,
        buildEffort,
        certifierProvider,
        certifierModel,
        certifierEffort,
        fixProvider,
        fixModel,
        fixEffort,
        certification,
        planning,
        specFilePath: specFilePath.trim(),
      });
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
      <form
        ref={dialogRef}
        className="job-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="jobDialogHeading"
        aria-describedby={status ? "jobDialogStatus" : undefined}
        tabIndex={-1}
        onSubmit={(event) => void submit(event)}
      >
        <header>
          <h2 id="jobDialogHeading">New queue job</h2>
          <button type="button" onClick={onClose}>Close</button>
        </header>
        <label>Command
          <select data-testid="job-command-select" value={command} onChange={(event) => setCommand(event.target.value as JobCommand)}>
            <option value="build">Build</option>
            <option value="improve">Improve</option>
            <option value="certify">Certify</option>
          </select>
        </label>
        <div className={`target-guard ${project?.dirty ? "target-dirty" : ""}`} aria-label="Target project">
          <strong>Target project</strong>
          <dl>
            <dt>Path</dt><dd title={project?.path || ""}>{project?.path || "loading"}</dd>
            <dt>Branch</dt><dd>{project?.branch || "-"}</dd>
            <dt>State</dt><dd>{project ? project.dirty ? "dirty" : "clean" : "unknown"}</dd>
          </dl>
          <p>This job can create branches/worktrees and modify files under this folder.</p>
          {targetNeedsConfirmation && (
            <label className="check-label target-confirm">
              <input
                checked={targetConfirmed}
                data-testid="target-project-confirm"
                type="checkbox"
                onChange={(event) => setTargetConfirmed(event.target.checked)}
              />
              I understand this dirty project may affect the queued work
            </label>
          )}
        </div>
        {command === "improve" && (
          <label>Improve mode
            <select data-testid="job-improve-mode-select" value={subcommand} onChange={(event) => setSubcommand(event.target.value as ImproveSubcommand)}>
              <option value="bugs">Bugs</option>
              <option value="feature">Feature</option>
              <option value="target">Target</option>
            </select>
          </label>
        )}
        <label>Intent / focus
          <textarea value={intent} rows={5} placeholder="Describe the requested outcome" onChange={(event) => setIntent(event.target.value)} />
        </label>
        <details className="job-advanced">
          <summary>Advanced options</summary>
          {command !== "certify" && (
            <label>Execution mode
              <select data-testid="job-execution-mode-select" value={executionMode} onChange={(event) => setExecutionMode(event.target.value as ExecutionMode)}>
                <option value="split">Reliable split mode</option>
                <option value="agentic">Agentic single session</option>
              </select>
              <span className="field-hint">{executionModeHelp(executionMode, command)}</span>
            </label>
          )}
          {command === "build" && (
            <label>Planning
              <select data-testid="job-planning-select" value={planning} onChange={(event) => setPlanning(event.target.value as PlanningMode)}>
                <option value="direct">Direct build</option>
                <option value="spec-review">Generate spec for review</option>
                <option value="spec-auto">Generate spec and approve automatically</option>
                <option value="spec-file">Use spec file</option>
              </select>
              <span className="field-hint">{planningHelp(planning)}</span>
            </label>
          )}
          {command === "build" && planning === "spec-file" && (
            <label>Spec file path
              <input
                data-testid="job-spec-file-input"
                value={specFilePath}
                type="text"
                placeholder="/path/to/spec.md"
                onChange={(event) => setSpecFilePath(event.target.value)}
              />
            </label>
          )}
          <div className="field-grid">
            <label>Task id
              <input value={taskId} type="text" placeholder="auto-generated" onChange={(event) => setTaskId(event.target.value)} />
            </label>
            <label>After
              <input value={after} type="text" placeholder="optional dependencies" onChange={(event) => setAfter(event.target.value)} />
            </label>
          </div>
          <div className="field-grid">
            <label>Provider
              <select data-testid="job-provider-select" value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="">{providerDefaultLabel(project)}</option>
                <option value="codex">Codex</option>
                <option value="claude">Claude</option>
              </select>
            </label>
            <label>Reasoning effort
              <select data-testid="job-effort-select" value={effort} onChange={(event) => setEffort(event.target.value)}>
                <option value="">{effortDefaultLabel(project)}</option>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="max">Max</option>
              </select>
            </label>
          </div>
          <label>Model
            <input value={model} type="text" placeholder={modelDefaultPlaceholder(project)} onChange={(event) => setModel(event.target.value)} />
          </label>
          <details className="job-agent-routing">
            <summary>{executionMode === "agentic" && command !== "certify" ? "Agent session" : "Phase routing"}</summary>
            {command !== "certify" && executionMode === "agentic" && (
              <div className="static-field">
                <span>Routing model</span>
                <strong>Single session</strong>
                <p className="field-hint">Use Provider, Model, and Reasoning above for the main agent. Split-only phase overrides are hidden because agentic mode does not run separate build/certify/fix calls.</p>
              </div>
            )}
            {command === "build" && executionMode === "split" && (
              <PhaseRoutingFields
                label="Build"
                testKey="build"
                provider={buildProvider}
                model={buildModel}
                effort={buildEffort}
                onProvider={setBuildProvider}
                onModel={setBuildModel}
                onEffort={setBuildEffort}
              />
            )}
            {(command === "certify" || executionMode === "split") && (
              <PhaseRoutingFields
                label={command === "improve" ? "Certifier / evaluator" : "Certifier"}
                testKey="certifier"
                provider={certifierProvider}
                model={certifierModel}
                effort={certifierEffort}
                onProvider={setCertifierProvider}
                onModel={setCertifierModel}
                onEffort={setCertifierEffort}
              />
            )}
            {command !== "certify" && executionMode === "split" && (
              <PhaseRoutingFields
                label={command === "improve" ? "Improver / fixer" : "Fix"}
                testKey="fix"
                provider={fixProvider}
                model={fixModel}
                effort={fixEffort}
                onProvider={setFixProvider}
                onModel={setFixModel}
                onEffort={setFixEffort}
              />
            )}
          </details>
          {certificationOptions(command, subcommand, project).length > 0 ? (
            <label>Certification
              <select
                data-testid="job-certification-select"
                value={certification}
                onChange={(event) => setCertification(event.target.value as CertificationPolicy)}
              >
                {certificationOptions(command, subcommand, project).map((option) => (
                  <option key={option.value || "inherit"} value={option.value}>{option.label}</option>
                ))}
              </select>
              <span className="field-hint">{certificationHelp(command, subcommand, certification, project)}</span>
            </label>
          ) : (
            <div className="static-field" data-testid="job-certification-static">
              <span>Evaluation policy</span>
              <strong>{staticCertificationLabel(command, subcommand)}</strong>
            </div>
          )}
        </details>
        <footer>
          <span id="jobDialogStatus" className="muted" aria-live="polite">{status}</span>
          <button className="primary" type="submit" disabled={submitDisabled}>{submitting ? "Queueing" : "Queue job"}</button>
        </footer>
      </form>
    </div>
  );
}

function PhaseRoutingFields({label, testKey, provider, model, effort, onProvider, onModel, onEffort}: {
  label: string;
  testKey: string;
  provider: string;
  model: string;
  effort: string;
  onProvider: (value: string) => void;
  onModel: (value: string) => void;
  onEffort: (value: string) => void;
}) {
  return (
    <section className="phase-routing-group" aria-label={`${label} routing`}>
      <h3>{label}</h3>
      <div className="field-grid">
        <label>Provider
          <select data-testid={`job-${testKey}-provider-select`} value={provider} onChange={(event) => onProvider(event.target.value)}>
            <option value="">Inherit</option>
            <option value="codex">Codex</option>
            <option value="claude">Claude</option>
          </select>
        </label>
        <label>Reasoning
          <select data-testid={`job-${testKey}-effort-select`} value={effort} onChange={(event) => onEffort(event.target.value)}>
            <option value="">Inherit</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="max">Max</option>
          </select>
        </label>
      </div>
      <label>Model
        <input value={model} type="text" placeholder="inherit" onChange={(event) => onModel(event.target.value)} />
      </label>
    </section>
  );
}

function executionModeHelp(mode: ExecutionMode, command: JobCommand): string {
  if (mode !== "split") {
    return "A single agent session owns the whole loop; useful for exploration or fallback.";
  }
  return command === "improve"
    ? "Otto owns evaluation, improvement/fix, and recovery as separate phases."
    : "Otto owns build, certify, fix, and recovery as separate phases.";
}

function planningHelp(planning: PlanningMode): string {
  if (planning === "spec-review") return "Otto generates a spec, pauses, and waits for approval in Mission Control.";
  if (planning === "spec-auto") return "Otto generates a spec and uses it immediately without a human gate.";
  if (planning === "spec-file") return "Use an existing approved spec file; the CLI validates it before building.";
  return "Start implementation directly from the intent.";
}

function certificationOptions(
  command: JobCommand,
  subcommand: ImproveSubcommand,
  project: StateResponse["project"] | undefined,
): Array<{value: CertificationPolicy; label: string}> {
  if (command === "improve" && subcommand !== "bugs") return [];
  const inherited = command === "improve" && subcommand === "bugs"
    ? "Inherit: thorough bug certification (improve default)"
    : certificationDefaultLabel(project);
  const options: Array<{value: CertificationPolicy; label: string}> = [
    {value: "", label: inherited},
    {value: "fast", label: "Fast certification (--fast)"},
    {value: "standard", label: "Standard certification (--standard)"},
    {value: "thorough", label: "Thorough certification (--thorough)"},
  ];
  if (command === "build") {
    options.push({value: "skip", label: "Skip certification (--no-qa)"});
  }
  return options;
}

function certificationPolicyAllowed(command: JobCommand, subcommand: ImproveSubcommand, policy: CertificationPolicy): boolean {
  if (!policy) return true;
  if (policy === "skip") return command === "build";
  return command === "build" || command === "certify" || (command === "improve" && subcommand === "bugs");
}

function providerDefaultLabel(project: StateResponse["project"] | undefined): string {
  const defaults = project?.defaults;
  if (!defaults) return "Inherit from otto.yaml";
  return `Inherit: ${titleCase(defaults.provider || "claude")} (${configSourceLabel(defaults.config_file_exists)})`;
}

function effortDefaultLabel(project: StateResponse["project"] | undefined): string {
  const defaults = project?.defaults;
  if (!defaults) return "Inherit from otto.yaml";
  const effort = defaults.reasoning_effort ? titleCase(defaults.reasoning_effort) : "Provider default";
  return `Inherit: ${effort} (${configSourceLabel(defaults.config_file_exists)})`;
}

function modelDefaultPlaceholder(project: StateResponse["project"] | undefined): string {
  const model = project?.defaults?.model;
  return model ? `project default: ${model}` : "provider default";
}

function certificationDefaultLabel(project: StateResponse["project"] | undefined): string {
  const defaults = project?.defaults;
  if (!defaults) return "Inherit certification policy";
  const policy = defaults.skip_product_qa ? "skip certification" : `${defaults.certifier_mode || "fast"} certification`;
  return `Inherit: ${policy} (${configSourceLabel(defaults.config_file_exists)})`;
}

function certificationHelp(
  command: JobCommand,
  subcommand: ImproveSubcommand,
  certification: CertificationPolicy,
  project: StateResponse["project"] | undefined,
): string {
  if (certification === "skip") return "Build runs without post-build product certification.";
  if (certification) return "Applies only to the certification phase for this queued job.";
  const defaults = project?.defaults;
  if (defaults?.config_error) return `Using built-in defaults because otto.yaml could not be read: ${defaults.config_error}`;
  if (command === "improve" && subcommand === "bugs") return "Improve bugs defaults to thorough certification unless you choose fast or standard here.";
  return "Inherits certifier_mode and skip_product_qa from otto.yaml, then built-in defaults.";
}

function staticCertificationLabel(command: JobCommand, subcommand: ImproveSubcommand): string {
  if (command === "improve" && subcommand === "feature") return "Feature improvement uses hillclimb evaluation";
  if (command === "improve" && subcommand === "target") return "Target improvement uses target evaluation";
  return "Managed by this command";
}

function configSourceLabel(configFileExists: boolean): string {
  return configFileExists ? "otto.yaml" : "built-in default";
}

function titleCase(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

function ConfirmDialog({confirm, pending, onCancel, onConfirm}: {
  confirm: ConfirmState;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const confirmClass = confirm.tone === "danger" ? "danger-button" : "primary";
  const dialogRef = useDialogFocus<HTMLDivElement>(onCancel, pending);

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        ref={dialogRef}
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirmHeading"
        aria-describedby="confirmBody"
        tabIndex={-1}
      >
        <header>
          <h2 id="confirmHeading">{confirm.title}</h2>
          <button type="button" disabled={pending} onClick={onCancel}>Close</button>
        </header>
        <p id="confirmBody">{confirm.body}</p>
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

function missionFocus(data: StateResponse | null): {
  kicker: string;
  title: string;
  body: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
  primary: "new" | "start" | "land" | "diagnostics" | "recover" | "resolve";
  working: number;
  needsAction: number;
  ready: number;
} {
  if (!data) {
    return {
      kicker: "Loading",
      title: "Reading project state",
      body: "Mission Control is loading the queue, runs, and repository status.",
      tone: "info",
      primary: "new",
      working: 0,
      needsAction: 0,
      ready: 0,
    };
  }
  const columns = taskBoardColumns(data);
  const working = columns.find((column) => column.stage === "working")?.items.length || 0;
  const needsAction = columns.find((column) => column.stage === "attention")?.items.length || 0;
  const ready = columns.find((column) => column.stage === "ready")?.items.length || 0;
  const rawReady = data.landing.counts.ready || 0;
  const queued = data.watcher.counts.queued || 0;
  const commandBacklog = Number(data.runtime.command_backlog.pending || 0) + Number(data.runtime.command_backlog.processing || 0);
  const target = data.landing.target || "main";
  if (mergeRecoveryNeeded(data.landing)) {
    return {
      kicker: "Landing",
      title: "Landing needs recovery",
      body: "A previous landing left git mid-merge. Recover will clean up that state and relaunch conflict-resolving landing for the remaining ready work.",
      tone: "danger",
      primary: "recover",
      working,
      needsAction,
      ready,
    };
  }
  if (commandBacklog && data.watcher.health.state !== "running") {
    return {
      kicker: "Commands",
      title: `${commandBacklog} command${commandBacklog === 1 ? "" : "s"} waiting`,
      body: "Start the watcher to apply pending operator actions.",
      tone: "warning",
      primary: "start",
      working,
      needsAction,
      ready,
    };
  }
  if (data.landing.merge_blocked && rawReady) {
    const dirty = data.landing.dirty_files.slice(0, 3).join(", ");
    return {
      kicker: "Repository",
      title: "Cleanup required before landing",
      body: dirty ? `Local changes block landing: ${dirty}.` : "Local repository state blocks landing.",
      tone: "danger",
      primary: "diagnostics",
      working,
      needsAction,
      ready,
    };
  }
  if (needsAction) {
    if (supersededFailedTaskIds(data.landing).length) {
      return {
        kicker: "Release",
        title: `${needsAction} stale task${needsAction === 1 ? "" : "s"} can be cleaned`,
        body: "A failed attempt is superseded by landed work. Otto can clean the stale card and leave the board focused on current release state.",
        tone: "warning",
        primary: "resolve",
        working,
        needsAction,
        ready,
      };
    }
    return {
      kicker: "Attention",
      title: `${needsAction} task${needsAction === 1 ? "" : "s"} need action`,
      body: "Open blocked work or the health view to inspect the failure, stale run, missing branch, or recovery action.",
      tone: "warning",
      primary: "diagnostics",
      working,
      needsAction,
      ready,
    };
  }
  if (ready) {
    return {
      kicker: "Review",
      title: `${ready} task${ready === 1 ? "" : "s"} ready to land`,
      body: `Review evidence and changed files, then land ready work into ${target}.`,
      tone: "success",
      primary: "land",
      working,
      needsAction,
      ready,
    };
  }
  if (queued && data.watcher.health.state !== "running") {
    return {
      kicker: "Queue",
      title: `${queued} queued task${queued === 1 ? "" : "s"} waiting`,
      body: "Start the watcher to run queued work.",
      tone: "info",
      primary: "start",
      working,
      needsAction,
      ready,
    };
  }
  if (working) {
    return {
      kicker: "Working",
      title: `${working} task${working === 1 ? "" : "s"} in flight`,
      body: "Runs are active. Review packets will update as tasks finish.",
      tone: "info",
      primary: "new",
      working,
      needsAction,
      ready,
    };
  }
  if (data.landing.counts.total || data.history.total_rows) {
    return {
      kicker: "Idle",
      title: "No task needs action",
      body: "Queue the next product task when the current work is complete.",
      tone: "neutral",
      primary: "new",
      working,
      needsAction,
      ready,
    };
  }
  return {
    kicker: "Start",
    title: "Queue the first job",
    body: "Create a build, improve, or certify task for this project.",
    tone: "neutral",
    primary: "new",
    working,
    needsAction,
    ready,
  };
}

function taskBoardColumns(data: StateResponse | null, filters: Filters = defaultFilters): Array<{
  stage: BoardStage;
  title: string;
  empty: string;
  items: BoardTask[];
}> {
  const columns: Array<{stage: BoardStage; title: string; empty: string; items: BoardTask[]}> = [
    {stage: "attention", title: "Needs Action", empty: "No blocked work.", items: []},
    {stage: "working", title: "Queued / Running", empty: "No queued or running tasks.", items: []},
    {stage: "ready", title: "Ready To Land", empty: "Nothing ready yet.", items: []},
    {stage: "landed", title: "Landed", empty: "Nothing landed yet.", items: []},
  ];
  if (!data) return columns;
  const liveByTask = new Map<string, LiveRunItem>();
  const cardsByKey = new Map<string, BoardTask>();
  for (const item of data.live.items) {
    if (item.queue_task_id) liveByTask.set(item.queue_task_id, item);
  }
  for (const item of data.landing.items) {
    const live = liveByTask.get(item.task_id);
    const runId = item.run_id || live?.run_id || null;
    const card = boardTaskFromLanding(item, runId, !data.landing.merge_blocked, live);
    cardsByKey.set(item.task_id, card);
  }
  for (const item of data.live.items) {
    const key = item.queue_task_id || item.run_id;
    if (cardsByKey.has(key)) continue;
    if (!item.queue_task_id && !item.active && !isAttentionStatus(item.display_status)) continue;
    cardsByKey.set(key, boardTaskFromLive(item));
  }
  for (const card of cardsByKey.values()) {
    if (!boardTaskMatchesFilters(card, filters)) continue;
    const column = columns.find((candidate) => candidate.stage === card.stage);
    column?.items.push(card);
  }
  for (const column of columns) {
    column.items.sort(compareBoardTasks);
  }
  return columns;
}

function boardTaskMatchesFilters(task: BoardTask, filters: Filters): boolean {
  const query = filters.query.trim().toLowerCase();
  if (query) {
    const haystack = [task.id, task.title, task.summary, task.status, task.branch || "", task.reason, task.proof]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(query)) return false;
  }
  if (filters.activeOnly && task.stage !== "working") return false;
  if (filters.outcome !== "all" && !boardTaskMatchesOutcome(task, filters.outcome)) return false;
  return true;
}

function boardTaskMatchesOutcome(task: BoardTask, outcome: OutcomeFilter): boolean {
  const status = task.status.toLowerCase();
  if (outcome === "success") return ["ready", "landed", "done", "success"].some((value) => status.includes(value));
  if (outcome === "failed") return status.includes("failed") || task.stage === "attention";
  if (outcome === "interrupted") return status.includes("interrupted") || status.includes("stale");
  if (outcome === "cancelled") return status.includes("cancelled");
  if (outcome === "removed") return status.includes("removed");
  if (outcome === "other") return !["ready", "landed", "done", "success", "failed", "interrupted", "stale", "cancelled", "removed"].some((value) => status.includes(value));
  return true;
}

function boardTaskFromLanding(item: LandingItem, runId: string | null, mergeAllowed: boolean, live?: LiveRunItem): BoardTask {
  const stage = boardStageForLanding(item, mergeAllowed);
  const active = Boolean(live?.active) || isActiveQueueStatus(item.queue_status);
  return {
    id: item.task_id,
    runId,
    title: item.task_id || item.summary || "queue task",
    summary: item.summary || item.task_id || "",
    stage,
    status: boardStatusLabel(item, mergeAllowed),
    branch: item.branch,
    changedFileCount: item.changed_file_count,
    proof: proofLine(item),
    reason: boardReasonForLanding(item, mergeAllowed, live),
    active,
    elapsedDisplay: live?.elapsed_display || landingDurationDisplay(item),
    lastEvent: live?.last_event || null,
    progress: live?.progress || null,
    buildConfig: live?.build_config || item.build_config || null,
    source: "landing",
  };
}

function landingDurationDisplay(item: LandingItem): string | null {
  if (!["done", "failed", "cancelled", "interrupted", "removed"].includes(item.queue_status)) return null;
  return typeof item.duration_s === "number" ? formatDuration(item.duration_s) : null;
}

function boardTaskFromLive(item: LiveRunItem): BoardTask {
  const stage: BoardStage = item.active ? "working" : isAttentionStatus(item.display_status) ? "attention" : "working";
  return {
    id: item.queue_task_id || item.run_id,
    runId: item.run_id,
    title: item.queue_task_id || item.display_id || item.run_id,
    summary: item.command || item.last_event || item.run_id,
    stage,
    status: item.display_status,
    branch: item.branch,
    changedFileCount: null,
    proof: item.cost_display || "-",
    reason: item.overlay?.reason || item.last_event || item.elapsed_display || item.display_status,
    active: item.active,
    elapsedDisplay: item.elapsed_display || null,
    lastEvent: item.last_event || null,
    progress: item.progress || null,
    buildConfig: item.build_config || null,
    source: "live",
  };
}

function boardStageForLanding(item: LandingItem, mergeAllowed: boolean): BoardStage {
  if (item.landing_state === "merged") return "landed";
  if (item.landing_state === "ready") return mergeAllowed ? "ready" : "attention";
  if (isWaitingLandingItem(item)) return "working";
  return "attention";
}

function boardStatusLabel(item: LandingItem, mergeAllowed: boolean): string {
  if (item.landing_state === "ready") return mergeAllowed ? "ready" : "blocked";
  if (item.landing_state === "merged") return "landed";
  return item.queue_status || item.landing_state || "blocked";
}

function boardReasonForLanding(item: LandingItem, mergeAllowed: boolean, live?: LiveRunItem): string {
  if (item.landing_state === "ready" && !mergeAllowed) return "Repository cleanup required before landing.";
  if (item.landing_state === "ready") return `${changeLine(item)} changed; ${proofLine(item)} recorded.`;
  if (item.landing_state === "merged") return item.merge_id ? `Landed by ${item.merge_id}.` : "Already landed.";
  if (item.queue_status === "queued") return "Waiting for the watcher.";
  if (item.queue_status === "initializing") return "Child process started; waiting for Otto session readiness.";
  if (["starting", "running", "terminating"].includes(item.queue_status)) {
    if (live?.elapsed_display) return `Running for ${live.elapsed_display}.`;
    return "Task is still in flight.";
  }
  if (item.diff_error) return formatTechnicalIssue(item.diff_error);
  if (!item.branch) return "No branch is recorded.";
  if (["failed", "cancelled", "interrupted", "stale"].includes(item.queue_status)) return "Open the review packet for recovery actions.";
  return "Not ready to land yet.";
}

function taskChangeLine(task: BoardTask): string {
  if (task.changedFileCount === null) return "diff pending";
  if (task.stage === "working" && task.status.toLowerCase() === "queued") return "not built yet";
  if (task.stage === "working") return "diff pending";
  if (task.stage === "landed") return "no unlanded diff";
  return `${task.changedFileCount} file${task.changedFileCount === 1 ? "" : "s"}`;
}

function taskProofMeta(task: BoardTask): string {
  const proof = task.proof.trim();
  if (task.active && ["running", "starting", "initializing"].includes(proof.toLowerCase())) return "";
  return proof;
}

function taskConfigChip(task: BoardTask): string {
  const config = task.buildConfig;
  if (!config) return "";
  const primaryAgent = primaryAgentConfig(config);
  const provider = primaryAgent?.provider || config.provider || "provider default";
  const mode = config.split_mode ? "split" : "agentic";
  const planning = config.planning === "spec_review" ? "spec review" : config.planning === "spec_auto" ? "spec" : config.planning === "spec_file" ? "spec file" : "";
  const cert = config.skip_product_qa ? "no cert" : (config.certification || `${config.certifier_mode || "fast"} certification`);
  const timeout = config.queue?.task_timeout_s ? formatDuration(config.queue.task_timeout_s) : "";
  return [mode, provider, planning, cert, timeout].filter(Boolean).join(" · ");
}

function taskConfigSummary(config: RunBuildConfig | null): string {
  if (!config) return "No build config recorded.";
  return [
    config.split_mode ? "split mode" : "agentic mode",
    providerConfigLine(config),
    planningLine(config),
    certificationLine(config),
    timeoutLine(config),
  ].filter(Boolean).join(" · ");
}

function isActiveQueueStatus(status: string): boolean {
  return ["initializing", "starting", "running", "terminating"].includes(status);
}

function liveEventLabel(task: BoardTask): string | null {
  const event = String(task.lastEvent || "").trim();
  if (!event || event === "-") return null;
  const normalized = event.toLowerCase();
  const status = task.status.toLowerCase();
  if (normalized === status || ["running", "queued", "starting", "initializing"].includes(normalized)) return null;
  return shortText(event, 56);
}

function progressLabel(task: BoardTask): string | null {
  const progress = String(task.progress || "").trim();
  if (!task.active || !progress) return null;
  return shortText(progress, 110);
}

function activeRunSummary(data: StateResponse | null): {label: string; detail: string} | null {
  if (!data) return null;
  const active = data.live.items.filter((item) => item.active);
  if (!active.length) return null;
  if (active.length === 1) {
    const item = active[0];
    if (!item) return null;
    const label = shortText(item.queue_task_id || item.display_id || item.run_id, 56);
    const status = titleCase(item.display_status || item.status || "running");
    return {label, detail: [status, item.elapsed_display].filter(Boolean).join(" · ")};
  }
  return {label: `${active.length} active runs`, detail: "Detailed progress is shown on each task card."};
}

function compareBoardTasks(left: BoardTask, right: BoardTask): number {
  const stageOrder: Record<BoardStage, number> = {attention: 0, ready: 1, working: 2, landed: 3};
  const byStage = stageOrder[left.stage] - stageOrder[right.stage];
  if (byStage) return byStage;
  return left.title.localeCompare(right.title);
}

function taskBoardSubtitle(data: StateResponse | null, filters: Filters = defaultFilters): string {
  if (!data) return "Loading tasks.";
  const total = taskBoardColumns(data, filters).reduce((sum, column) => sum + column.items.length, 0);
  if (!total) return "No work queued.";
  const target = data.landing.target || "main";
  return `${total} visible task${total === 1 ? "" : "s"} for ${target}.`;
}

function testIdForTask(taskId: string): string {
  return `task-card-${taskId.replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
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
  return Number(counts.running || 0)
    + Number(counts.initializing || 0)
    + Number(counts.starting || 0)
    + Number(counts.terminating || 0);
}

function canStartWatcher(data?: StateResponse | null): boolean {
  const queued = Number(data?.watcher.counts.queued || 0);
  const backlog = Number(data?.runtime.command_backlog.pending || 0) + Number(data?.runtime.command_backlog.processing || 0);
  return Boolean(data?.runtime.supervisor.can_start && (queued > 0 || backlog > 0));
}

function canStopWatcher(data?: StateResponse | null): boolean {
  return Boolean(data?.runtime.supervisor.can_stop);
}

function watcherControlHint(data?: StateResponse | null): string {
  if (!data) return "Loading watcher controls.";
  const queued = Number(data.watcher.counts.queued || 0);
  const backlog = Number(data.runtime.command_backlog.pending || 0) + Number(data.runtime.command_backlog.processing || 0);
  if (canStartWatcher(data)) {
    const work = [queued ? `${queued} queued` : "", backlog ? `${backlog} command${backlog === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ");
    return `Start watcher to process ${work}.`;
  }
  if (canStopWatcher(data)) return "Watcher is running; stop it only when you need to pause queue processing.";
  if (data.runtime.supervisor.start_blocked_reason) return `Start unavailable: ${data.runtime.supervisor.start_blocked_reason}`;
  if (!queued && !backlog) return "Queue a job before starting the watcher.";
  return data.watcher.health.next_action || "Watcher controls are unavailable.";
}

function watcherSummary(watcher?: WatcherInfo): string {
  const health = watcher?.health;
  if (!health) return "stopped";
  if (health.state === "running") return `running pid ${health.blocking_pid || "-"}`;
  if (health.state === "stale") return `stale pid ${health.blocking_pid || "-"}`;
  return "stopped";
}

function commandBacklogLine(command: CommandBacklogItem): string {
  const id = command.command_id || "command id unknown";
  const target = command.run_id || command.task_id || command.command_id || "target unknown";
  const age = command.age_s === null || command.age_s === undefined ? "" : ` · ${formatDuration(command.age_s)} old`;
  return `${id} · ${target}${age}`;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

function tokenBreakdownLine(tokenUsage?: StateResponse["project_stats"]["token_usage"]): string {
  if (!tokenUsage) return "No token usage recorded";
  const input = Number(tokenUsage.input_tokens || 0);
  const cacheRead = Number(tokenUsage.cache_read_input_tokens || tokenUsage.cached_input_tokens || 0);
  const cacheWrite = Number(tokenUsage.cache_creation_input_tokens || 0);
  const output = Number(tokenUsage.output_tokens || 0);
  const reasoning = Number(tokenUsage.reasoning_tokens || 0);
  const parts = [
    input ? `${formatCompactNumber(input)} input` : "",
    cacheRead ? `${formatCompactNumber(cacheRead)} cache read` : "",
    cacheWrite ? `${formatCompactNumber(cacheWrite)} cache write` : "",
    output ? `${formatCompactNumber(output)} output` : "",
    reasoning ? `${formatCompactNumber(reasoning)} reasoning` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "No token usage recorded";
}

function usageLine(item: HistoryItem): string {
  const tokens = tokenTotal(item.token_usage);
  const cost = item.cost_usd && item.cost_usd > 0 ? `$${item.cost_usd.toFixed(2)}` : "";
  const tokenText = tokens ? `${formatCompactNumber(tokens)} tokens` : item.cost_display || "";
  return [tokenText, cost && cost !== tokenText ? cost : ""].filter(Boolean).join(" · ") || "-";
}

function storyTotalsFromLanding(items: LandingItem[]): {passed: number; tested: number} {
  return items.reduce(
    (totals, item) => {
      totals.passed += Number(item.stories_passed || 0);
      totals.tested += Number(item.stories_tested || 0);
      return totals;
    },
    {passed: 0, tested: 0},
  );
}

function tokenTotal(tokenUsage?: StateResponse["project_stats"]["token_usage"]): number {
  if (!tokenUsage) return 0;
  const explicit = Number(tokenUsage.total_tokens || 0);
  const cacheCreation = Number(tokenUsage.cache_creation_input_tokens || 0);
  let cacheRead = Number(tokenUsage.cache_read_input_tokens || 0);
  if (!cacheCreation && !cacheRead) cacheRead = Number(tokenUsage.cached_input_tokens || 0);
  const derived = Number(tokenUsage.input_tokens || 0)
    + cacheCreation
    + cacheRead
    + Number(tokenUsage.output_tokens || 0)
    + Number(tokenUsage.reasoning_tokens || 0);
  return Math.max(explicit, derived);
}

function formatCompactNumber(value: number): string {
  const amount = Math.max(Number(value || 0), 0);
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(Math.round(amount));
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

function useDialogFocus<T extends HTMLElement>(onCancel: () => void, disabled: boolean) {
  const dialogRef = useRef<T | null>(null);
  const onCancelRef = useRef(onCancel);
  const disabledRef = useRef(disabled);

  useEffect(() => {
    onCancelRef.current = onCancel;
  }, [onCancel]);

  useEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    window.setTimeout(() => {
      const first = focusableDialogElements(dialog)[0] || dialog;
      first.focus();
    }, 0);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !disabledRef.current) {
        event.preventDefault();
        onCancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableDialogElements(dialog);
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    dialog.addEventListener("keydown", onKeyDown);
    return () => {
      dialog.removeEventListener("keydown", onKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);

  return dialogRef;
}

function focusableDialogElements(root: HTMLElement): HTMLElement[] {
  const selector = [
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "a[href]",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  return Array.from(root.querySelectorAll<HTMLElement>(selector)).filter((element) => {
    const style = window.getComputedStyle(element);
    return style.visibility !== "hidden" && style.display !== "none";
  });
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

function canResolveRelease(data?: StateResponse | null): boolean {
  const landing = data?.landing;
  if (!landing) return false;
  if (mergeRecoveryNeeded(landing)) return true;
  if (canMerge(landing)) return true;
  return supersededFailedTaskIds(landing).length > 0;
}

function mergeRecoveryNeeded(landing?: LandingState): boolean {
  if (!landing?.merge_blocked) return false;
  const blockers = landing.merge_blockers.join(" ").toLowerCase();
  return blockers.includes("merge in progress") || blockers.includes("unmerged path");
}

function mergeButtonTitle(landing?: LandingState): string {
  if (mergeRecoveryNeeded(landing)) return "Recover or abort the interrupted landing before merging.";
  return landing?.merge_blocked ? "Commit, stash, or revert local project changes before merging." : "";
}

function mergeBlockedText(landing: LandingState): string {
  if (mergeRecoveryNeeded(landing)) return "Landing is blocked by an interrupted git merge. Use Recover landing.";
  const suffix = landing.dirty_files.length ? `: ${landing.dirty_files.slice(0, 3).join(", ")}` : "";
  return `Merge blocked by local changes${suffix}`;
}

function landingBulkConfirmation(landing?: LandingState): string {
  const ready = (landing?.items || []).filter((item) => item.landing_state === "ready");
  const target = landing?.target || "main";
  const taskList = ready.slice(0, 5).map((item) => item.task_id).join(", ");
  const suffix = ready.length > 5 ? `, +${ready.length - 5} more` : "";
  const changed = ready.reduce((sum, item) => sum + Number(item.changed_file_count || 0), 0);
  const collisionCount = landing?.collisions.length || 0;
  const collisionNote = collisionCount
    ? ` ${collisionCount} ready-task collision${collisionCount === 1 ? "" : "s"} detected; Otto will fail safely if git cannot merge them.`
    : "";
  return `Land ${ready.length} ready task${ready.length === 1 ? "" : "s"} into ${target}: ${taskList}${suffix}. This uses transactional fast merge, so ${target} updates only if every branch merges cleanly. It will stage ${changed} changed file${changed === 1 ? "" : "s"} across the ready work.${collisionNote}`;
}

function releaseResolutionConfirmation(data: StateResponse | null): string {
  const landing = data?.landing;
  if (!landing) return "Otto will inspect release state and run the first safe recovery action it can prove.";
  if (mergeRecoveryNeeded(landing)) {
    return "Otto will abort the interrupted git merge, then relaunch conflict-resolving landing for the remaining ready work. This may invoke the configured merge provider.";
  }
  if (canMerge(landing)) {
    return landingBulkConfirmation(landing);
  }
  const cleanup = supersededFailedTaskIds(landing);
  if (cleanup.length) {
    const preview = cleanup.slice(0, 4).join(", ");
    const suffix = cleanup.length > 4 ? `, +${cleanup.length - 4} more` : "";
    return `Otto will clean ${cleanup.length} failed card${cleanup.length === 1 ? "" : "s"} already superseded by landed work: ${preview}${suffix}. Branches and history stay preserved.`;
  }
  return "Otto will inspect release state and report if no safe automated action is available.";
}

function supersededFailedTaskIds(landing?: LandingState): string[] {
  if (!landing) return [];
  const landed = new Set(
    landing.items
      .filter((item) => item.landing_state === "merged")
      .map((item) => summarySignature(item.summary))
      .filter(Boolean),
  );
  if (!landed.size) return [];
  return landing.items
    .filter((item) => ["failed", "interrupted", "cancelled", "stale"].includes(item.queue_status))
    .filter((item) => item.landing_state === "blocked")
    .filter((item) => landed.has(summarySignature(item.summary)))
    .map((item) => item.task_id);
}

function summarySignature(value: string | null | undefined): string {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, " ").slice(0, 500);
}

function isWaitingLandingItem(item: LandingItem): boolean {
  return item.landing_state === "blocked" && ["queued", "starting", "initializing", "running", "terminating"].includes(item.queue_status);
}

function providerLine(detail: RunDetail): string {
  return providerConfigLine(detail.build_config) || [detail.provider, detail.model, detail.reasoning_effort].filter(Boolean).join(" / ") || "-";
}

function providerConfigLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "";
  const buildAgent = primaryAgentConfig(config);
  return [
    buildAgent?.provider || config.provider,
    buildAgent?.model || config.model || "provider default model",
    buildAgent?.reasoning_effort || config.reasoning_effort || "provider default reasoning",
  ]
    .filter(Boolean)
    .join(" / ");
}

function certificationLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  if (config.skip_product_qa) return "Skipped product certification";
  return capitalize(config.certification || `${config.certifier_mode || "fast"} certification`);
}

function planningLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "";
  if (config.planning === "spec_review") return "Spec review gate";
  if (config.planning === "spec_auto") return "Spec auto-approved";
  if (config.planning === "spec_file") return config.spec_file_path ? `Spec file ${config.spec_file_path}` : "Spec file";
  return "";
}

function timeoutLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  return [
    config.queue?.task_timeout_s !== null && config.queue?.task_timeout_s !== undefined
      ? `queue timeout ${formatDuration(config.queue.task_timeout_s)}`
      : "queue timeout disabled",
    config.run_budget_seconds ? `run budget ${formatDuration(config.run_budget_seconds)}` : "",
  ].filter(Boolean).join(" · ");
}

function limitLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  return [
    config.max_certify_rounds ? `${config.max_certify_rounds} cert rounds` : "",
    config.max_turns_per_call ? `${config.max_turns_per_call} max turns/call` : "",
  ].filter(Boolean).join(" · ") || "-";
}

function flagsLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  const flags = [
    config.split_mode ? "split mode" : "agentic mode",
    config.strict_mode ? "strict" : "",
    config.allow_dirty_repo ? "dirty repo allowed" : "",
  ].filter(Boolean);
  return flags.length ? flags.join(" · ") : "default safeguards";
}

function agentsLine(config: RunBuildConfig | null | undefined): string {
  const agents = config?.agents;
  if (!agents) return "-";
  const rows = agentRowsForConfig(config).map(([name, label]) => {
    const agent = agents[name];
    const parts = [agent?.provider, agent?.model, agent?.reasoning_effort].filter(Boolean);
    return `${label}: ${parts.join("/") || "default"}`;
  });
  return rows.join(" · ");
}

function primaryAgentConfig(config: RunBuildConfig): AgentBuildConfig | undefined {
  if (config.command_family === "certify") return config.agents?.certifier;
  if (config.command_family === "improve" && config.split_mode) return config.agents?.fix;
  return config.agents?.build;
}

function agentRowsForConfig(config: RunBuildConfig): Array<["build" | "certifier" | "spec" | "fix", string]> {
  if (config.command_family === "certify") return [["certifier", "certifier"]];
  if (config.command_family === "improve") {
    return config.split_mode
      ? [["certifier", "evaluator"], ["fix", "improver"]]
      : [["build", "improver"]];
  }
  return config.split_mode
    ? [["build", "builder"], ["certifier", "certifier"], ["fix", "fixer"]]
    : [["build", "builder"]];
}

function projectConfigLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  return [
    config.default_branch ? `target ${config.default_branch}` : "",
    config.test_command ? `tests: ${config.test_command}` : "",
    config.queue?.concurrent ? `${config.queue.concurrent} parallel` : "",
    config.queue?.worktree_dir ? `worktrees ${config.queue.worktree_dir}` : "",
    config.queue?.merge_certifier_mode ? `merge cert ${config.queue.merge_certifier_mode}` : "",
  ].filter(Boolean).join(" · ") || "-";
}

function productKindHint(kind: string): string {
  switch (kind) {
    case "web":
      return "Start the web server, open the local URL, and exercise the primary browser workflow.";
    case "api":
      return "Start the API service, call the documented endpoints, and verify response bodies and status codes.";
    case "cli":
      return "Run the CLI help, then execute the main happy-path command and check stdout, stderr, and exit code.";
    case "desktop":
      return "Launch the desktop app and walk through the primary window interaction.";
    case "library":
      return "Import the public package from a fresh script and call the documented API.";
    case "worker":
    case "service":
    case "pipeline":
      return "Run the process with a small fixture and verify its output, side effects, and logs.";
    default:
      return "Use the README and artifacts to run the product's main user workflow.";
  }
}

function shortPath(path: string | null | undefined): string {
  if (!path) return "-";
  const parts = path.split("/");
  if (parts.length <= 4) return path;
  return `.../${parts.slice(-3).join("/")}`;
}

function detailStatusLabel(detail: RunDetail): string {
  const readiness = detail.review_packet.readiness.state;
  if (readiness === "blocked" || readiness === "merged") return readiness;
  return detail.display_status || "-";
}

function actionName(key: string): string {
  return {c: "cancel", r: "resume", R: "retry", x: "cleanup", m: "merge", M: "merge-all", a: "approve-spec", g: "regenerate-spec"}[key] || key;
}

function actionConfirmationBody(action: string, label?: string): string {
  const normalized = (label || action).toLowerCase();
  if (action === "cancel") return "Cancel this run?";
  if (action === "merge") return "Land this task into the target branch?";
  if (action === "approve-spec") return "Approve this spec and start the build?";
  if (action === "regenerate-spec") return "Request spec changes and regenerate before build starts?";
  if (normalized === "remove") return "Remove this queue task?";
  if (normalized === "cleanup") return "Clean up this run?";
  if (normalized === "requeue") return "Requeue this task?";
  if (normalized.startsWith("resume")) return "Resume this run from the saved checkpoint?";
  return `${capitalize(normalized)} this run?`;
}

function proofLine(item: LandingItem): string {
  const passed = Number(item.stories_passed || 0);
  const tested = Number(item.stories_tested || 0);
  if (tested) return `${passed}/${tested} stories`;
  if (item.queue_status) return item.queue_status;
  return "-";
}

function evidenceLine(packet: RunDetail["review_packet"]): string {
  if (packet.readiness.state === "in_progress") return "-";
  if (isRepositoryBlockedPacket(packet)) return "-";
  const reviewEvidence = packet.evidence.filter(isReviewEvidenceArtifact);
  const existing = reviewEvidence.filter(isReadableArtifact).length;
  if (!reviewEvidence.length) return "-";
  if (!existing) return "not attached";
  return `${existing}/${reviewEvidence.length}`;
}

function preferredProofArtifact(artifacts: ArtifactRef[]): ArtifactRef | null {
  const existing = artifacts.filter(isReadableArtifact);
  if (!existing.length) return null;
  const preferredLabels = ["proof markdown", "proof json", "summary", "queue manifest", "manifest", "primary log", "intent"];
  for (const label of preferredLabels) {
    const match = existing.find((artifact) => artifact.label.toLowerCase() === label);
    if (match) return match;
  }
  return existing[0] || null;
}

function canShowDiff(detail: RunDetail | null): boolean {
  if (!detail) return false;
  const packet = detail.review_packet;
  if (!packet.changes.branch || packet.changes.diff_error) return false;
  return packet.readiness.state !== "in_progress";
}

function isReadableArtifact(artifact: ArtifactRef): boolean {
  return artifact.exists && artifact.kind !== "directory";
}

function isReviewEvidenceArtifact(artifact: ArtifactRef): boolean {
  return artifact.kind !== "directory";
}

function isLogArtifact(artifact: ArtifactRef | null): boolean {
  if (!artifact) return false;
  const kind = artifact.kind.toLowerCase();
  const label = artifact.label.toLowerCase();
  const path = artifact.path.toLowerCase();
  return kind === "log" || label.includes("log") || path.endsWith(".log");
}

function artifactKindLabel(artifact: ArtifactRef): string {
  if (!artifact.exists) return `${artifact.kind} (missing)`;
  if (artifact.kind === "directory") return "directory - use Diff for code review";
  if (artifact.kind === "html") return "HTML report";
  if (artifact.kind === "json") return "JSON metadata";
  if (artifact.kind === "text") return "readable text";
  if (artifact.kind === "image") return "image evidence";
  if (artifact.kind === "video") return "video evidence";
  return artifact.kind;
}

function formatArtifactContent(content: string): string {
  const trimmed = content.trim();
  if (!trimmed) return "";
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return content;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return content;
  }
}

function renderDiffText(text: string) {
  return text.split(/(\n)/).map((part, index) => {
    if (part === "\n") return part;
    const className = diffLineClass(part);
    return <span className={className} key={`${index}-${part.slice(0, 12)}`}>{part}</span>;
  });
}

interface DiffFileSection {
  path: string;
  text: string;
}

function splitDiffIntoFiles(text: string, files: string[]): DiffFileSection[] {
  const sections: DiffFileSection[] = [];
  let current: DiffFileSection | null = null;
  const lines = text ? text.split("\n") : [];
  for (const line of lines) {
    const match = line.match(/^diff --git a\/(.*) b\/(.*)$/);
    if (match) {
      current = {path: match[2] || match[1] || `file-${sections.length + 1}`, text: line};
      sections.push(current);
      continue;
    }
    if (current) {
      current.text += `\n${line}`;
    }
  }
  if (sections.length) return sections;
  if (text.trim()) return [{path: files[0] || "diff", text}];
  return files.map((path) => ({path, text: ""}));
}

function diffLineClass(line: string): string {
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")) return "diff-meta";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-context";
}

function renderLogText(text: string) {
  const lines = text.split("\n");
  return lines.map((line, index) => (
    <span className={`log-line ${logLineClass(line)}`} key={`log-${index}`}>
      {line ? renderAnsiText(line) : ""}
      {index < lines.length - 1 ? "\n" : ""}
    </span>
  ));
}

function logLineClass(line: string): string {
  const clean = stripAnsi(line).toLowerCase();
  if (/(— .*starting —|— .*complete —|certify round|fix round|run summary|━━━)/.test(clean)) return "log-line-phase";
  if (/(\bfatal\b|\berror\b|traceback|exception|\bfailed\b|\bfail\b|exit code [1-9])/.test(clean)) return "log-line-error";
  if (/(\bwarn\b|warning|blocked|stale|retry|skipped|caution)/.test(clean)) return "log-line-warn";
  if (/(\bpass\b|passed|success|completed|ready|done)/.test(clean)) return "log-line-success";
  if (/(story_result|story result|stories_tested|stories_passed|verdict|diagnosis|coverage_observed|coverage_gaps|pytest|npm|uv run|\btest\b|collecting|running|\[build\]|\[certify\]|\[merge\]|\[queue\]|\binfo\b)/.test(clean)) return "log-line-info";
  return "log-line-muted";
}

function stripAnsi(text: string): string {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

function renderAnsiText(text: string) {
  const segments: ReactNode[] = [];
  const pattern = /\x1b\[([0-9;]*)m/g;
  let lastIndex = 0;
  let style: {fg: string; bold: boolean} = {fg: "", bold: false};
  let key = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > lastIndex) {
      appendAnsiSegment(segments, text.slice(lastIndex, match.index), style, key++);
    }
    style = applyAnsiCodes(style, match[1] || "0");
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    appendAnsiSegment(segments, text.slice(lastIndex), style, key++);
  }
  return segments.length ? segments : text;
}

function appendAnsiSegment(segments: ReactNode[], text: string, style: {fg: string; bold: boolean}, key: number) {
  if (!text) return;
  const className = [style.fg ? `ansi-${style.fg}` : "", style.bold ? "ansi-bold" : ""].filter(Boolean).join(" ");
  if (!className) {
    segments.push(text);
    return;
  }
  segments.push(<span className={className} key={`ansi-${key}`}>{text}</span>);
}

function applyAnsiCodes(current: {fg: string; bold: boolean}, rawCodes: string): {fg: string; bold: boolean} {
  const codes = rawCodes.split(";").filter(Boolean).map((code) => Number(code));
  if (!codes.length) return {fg: "", bold: false};
  let next = {...current};
  for (const code of codes) {
    if (code === 0) next = {fg: "", bold: false};
    else if (code === 1) next.bold = true;
    else if (code === 22) next.bold = false;
    else if (code === 39) next.fg = "";
    else if (ANSI_COLOR_CLASS[code]) next.fg = ANSI_COLOR_CLASS[code];
  }
  return next;
}

const ANSI_COLOR_CLASS: Record<number, string> = {
  30: "black",
  31: "red",
  32: "green",
  33: "yellow",
  34: "blue",
  35: "magenta",
  36: "cyan",
  37: "white",
  90: "gray",
  91: "red",
  92: "green",
  93: "yellow",
  94: "blue",
  95: "magenta",
  96: "cyan",
  97: "white",
};

function isRepositoryBlockedPacket(packet: RunDetail["review_packet"]): boolean {
  return packet.readiness.blockers.some((blocker) => blocker.startsWith("Repository has local changes"));
}

function runEventText(item: LiveRunItem, landingByTask: Map<string, LandingItem>): string {
  const landingItem = item.queue_task_id ? landingByTask.get(item.queue_task_id) : undefined;
  if (landingItem?.landing_state === "ready") return "Ready for review";
  if (landingItem?.landing_state === "merged") return "Landed";
  if (landingItem && isWaitingLandingItem(landingItem)) return landingItem.queue_status === "queued" ? "Queued" : "In progress";
  if (String(item.last_event || "").toLowerCase() === "legacy queue mode") return "Queue task";
  return item.last_event || "-";
}

function landingStateText(item: LandingItem): string {
  if (item.landing_state === "ready") return "Ready to land";
  if (item.landing_state === "merged") return "Landed";
  if (isWaitingLandingItem(item)) return item.queue_status === "queued" ? "Queued" : "In progress";
  return item.label || "Needs action";
}

function diagnosticLandingAction(item: LandingItem): string {
  if (item.landing_state === "ready") return `${changeLine(item)} changed; review evidence before landing.`;
  if (item.landing_state === "merged") return item.merge_id ? `Landed by ${item.merge_id}.` : "Already landed.";
  if (item.queue_status === "queued") return "Start the watcher to run this task.";
  if (item.queue_status === "failed") return "Open review packet and requeue or remove.";
  if (item.queue_status === "stale") return "Open review packet and remove stale work.";
  if (item.diff_error) return formatTechnicalIssue(item.diff_error);
  return "Open review packet for next action.";
}

function changeLine(item: LandingItem): string {
  if (item.diff_error) return "diff error";
  const count = Number(item.changed_file_count || 0);
  if (!count) return "-";
  return `${count} file${count === 1 ? "" : "s"}`;
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

function storiesLine(packet: RunDetail["review_packet"]): string {
  const tested = Number(packet.certification.stories_tested || 0);
  const passed = Number(packet.certification.stories_passed || 0);
  return tested ? `${passed}/${tested}` : "-";
}

function reviewActionLabel(label: string): string {
  const normalized = label.toLowerCase();
  if (normalized === "merge selected") return "Land selected";
  if (normalized === "cleanup") return "Clean run record";
  if (normalized === "remove") return "Remove task";
  return normalized.includes("merge") ? "Land task" : capitalize(label);
}

function formatReviewText(message: string): string {
  return formatTechnicalIssue(message);
}

function userVisibleDetailLine(line: string): string | null {
  const normalized = line.toLowerCase();
  if (normalized.startsWith("compat:")) return null;
  return line.replace("legacy queue mode", "queue compatibility mode");
}

function formatTechnicalIssue(message: string): string {
  const value = message.trim();
  if (/unknown revision|ambiguous argument|bad revision|invalid object name/i.test(value)) {
    return "Changed files could not be inspected because the source branch is missing or not reachable. Refresh after the task creates its branch, or remove and requeue the task.";
  }
  if (/working tree has|unstaged changes|uncommitted changes/i.test(value)) {
    return "Repository has local changes. Commit, stash, or revert them before landing.";
  }
  return value;
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

function storyStatusLabel(status: string): string {
  return {
    pass: "Pass",
    warn: "Warn",
    fail: "Fail",
    skipped: "Skip",
    unknown: "Info",
  }[status] || capitalize(status || "info");
}

function storyStatusClass(status: string): string {
  const normalized = String(status || "unknown").toLowerCase();
  if (["pass", "warn", "fail", "skipped"].includes(normalized)) return normalized;
  return "unknown";
}

function shortText(value: string, maxLength: number): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3))}...`;
}

function compactLongText(value: string, maxLength: number): {text: string; truncated: boolean} {
  if (value.length <= maxLength) return {text: value, truncated: false};
  const tail = value.slice(-maxLength);
  const firstLineBreak = tail.indexOf("\n");
  const lineAlignedTail = firstLineBreak >= 0 ? tail.slice(firstLineBreak + 1) : tail;
  const visibleLines = lineAlignedTail ? lineAlignedTail.split(/\n/).filter((line) => line.length > 0).length : 0;
  return {
    text: `[showing latest ${visibleLines.toLocaleString()} complete lines]\n\n${lineAlignedTail}`,
    truncated: true,
  };
}

function capitalize(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : value;
}

function refreshLabel(status: string): string {
  if (status === "refreshing") return "refreshing";
  if (status === "error") return "refresh failed";
  return "";
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "Unknown error");
}

function detailWasRemoved(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}
