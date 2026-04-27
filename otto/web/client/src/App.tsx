import {FormEvent, useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent, ReactNode} from "react";
import {ApiError, api, buildQueuePayload, friendlyApiMessage, runDetailUrl, stateQueryParams} from "./api";
import {Spinner} from "./components/Spinner";
import type {PillTone} from "./components/Pill";
import {BrandMark} from "./components/BrandMark";
import {HelpOverlay} from "./components/HelpOverlay";
import {LauncherExplainer} from "./components/launcher/LauncherExplainer";
import {ToastDisplay} from "./components/ToastDisplay";
import {TaskQueueList} from "./components/tasks/TaskQueueList";
import {TopBar} from "./components/topbar/TopBar";
import {Toolbar} from "./components/toolbar/Toolbar";
import {ProjectLauncher} from "./components/launcher/ProjectLauncher";
import {OperationalOverview, ProjectOverview} from "./components/overview/Overview";
import {RecentActivity, LiveRuns} from "./components/overview/ActivityPanels";
import {SystemHealth, DiagnosticsSummary} from "./components/health/SystemHealth";
import {History} from "./components/history/History";
import {RunDetailPanel, RunInspector} from "./components/inspector/RunInspector";
import {JobDialog, collectPriorRunOptions} from "./components/new-job/JobDialog";
import {
  BulkLandingConfirmList,
  SingleMergeConfirmDetails,
  describeCancelConfirm,
  describeCleanupConfirm,
  describeWatcherStopConfirm,
} from "./components/review/ConfirmDetails";
import {CommandPalette} from "./components/CommandPalette";
import {InertEffect, LiveRegion} from "./components/a11y";
import {ConfirmDialog, type ConfirmState} from "./components/ConfirmDialog";
import {EventTimeline} from "./components/EventTimeline";
import {
  CommandList,
  FocusMetric,
  HealthCard,
  MetaItem,
  OverviewMetric,
  ProjectStatCard,
  ReviewDrawer,
  ReviewMetric,
} from "./components/MicroComponents";
import {useInFlight} from "./hooks/useInFlight";
import {useDebouncedValue} from "./hooks/useDebouncedValue";
import {useCrossTabChannel} from "./hooks/useCrossTabChannel";
import {useToastController} from "./hooks/useToastController";
import {
  LOG_BUFFER_MAX_BYTES,
  LOG_POLL_BACKOFF_MS,
  LOG_POLL_BASE_MS,
  appendToLogBuffer,
  bytesToString,
  countLines,
  initialLogState,
  type LogState,
  type LogStatus,
} from "./logBuffer";
import {
  capitalize,
  configSourceLabel,
  formatCompactNumber,
  formatDiffTruncationBanner,
  formatDuration,
  formatEventTime,
  formatRelativeFreshness,
  formatTechnicalIssue,
  humanBytes,
  refreshLabel,
  shortText,
  storiesLine,
  storyTotalsFromLanding,
  titleCase,
  tokenBreakdownLine,
  tokenTotal,
  usageLine,
} from "./utils/format";
import type {
  ActionResult,
  ActionState,
  AgentBuildConfig,
  ArtifactContentResponse,
  ArtifactRef,
  CertificationPolicy,
  CertificationRound,
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
  ProductHandoff,
  ProjectMutationResponse,
  ProjectsResponse,
  ProofReportInfo,
  QueueResult,
  RunBuildConfig,
  RunDetail,
  RunTypeFilter,
  StateResponse,
  WatcherInfo,
} from "./types";
import {DEFAULT_HISTORY_PAGE_SIZE, defaultFilters, HISTORY_PAGE_SIZE_OPTIONS} from "./uiTypes";
import type {
  BoardTask,
  Filters,
  HistorySortColumn,
  HistorySortDir,
  InspectorMode,
  ResultBannerState,
  ViewMode,
} from "./uiTypes";
import {
  actionConfirmationBody,
  actionToastSeverity,
  applyOptimisticRunStates,
  canMerge,
  canResolveRelease,
  canShowDiff,
  dedupeLiveAgainstHistory,
  detailWasRemoved,
  errorMessage,
  handleActionResult,
  isTypingTarget,
  landingBulkConfirmation,
  mergeBlockedText,
  mergeButtonTitle,
  mergeConfirmationBody,
  mergeRecoveryNeeded,
  activeCount,
  preferredProofArtifact,
  releaseResolutionConfirmation,
  requestNotificationPermissionOnce,
  selectOnKeyboard,
  supersededFailedTaskIds,
  useDocumentTitle,
  useLiveAnnouncement,
  useNotificationsOnRunFinish,
  visibleRunIds,
  watcherControlHint,
  refreshIntervalMs,
} from "./utils/missionControl";
import {defaultRouteState, readRouteState, writeRouteState} from "./routeState";
import type {RouteState} from "./routeState";

// W9-CRITICAL-1: cadence used for `/api/state` polling while the tab is
// hidden. We don't STOP polling (otherwise notification gates and live
// state go stale for the whole hide window — see live-findings W9), we
// just slow it down so a backgrounded tab doesn't hammer the server while
// the user is elsewhere. Browser timer-throttling already brings this
// close to once-per-minute when truly hidden, but we set it explicitly
// so the resume catch-up is bounded.
const STATE_POLL_HIDDEN_MS = 30_000;
// W9-CRITICAL-1: hard floor between consecutive `/api/state` polls.
// Defends against the 175-poll-in-10s burst that browsers fire when they
// unthrottle a previously-hidden tab — without this, accumulated state
// updates / re-rendered effects can re-fire `refresh()` in a tight loop.
// 1s is well below LOG_POLL_BASE_MS / refreshIntervalMs() floor (700ms)
// so it never throttles the steady-state cadence in practice; it only
// gates the burst.
const STATE_POLL_MIN_GAP_MS = 1000;

// Codex error-empty-states #1: number of consecutive `/api/state` poll
// failures required before the connection-lost banner appears. Three
// failures means the user has waited ~3 poll intervals (≈4–15s depending
// on cadence) before we surface a sticky "Lost connection" banner —
// long enough to ride out a single hiccup, short enough to be actionable
// before the operator notices the page is stale.
const CONNECTION_LOST_THRESHOLD = 3;

export function App() {
  const initialRoute = useMemo(() => readRouteState(), []);
  // Heavy-user paper-cut #1: hydrate filters from the initial URL so refresh,
  // back-forward, and shared deep-links land on the same filtered view.
  const [filters, setFilters] = useState<Filters>(() => ({
    type: initialRoute.filterType,
    outcome: initialRoute.filterOutcome,
    query: initialRoute.filterQuery,
    activeOnly: initialRoute.filterActiveOnly,
  }));
  const [data, setData] = useState<StateResponse | null>(null);
  const [projectsState, setProjectsState] = useState<ProjectsResponse | null>(null);
  // `projectsLoaded` flips true the first time `/api/projects` resolves
  // (success or recoverable failure). Until then we render a boot-loading
  // placeholder instead of either the launcher OR the main shell — see
  // mc-audit codex-first-time-user.md #1: before /api/projects returns,
  // projectsState is null so the launcher gate fails and the main shell
  // can render with an enabled "New job" button + project undefined.
  const [projectsLoaded, setProjectsLoaded] = useState<boolean>(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRoute.selectedRunId);
  // mc-audit codex-first-time-user #19: when a queued task has no run_id yet
  // (waiting for the watcher to spawn it), the user should still be able to
  // click the card to inspect *what* is queued. We store a "selected queued
  // task" hint here and the RunDetailPanel renders a "Waiting for watcher"
  // placeholder. Cleared when a real run is selected or polling resolves it.
  const [selectedQueuedTask, setSelectedQueuedTask] = useState<BoardTask | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logState, setLogState] = useState<LogState>(initialLogState);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorMode, setInspectorMode] = useState<InspectorMode>("proof");
  const [selectedArtifactIndex, setSelectedArtifactIndex] = useState<number | null>(null);
  const [artifactContent, setArtifactContent] = useState<ArtifactContentResponse | null>(null);
  const [proofArtifactIndex, setProofArtifactIndex] = useState<number | null>(null);
  const [proofContent, setProofContent] = useState<ArtifactContentResponse | null>(null);
  const [diffContent, setDiffContent] = useState<DiffResponse | null>(null);
  const [refreshStatus, setRefreshStatus] = useState("idle");
  // Codex error-empty-states #1: count of consecutive `/api/state` poll
  // failures. When this hits ``CONNECTION_LOST_THRESHOLD`` we render a
  // sticky banner ("Lost connection to Mission Control. Retrying every
  // 5s…") with a manual retry button. A successful refresh resets it to
  // 0 and the banner disappears (restore-on-reconnect).
  const [stateFailureStreak, setStateFailureStreak] = useState(0);
  const [jobOpen, setJobOpen] = useState(false);
  const {
    toast,
    lastError,
    setLastError,
    dismissToast,
    pauseToastDismiss,
    resumeToastDismiss,
    showToast,
  } = useToastController();
  const [resultBanner, setResultBanner] = useState<ResultBannerState | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  // Inline error rendered inside the confirm dialog when the action POST
  // fails. Dialog STAYS OPEN so the user can read the server reason and
  // retry / close. mc-audit codex-destructive-action-safety #6 — the prior
  // behavior swallowed 4xx as a successful confirm and silently closed.
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [confirmCheckboxAck, setConfirmCheckboxAck] = useState(false);
  // Synchronous lock for the confirm dialog. `confirmPending` is React state
  // and updates asynchronously; a fast double-click on the modal Confirm
  // button can read `pending=false` for both clicks. The ref is checked and
  // set in the same microtask, eliminating the duplicate POST race that
  // mc-audit codex-state-management #10 documented.
  const confirmLockRef = useRef(false);
  // Forward-ref to `refresh` so callbacks defined BEFORE refresh's
  // useCallback can still trigger a state refresh without forming a
  // dependency cycle. We populate the ref in a useEffect below the
  // refresh declaration. Used by `executeConfirmedAction` after a
  // failed action POST per mc-audit codex-destructive-action-safety #6.
  const refreshRef = useRef<((showStatus?: boolean) => Promise<void>) | null>(null);
  // W10-CRITICAL-1/2: ref for the cross-tab BroadcastChannel publisher.
  // Wired by ``useCrossTabChannel`` once the hook mounts; mutation
  // callbacks (executeAction, mergeReadyTasks, runWatcherAction, queue
  // submit) read it through this ref so they don't need to be in
  // execution-order dependency-graph order with the hook.
  const crossTabPublishRef = useRef<((kind: import("./hooks/useCrossTabChannel").MutationKind, opts?: {runId?: string; taskId?: string}) => void) | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>(initialRoute.viewMode);
  // Heavy-user paper-cut #4 (Cmd-K palette). Hidden by default, opened by
  // Cmd/Ctrl+K from anywhere on the page (skipped while a modal is open so
  // we don't stack a palette on top of a confirm dialog).
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const watcherInFlight = useInFlight();
  const refreshInFlight = useInFlight();
  const mergeAllInFlight = useInFlight();
  // mc-audit microinteractions I4: optimistic UI for high-latency actions.
  // When the operator confirms a cancel, we immediately overlay
  // display_status="cancelling" on the affected row so the card flips state
  // within ~16ms instead of waiting for the next /api/state poll (up to
  // refresh_interval_s seconds). On POST failure, we drop the overlay so
  // the row reverts to its server-provided state.
  const [optimisticRunStates, setOptimisticRunStates] = useState<Record<string, "cancelling">>({});
  // Server byte-offset for the next log fetch. Tracked in a ref because the
  // poll loop reads-modifies it from inside a setInterval callback that must
  // not retrigger React effects.
  const logOffsetRef = useRef(0);
  // Mirror of logState.text — the poll loop computes the next buffer
  // synchronously without going through React state, so the next chunk's
  // append doesn't race against an unflushed state update.
  const logTextRef = useRef("");
  // setTimeout id for the recurring poll, plus a flag for whether the tab is
  // currently visible. The poll is rescheduled at variable cadence to
  // implement exponential backoff on errors, so we keep the id outside React.
  const logPollTimeoutRef = useRef<number | null>(null);
  const logPollVisibleRef = useRef(true);
  // W9-CRITICAL-1: state-poll timer book-keeping. Single timer ref ensures
  // a re-rendered effect cannot stack a second `setInterval` alongside the
  // first (which is how visibility-restore previously fired multi-hundred
  // request bursts). `statePollVisibleRef` mirrors document.visibilityState
  // so the recursive scheduler can pick the right cadence without forcing
  // a re-render. `statePollLastAtRef` enforces a 1s minimum gap between
  // polls (`STATE_POLL_MIN_GAP_MS`) — defense-in-depth against any future
  // code path that calls into the poll loop too eagerly.
  const statePollTimerRef = useRef<number | null>(null);
  const statePollVisibleRef = useRef(true);
  const statePollLastAtRef = useRef<number>(0);
  const statePollAbortRef = useRef<AbortController | null>(null);
  const selectedRunIdRef = useRef<string | null>(initialRoute.selectedRunId);
  const viewModeRef = useRef<ViewMode>(initialRoute.viewMode);
  // History pagination state. We keep refs alongside the state so the
  // route-writer (which is shared with selectedRunId / viewMode) sees the
  // current value without having to re-derive every dependency. 1-based.
  const [historyPage, setHistoryPage] = useState<number>(initialRoute.historyPage);
  const [historyPageSize, setHistoryPageSize] = useState<number>(
    initialRoute.historyPageSize ?? DEFAULT_HISTORY_PAGE_SIZE,
  );
  const historyPageRef = useRef<number>(initialRoute.historyPage);
  const historyPageSizeRef = useRef<number>(initialRoute.historyPageSize ?? DEFAULT_HISTORY_PAGE_SIZE);

  // Heavy-user paper-cut #2 (history sort). Sort is applied client-side over
  // the current page; server-side sort would be more honest across all
  // pages, but page-local sort covers the common "scan this page for the
  // heaviest run" case. Refs let writeRouteState read the current value
  // without forming a dep cycle, mirroring viewModeRef / historyPageRef.
  const [historySort, setHistorySort] = useState<HistorySortColumn | null>(initialRoute.historySort);
  const [historySortDir, setHistorySortDir] = useState<HistorySortDir | null>(initialRoute.historySortDir);
  const historySortRef = useRef<HistorySortColumn | null>(initialRoute.historySort);
  const historySortDirRef = useRef<HistorySortDir | null>(initialRoute.historySortDir);
  // Filter refs — needed because writeRouteState reads from currentRouteState
  // and the filter state isn't in the existing refs scope. Without this the
  // route writer would see a stale filter set whenever a non-filter action
  // (e.g. selectRun) wrote the URL right after a filter change.
  const filtersRef = useRef<Filters>(filters);
  useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);

  // currentRouteState is the single source of truth for what the URL reflects;
  // every push/replace passes through it so we don't accidentally drop a
  // param that other code paths persist. The page-size is only persisted
  // when it differs from the default — see writeRouteState's `?ps=` rules.
  const currentRouteState = useCallback((): RouteState => ({
    viewMode: viewModeRef.current,
    selectedRunId: selectedRunIdRef.current,
    historyPage: historyPageRef.current,
    historyPageSize: historyPageSizeRef.current === DEFAULT_HISTORY_PAGE_SIZE ? null : historyPageSizeRef.current,
    filterType: filtersRef.current.type,
    filterOutcome: filtersRef.current.outcome,
    filterQuery: filtersRef.current.query,
    filterActiveOnly: filtersRef.current.activeOnly,
    historySort: historySortRef.current,
    historySortDir: historySortDirRef.current,
  }), []);

  useEffect(() => {
    selectedRunIdRef.current = selectedRunId;
  }, [selectedRunId]);

  useEffect(() => {
    viewModeRef.current = viewMode;
  }, [viewMode]);

  useEffect(() => {
    historyPageRef.current = historyPage;
  }, [historyPage]);

  useEffect(() => {
    historyPageSizeRef.current = historyPageSize;
  }, [historyPageSize]);

  // ---- W3-CRITICAL-2: deterministic shell-ready marker for external automation.
  //
  // Without this, the only public boot signal was "#root has children" — but
  // the boot-loading skeleton renders into #root itself, so external probes
  // (Playwright, MCP tools, smoke harnesses) can race the SPA and click
  // controls that do not exist yet (the live W3 dogfood lost a $0 build that
  // way). We expose two equivalent probes:
  //   * `[data-mc-shell="ready"]` on the top-level shell wrapper — covers
  //     DOM-attribute selectors (Playwright `wait_for_selector`, the codex
  //     audit harness, third-party tooling).
  //   * `window.__OTTO_MC_READY = true` — covers headless/eval contexts that
  //     don't have data-attribute access (page.evaluate, jsdom snapshots).
  //
  // The marker flips ONLY after BOTH /api/projects and /api/state have
  // resolved AND the boot-loading gate (cluster F) has cleared. While the
  // launcher placeholder is showing the marker stays unset — the launcher
  // is its own destination, not the actionable Mission Control shell.
  const mcShellReady =
    projectsLoaded && !!data && !!data.project && !projectsState?.launcher_enabled;

  useEffect(() => {
    if (typeof window === "undefined") return;
    // Use a typed cast for the ambient property — TypeScript strict mode
    // disallows expandos on Window without a declaration. The shape is
    // small enough (single boolean) that a cast is clearer than a global
    // augmentation file for one flag.
    const w = window as unknown as {__OTTO_MC_READY?: boolean};
    if (mcShellReady) {
      w.__OTTO_MC_READY = true;
    } else {
      // Reset when the shell un-readies (e.g. project switch routes us back
      // through the boot gate). External probes that polled and missed must
      // NOT see a stale `true`.
      w.__OTTO_MC_READY = false;
    }
    return () => {
      w.__OTTO_MC_READY = false;
    };
  }, [mcShellReady]);

  useEffect(() => {
    writeRouteState(currentRouteState(), "replace");
    const onPopState = () => {
      const next = readRouteState();
      viewModeRef.current = next.viewMode;
      selectedRunIdRef.current = next.selectedRunId;
      historyPageRef.current = next.historyPage;
      const nextSize = next.historyPageSize ?? DEFAULT_HISTORY_PAGE_SIZE;
      historyPageSizeRef.current = nextSize;
      historySortRef.current = next.historySort;
      historySortDirRef.current = next.historySortDir;
      const nextFilters: Filters = {
        type: next.filterType,
        outcome: next.filterOutcome,
        query: next.filterQuery,
        activeOnly: next.filterActiveOnly,
      };
      filtersRef.current = nextFilters;
      setViewMode(next.viewMode);
      setSelectedRunId(next.selectedRunId);
      setHistoryPage(next.historyPage);
      setHistoryPageSize(nextSize);
      setHistorySort(next.historySort);
      setHistorySortDir(next.historySortDir);
      // Heavy-user paper-cut #1: restore filters on Back/Forward so a power
      // user navigating from a filtered list into a run detail and hitting
      // Back lands on the same filtered list, not on defaults.
      setFilters(nextFilters);
      setInspectorOpen(false);
      setJobOpen(false);
      setConfirm(null);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [currentRouteState]);

  const navigateView = useCallback((nextView: ViewMode) => {
    if (nextView === viewModeRef.current) return;
    viewModeRef.current = nextView;
    setViewMode(nextView);
    setInspectorOpen(false);
    writeRouteState(currentRouteState(), "push");
  }, [currentRouteState]);

  const requestConfirm = useCallback((next: ConfirmState) => {
    // Reset transient per-confirm state so a stale error or checkbox tick
    // from a previous dialog does not leak into the new one.
    setConfirmError(null);
    setConfirmCheckboxAck(false);
    setConfirm(next);
  }, []);

  const dismissConfirm = useCallback(() => {
    if (confirmPending) return;
    setConfirm(null);
    setConfirmError(null);
    setConfirmCheckboxAck(false);
  }, [confirmPending]);

  const openJobDialog = useCallback(() => {
    setInspectorOpen(false);
    setJobOpen(true);
    // Heavy-user paper-cut #3: lazy-request notification permission on
    // first user-initiated action. The browser only allows the prompt as a
    // direct response to a user gesture; tying it to "open job dialog" /
    // "start watcher" gives us natural triggers without a popup-on-load.
    requestNotificationPermissionOnce();
  }, []);

  const selectRun = useCallback((runId: string) => {
    setInspectorOpen(false);
    if (runId !== selectedRunIdRef.current) {
      setDetail(null);
      logOffsetRef.current = 0;
      logTextRef.current = "";
      setLogState(initialLogState);
      setArtifactContent(null);
      setProofContent(null);
      setDiffContent(null);
      setProofArtifactIndex(null);
      setSelectedArtifactIndex(null);
      setInspectorMode("proof");
    }
    selectedRunIdRef.current = runId;
    setSelectedRunId(runId);
    // Clear queued-task hint — real run selection wins.
    setSelectedQueuedTask(null);
    writeRouteState(currentRouteState(), "push");
  }, [currentRouteState]);

  // mc-audit codex-first-time-user #19: select a queued task that has no
  // runId yet. We don't change selectedRunId (no run exists), but the
  // detail panel reads selectedQueuedTask to render a "waiting" placeholder.
  const selectQueuedTask = useCallback((task: BoardTask) => {
    setInspectorOpen(false);
    setSelectedRunId(null);
    selectedRunIdRef.current = null;
    setDetail(null);
    setSelectedQueuedTask(task);
  }, []);

  // mc-audit microinteractions I4: drop optimistic "cancelling" overlays
  // once the server reflects a terminal/cancelled state for that run, so
  // the data stops being shadowed after the refresh confirms the action.
  useEffect(() => {
    if (!data) return;
    setOptimisticRunStates((prev) => {
      const keys = Object.keys(prev);
      if (!keys.length) return prev;
      const liveById = new Map((data.live.items || []).map((item) => [item.run_id, item]));
      let changed = false;
      const next = {...prev};
      for (const runId of keys) {
        const live = liveById.get(runId);
        // No live row for this id (server already removed it) → drop.
        if (!live) {
          delete next[runId];
          changed = true;
          continue;
        }
        // Server has caught up to a terminal/cancelled status → drop overlay.
        const status = (live.display_status || "").toLowerCase();
        if (
          status === "cancelled" ||
          status === "cancelling" ||
          status === "terminating" ||
          status === "failed" ||
          status === "done" ||
          status === "success"
        ) {
          delete next[runId];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [data]);

  // Auto-promote selectedQueuedTask -> selectedRunId once the watcher picks
  // up the task and a real run materializes. Without this, the user stays
  // on the "waiting for watcher" placeholder even after the run is live.
  // We watch `data` for a live row whose queue_task_id matches the queued
  // task's id, then call selectRun on that real run id.
  useEffect(() => {
    if (!selectedQueuedTask || !data) return;
    const live = data.live.items.find(
      (item) => item.queue_task_id === selectedQueuedTask.id && item.run_id,
    );
    if (live) {
      selectRun(live.run_id);
    }
  }, [data, selectedQueuedTask, selectRun]);

  const executeConfirmedAction = useCallback(async () => {
    // `confirmLockRef` is the synchronous half of the dedup. `confirmPending`
    // drives the visual disabled state but cannot block a click that arrived
    // in the same React batch as the previous one.
    if (!confirm || confirmLockRef.current) return;
    if (confirm.requireCheckbox && !confirmCheckboxAck) return;
    confirmLockRef.current = true;
    setConfirmPending(true);
    setConfirmError(null);
    try {
      await confirm.onConfirm();
      // Success path: clear dialog + transient state.
      setConfirm(null);
      setConfirmError(null);
      setConfirmCheckboxAck(false);
    } catch (error) {
      // Action POST failed (4xx/5xx, network, etc). Per
      // mc-audit codex-destructive-action-safety #6 we MUST NOT close the
      // dialog and we MUST NOT swallow the error as success. Surface the
      // server's reason inline in the dialog footer; user can read it,
      // retry, or close. We also kick a state refresh so the surrounding
      // app reflects the now-known server state (action may no longer be
      // applicable, e.g. cancelled by another tab). `refresh` is hoisted
      // through a ref so this callback does not depend on its identity.
      setConfirmError(errorMessage(error));
      const refreshFn = refreshRef.current;
      if (refreshFn) {
        void refreshFn(false).catch(() => {/* best-effort */});
      }
    } finally {
      confirmLockRef.current = false;
      setConfirmPending(false);
    }
  }, [confirm, confirmCheckboxAck, showToast]);

  const loadLogs = useCallback(async (runId: string, reset = false) => {
    if ((inspectorMode !== "logs" || !inspectorOpen) && !reset) return;
    if (reset) {
      logOffsetRef.current = 0;
      logTextRef.current = "";
      setLogState({...initialLogState, status: "loading"});
    } else {
      setLogState((prev) => (prev.status === "idle" ? {...prev, status: "loading"} : prev));
    }
    const offset = reset ? 0 : logOffsetRef.current;
    try {
      const logs = await api<LogsResponse>(`/api/runs/${encodeURIComponent(runId)}/logs?offset=${offset}`);
      if (selectedRunIdRef.current !== runId) return;
      logOffsetRef.current = typeof logs.next_offset === "number" ? logs.next_offset : offset;
      // Build the next buffer synchronously off `logTextRef` so concurrent
      // reset+poll cycles do not race over the React state update.
      const baseText = reset ? "" : logTextRef.current;
      const incoming = logs.text || "";
      const {text: nextText, droppedBytes: newlyDropped} = appendToLogBuffer(baseText, incoming, LOG_BUFFER_MAX_BYTES);
      logTextRef.current = nextText;
      const incomingLines = countLines(incoming);
      const incomingBytes = bytesToString(incoming);
      setLogState((prev) => {
        const droppedBytes = (reset ? 0 : prev.droppedBytes) + newlyDropped;
        // Total lines/bytes accumulate unbounded — they describe the original
        // file, not the rendered tail. Use server-provided total_bytes when
        // available; fall back to the running counter so older payloads still
        // render a sensible "Final · {N} bytes" label.
        const baseLines = reset ? 0 : prev.totalLines;
        const baseBytes = reset ? 0 : prev.totalBytes;
        const totalBytes = typeof logs.total_bytes === "number" && logs.total_bytes > 0
          ? logs.total_bytes
          : baseBytes + incomingBytes;
        return {
          text: nextText,
          totalLines: baseLines + incomingLines,
          totalBytes,
          droppedBytes,
          path: logs.path ?? null,
          status: logs.exists ? "ok" : "missing",
          error: null,
          lastUpdatedAt: Date.now(),
          pollIntervalMs: LOG_POLL_BASE_MS,
          consecutiveErrors: 0,
        };
      });
    } catch (error) {
      if (selectedRunIdRef.current !== runId) return;
      if (detailWasRemoved(error)) {
        setLogState((prev) => ({...prev, status: "missing", error: null}));
        return;
      }
      const message = errorMessage(error);
      setLogState((prev) => {
        const consecutiveErrors = prev.consecutiveErrors + 1;
        const idx = Math.min(consecutiveErrors - 1, LOG_POLL_BACKOFF_MS.length - 1);
        const backoff = LOG_POLL_BACKOFF_MS[idx] ?? LOG_POLL_BACKOFF_MS[LOG_POLL_BACKOFF_MS.length - 1] ?? LOG_POLL_BASE_MS;
        return {
          ...prev,
          status: "error",
          error: message,
          consecutiveErrors,
          pollIntervalMs: backoff,
        };
      });
    }
  }, [inspectorMode, inspectorOpen]);

  const refreshDetail = useCallback(async (runId: string) => {
    // Per-run detail URL must NOT carry state-pane filter params
    // (type / outcome / query / active_only / history_page). Reusing
    // `stateQueryParams` here mis-routed `queue-compat:<task>` lookups to
    // 404 (live-findings W2-IMPORTANT-1 / W13-IMPORTANT-1). Use the
    // detail-specific URL builder which only forwards `history_page_size`
    // for the inspector's history paging.
    const url = runDetailUrl(runId, {historyPageSize});
    const nextDetail = await api<RunDetail>(url);
    if (selectedRunIdRef.current !== runId) return;
    setDetail(nextDetail);
  }, [historyPageSize]);

  const loadProjects = useCallback(async () => {
    try {
      const next = await api<ProjectsResponse>("/api/projects");
      setProjectsState(next);
      setBootError(null);
      setProjectsLoaded(true);
      return next;
    } catch (error) {
      setBootError(errorMessage(error));
      setProjectsLoaded(true);
      throw error;
    }
  }, []);

  const refresh = useCallback(async (showStatus = false, signal?: AbortSignal) => {
    if (signal?.aborted) return;
    if (showStatus) setRefreshStatus("refreshing");
    try {
      const projectStatus = await loadProjects();
      if (signal?.aborted) return;
      if (projectStatus.launcher_enabled && !projectStatus.current) {
        setData(null);
        setSelectedRunId(null);
        setDetail(null);
        logOffsetRef.current = 0;
        logTextRef.current = "";
        setLogState(initialLogState);
        setArtifactContent(null);
        setProofContent(null);
        setDiffContent(null);
        setProofArtifactIndex(null);
        setInspectorOpen(false);
        setLastError(null);
        setRefreshStatus((current) => showStatus || current === "error" ? "idle" : current);
        return;
      }
      const rawNext = await api<StateResponse>(
        `/api/state?${stateQueryParams({...filters, historyPage, historyPageSize}).toString()}`,
        signal ? {signal} : {},
      );
      if (signal?.aborted) return;
      // W9-IMPORTANT-2: drop live rows whose run_id is already a terminal
      // history row before any consumer (LiveRuns, TaskBoard, history table)
      // sees the response. The backend's live→history transition is not
      // atomic so the same run can appear in both lists for one poll cycle.
      const next = dedupeLiveAgainstHistory(rawNext);
      setData(next);
      setLastError(null);
      // Codex error-empty-states #1: a successful poll restores the connection.
      setStateFailureStreak(0);
      const visible = visibleRunIds(next);
      if (selectedRunId && visible.has(selectedRunId)) {
        void refreshDetail(selectedRunId).catch((error) => {
          if (!detailWasRemoved(error)) showToast(errorMessage(error), "error");
        });
      }
      setSelectedRunId((current) => {
        if (current && visible.has(current)) return current;
        // mc-audit redesign Phase C: drawer-mode means an empty selection is
        // the *closed* state. Don't auto-select on first poll if `current`
        // is null — the user has explicitly chosen "no drawer." Only
        // auto-select when an existing selection has gone away (e.g. the
        // run was deleted) so we degrade gracefully without a stale id.
        if (!current) return null;
        const nextRunId = next.live.items[0]?.run_id || next.landing.items.find((item) => item.run_id)?.run_id || next.history.items[0]?.run_id || null;
        selectedRunIdRef.current = nextRunId;
        writeRouteState(currentRouteState(), "replace");
        return nextRunId;
      });
      setRefreshStatus((current) => showStatus || current === "error" ? "idle" : current);
    } catch (error) {
      // AbortError fires when the visibility-aware poller cancels an
      // in-flight request (e.g. tab went hidden mid-fetch, or two effect
      // re-runs raced). Silently swallow — neither the status badge nor
      // a toast should reflect a deliberate cancellation.
      if (signal?.aborted || (error instanceof DOMException && error.name === "AbortError")) {
        return;
      }
      setRefreshStatus("error");
      // Codex error-empty-states #1: track consecutive failures so the
      // sticky banner appears at the threshold. The banner is the durable
      // connection-lost surface; transient toasts also stay so existing
      // user-initiated refresh flows behave the same way.
      setStateFailureStreak((prev) => prev + 1);
      showToast(errorMessage(error), "error");
    }
  }, [filters, historyPage, historyPageSize, loadProjects, refreshDetail, selectedRunId, showToast, currentRouteState]);

  // Keep `refreshRef` pointed at the latest `refresh` closure so callbacks
  // declared BEFORE this declaration (e.g. executeConfirmedAction) can fire
  // a refresh without forming a dependency cycle.
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  // W10-CRITICAL-1/2: cross-tab mutation broadcast. When a peer tab on
  // the same origin issues a mutation (queue submit / cancel / merge /
  // watcher start-stop), it posts a `mc-state-mutation` BroadcastChannel
  // message; we receive it here and fire an immediate /api/state refresh
  // instead of waiting up to ~1.5s for the next poll tick. Outgoing
  // publishes happen at the mutation call sites (executeAction,
  // mergeReadyTasks, runWatcherAction, JobDialog onQueued) via the
  // module-scope ref ``crossTabPublishRef`` set up at the top of this
  // component.
  const crossTab = useCrossTabChannel(() => {
    void refreshRef.current?.(false);
  });
  useEffect(() => {
    crossTabPublishRef.current = crossTab.publish;
  }, [crossTab.publish]);

  // W9-CRITICAL-1: visibility-aware single-timer poll for `/api/state`.
  //
  // The previous implementation used `window.setInterval(refresh, fastMs)`
  // and relied on the browser's natural timer-throttling under hidden
  // tabs to slow it down. Two compound bugs resulted:
  //   1. Hidden behaviour was implementation-defined — Chrome stopped the
  //      poll entirely (~0 polls during a 110s hide window in W9), leaving
  //      the SPA stale and the heavy-user notification gate cold.
  //   2. On visibility restore, the unthrottled timer combined with
  //      cascading effect re-runs (every state update changed `refresh`'s
  //      identity, re-firing the effect) to send a 175-poll burst within a
  //      single 10s window — request-flood territory on hosted backends.
  //
  // The replacement schedules ONE recursive `setTimeout` whose cadence
  // varies with visibility: 1.2-5s while visible (driven by the server's
  // refresh_interval_s hint), 30s while hidden. A `visibilitychange`
  // handler aborts any in-flight request, fires a single immediate
  // catch-up poll on hidden→visible, and reschedules. The
  // `STATE_POLL_MIN_GAP_MS` floor caps the worst case at one poll/sec
  // even if multiple call paths race.
  useEffect(() => {
    let cancelled = false;

    const cadenceMs = (): number =>
      statePollVisibleRef.current ? refreshIntervalMs(data) : STATE_POLL_HIDDEN_MS;

    const cancelPending = () => {
      if (statePollTimerRef.current !== null) {
        window.clearTimeout(statePollTimerRef.current);
        statePollTimerRef.current = null;
      }
    };

    const fireOnce = async () => {
      if (cancelled) return;
      // Hard floor between polls. We don't drop the call entirely — we
      // reschedule it so the cadence stays approximately on track.
      const now = Date.now();
      const sinceLast = now - statePollLastAtRef.current;
      if (sinceLast < STATE_POLL_MIN_GAP_MS) {
        scheduleNext(STATE_POLL_MIN_GAP_MS - sinceLast);
        return;
      }
      statePollLastAtRef.current = now;
      // Cancel any prior in-flight refresh — visibility flips can otherwise
      // leave the previous fetch hanging while the new one starts, doubling
      // server load briefly.
      if (statePollAbortRef.current) {
        statePollAbortRef.current.abort();
      }
      const controller = new AbortController();
      statePollAbortRef.current = controller;
      try {
        await refresh(false, controller.signal);
      } finally {
        if (statePollAbortRef.current === controller) {
          statePollAbortRef.current = null;
        }
      }
      if (cancelled) return;
      scheduleNext(cadenceMs());
    };

    const scheduleNext = (delayMs: number) => {
      if (cancelled) return;
      cancelPending();
      statePollTimerRef.current = window.setTimeout(() => {
        statePollTimerRef.current = null;
        void fireOnce();
      }, delayMs);
    };

    const onVisibilityChange = () => {
      if (typeof document === "undefined") return;
      const visible = document.visibilityState !== "hidden";
      const wasVisible = statePollVisibleRef.current;
      statePollVisibleRef.current = visible;
      if (visible === wasVisible) return;
      // Tab transitioned. Cancel any in-flight request (its result may be
      // about to land in the wrong cadence) and reset the timer at the
      // new cadence. On hidden→visible we ALSO fire a single immediate
      // catch-up poll so the user sees fresh state right away — but the
      // STATE_POLL_MIN_GAP_MS floor + single-timer ref guarantee we never
      // burst.
      if (statePollAbortRef.current) {
        statePollAbortRef.current.abort();
        statePollAbortRef.current = null;
      }
      cancelPending();
      if (visible) {
        // Single catch-up. fireOnce() will reschedule after it returns.
        void fireOnce();
      } else {
        scheduleNext(cadenceMs());
      }
    };

    // Seed visibility state from the document so the first tick uses the
    // correct cadence (e.g. tests that pre-set `visibilityState=hidden`).
    if (typeof document !== "undefined") {
      statePollVisibleRef.current = document.visibilityState !== "hidden";
    }

    void fireOnce();
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return () => {
      cancelled = true;
      cancelPending();
      if (statePollAbortRef.current) {
        statePollAbortRef.current.abort();
        statePollAbortRef.current = null;
      }
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    };
  }, [refresh, data?.live.refresh_interval_s]);

  // Heavy-user paper-cut #3: browser Notification when a long run finishes
  // while the tab is hidden. We track the previous live-run set; any run
  // that disappears from "live" (transitioned to a terminal state) AND was
  // observed live for at least one poll AND was running while the tab was
  // hidden gets a notification. We request permission lazily on first user
  // action (job submit / watcher start) — see useEffect below.
  useNotificationsOnRunFinish(data);

  // Heavy-user paper-cut #4: global Cmd-K / Ctrl-K opens the palette. We
  // also listen for `Escape` here so the palette closes consistently
  // independent of focus position. Other typing keys are ignored when an
  // input or contentEditable is focused so we don't steal "k" from a
  // search field. See `isTypingTarget`.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onKeyDown = (event: KeyboardEvent) => {
      const cmdK = (event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey
        && event.key.toLowerCase() === "k";
      if (cmdK) {
        // Always intercept — Cmd-K is otherwise the browser's location-bar
        // shortcut on some platforms, but for an SPA the palette is the
        // expected affordance. mc-audit cluster G respect: skip when a
        // higher modal is already open so we never stack two.
        if (jobOpen || confirm) return;
        event.preventDefault();
        setPaletteOpen((prev) => !prev);
        return;
      }
      if (event.key === "Escape" && paletteOpen) {
        // Palette close is also handled by useDialogFocus inside the
        // component; we add this guard so an Escape from outside the
        // palette's focus subtree still closes it.
        setPaletteOpen(false);
      }
      // mc-audit redesign §9 W9.4: global "new job" shortcut. Honours the
      // typing-target check so it doesn't steal "n" inside the intent
      // textarea or filter inputs, and honours the modal-stack check so
      // it never opens a second dialog over an already-open one.
      const noMods = !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey;
      const inputBlocked = isTypingTarget(event.target);
      const anyModalOpen = jobOpen || confirm || paletteOpen;
      if (event.key === "n" && noMods && !anyModalOpen && !inputBlocked) {
        event.preventDefault();
        setJobOpen(true);
        return;
      }
      // mc-audit redesign Phase D: navigation shortcuts.
      // j/k: move selection down/up through visible task rows.
      // o: open inspector for selected row (default = Result tab).
      // Esc: close drawer / inspector if open.
      // ?: show shortcut overlay (handled separately below).
      if ((event.key === "j" || event.key === "k") && noMods && !anyModalOpen && !inputBlocked) {
        event.preventDefault();
        const rows = Array.from(document.querySelectorAll<HTMLElement>('[data-stage] [data-testid^="task-card-"]'));
        if (!rows.length) return;
        const currentIndex = rows.findIndex((row) => {
          const id = row.getAttribute("data-run-id") || row.closest("[data-run-id]")?.getAttribute("data-run-id");
          return id && id === selectedRunIdRef.current;
        });
        const nextIndex = event.key === "j"
          ? Math.min(rows.length - 1, currentIndex < 0 ? 0 : currentIndex + 1)
          : Math.max(0, currentIndex < 0 ? 0 : currentIndex - 1);
        rows[nextIndex]?.click();
        rows[nextIndex]?.scrollIntoView({block: "nearest"});
        return;
      }
      if (event.key === "Escape" && !anyModalOpen) {
        // Close help overlay first, then drawer/inspector.
        if (helpOpen) {
          setHelpOpen(false);
          return;
        }
        if (inspectorOpen) {
          setInspectorOpen(false);
          return;
        }
        if (selectedRunIdRef.current || selectedQueuedTask) {
          setSelectedRunId(null);
          setSelectedQueuedTask(null);
          selectedRunIdRef.current = null;
          const route = readRouteState();
          route.selectedRunId = null;
          writeRouteState(route, "replace");
          return;
        }
      }
      // ? opens shortcut help overlay (Shift+/ on US layout).
      if (event.key === "?" && !inputBlocked && !anyModalOpen) {
        event.preventDefault();
        setHelpOpen((prev) => !prev);
        return;
      }
      // 1..5 switch inspector tabs while inspector is open.
      // Loading for logs/diff is handled by separate effects keyed on
      // inspectorMode/inspectorOpen — we just flip the mode here.
      if (inspectorOpen && noMods && !inputBlocked && /^[1-5]$/.test(event.key)) {
        const tabModes: InspectorMode[] = ["try", "proof", "diff", "logs", "artifacts"];
        const mode = tabModes[Number(event.key) - 1];
        if (mode) {
          event.preventDefault();
          setInspectorMode(mode);
        }
        return;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [jobOpen, confirm, paletteOpen, inspectorOpen, selectedQueuedTask, helpOpen]);

  // Manual-refresh handler: wraps `refresh(true)` in a synchronous in-flight
  // latch so the toolbar/launcher Refresh buttons disable while a fetch is in
  // flight. The polling interval above continues uninhibited — only the
  // user-initiated path is gated. mc-audit microinteractions I2.
  const onManualRefresh = useCallback(() => {
    void refreshInFlight.run(() => refresh(true));
  }, [refresh, refreshInFlight]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      logOffsetRef.current = 0;
      logTextRef.current = "";
      setLogState(initialLogState);
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
    logTextRef.current = "";
    setLogState(initialLogState);
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
        logOffsetRef.current = 0;
        logTextRef.current = "";
        setLogState(initialLogState);
        setArtifactContent(null);
        setProofContent(null);
        setDiffContent(null);
        setProofArtifactIndex(null);
        setInspectorOpen(false);
        writeRouteState(currentRouteState(), "replace");
        return;
      }
      showToast(errorMessage(error), "error");
    });
  }, [refreshDetail, selectedRunId, showToast, currentRouteState]);

  // Log polling with three controls the simple `setInterval` version lacked:
  //   1. Exponential backoff on consecutive errors (1.2s -> 2s -> 5s -> ...).
  //      `pollIntervalMs` lives in `logState`; on error the previous loadLogs
  //      raised it, on success it dropped it back to LOG_POLL_BASE_MS.
  //   2. Pause when the tab is hidden — keeps the SPA from flooding the
  //      server when the user has the inspector parked in a background tab.
  //   3. Stop entirely when the run is terminal AND we've drained the file.
  //      `detail.active` flips to false on completion, so once we have one
  //      successful read after that we stop scheduling.
  useEffect(() => {
    if (!selectedRunId || inspectorMode !== "logs" || !inspectorOpen) return;
    const runIsActive = detail?.active === true;
    // Stop polling when the run has terminated and we have at least one
    // successful read of the final state. The first successful read after
    // termination is what makes the header flip to "Final · ..." too.
    const shouldKeepPolling = runIsActive || logState.status === "loading" || logState.status === "idle" || logState.status === "error";
    if (!shouldKeepPolling) return;

    let cancelled = false;

    const scheduleNext = (delayMs: number) => {
      if (cancelled) return;
      logPollTimeoutRef.current = window.setTimeout(async () => {
        if (cancelled) return;
        if (!logPollVisibleRef.current) {
          // Tab is hidden — re-check on visibility change, do not poll now.
          // The visibilitychange handler below will resume by re-running this
          // effect (it sets state which forces a render).
          return;
        }
        await loadLogs(selectedRunId);
        if (cancelled) return;
        scheduleNext(logState.pollIntervalMs);
      }, delayMs);
    };

    scheduleNext(logState.pollIntervalMs);

    return () => {
      cancelled = true;
      if (logPollTimeoutRef.current !== null) {
        window.clearTimeout(logPollTimeoutRef.current);
        logPollTimeoutRef.current = null;
      }
    };
  }, [inspectorMode, inspectorOpen, loadLogs, selectedRunId, detail?.active, logState.status, logState.pollIntervalMs]);

  // Pause/resume polling on tab visibility. We track visibility in a ref so
  // the polling timer can cheaply consult it without re-rendering, and bump
  // a state setter on transitions so the polling effect's deps fire and a
  // hidden->visible flip resumes the loop immediately.
  const [logVisibilityTick, setLogVisibilityTick] = useState(0);
  useEffect(() => {
    if (typeof document === "undefined") return;
    const update = () => {
      const visible = document.visibilityState !== "hidden";
      const wasVisible = logPollVisibleRef.current;
      logPollVisibleRef.current = visible;
      if (visible && !wasVisible) {
        // Re-arm polling immediately on resume rather than waiting up to
        // `pollIntervalMs` for the previously-scheduled tick to run.
        setLogVisibilityTick((tick) => tick + 1);
      }
    };
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  // When the visibility tick advances *and* the inspector is showing logs,
  // kick a single immediate fetch so the user sees fresh content the moment
  // they return to the tab. The recurring polling effect above continues
  // from there at the normal cadence.
  useEffect(() => {
    if (logVisibilityTick === 0) return;
    if (!selectedRunId || inspectorMode !== "logs" || !inspectorOpen) return;
    void loadLogs(selectedRunId);
  }, [logVisibilityTick, selectedRunId, inspectorMode, inspectorOpen, loadLogs]);

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
    const isMerge = action === "merge";
    const isCleanup = action === "cleanup";
    const isCancel = action === "cancel";
    // For merge actions we forward the SHAs from the most-recent diff
    // fetch so the server can refuse to merge code that differs from
    // what the operator reviewed (CRITICAL diff-freshness contract). The
    // confirm body is rebuilt to spell out exactly which branch+SHA is
    // about to land into which target+SHA.
    const liveDiff = isMerge ? diffContent : null;
    const requestPayload: Record<string, string> = {...actionPayload};
    if (isMerge && liveDiff?.target_sha) requestPayload.expected_target_sha = liveDiff.target_sha;
    if (isMerge && liveDiff?.branch_sha) requestPayload.expected_branch_sha = liveDiff.branch_sha;
    // Build per-action confirm payload. mc-audit
    // codex-destructive-action-safety #2/#3/#4: enrich copy so the operator
    // can identify exactly which run + branch + worktree is being changed.
    let title: string;
    let body: string;
    let bodyContent: ReactNode | undefined;
    let confirmLabel: string;
    if (isMerge) {
      title = "Land task";
      body = liveDiff ? mergeConfirmationBody(liveDiff) : message;
      bodyContent = <SingleMergeConfirmDetails detail={detail} diff={liveDiff} />;
      confirmLabel = "Land task";
    } else if (specAction) {
      title = actionLabel;
      body = message;
      confirmLabel = actionLabel;
    } else if (isCleanup) {
      const cleanup = describeCleanupConfirm(detail);
      title = cleanup.title;
      body = cleanup.body;
      confirmLabel = cleanup.confirmLabel;
    } else if (isCancel) {
      const cancelInfo = describeCancelConfirm(detail, runId);
      title = cancelInfo.title;
      body = cancelInfo.body;
      confirmLabel = cancelInfo.confirmLabel;
    } else {
      title = `${actionLabel} run`;
      body = message;
      confirmLabel = actionLabel;
    }
    requestConfirm({
      title,
      body,
      bodyContent,
      confirmLabel,
      tone: isCancel || isCleanup ? "danger" : "primary",
      // The onConfirm contract: throw on failure. `executeConfirmedAction`
      // surfaces the thrown message inline in the dialog (per mc-audit
      // codex-destructive-action-safety #6). Do NOT catch + showToast here —
      // that would silently close the dialog on 4xx/5xx.
      onConfirm: async () => {
        // mc-audit microinteractions I4: optimistic transition for cancel.
        // Set BEFORE the POST so the row visibly flips to "cancelling" the
        // moment the confirm dialog closes. Cleared in the catch on failure
        // (toast surfaces the revert) and naturally superseded by the next
        // refresh on success.
        if (isCancel) {
          setOptimisticRunStates((prev) => ({...prev, [runId]: "cancelling"}));
        }
        try {
          const result = await api<ActionResult>(`/api/runs/${encodeURIComponent(runId)}/actions/${action}`, {
            method: "POST",
            body: JSON.stringify(requestPayload),
          });
          handleActionResult(result, `${action} requested`, showToast, setResultBanner);
          // W10-CRITICAL-2: tell peer tabs about the mutation so a cancel
          // (or any per-run action) issued from this tab is reflected in
          // their UI within ~one render frame instead of one poll tick.
          crossTabPublishRef.current?.("queue.action", {runId});
          if (result.refresh !== false) await refresh(true);
        } catch (err) {
          if (isCancel) {
            // Roll back the optimistic flip so the row returns to the
            // server-provided status. Toast already surfaced via
            // executeConfirmedAction's confirmError path; add a warning
            // so the user knows the row reverted.
            setOptimisticRunStates((prev) => {
              if (!(runId in prev)) return prev;
              const next = {...prev};
              delete next[runId];
              return next;
            });
            showToast("Cancel reverted.", "warning");
          }
          throw err;
        }
      },
    });
  }, [data?.landing, detail, diffContent, refresh, requestConfirm, showToast]);

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
    // mc-audit codex-destructive-action-safety #1 (CRITICAL): replace the
    // first-five preview with a SCROLLABLE LIST of every task that will
    // land. For N>1 require an explicit checkbox tick — a typed phrase was
    // considered but rejected as too friction-heavy for a frequent op.
    const readyItems = (landing?.items || []).filter((item) => item.landing_state === "ready");
    const target = landing?.target || "main";
    requestConfirm({
      title: "Land ready tasks",
      body: landingBulkConfirmation(landing),
      bodyContent: <BulkLandingConfirmList items={readyItems} target={target} />,
      confirmLabel: ready === 1 ? "Land 1 task" : `Land ${ready} tasks`,
      tone: "primary",
      requireCheckbox: ready > 1
        ? {label: `Yes, land all ${ready} tasks above into ${target}.`}
        : undefined,
      onConfirm: () => mergeAllInFlight.run(async () => {
        const result = await api<ActionResult>("/api/actions/merge-all", {method: "POST", body: "{}"});
        handleActionResult(result, "merge all requested", showToast, setResultBanner);
        // W10-CRITICAL-1/2: notify peer tabs so the landing/task board
        // reflects the merge before their next poll tick.
        crossTabPublishRef.current?.("merge-all");
        if (result.refresh !== false) await refresh(true);
      }),
    });
  }, [data?.landing, mergeAllInFlight, refresh, requestConfirm, showToast]);

  const recoverLanding = useCallback(async () => {
    requestConfirm({
      title: "Recover landing",
      body: "Abort the merge and re-run conflict resolution. May invoke the merge provider.",
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
      body: "Abort the merge and don't land. Returns the repo to a clean state.",
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
    // Heavy-user paper-cut #3: requesting permission on watcher start gives
    // us a second natural moment to capture consent — in case the user
    // skipped the New Job dialog and went straight to "start watcher" on a
    // pre-queued task.
    requestNotificationPermissionOnce();
    // Both paths share `watcherInFlight` — start fires directly (no confirm
    // gate per existing UX), stop goes through the confirm modal but the
    // trigger button must also disable for the duration of the POST, not just
    // the modal pause. mc-audit microinteractions C2 / first-time-user #14.
    // mc-audit live W11-IMPORTANT-3: any rejection (silent enqueue failure,
    // already-running watcher, supervisor lockout) must surface a toast so
    // the operator never gets stuck staring at a disabled button with no
    // feedback. The catch wraps the POST so caller `void` semantics still
    // work but never silently swallow an error.
    const execute = () => watcherInFlight.run(async () => {
      try {
        const result = await api<ActionResult | {message?: string}>(`/api/watcher/${action}`, {
          method: "POST",
          body: "{}",
        });
        showToast(result.message || `watcher ${action} requested`);
        // W10-CRITICAL-1/2: peer tabs need to see watcher start/stop
        // immediately — a queued task transitioning to running on tab A
        // should not stay invisible in tab B until the next poll fires.
        crossTabPublishRef.current?.(action === "start" ? "watcher.start" : "watcher.stop");
        await refresh(true);
      } catch (error) {
        showToast(`watcher ${action} failed: ${errorMessage(error)}`, "error");
        throw error;
      }
    });
    if (action === "stop") {
      // mc-audit codex-destructive-action-safety #5: build the confirm body
      // from live runtime counts so the operator sees the operational impact
      // (pid, running tasks at risk, queued+backlog that will wait).
      const stopInfo = describeWatcherStopConfirm(data);
      requestConfirm({
        title: "Stop watcher",
        body: stopInfo.body,
        bodyContent: stopInfo.detail,
        confirmLabel: "Stop watcher",
        tone: "danger",
        // For a non-empty workload, require explicit ack to avoid an Enter
        // keystroke from the previous dialog interrupting running tasks.
        requireCheckbox: stopInfo.requireAck
          ? {label: "Stop watcher"}
          : undefined,
        onConfirm: () => execute(),
      });
      return;
    }
    await execute();
  }, [data, refresh, requestConfirm, showToast, watcherInFlight]);

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

  // State-derived "open inspector" — picks the most useful tab for the
  // selected run's state instead of always opening on Result. mc-audit
  // redesign §5 W5.2 / §1.3.
  //   • In-flight                    → Logs
  //   • Ready / needs-review         → Code changes
  //   • Merged / has proof           → Result
  //   • Otherwise                    → Result
  const showInspectorContextual = useCallback(() => {
    setInspectorOpen(true);
    const next: InspectorMode = (() => {
      if (!detail) return "proof";
      if (detail.active) return "logs";
      const state = detail.review_packet?.readiness?.state;
      if (state === "ready") return "diff";
      return "proof";
    })();
    setInspectorMode(next);
    if (next === "logs") {
      const runId = selectedRunIdRef.current;
      if (runId) void loadLogs(runId, true);
    } else if (next === "diff") {
      void loadDiff();
    }
  }, [detail, loadLogs, loadDiff]);

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

  // Cluster-evidence-trustworthiness #3: invalidate the cached proof
  // content whenever the proof-of-work file's mtime/sha changes (a
  // re-cert wrote a fresh report) or when the run version bumps. Without
  // this the drawer keeps showing stale evidence after the certifier
  // re-runs against the same artifact path.
  const proofReport = detail?.review_packet?.certification?.proof_report;
  const proofIdentity = `${detail?.run_id || ""}|${detail?.version ?? ""}|${proofReport?.sha256 || ""}|${proofReport?.file_mtime || ""}`;
  useEffect(() => {
    setProofContent(null);
    setProofArtifactIndex(null);
    // Effect intentionally depends only on the identity string — we
    // want a fresh fetch whenever any of the provenance fields move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proofIdentity]);

  const project = data?.project;
  const watcher = data?.watcher;
  const landing = data?.landing;
  // mc-audit live W11-IMPORTANT-1: in-flight must include atomic-domain runs
  // (standalone `otto build` etc.), not just queue counts. The server already
  // computes `live.active_count` across every domain — prefer that and fall
  // back to watcher counts only if `live` is missing (early hydration).
  const active = data?.live?.active_count ?? activeCount(watcher);
  const watcherHint = watcherControlHint(data);
  // `modalOpen` is reserved for the topmost overlay (job/confirm dialogs)
  // that needs the rest of the page to be inert. The inspector has its own
  // inert ownership (see sidebarInert / mainSiblingsInert below) so that an
  // open inspector with a stacked confirm/job still keeps the inspector
  // interactive while the rest of the page goes quiet. mc-audit a11y A11Y-01,
  // A11Y-02.
  const modalOpen = jobOpen || Boolean(confirm) || paletteOpen;
  // Sidebar is inert whenever ANY overlay (inspector, job, confirm) is open.
  const sidebarInert = inspectorOpen || modalOpen;
  // The mid-layer (toolbar + main content sans inspector) is inert whenever
  // the inspector or a top-level dialog is open. The inspector itself lives
  // in the same layer but its container is given an `inertSiblings` flag that
  // applies inert ONLY to its non-inspector siblings. See `MainShellInert`.
  const mainContentInert = inspectorOpen || modalOpen;
  // The inspector is inert when a job/confirm dialog stacks above it — the
  // dialog has focus + Tab trap, so the inspector should not capture either.
  const inspectorInert = modalOpen;

  // Document title — announces "page" change to screen readers via SR
  // re-read of the title bar. mc-audit a11y A11Y-09.
  useDocumentTitle({
    viewMode,
    selectedRunId,
    selectedDetail: detail,
    inspectorOpen,
    inspectorMode,
  });

  // aria-live region content. Updated when view, run selection, inspector
  // open/close, or inspector tab changes. mc-audit a11y A11Y-10.
  const liveAnnouncement = useLiveAnnouncement({
    viewMode,
    selectedRunId,
    inspectorOpen,
    inspectorMode,
  });

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
    historyPageRef.current = 1;
    historyPageSizeRef.current = DEFAULT_HISTORY_PAGE_SIZE;
    historySortRef.current = null;
    historySortDirRef.current = null;
    filtersRef.current = defaultFilters;
    setHistoryPage(1);
    setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE);
    setHistorySort(null);
    setHistorySortDir(null);
    setFilters(defaultFilters);
    writeRouteState(defaultRouteState(), "replace");
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
    // mc-audit redesign §5 W5.4: preserve a deep-linked run param across
    // the project-open transition. Before this change, opening a project
    // from a `?view=tasks&run=<id>` URL silently dropped the run id and
    // navigated to the latest run instead, defeating share-link UX.
    const carryRunId = (() => {
      try {
        const route = readRouteState();
        return route.selectedRunId;
      } catch { return null; }
    })();
    viewModeRef.current = "tasks";
    selectedRunIdRef.current = carryRunId;
    setViewMode("tasks");
    setSelectedRunId(carryRunId);
    historyPageRef.current = 1;
    historyPageSizeRef.current = DEFAULT_HISTORY_PAGE_SIZE;
    historySortRef.current = null;
    historySortDirRef.current = null;
    filtersRef.current = defaultFilters;
    setHistoryPage(1);
    setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE);
    setHistorySort(null);
    setHistorySortDir(null);
    setFilters(defaultFilters);
    const nextRoute = defaultRouteState();
    if (carryRunId) nextRoute.selectedRunId = carryRunId;
    writeRouteState(nextRoute, "replace");
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
    logOffsetRef.current = 0;
    logTextRef.current = "";
    setLogState(initialLogState);
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
    historyPageRef.current = 1;
    historyPageSizeRef.current = DEFAULT_HISTORY_PAGE_SIZE;
    historySortRef.current = null;
    historySortDirRef.current = null;
    filtersRef.current = defaultFilters;
    setHistoryPage(1);
    setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE);
    setHistorySort(null);
    setHistorySortDir(null);
    setFilters(defaultFilters);
    writeRouteState(defaultRouteState(), "replace");
    showToast("Choose a project");
  }, [showToast]);

  // Wrap setFilters so any filter change resets the history page back to 1.
  // Without this, filtering on page 5 with 0 matches would render an
  // empty table without an obvious explanation. Page-size changes go
  // through `changeHistoryPageSize` and also reset to page 1.
  // Heavy-user paper-cut #1: also persist filter state into the URL so the
  // power user can refresh / back / share a filtered triage view. We
  // *replace* not push so debounced typing in the search box doesn't spam
  // the browser history with one entry per keystroke.
  const updateFilters = useCallback((next: Filters) => {
    setFilters((prev) => {
      const sameType = prev.type === next.type;
      const sameOutcome = prev.outcome === next.outcome;
      const sameQuery = prev.query === next.query;
      const sameActive = prev.activeOnly === next.activeOnly;
      if (sameType && sameOutcome && sameQuery && sameActive) return next;
      historyPageRef.current = 1;
      setHistoryPage(1);
      filtersRef.current = next;
      writeRouteState({...currentRouteState(), historyPage: 1}, "replace");
      return next;
    });
  }, [currentRouteState]);

  // Heavy-user paper-cut #2 (history sort): cycle asc → desc → off and
  // persist into the URL. Each click transitions the sort state through:
  //   not-this-column            → asc
  //   this column, asc           → desc
  //   this column, desc          → cleared
  // The cleared state writes ?hs= and ?hd= out of the URL — see
  // writeRouteState — so a "default order" link stays clean.
  const cycleHistorySort = useCallback((column: HistorySortColumn) => {
    let nextCol: HistorySortColumn | null;
    let nextDir: HistorySortDir | null;
    if (historySortRef.current !== column) {
      nextCol = column;
      nextDir = "asc";
    } else if (historySortDirRef.current === "asc") {
      nextCol = column;
      nextDir = "desc";
    } else {
      nextCol = null;
      nextDir = null;
    }
    historySortRef.current = nextCol;
    historySortDirRef.current = nextDir;
    setHistorySort(nextCol);
    setHistorySortDir(nextDir);
    writeRouteState(currentRouteState(), "replace");
  }, [currentRouteState]);

  const changeHistoryPage = useCallback((nextPage: number) => {
    const totalPages = Math.max(1, data?.history.total_pages || 1);
    const clamped = Math.max(1, Math.min(nextPage, totalPages));
    if (clamped === historyPageRef.current) return;
    historyPageRef.current = clamped;
    setHistoryPage(clamped);
    // Page changes are real navigation steps — push so the browser Back
    // button reverses them. Filters, by contrast, replace.
    writeRouteState({...currentRouteState(), historyPage: clamped}, "push");
  }, [data?.history.total_pages, currentRouteState]);

  const changeHistoryPageSize = useCallback((nextSize: number) => {
    if (!HISTORY_PAGE_SIZE_OPTIONS.includes(nextSize)) return;
    if (nextSize === historyPageSizeRef.current) return;
    historyPageSizeRef.current = nextSize;
    historyPageRef.current = 1;
    setHistoryPageSize(nextSize);
    setHistoryPage(1);
    writeRouteState({
      ...currentRouteState(),
      historyPage: 1,
      historyPageSize: nextSize === DEFAULT_HISTORY_PAGE_SIZE ? null : nextSize,
    }, "replace");
  }, [currentRouteState]);

  // Boot-loading gate: until /api/projects has resolved at least once, render
  // a minimal centered placeholder. Without this, `projectsState` is null and
  // `projectsState?.launcher_enabled` is false, so the launcher branch falls
  // through to the main shell — which renders with `project` undefined and an
  // ENABLED "New job" button. A click would then submit a queue request that
  // 409s on the server. See codex-first-time-user.md #1.
  if (!projectsLoaded) {
    return (
      <div className="app-shell boot-loading" data-testid="boot-loading">
        <main className="boot-loading-panel" role="status" aria-live="polite">
          <div className="boot-loading-mark">
            <Spinner />
            <strong>Loading Mission Control…</strong>
          </div>
          <p>Reading project state. Actions will appear once the workspace is ready.</p>
          {bootError ? (
            <div className="boot-loading-error" data-testid="boot-loading-error">
              <span>Could not reach the server: {bootError}</span>
              <button
                className="primary"
                type="button"
                disabled={refreshInFlight.pending}
                aria-busy={refreshInFlight.pending}
                onClick={onManualRefresh}
              >{refreshInFlight.pending ? <><Spinner /> Retrying…</> : "Retry"}</button>
            </div>
          ) : null}
        </main>
      </div>
    );
  }

  if (projectsState?.launcher_enabled && !data) {
    return (
      <div className="app-shell launcher-shell">
        <main className="launcher-shell-main">
          <ProjectLauncher
            projectsState={projectsState}
            refreshStatus={refreshStatus}
            refreshPending={refreshInFlight.pending}
            onCreate={createManagedProject}
            onSelect={selectManagedProject}
            onRefresh={onManualRefresh}
          />
        </main>
        <ToastDisplay
          toast={toast}
          onMouseEnter={pauseToastDismiss}
          onMouseLeave={resumeToastDismiss}
          onDismiss={dismissToast}
        />
      </div>
    );
  }

  // Codex error-empty-states #1: connection-lost banner. Renders when
  // the polling streak has crossed the threshold. Both the boot screen
  // and the main shell mount the same node so the operator sees a
  // sticky reminder regardless of how stale the cached data is. The
  // manual retry button calls `onManualRefresh` which itself toggles
  // `refreshInFlight` so the button shows a spinner during the retry.
  const connectionLost = stateFailureStreak >= CONNECTION_LOST_THRESHOLD;
  const connectionBanner = connectionLost ? (
    <div
      className="connection-lost-banner"
      data-testid="connection-lost-banner"
      role="alert"
      aria-live="assertive"
    >
      <span className="connection-lost-message">
        Lost connection to Mission Control. Retrying every 5s…
      </span>
      <button
        type="button"
        className="connection-lost-retry"
        data-testid="connection-lost-retry-button"
        onClick={onManualRefresh}
        disabled={refreshInFlight.pending}
        aria-busy={refreshInFlight.pending}
      >
        {refreshInFlight.pending ? <><Spinner /> Retrying…</> : "Retry now"}
      </button>
    </div>
  ) : null;

  // Defensive secondary gate: even once projectsLoaded is true, the main
  // shell must not render until we have a `project` from /api/state. This
  // prevents the dialog from opening with `project` undefined if the
  // launcher mode is off but /api/state has not yet returned.
  if (!data || !data.project) {
    return (
      <div className="app-shell boot-loading" data-testid="boot-loading">
        {connectionBanner}
        <main className="boot-loading-panel" role="status" aria-live="polite">
          <div className="boot-loading-mark">
            <Spinner />
            <strong>Loading Mission Control…</strong>
          </div>
          <p>Reading queue, runs, and repository status…</p>
        </main>
      </div>
    );
  }

  return (
    <div
      className="app-shell"
      data-mc-shell="ready"
      data-drawer-open={(detail || selectedQueuedTask) ? "true" : "false"}
      data-inspector-open={inspectorOpen ? "true" : "false"}
    >
      {/* Skip link must be the first focusable element so a single Tab from
          page load lands on it. Visually hidden until focused. mc-audit a11y
          A11Y-08, K-09. */}
      <a href="#main-content" className="skip-link" data-testid="skip-link">
        Skip to main content
      </a>
      {connectionBanner}
      <InertEffect active={sidebarInert} selector=".topbar" />
      <InertEffect active={mainContentInert} selector=".main-shell-content" />
      <InertEffect active={inspectorInert} selector="[data-mc-inspector]" />
      <LiveRegion message={liveAnnouncement} />
      <TopBar
        data={data}
        project={project}
        watcher={watcher}
        watcherPending={watcherInFlight.pending}
        projectsState={projectsState}
        onNewJob={openJobDialog}
        onSwitchProject={() => void switchProject()}
        onStartWatcher={() => void runWatcherAction("start")}
        onStopWatcher={() => void runWatcherAction("stop")}
      />
      <main className="workspace main-shell-content" id="main-content" tabIndex={-1}>
          <Toolbar
            filters={filters}
            refreshStatus={refreshStatus}
            refreshPending={refreshInFlight.pending}
            viewMode={viewMode}
            onChange={updateFilters}
            onRefresh={onManualRefresh}
            onViewChange={navigateView}
          />
        {viewMode === "tasks" ? (
          <section className="mission-layout" aria-label="Mission Control task workflow">
            <div className="main-stack">
              {/* Top-of-page banners only show when there's something to say.
                  No persistent IDLE hero. mc-audit redesign Phase C. */}
              {(lastError || resultBanner) && (
                <div className="page-banners">
                  {lastError && (
                    <div className="status-banner error">
                      <strong>Last error</strong>
                      <span>{lastError}</span>
                      <button type="button" onClick={() => setLastError(null)}>Dismiss</button>
                    </div>
                  )}
                  {resultBanner && (
                    <div className={`status-banner ${resultBanner.severity === "error" ? "error" : "warning"}`}>
                      <strong>{resultBanner.title}</strong>
                      <span>{resultBanner.body}</span>
                      <button type="button" onClick={() => setResultBanner(null)}>Dismiss</button>
                    </div>
                  )}
                </div>
              )}
              <TaskQueueList
                data={data}
                filters={filters}
                selectedRunId={selectedRunId}
                selectedQueuedTaskId={selectedQueuedTask?.id || null}
                onSelect={selectRun}
                onSelectQueued={selectQueuedTask}
                onLandReady={() => void mergeReadyTasks()}
                onCancelRun={(runId, taskTitle) => void runActionForRun(
                  runId,
                  "cancel",
                  actionConfirmationBody("cancel", `Cancel ${taskTitle}`),
                  "Cancel",
                )}
                onClearFilters={() => updateFilters(defaultFilters)}
                onNewJob={openJobDialog}
              />
              <details className="tasks-supplementary" data-testid="tasks-supplementary">
                <summary><span>Project info & activity</span></summary>
                <div className="tasks-supplementary-body">
                  <ProjectOverview data={data} />
                  <RecentActivity events={data?.events} history={data?.history.items || []} selectedRunId={selectedRunId} onSelect={selectRun} />
                </div>
              </details>
              <RunDetailPanel
                detail={detail}
                landing={landing}
                inspectorOpen={inspectorOpen}
                queuedTask={selectedQueuedTask}
                watcherRunning={data?.watcher.health.state === "running"}
                onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
                onShowTryProduct={showTryProduct}
                onShowProof={showProof}
                onShowLogs={showLogs}
                onShowDiff={showDiff}
                onShowArtifacts={showArtifacts}
                onLoadArtifact={(index) => void loadArtifact(index)}
                onStartWatcher={() => void runWatcherAction("start")}
                onClose={() => {
                  setSelectedRunId(null);
                  setSelectedQueuedTask(null);
                  selectedRunIdRef.current = null;
                  // Drop ?run= from the URL so a reload doesn't re-open
                  // the drawer. mc-audit redesign Phase C.
                  const route = readRouteState();
                  route.selectedRunId = null;
                  writeRouteState(route, "replace");
                }}
              />
            </div>
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
                <DiagnosticsSummary data={data} onSelect={selectRun} />
                <LiveRuns items={applyOptimisticRunStates(data?.live.items || [], optimisticRunStates)} landing={landing} selectedRunId={selectedRunId} onSelect={selectRun} />
                <EventTimeline events={data?.events} />
                <History
                  items={data?.history.items || []}
                  totalRows={data?.history.total_rows || 0}
                  page={data?.history.page != null ? data.history.page + 1 : historyPage}
                  totalPages={data?.history.total_pages || 1}
                  pageSize={data?.history.page_size || historyPageSize}
                  requestedPage={historyPage}
                  loaded={data != null}
                  selectedRunId={selectedRunId}
                  sortColumn={historySort}
                  sortDir={historySortDir}
                  onSelect={selectRun}
                  onChangePage={changeHistoryPage}
                  onChangePageSize={changeHistoryPageSize}
                  onCycleSort={cycleHistorySort}
                />
              </div>
              <RunDetailPanel
                detail={detail}
                landing={landing}
                inspectorOpen={inspectorOpen}
                queuedTask={selectedQueuedTask}
                watcherRunning={data?.watcher.health.state === "running"}
                onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
                onShowTryProduct={showTryProduct}
                onShowProof={showProof}
                onShowLogs={showLogs}
                onShowDiff={showDiff}
                onShowArtifacts={showArtifacts}
                onLoadArtifact={(index) => void loadArtifact(index)}
                onStartWatcher={() => void runWatcherAction("start")}
                onClose={() => {
                  setSelectedRunId(null);
                  setSelectedQueuedTask(null);
                  selectedRunIdRef.current = null;
                  // Drop ?run= from the URL so a reload doesn't re-open
                  // the drawer. mc-audit redesign Phase C.
                  const route = readRouteState();
                  route.selectedRunId = null;
                  writeRouteState(route, "replace");
                }}
              />
            </div>
          </section>
        )}
      </main>

      {/* Inspector mounted as a sibling of <main>, OUTSIDE main-shell-content,
          so the inert flag on main-shell-content doesn't propagate down into
          the inspector. mc-audit a11y A11Y-01, A11Y-02. */}
      {inspectorOpen && detail && (
        <RunInspector
          detail={detail}
          mode={inspectorMode}
          logState={logState}
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
          onRefreshDiff={() => void loadDiff()}
          onBackToArtifacts={() => {
            setSelectedArtifactIndex(null);
            setArtifactContent(null);
          }}
          onClose={() => setInspectorOpen(false)}
        />
      )}

      {jobOpen && (
        <JobDialog
          project={project}
          dirtyFiles={landing?.dirty_files || []}
          priorRunOptions={collectPriorRunOptions(landing?.items || [], data?.history.items || [])}
          onClose={() => setJobOpen(false)}
          onQueued={async (message) => {
            setJobOpen(false);
            showToast(message || "queued");
            // W10-CRITICAL-1: tell peer tabs immediately about the new
            // queued job so their task board renders the row within
            // a render frame instead of waiting for the next poll.
            crossTabPublishRef.current?.("queue.submit");
            await refresh();
          }}
          onError={(message) => showToast(message, "error")}
        />
      )}
      {paletteOpen && (
        <CommandPalette
          projects={projectsState?.projects || []}
          currentPath={project?.path || projectsState?.current?.path || null}
          onSelect={(path) => {
            setPaletteOpen(false);
            // Selecting the current project is a no-op; otherwise switch.
            if (!path) return;
            const currentPath = project?.path || projectsState?.current?.path || null;
            if (path === currentPath) return;
            void selectManagedProject(path).catch((error) => {
              showToast(errorMessage(error), "error");
            });
          }}
          onClose={() => setPaletteOpen(false)}
        />
      )}
      {confirm && (
        <ConfirmDialog
          confirm={confirm}
          pending={confirmPending}
          error={confirmError}
          checkboxAck={confirmCheckboxAck}
          onChangeCheckboxAck={setConfirmCheckboxAck}
          onCancel={dismissConfirm}
          onConfirm={() => void executeConfirmedAction()}
        />
      )}
      {helpOpen && <HelpOverlay onClose={() => setHelpOpen(false)} />}
      <ToastDisplay
        toast={toast}
        onMouseEnter={pauseToastDismiss}
        onMouseLeave={resumeToastDismiss}
        onDismiss={dismissToast}
      />
    </div>
  );
}

// ToastDisplay moved to components/ToastDisplay.tsx

// ProjectMeta moved to components/launcher/ProjectLauncher.tsx

// MetaItem moved to components/MicroComponents.tsx

// ============================================================================
// TopBar — replaces the sidebar with a slim horizontal bar. mc-audit redesign
// Phase C. Brand on the left, project context in the middle, status pill +
// New Job CTA on the right. Designed to read like Linear / Vercel /
// Posthog header rather than a 2010s admin dashboard.
// ============================================================================
// TopBar moved to components/topbar/TopBar.tsx

// ProjectLauncher + launcherErrorMessage moved to components/launcher/ProjectLauncher.tsx
// Toolbar moved to components/toolbar/Toolbar.tsx

// OverviewMetric moved to components/MicroComponents.tsx

// ProjectStatCard moved to components/MicroComponents.tsx

// HealthCard moved to components/MicroComponents.tsx

// FocusMetric moved to components/MicroComponents.tsx

// ============================================================================
// TaskQueueList — replaces the kanban TaskBoard with a single ordered list,
// Linear-style. Tasks render as table rows ranked by the natural workflow:
// active runs first, then needs-action, then ready-to-land, then queued, then
// recently landed. Click a row → opens RunDrawer. mc-audit redesign Phase C.
// ============================================================================
// TaskQueueList + TaskRow moved to components/tasks/TaskQueueList.tsx

// Glossary tooltips for the kanban column labels. mc-audit redesign §7 W7.3.
// mc-audit error-empty-states #11: classify *why* the task board is empty.
// "filtered-empty" lets the UI offer "Clear filters" instead of misleading
// "No work queued." copy when the user actually has tasks but they're hidden.
