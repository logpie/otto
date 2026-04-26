import {FormEvent, useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {ReactNode} from "react";
import {ApiError, api, buildQueuePayload, friendlyApiMessage, stateQueryParams} from "./api";
import {Spinner} from "./components/Spinner";
import {useInFlight} from "./hooks/useInFlight";
import {useDebouncedValue} from "./hooks/useDebouncedValue";
import type {
  ActionResult,
  ActionState,
  ArtifactContentResponse,
  ArtifactRef,
  CertificationPolicy,
  CommandBacklogItem,
  DiffResponse,
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
  ProjectMutationResponse,
  ProjectsResponse,
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

type ViewMode = "tasks" | "diagnostics";

type BoardStage = "attention" | "working" | "ready" | "landed";
type InspectorMode = "proof" | "logs" | "artifacts" | "diff";

interface RouteState {
  viewMode: ViewMode;
  selectedRunId: string | null;
  // 1-based history page persisted in the URL as `?hp=N`. We picked the
  // short key intentionally — the URL bar is busy and `?history_page=`
  // would crowd out other params. Default page is 1 and is omitted from
  // the URL so a stripped link stays clean.
  historyPage: number;
  // Persisted page-size selection (10/25/50/100). Optional in the URL —
  // null means "no override; use the front-end default".
  historyPageSize: number | null;
}

// Default page size for History pane. 25 is enough to fill an MBA viewport
// without scrolling the table off-screen, and is the middle option in the
// selector so Up/Down arrows on the <select> reach 10 and 50 quickly.
const DEFAULT_HISTORY_PAGE_SIZE = 25;
const HISTORY_PAGE_SIZE_OPTIONS: readonly number[] = [10, 25, 50, 100];

// Cap the log buffer at ~1MB of text — the browser can render that without
// jank, and we display "{N} earlier bytes elided" to make the truncation
// honest. We chose bytes (not lines) because a runaway log can still emit
// short lines indefinitely and overrun a line-based cap. See
// docs/mc-audit/_hunter-findings/codex-long-string-overflow.md finding #1.
const LOG_BUFFER_MAX_BYTES = 1_048_576;
const LOG_POLL_BASE_MS = 1200;
// Backoff schedule when the log fetch fails repeatedly. Index 0 is the
// "first failure" delay; we cap at 30s so a sustained outage stops hammering
// the API. On the first successful read we drop back to LOG_POLL_BASE_MS.
const LOG_POLL_BACKOFF_MS = [2000, 5000, 15000, 30000];

type LogStatus = "idle" | "loading" | "ok" | "missing" | "error";

interface LogState {
  text: string;
  totalLines: number;
  totalBytes: number;
  droppedBytes: number;
  path: string | null;
  status: LogStatus;
  error: string | null;
  lastUpdatedAt: number | null;
  pollIntervalMs: number;
  consecutiveErrors: number;
}

const initialLogState: LogState = {
  text: "",
  totalLines: 0,
  totalBytes: 0,
  droppedBytes: 0,
  path: null,
  status: "idle",
  error: null,
  lastUpdatedAt: null,
  pollIntervalMs: LOG_POLL_BASE_MS,
  consecutiveErrors: 0,
};

function bytesToString(value: string): number {
  if (typeof TextEncoder === "undefined") return value.length;
  return new TextEncoder().encode(value).length;
}

// Count newline characters (\n). When appending an incremental chunk this
// gives the number of *additional* lines closed by the chunk, which lets us
// maintain a running totalLines counter without ever re-splitting the full
// log. The display rounds up to "1 line" for any non-empty buffer so an
// always-tailing log doesn't read as "0 lines" until the first newline.
function countLines(text: string): number {
  if (!text) return 0;
  let count = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) count += 1;
  }
  return count;
}

function appendToLogBuffer(prev: string, chunk: string, maxBytes: number): {text: string; droppedBytes: number} {
  if (!chunk) return {text: prev, droppedBytes: 0};
  const combined = prev + chunk;
  const combinedBytes = bytesToString(combined);
  if (combinedBytes <= maxBytes) return {text: combined, droppedBytes: 0};
  // Drop characters from the front until we are under the cap, then snap to
  // the next newline so partial lines don't sit at the head of the buffer.
  // We approximate "bytes" with "characters" for the slice search — exact
  // byte alignment is not meaningful when the original split between chunks
  // can already land mid-grapheme.
  const overshootChars = Math.max(0, combined.length - maxBytes);
  let cut = overshootChars;
  const newlineAfterCut = combined.indexOf("\n", cut);
  if (newlineAfterCut >= 0 && newlineAfterCut - cut < 4096) cut = newlineAfterCut + 1;
  const truncated = combined.slice(cut);
  const droppedBytes = bytesToString(combined.slice(0, cut));
  return {text: truncated, droppedBytes};
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
  source: "landing" | "live" | "history";
}

const defaultFilters: Filters = {
  type: "all",
  outcome: "all",
  query: "",
  activeOnly: false,
};

function readRouteState(): RouteState {
  if (typeof window === "undefined") {
    return {viewMode: "tasks", selectedRunId: null, historyPage: 1, historyPageSize: null};
  }
  const params = new URLSearchParams(window.location.search);
  return {
    viewMode: params.get("view") === "diagnostics" ? "diagnostics" : "tasks",
    selectedRunId: params.get("run") || null,
    historyPage: parseHistoryPageParam(params.get("hp")),
    historyPageSize: parseHistoryPageSizeParam(params.get("ps")),
  };
}

function parseHistoryPageParam(raw: string | null): number {
  if (!raw) return 1;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1) return 1;
  return parsed;
}

function parseHistoryPageSizeParam(raw: string | null): number | null {
  if (!raw) return null;
  const parsed = Number.parseInt(raw, 10);
  if (!HISTORY_PAGE_SIZE_OPTIONS.includes(parsed)) return null;
  return parsed;
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
  // Drop the page param when on page 1; the URL stays clean for the common
  // case and copy-paste links from page 1 don't accumulate `?hp=1` cruft.
  if (route.historyPage > 1) {
    url.searchParams.set("hp", String(route.historyPage));
  } else {
    url.searchParams.delete("hp");
  }
  if (route.historyPageSize && route.historyPageSize !== DEFAULT_HISTORY_PAGE_SIZE) {
    url.searchParams.set("ps", String(route.historyPageSize));
  } else {
    url.searchParams.delete("ps");
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
  // `projectsLoaded` flips true the first time `/api/projects` resolves
  // (success or recoverable failure). Until then we render a boot-loading
  // placeholder instead of either the launcher OR the main shell — see
  // mc-audit codex-first-time-user.md #1: before /api/projects returns,
  // projectsState is null so the launcher gate fails and the main shell
  // can render with an enabled "New job" button + project undefined.
  const [projectsLoaded, setProjectsLoaded] = useState<boolean>(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRoute.selectedRunId);
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
  const [jobOpen, setJobOpen] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [resultBanner, setResultBanner] = useState<ResultBannerState | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  // Synchronous lock for the confirm dialog. `confirmPending` is React state
  // and updates asynchronously; a fast double-click on the modal Confirm
  // button can read `pending=false` for both clicks. The ref is checked and
  // set in the same microtask, eliminating the duplicate POST race that
  // mc-audit codex-state-management #10 documented.
  const confirmLockRef = useRef(false);
  const [viewMode, setViewMode] = useState<ViewMode>(initialRoute.viewMode);
  const watcherInFlight = useInFlight();
  const refreshInFlight = useInFlight();
  const mergeAllInFlight = useInFlight();
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

  // currentRouteState is the single source of truth for what the URL reflects;
  // every push/replace passes through it so we don't accidentally drop a
  // param that other code paths persist. The page-size is only persisted
  // when it differs from the default — see writeRouteState's `?ps=` rules.
  const currentRouteState = useCallback((): RouteState => ({
    viewMode: viewModeRef.current,
    selectedRunId: selectedRunIdRef.current,
    historyPage: historyPageRef.current,
    historyPageSize: historyPageSizeRef.current === DEFAULT_HISTORY_PAGE_SIZE ? null : historyPageSizeRef.current,
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

  useEffect(() => {
    writeRouteState(currentRouteState(), "replace");
    const onPopState = () => {
      const next = readRouteState();
      viewModeRef.current = next.viewMode;
      selectedRunIdRef.current = next.selectedRunId;
      historyPageRef.current = next.historyPage;
      const nextSize = next.historyPageSize ?? DEFAULT_HISTORY_PAGE_SIZE;
      historyPageSizeRef.current = nextSize;
      setViewMode(next.viewMode);
      setSelectedRunId(next.selectedRunId);
      setHistoryPage(next.historyPage);
      setHistoryPageSize(nextSize);
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
    writeRouteState(currentRouteState(), "push");
  }, [currentRouteState]);

  const executeConfirmedAction = useCallback(async () => {
    // `confirmLockRef` is the synchronous half of the dedup. `confirmPending`
    // drives the visual disabled state but cannot block a click that arrived
    // in the same React batch as the previous one.
    if (!confirm || confirmLockRef.current) return;
    confirmLockRef.current = true;
    setConfirmPending(true);
    try {
      await confirm.onConfirm();
      setConfirm(null);
    } catch (error) {
      showToast(errorMessage(error), "error");
    } finally {
      confirmLockRef.current = false;
      setConfirmPending(false);
    }
  }, [confirm, showToast]);

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
    // Detail responses don't carry the history page slice, but the server
    // accepts the params and we send them so a future detail-side feature
    // can rely on the same query convention.
    const params = stateQueryParams({...filters, historyPage, historyPageSize}).toString();
    const nextDetail = await api<RunDetail>(`/api/runs/${encodeURIComponent(runId)}?${params}`);
    if (selectedRunIdRef.current !== runId) return;
    setDetail(nextDetail);
  }, [filters, historyPage, historyPageSize]);

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

  const refresh = useCallback(async (showStatus = false) => {
    if (showStatus) setRefreshStatus("refreshing");
    try {
      const projectStatus = await loadProjects();
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
      const next = await api<StateResponse>(`/api/state?${stateQueryParams({...filters, historyPage, historyPageSize}).toString()}`);
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
        writeRouteState(currentRouteState(), "replace");
        return nextRunId;
      });
      setRefreshStatus((current) => showStatus || current === "error" ? "idle" : current);
    } catch (error) {
      setRefreshStatus("error");
      showToast(errorMessage(error), "error");
    }
  }, [filters, historyPage, historyPageSize, loadProjects, refreshDetail, selectedRunId, showToast, currentRouteState]);

  useEffect(() => {
    void refresh(false);
    const interval = window.setInterval(() => void refresh(false), refreshIntervalMs(data));
    return () => window.clearInterval(interval);
  }, [refresh, data?.live.refresh_interval_s]);

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
    const actionLabel = capitalize(label || action);
    // For merge actions we forward the SHAs from the most-recent diff
    // fetch so the server can refuse to merge code that differs from
    // what the operator reviewed (CRITICAL diff-freshness contract). The
    // confirm body is rebuilt to spell out exactly which branch+SHA is
    // about to land into which target+SHA.
    const isMerge = action === "merge";
    const liveDiff = isMerge ? diffContent : null;
    const mergeBody = isMerge && liveDiff
      ? mergeConfirmationBody(liveDiff)
      : message;
    const requestPayload: Record<string, string> = {};
    if (isMerge && liveDiff?.target_sha) requestPayload.expected_target_sha = liveDiff.target_sha;
    if (isMerge && liveDiff?.branch_sha) requestPayload.expected_branch_sha = liveDiff.branch_sha;
    requestConfirm({
      title: isMerge ? "Land task" : `${actionLabel} run`,
      body: mergeBody,
      confirmLabel: isMerge ? "Land task" : actionLabel,
      tone: ["cancel", "cleanup"].includes(action) ? "danger" : "primary",
      onConfirm: async () => {
        try {
          const result = await api<ActionResult>(`/api/runs/${encodeURIComponent(runId)}/actions/${action}`, {
            method: "POST",
            body: JSON.stringify(requestPayload),
          });
          handleActionResult(result, `${action} requested`, showToast, setResultBanner);
          if (result.refresh !== false) await refresh(true);
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      },
    });
  }, [data?.landing, diffContent, refresh, requestConfirm, showToast]);

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
      onConfirm: () => mergeAllInFlight.run(async () => {
        try {
          const result = await api<ActionResult>("/api/actions/merge-all", {method: "POST", body: "{}"});
          handleActionResult(result, "merge all requested", showToast, setResultBanner);
          if (result.refresh !== false) await refresh(true);
        } catch (error) {
          showToast(errorMessage(error), "error");
        }
      }),
    });
  }, [data?.landing, mergeAllInFlight, refresh, requestConfirm, showToast]);

  const runWatcherAction = useCallback(async (action: "start" | "stop") => {
    // Both paths share `watcherInFlight` — start fires directly (no confirm
    // gate per existing UX), stop goes through the confirm modal but the
    // trigger button must also disable for the duration of the POST, not just
    // the modal pause. mc-audit microinteractions C2 / first-time-user #14.
    const execute = () => watcherInFlight.run(async () => {
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
    });
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
  }, [refresh, requestConfirm, showToast, watcherInFlight]);

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
    historyPageRef.current = 1;
    historyPageSizeRef.current = DEFAULT_HISTORY_PAGE_SIZE;
    setHistoryPage(1);
    setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE);
    writeRouteState({viewMode: "tasks", selectedRunId: null, historyPage: 1, historyPageSize: null}, "replace");
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
    historyPageRef.current = 1;
    historyPageSizeRef.current = DEFAULT_HISTORY_PAGE_SIZE;
    setHistoryPage(1);
    setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE);
    writeRouteState({viewMode: "tasks", selectedRunId: null, historyPage: 1, historyPageSize: null}, "replace");
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
    setHistoryPage(1);
    setHistoryPageSize(DEFAULT_HISTORY_PAGE_SIZE);
    writeRouteState({viewMode: "tasks", selectedRunId: null, historyPage: 1, historyPageSize: null}, "replace");
    showToast("Choose a project");
  }, [showToast]);

  // Wrap setFilters so any filter change resets the history page back to 1.
  // Without this, filtering on page 5 with 0 matches would render an
  // empty table without an obvious explanation. Page-size changes go
  // through `changeHistoryPageSize` and also reset to page 1.
  const updateFilters = useCallback((next: Filters) => {
    setFilters((prev) => {
      const sameType = prev.type === next.type;
      const sameOutcome = prev.outcome === next.outcome;
      const sameQuery = prev.query === next.query;
      const sameActive = prev.activeOnly === next.activeOnly;
      // Only reset the page when something actually changed; React would
      // otherwise reset on every parent rerender that re-passes the same
      // filters object.
      if (!(sameType && sameOutcome && sameQuery && sameActive)) {
        historyPageRef.current = 1;
        setHistoryPage(1);
        writeRouteState({...currentRouteState(), historyPage: 1}, "replace");
      }
      return next;
    });
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
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">O</div>
            <div>
              <h1>Otto</h1>
              <p>Project Launcher</p>
            </div>
          </div>
          <p className="sidebar-hint">Choose a project folder before queueing work.</p>
        </aside>
        <main className="workspace launcher-workspace">
          <ProjectLauncher
            projectsState={projectsState}
            refreshStatus={refreshStatus}
            refreshPending={refreshInFlight.pending}
            onCreate={createManagedProject}
            onSelect={selectManagedProject}
            onRefresh={onManualRefresh}
          />
        </main>
        {toast && <div id="toast" className={`visible toast-${toast.severity}`} role="status" aria-live="polite">{toast.message}</div>}
      </div>
    );
  }

  // Defensive secondary gate: even once projectsLoaded is true, the main
  // shell must not render until we have a `project` from /api/state. This
  // prevents the dialog from opening with `project` undefined if the
  // launcher mode is off but /api/state has not yet returned.
  if (!data || !data.project) {
    return (
      <div className="app-shell boot-loading" data-testid="boot-loading">
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
        <button type="button" data-testid="start-watcher-button" disabled={!canStartWatcher(data) || watcherInFlight.pending} aria-describedby="watcher-action-hint" aria-busy={watcherInFlight.pending} title={data?.runtime.supervisor.start_blocked_reason || watcher?.health.next_action || ""} onClick={() => void runWatcherAction("start")}>{watcherInFlight.pending ? <><Spinner /> Starting…</> : "Start watcher"}</button>
        <button type="button" data-testid="stop-watcher-button" disabled={!canStopWatcher(data) || watcherInFlight.pending} aria-describedby="watcher-action-hint" aria-busy={watcherInFlight.pending} title={watcher?.health.next_action || ""} onClick={() => void runWatcherAction("stop")}>{watcherInFlight.pending ? <><Spinner /> Stopping…</> : "Stop watcher"}</button>
        <p id="watcher-action-hint" className="sidebar-hint">{watcherHint}</p>
      </aside>

      <main className="workspace" aria-hidden={modalOpen ? true : undefined}>
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
              <MissionFocus
                data={data}
                lastError={lastError}
                resultBanner={resultBanner}
                watcherPending={watcherInFlight.pending}
                landPending={mergeAllInFlight.pending}
                onNewJob={openJobDialog}
                onStartWatcher={() => void runWatcherAction("start")}
                onLandReady={() => void mergeReadyTasks()}
                onOpenDiagnostics={() => navigateView("diagnostics")}
                onDismissError={() => setLastError(null)}
                onDismissResult={() => setResultBanner(null)}
              />
              <TaskBoard
                data={data}
                filters={filters}
                selectedRunId={selectedRunId}
                onSelect={selectRun}
              />
              <RecentActivity events={data?.events} history={data?.history.items || []} selectedRunId={selectedRunId} onSelect={selectRun} />
            </div>
            <RunDetailPanel
              detail={detail}
              landing={landing}
              onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
              onShowProof={showProof}
              onShowLogs={showLogs}
              onShowDiff={showDiff}
              onShowArtifacts={showArtifacts}
              onLoadArtifact={(index) => void loadArtifact(index)}
            />
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
              <div className="diagnostics-grid">
                <DiagnosticsSummary data={data} onSelect={selectRun} />
                <LiveRuns items={data?.live.items || []} landing={landing} selectedRunId={selectedRunId} onSelect={selectRun} />
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
                  onSelect={selectRun}
                  onChangePage={changeHistoryPage}
                  onChangePageSize={changeHistoryPageSize}
                />
              </div>
              <RunDetailPanel
                detail={detail}
                landing={landing}
                onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
                onShowProof={showProof}
                onShowLogs={showLogs}
                onShowDiff={showDiff}
                onShowArtifacts={showArtifacts}
                onLoadArtifact={(index) => void loadArtifact(index)}
              />
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
            </div>
          </section>
        )}
      </main>

      {jobOpen && (
        <JobDialog
          project={project}
          dirtyFiles={landing?.dirty_files || []}
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

function ProjectLauncher({projectsState, refreshStatus, refreshPending, onCreate, onSelect, onRefresh}: {
  projectsState: ProjectsResponse;
  refreshStatus: string;
  refreshPending: boolean;
  onCreate: (name: string) => Promise<void>;
  onSelect: (path: string) => Promise<void>;
  onRefresh: () => void;
}) {
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");
  const [statusKind, setStatusKind] = useState<"info" | "error">("info");
  const [pending, setPending] = useState(false);
  const projects = projectsState.projects || [];
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  // When the project list is empty, focus the create-form's name input so the
  // first-run user lands directly on the only sensible next step. mc-audit
  // codex-first-time-user.md #5.
  useEffect(() => {
    if (projects.length === 0) {
      nameInputRef.current?.focus();
    }
  }, [projects.length]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setStatus("Project name is required.");
      setStatusKind("error");
      return;
    }
    setPending(true);
    setStatus("Creating project");
    setStatusKind("info");
    try {
      await onCreate(trimmed);
      setName("");
      setStatus("");
    } catch (error) {
      setStatus(launcherErrorMessage(error, {projectName: trimmed}));
      setStatusKind("error");
    } finally {
      setPending(false);
    }
  }

  async function openProject(project: ManagedProjectInfo) {
    if (!project.path || pending) return;
    setPending(true);
    setStatus(`Opening ${project.name}`);
    setStatusKind("info");
    try {
      await onSelect(project.path);
      setStatus("");
    } catch (error) {
      setStatus(launcherErrorMessage(error, {projectPath: project.path}));
      setStatusKind("error");
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="project-launcher" aria-labelledby="projectLauncherHeading">
      <div className="launcher-head">
        <div>
          <span>Project folder</span>
          <h2 id="projectLauncherHeading">Project Launcher</h2>
          <p data-testid="launcher-subhead">
            Otto runs AI coding jobs in isolated git worktrees, then lets you review logs, diffs, and merge results.
          </p>
        </div>
        <div className="launcher-actions">
          {refreshLabel(refreshStatus) && <span className="muted">{refreshLabel(refreshStatus)}</span>}
          <button type="button" data-testid="launcher-refresh-button" disabled={refreshPending} aria-busy={refreshPending} onClick={onRefresh}>{refreshPending ? <><Spinner /> Refreshing…</> : "Refresh"}</button>
        </div>
      </div>

      <div className="launcher-grid">
        <form className="launcher-panel launcher-form" onSubmit={(event) => void submit(event)}>
          <div>
            <h3>Create project</h3>
            <p>Otto creates a folder and initializes a git repo under the projects root.</p>
          </div>
          <label>Project name
            <input
              ref={nameInputRef}
              value={name}
              data-testid="launcher-create-name-input"
              autoFocus
              type="text"
              placeholder="Expense approval portal"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <button className="primary" type="submit" data-testid="launcher-create-submit" disabled={pending}>{pending ? "Working" : "Create project"}</button>
          <p
            className={`launcher-status ${statusKind === "error" ? "launcher-status-error" : ""}`}
            data-testid="launcher-form-status"
            aria-live="polite"
          >{status}</p>
        </form>

        <div className="launcher-panel managed-root">
          <h3>Project folder root</h3>
          <code title={projectsState.projects_root}>{projectsState.projects_root}</code>
          <p data-testid="launcher-managed-root-help">
            All projects live under this directory. Otto creates a git worktree per project so other repos on this machine aren&apos;t affected. The repo that launched Mission Control is intentionally excluded — pick or create a project here to begin.
          </p>
        </div>
      </div>

      <div className="launcher-panel project-list-panel">
        <div className="panel-heading">
          <div>
            <h3>Open project</h3>
            <p className="panel-subtitle">Existing git repos under the projects root.</p>
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
            <div className="launcher-empty" data-testid="launcher-empty-state">
              <strong>Create your first Otto project below.</strong>
              <p>Pick a name in the form above. Otto initializes a git repo and you can queue your first build right after.</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

/**
 * Translate launcher mutation errors into recovery copy. Wraps
 * `friendlyApiMessage` for ApiError instances and falls back to the raw
 * message otherwise. mc-audit codex-first-time-user.md #15/#24.
 */
function launcherErrorMessage(error: unknown, context: {projectName?: string; projectPath?: string}): string {
  if (error instanceof ApiError) {
    return friendlyApiMessage(error.status, error.rawMessage, context);
  }
  return errorMessage(error);
}

function Toolbar({filters, refreshStatus, refreshPending, viewMode, onChange, onRefresh, onViewChange}: {
  filters: Filters;
  refreshStatus: string;
  refreshPending: boolean;
  viewMode: ViewMode;
  onChange: (filters: Filters) => void;
  onRefresh: () => void;
  onViewChange: (viewMode: ViewMode) => void;
}) {
  // Local mirror of the search query so we can debounce its commit to the
  // shared filters state without rate-limiting the visible textbox itself.
  // mc-audit microinteractions I3.
  const [localQuery, setLocalQuery] = useState(filters.query);
  const debouncedQuery = useDebouncedValue(localQuery, 200);
  const lastCommittedRef = useRef(filters.query);
  // Push the debounced value upward when it changes; do not loop back when
  // the parent prop changes externally (e.g. clear-filters resets us).
  useEffect(() => {
    if (debouncedQuery === lastCommittedRef.current) return;
    if (debouncedQuery === filters.query) return;
    lastCommittedRef.current = debouncedQuery;
    onChange({...filters, query: debouncedQuery});
  }, [debouncedQuery]);
  // Keep local in sync if the parent resets filters (e.g. Clear filters).
  useEffect(() => {
    if (filters.query !== lastCommittedRef.current) {
      lastCommittedRef.current = filters.query;
      setLocalQuery(filters.query);
    }
  }, [filters.query]);
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
          Diagnostics
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
            value={localQuery}
            type="search"
            placeholder="run, task, branch"
            data-testid="filter-search-input"
            onChange={(event) => setLocalQuery(event.target.value)}
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
        <button type="button" data-testid="toolbar-refresh-button" disabled={refreshPending} aria-busy={refreshPending} onClick={onRefresh}>{refreshPending ? <><Spinner /> Refreshing…</> : "Refresh"}</button>
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

function MissionFocus({data, lastError, resultBanner, watcherPending, landPending, onNewJob, onStartWatcher, onLandReady, onOpenDiagnostics, onDismissError, onDismissResult}: {
  data: StateResponse | null;
  lastError: string | null;
  resultBanner: ResultBannerState | null;
  watcherPending: boolean;
  landPending: boolean;
  onNewJob: () => void;
  onStartWatcher: () => void;
  onLandReady: () => void;
  onOpenDiagnostics: () => void;
  onDismissError: () => void;
  onDismissResult: () => void;
}) {
  const focus = missionFocus(data);
  return (
    <section className={`mission-focus focus-${focus.tone}`} data-testid="mission-focus" aria-label="Mission focus">
      <div className="focus-copy">
        <span>{focus.kicker}</span>
        <h2>{focus.title}</h2>
        <p>{focus.body}</p>
      </div>
      <div className="focus-actions">
        {focus.primary === "land" && (
          <button className="primary" type="button" data-testid="mission-land-ready-button" disabled={!canMerge(data?.landing) || landPending} aria-busy={landPending} onClick={onLandReady}>{landPending ? <><Spinner /> Landing…</> : "Land all ready"}</button>
        )}
        {focus.primary === "start" && (
          <button
            className="primary"
            type="button"
            data-testid="mission-start-watcher-button"
            disabled={!canStartWatcher(data) || watcherPending}
            aria-busy={watcherPending}
            title="Start the watcher process to run queued jobs."
            onClick={onStartWatcher}
          >
            {watcherPending
              ? <><Spinner /> Starting…</>
              : (Number(data?.watcher.counts.queued || 0) > 0 ? "Start queued job" : "Start watcher")}
          </button>
        )}
        {focus.primary === "diagnostics" && (
          <button className="primary" type="button" onClick={onOpenDiagnostics}>Review cleanup</button>
        )}
        {focus.primary === "new" && (
          <button className="primary" type="button" data-testid="mission-new-job-button" onClick={onNewJob}>{focus.firstRun ? "Start first build" : "New job"}</button>
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

function TaskBoard({data, filters, selectedRunId, onSelect}: {
  data: StateResponse | null;
  filters: Filters;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  const columns = taskBoardColumns(data, filters);
  return (
    <section className="panel task-board-panel" data-testid="task-board" aria-labelledby="taskBoardHeading">
      <div className="panel-heading">
        <div>
          <h2 id="taskBoardHeading">Task Board</h2>
          <p className="panel-subtitle">{taskBoardSubtitle(data, filters)}</p>
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
  const meta = [taskChangeLine(task), task.proof].filter(Boolean);
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
        <span className="task-card-meta">
          {meta.map((item) => <span key={item}>{item}</span>)}
        </span>
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
            <dt>Reason</dt><dd>{task.reason}</dd>
          </dl>
        </div>
      ) : null}
    </article>
  );
}

function RecentActivity({events, history, selectedRunId, onSelect}: {
  events: StateResponse["events"] | undefined;
  history: HistoryItem[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  const recentEvents = events?.items.slice(0, 4) || [];
  const recentHistory = history.slice(0, 4);
  return (
    <section className="panel activity-panel" aria-labelledby="activityHeading">
      <div className="panel-heading">
        <div>
          <h2 id="activityHeading">Recent Activity</h2>
          <p className="panel-subtitle">Latest queue, watcher, land, and run outcomes.</p>
        </div>
        <span className="pill">{(events?.total_count || 0) + history.length}</span>
      </div>
      <div className="activity-list">
        {recentEvents.map((event) => (
          <div className={`activity-item event-${event.severity}`} key={event.event_id || `${event.created_at}-${event.message}`}>
            <span>{event.severity}</span>
            <strong title={event.message}>{event.message}</strong>
            <time dateTime={event.created_at}>{formatEventTime(event.created_at)}</time>
          </div>
        ))}
        {recentHistory.map((item) => (
          <button
            className={`activity-item history-activity ${item.run_id === selectedRunId ? "selected" : ""}`}
            type="button"
            key={item.run_id}
            onClick={() => onSelect(item.run_id)}
          >
            <span>{item.outcome_display || item.status}</span>
            <strong title={item.summary}>{item.queue_task_id || item.run_id}</strong>
            <time>{item.duration_display || "-"}</time>
          </button>
        ))}
        {!recentEvents.length && !recentHistory.length && <div className="timeline-empty">No activity yet.</div>}
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

function History({
  items,
  totalRows,
  page,
  totalPages,
  pageSize,
  requestedPage,
  loaded,
  selectedRunId,
  onSelect,
  onChangePage,
  onChangePageSize,
}: {
  items: HistoryItem[];
  totalRows: number;
  page: number;
  totalPages: number;
  pageSize: number;
  // The page the *user* asked for, in 1-based terms. May exceed totalPages
  // if a stale deep-link was pasted; in that case the server clamps and
  // returns the last valid page in `page`, and we render a recovery hint.
  requestedPage: number;
  // Whether we have a server response yet. Drives the "loading" copy when
  // navigating between pages so the table doesn't flash to "No matching
  // history" while the next response is in flight.
  loaded: boolean;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  onChangePage: (nextPage: number) => void;
  onChangePageSize: (nextSize: number) => void;
}) {
  // Local mirror for the jump-to-page input. Plain text input so the user
  // can clear it without us snapping back to the canonical page; we commit
  // on Enter or blur.
  const [jumpDraft, setJumpDraft] = useState<string>(String(page));
  useEffect(() => {
    setJumpDraft(String(page));
  }, [page]);

  const requestedOutOfRange = loaded && requestedPage > totalPages;
  const showRecovery = requestedOutOfRange && totalRows > 0;

  const commitJump = () => {
    const parsed = Number.parseInt(jumpDraft, 10);
    if (!Number.isFinite(parsed) || parsed < 1) {
      setJumpDraft(String(page));
      return;
    }
    onChangePage(parsed);
  };

  return (
    <section className="panel history-panel" aria-labelledby="historyHeading">
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
            {showRecovery ? (
              <tr>
                <td colSpan={5} className="empty-cell" data-testid="history-out-of-range">
                  Page {requestedPage} doesn&rsquo;t exist; only {totalPages} {totalPages === 1 ? "page" : "pages"} available.
                  {" "}
                  <button type="button" data-testid="history-recover-button" onClick={() => onChangePage(1)}>
                    Jump to page 1
                  </button>
                </td>
              </tr>
            ) : items.length ? items.map((item) => (
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
              <tr><td colSpan={5} className="empty-cell">{loaded ? "No matching history." : "Loading…"}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {(totalRows > 0 || totalPages > 1) && (
        <nav
          className="history-pagination"
          data-testid="history-pagination"
          aria-label="History pagination"
        >
          <span className="history-pagination-status" data-testid="history-pagination-status">
            Page {page} of {totalPages} &middot; {totalRows} {totalRows === 1 ? "run" : "runs"}
          </span>
          <div className="history-pagination-controls">
            <button
              type="button"
              data-testid="history-prev-button"
              disabled={page <= 1}
              aria-disabled={page <= 1}
              onClick={() => onChangePage(page - 1)}
            >
              &larr; Previous
            </button>
            <label className="history-pagination-jump">
              Go to
              <input
                type="number"
                min={1}
                max={totalPages}
                value={jumpDraft}
                data-testid="history-jump-input"
                aria-label="Jump to page"
                onChange={(event) => setJumpDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    commitJump();
                  }
                }}
                onBlur={commitJump}
              />
            </label>
            <button
              type="button"
              data-testid="history-next-button"
              disabled={page >= totalPages}
              aria-disabled={page >= totalPages}
              onClick={() => onChangePage(page + 1)}
            >
              Next &rarr;
            </button>
            <label className="history-pagination-size">
              Per page
              <select
                value={pageSize}
                data-testid="history-page-size-select"
                onChange={(event) => onChangePageSize(Number.parseInt(event.target.value, 10))}
              >
                {HISTORY_PAGE_SIZE_OPTIONS.map((option) => (
                  <option value={option} key={option}>{option}</option>
                ))}
              </select>
            </label>
          </div>
        </nav>
      )}
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

function RunDetailPanel({detail, landing, onRunAction, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadArtifact}: {
  detail: RunDetail | null;
  landing: LandingState | undefined;
  onRunAction: (action: string, label?: string) => void;
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
            <RecoveryActionBar actions={detail.legal_actions || []} status={detail.display_status} onRunAction={onRunAction} />
            <ReviewPacket packet={detail.review_packet} onRunAction={onRunAction} onLoadArtifact={onLoadArtifact} onShowArtifacts={onShowArtifacts} />
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
                  <dt>Artifacts</dt><dd>{detail.artifacts.length}</dd>
                  {detail.overlay && <><dt>Overlay</dt><dd>{detail.overlay.reason}</dd></>}
                  {detail.summary_lines.map((line, index) => <DetailLine key={`${line}-${index}`} line={line} />)}
                </dl>
              </div>
            </details>
            <ActionBar actions={detail.legal_actions || []} mergeBlocked={Boolean(landing?.merge_blocked)} onRunAction={onRunAction} />
          </div>
          <div className="detail-inspector-actions" aria-label="Evidence shortcuts">
            <button className="primary" type="button" data-testid="open-proof-button" onClick={onShowProof}>Review result</button>
            <button type="button" data-testid="open-diff-button" disabled={!canShowDiff(detail)} title={canShowDiff(detail) ? "" : diffDisabledReason(detail)} onClick={onShowDiff}>Code changes</button>
            <button type="button" data-testid="open-logs-button" onClick={onShowLogs}>Logs</button>
            <button type="button" data-testid="open-artifacts-button" onClick={onShowArtifacts}>Artifacts</button>
          </div>
        </>
      ) : (
        <div className="detail-body empty" data-testid="run-detail-empty">
          Select a task card to review logs, code changes, verification, and next action.
        </div>
      )}
    </aside>
  );
}

function RunInspector({detail, mode, logState, selectedArtifactIndex, artifactContent, proofArtifactIndex, proofContent, diffContent, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadProofArtifact, onLoadArtifact, onRefreshDiff, onBackToArtifacts, onClose}: {
  detail: RunDetail;
  mode: InspectorMode;
  logState: LogState;
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  proofArtifactIndex: number | null;
  proofContent: ArtifactContentResponse | null;
  diffContent: DiffResponse | null;
  onShowProof: () => void;
  onShowLogs: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
  onLoadProofArtifact: (index: number) => void;
  onLoadArtifact: (index: number) => void;
  onRefreshDiff: () => void;
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
          <p>{detailStatusLabel(detail)} review</p>
        </div>
        <div className="detail-tabs" role="tablist" aria-label="Evidence view">
          <button className={`tab ${mode === "proof" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "proof"} onClick={onShowProof}>Result</button>
          <button className={`tab ${mode === "diff" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "diff"} disabled={!canShowDiff(detail)} title={canShowDiff(detail) ? "" : diffDisabledReason(detail)} onClick={onShowDiff}>Code changes</button>
          <button className={`tab ${mode === "logs" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "logs"} onClick={onShowLogs}>Logs</button>
          <button className={`tab ${mode === "artifacts" ? "active" : ""}`} type="button" role="tab" aria-selected={mode === "artifacts"} onClick={onShowArtifacts}>Artifacts</button>
        </div>
        <button type="button" data-testid="close-inspector-button" onClick={onClose}>Close inspector</button>
      </div>
      <div className="run-inspector-body">
        {mode === "proof" ? (
          <ProofPane detail={detail} proofArtifactIndex={proofArtifactIndex} proofContent={proofContent} onShowDiff={onShowDiff} onLoadProofArtifact={onLoadProofArtifact} />
        ) : mode === "diff" ? (
          <DiffPane diff={diffContent} onRefresh={onRefreshDiff} />
        ) : mode === "logs" ? (
          <LogPane logState={logState} runActive={detail.active} onRetry={onShowLogs} />
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

function LogPane({logState, runActive, onRetry}: {logState: LogState; runActive: boolean; onRetry: () => void}) {
  const {text, status, error, path, totalBytes, totalLines, droppedBytes, lastUpdatedAt, pollIntervalMs} = logState;
  // Display lines are derived from the unbounded `totalLines` counter so the
  // header reflects the *full* log size, not just what fits in the tail
  // buffer. We never re-split the buffer per render — that's the whole bug.
  const displayLines = totalLines > 0 ? totalLines : (text ? 1 : 0);
  const headerStatus = describeLogHeader({runActive, status, lastUpdatedAt, pollIntervalMs, displayLines, totalBytes});
  const droppedNote = droppedBytes > 0 ? `${humanBytes(droppedBytes)} earlier bytes elided` : null;

  // Empty/missing/error rendering — these states replace the bare "waiting
  // for output" placeholder with state-specific copy + a recovery action.
  let body: ReactNode;
  if (status === "missing") {
    body = (
      <div className="log-empty" data-testid="log-empty-missing">
        {path ? `No log file at ${path}.` : "Log will appear when the agent starts writing."}
      </div>
    );
  } else if (status === "error") {
    body = (
      <div className="log-empty log-error" data-testid="log-empty-error">
        <p>Could not read log{error ? `: ${error}` : "."}</p>
        <button type="button" data-testid="log-retry-button" onClick={onRetry}>Retry</button>
      </div>
    );
  } else if (!text) {
    body = (
      <div className="log-empty" data-testid="log-empty-waiting">
        {status === "loading" ? "Loading log…" : "Log will appear when the agent starts writing."}
      </div>
    );
  } else {
    body = (
      <pre
        className="log-pane log-content"
        tabIndex={0}
        aria-label="Run log output"
        data-testid="run-log-pane"
      >{renderLogText(text)}</pre>
    );
  }

  return (
    <div className="log-viewer">
      <div className="log-toolbar">
        <strong>Run logs</strong>
        <span data-testid="log-pane-status">{headerStatus}</span>
        {droppedNote && (
          <span className="log-elided" data-testid="log-pane-elided">{droppedNote}</span>
        )}
      </div>
      {body}
    </div>
  );
}

function describeLogHeader({runActive, status, lastUpdatedAt, pollIntervalMs, displayLines, totalBytes}: {
  runActive: boolean;
  status: LogStatus;
  lastUpdatedAt: number | null;
  pollIntervalMs: number;
  displayLines: number;
  totalBytes: number;
}): string {
  if (runActive) {
    const cadence = (pollIntervalMs / 1000).toFixed(pollIntervalMs >= 10_000 ? 0 : 1);
    if (lastUpdatedAt === null) return `Live · polling every ${cadence}s`;
    const ageSec = Math.max(0, Math.round((Date.now() - lastUpdatedAt) / 1000));
    return `Live · polling every ${cadence}s · last update ${ageSec}s ago`;
  }
  if (status === "missing") return "No log file";
  if (displayLines === 0 && totalBytes === 0) return "waiting for output";
  return `Final · ${displayLines.toLocaleString()} line${displayLines === 1 ? "" : "s"} · ${humanBytes(totalBytes)}`;
}

function humanBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
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
        <h3 id="proofChecksHeading">Verification</h3>
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

function DiffPane({diff, onRefresh}: {diff: DiffResponse | null; onRefresh: () => void}) {
  // All hooks must run on every render — bail-out branches must come AFTER
  // the hook calls or React throws "Rendered more hooks than previous"
  // (#310). Order matters here.
  const sections = useMemo(() => splitDiffIntoFiles(diff?.text || "", diff?.files || []), [diff?.text, diff?.files]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  // Re-render the "captured X ago" relative time once a second so the
  // header doesn't lie when the operator stares at the panel.
  const [, setNowTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setNowTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    setSelectedPath(sections[0]?.path || null);
  }, [diff?.run_id, diff?.text, sections]);
  const command = diff?.command || null;
  const copyCommand = useCallback(() => {
    if (!command) return;
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(command);
    }
  }, [command]);
  const selected = sections.find((section) => section.path === selectedPath) || sections[0] || null;
  if (!diff) {
    return (
      <div className="diff-viewer" data-testid="diff-pane">
        <div className="diff-toolbar"><strong>Code diff</strong><span>loading</span></div>
        <pre className="diff-pane">Loading diff...</pre>
      </div>
    );
  }
  const targetShaShort = diff.target_sha ? diff.target_sha.slice(0, 7) : null;
  const branchShaShort = diff.branch_sha ? diff.branch_sha.slice(0, 7) : null;
  const mergeBaseShort = diff.merge_base ? diff.merge_base.slice(0, 7) : null;
  const ageLabel = diff.fetched_at ? formatRelativeFreshness(diff.fetched_at) : null;
  const truncationBanner = diff.truncated
    ? formatDiffTruncationBanner(diff)
    : null;
  return (
    <div className="diff-viewer" data-testid="diff-pane">
      <div className="diff-freshness" data-testid="diff-freshness">
        <div className="diff-freshness-meta">
          {ageLabel && <span data-testid="diff-fetched-at">Captured {ageLabel}</span>}
          {targetShaShort ? (
            <span data-testid="diff-target-sha" title={diff.target_sha || ""}>target {diff.target} @ {targetShaShort}</span>
          ) : (
            <span className="diff-warning" data-testid="diff-target-sha-missing">⚠ Could not resolve target SHA; diff may be stale.</span>
          )}
          {diff.branch && diff.branch !== diff.target ? (
            branchShaShort ? (
              <span data-testid="diff-branch-sha" title={diff.branch_sha || ""}>branch {diff.branch} @ {branchShaShort}</span>
            ) : (
              <span className="diff-warning" data-testid="diff-branch-sha-missing">⚠ Could not resolve branch SHA; diff may be stale.</span>
            )
          ) : null}
          {mergeBaseShort ? (
            <span data-testid="diff-merge-base" title={diff.merge_base || ""}>base {mergeBaseShort}</span>
          ) : null}
        </div>
        <button
          type="button"
          className="diff-refresh-button"
          data-testid="diff-refresh-button"
          onClick={onRefresh}
        >
          Refresh
        </button>
      </div>
      <div className="diff-toolbar">
        <strong>Code diff</strong>
        <span>{diff.branch || "-"} → {diff.target}</span>
      </div>
      {truncationBanner ? (
        <div className="diff-truncation" data-testid="diff-truncation">
          <span>{truncationBanner}</span>
          {diff.command ? (
            <button type="button" data-testid="diff-copy-command-button" onClick={copyCommand}>
              Copy diff command
            </button>
          ) : null}
        </div>
      ) : null}
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

// Render "Showing N hunks of M · X KB of Y MB" so the operator can tell
// how much of the diff is hidden by the 240k char slice. Falls back to a
// concise byte-only line when the server didn't report a hunk count.
function formatDiffTruncationBanner(diff: DiffResponse): string {
  const shownBytes = humanBytes(new TextEncoder().encode(diff.text || "").length);
  const totalBytes = humanBytes(new TextEncoder().encode("a".repeat(Math.max(0, diff.full_size_chars))).length);
  // Cheap upper-bound: char counts are ~bytes for ASCII source diff. Use
  // the raw chars to avoid encoding the entire diff just for a banner.
  const shownChars = diff.text ? diff.text.length : 0;
  const fullChars = diff.full_size_chars || 0;
  const shownLabel = humanBytes(shownChars);
  const totalLabel = humanBytes(fullChars);
  const hunksPart = diff.total_hunks > 0
    ? `Showing ${diff.shown_hunks.toLocaleString()} hunk${diff.shown_hunks === 1 ? "" : "s"} of ${diff.total_hunks.toLocaleString()}`
    : "Diff was truncated";
  const sizePart = `shown ${shownLabel} of ${totalLabel}`;
  // shownBytes/totalBytes only used to keep the helper imports honest in
  // case we later switch to a real byte count. Suppress unused warnings.
  void shownBytes;
  void totalBytes;
  return `${hunksPart} · ${sizePart}`;
}

// Render an ISO timestamp as "Xs ago", "Xm Ys ago", etc. Returns "just now"
// for sub-second deltas and "in the future" if the server clock is ahead.
function formatRelativeFreshness(iso: string): string {
  const fetched = Date.parse(iso);
  if (!Number.isFinite(fetched)) return "unknown";
  const deltaSec = Math.round((Date.now() - fetched) / 1000);
  if (deltaSec < 0) return "just now";
  if (deltaSec < 5) return "just now";
  if (deltaSec < 60) return `${deltaSec}s ago`;
  const minutes = Math.floor(deltaSec / 60);
  const seconds = deltaSec % 60;
  if (minutes < 60) {
    return seconds > 0 ? `${minutes}m ${seconds}s ago` : `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  return remMin > 0 ? `${hours}h ${remMin}m ago` : `${hours}h ago`;
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
  const artifactCount = packet.evidence.length;
  const readableEvidence = packet.evidence.filter(isReadableArtifact);
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
    ? `${readableEvidence.length}/${packet.evidence.length}`
    : `${packet.evidence.length}`;
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

/**
 * Surface the most-relevant recovery action (Retry / Resume / Cleanup /
 * Requeue) next to the run header for failed/paused/interrupted runs. The
 * full set still lives under "Advanced run actions" below — this bar is a
 * shortcut for the obvious-next-step. mc-audit codex-first-time-user.md #14.
 */
function RecoveryActionBar({actions, status, onRunAction}: {
  actions: ActionState[];
  status: string;
  onRunAction: (action: string, label?: string) => void;
}) {
  const recovery = pickRecoveryActions(actions, status);
  if (!recovery.length) return null;
  return (
    <div
      className="recovery-action-bar"
      data-testid="recovery-action-bar"
      role="toolbar"
      aria-label="Recovery actions"
    >
      {recovery.map((action, idx) => {
        const name = actionName(action.key);
        return (
          <button
            key={action.key}
            type="button"
            className={idx === 0 ? "primary" : ""}
            data-testid={`recovery-action-${name}`}
            disabled={!action.enabled}
            title={action.reason || action.preview || ""}
            onClick={() => onRunAction(name, action.label)}
          >
            {reviewActionLabel(action.label)}
          </button>
        );
      })}
    </div>
  );
}

const RECOVERABLE_STATUSES = new Set([
  "failed",
  "cancelled",
  "interrupted",
  "stale",
  "paused",
  "needs_attention",
]);

const RECOVERY_ACTION_KEYS = ["R", "r", "x"];

function pickRecoveryActions(actions: ActionState[], status: string | null | undefined): ActionState[] {
  const normalized = String(status || "").toLowerCase();
  if (!RECOVERABLE_STATUSES.has(normalized)) return [];
  // Honor the order in RECOVERY_ACTION_KEYS (Retry > Resume > Cleanup) so
  // the primary slot is the most-likely next step.
  const byKey = new Map<string, ActionState>();
  for (const action of actions) byKey.set(action.key, action);
  const result: ActionState[] = [];
  for (const key of RECOVERY_ACTION_KEYS) {
    const match = byKey.get(key);
    if (match) result.push(match);
  }
  return result;
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

function JobDialog({project, dirtyFiles, onClose, onQueued, onError}: {
  project: StateResponse["project"] | undefined;
  dirtyFiles: string[];
  onClose: () => void;
  onQueued: (message?: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [command, setCommand] = useState<JobCommand>("build");
  const [subcommand, setSubcommand] = useState<"bugs" | "feature" | "target">("bugs");
  const [intent, setIntent] = useState("");
  const [taskId, setTaskId] = useState("");
  const [after, setAfter] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [certification, setCertification] = useState<CertificationPolicy>("");
  const [targetConfirmed, setTargetConfirmed] = useState(false);
  const [status, setStatus] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Whether the Advanced section should be programmatically opened. The
  // pre-submit summary "Edit" link sets this so users get one-click access
  // to the provider/model/effort fields without scrolling through Otto
  // jargon. mc-audit codex-first-time-user.md #2.
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const advancedRef = useRef<HTMLDetailsElement | null>(null);
  const dialogRef = useDialogFocus<HTMLFormElement>(onClose, submitting);
  const targetNeedsConfirmation = Boolean(project?.dirty);
  // ALL commands now require a non-empty intent (or focus). Codex flagged
  // that improve/certify could queue blank — equivalent to "do something
  // unspecified", which is never what a user means. mc-audit
  // codex-first-time-user.md #8.
  const intentRequired = !intent.trim();
  const submitDisabled = submitting || intentRequired || (targetNeedsConfirmation && !targetConfirmed);

  // Pre-submit summary fields. We resolve the visible "will run with" line
  // by combining the user's selection with the project's defaults. mc-audit
  // codex-first-time-user.md #2.
  const summary = jobRunSummary({command, subcommand, project, provider, model, effort, certification});
  const intentLabelMap: Record<JobCommand, string> = {
    build: "Intent",
    improve: "Focus",
    certify: "Focus",
  };
  const intentPlaceholderMap: Record<JobCommand, string> = {
    build: "Describe what you want Otto to build.",
    improve: "Describe what to refine, fix, or extend in the existing run.",
    certify: "Describe what to verify in the existing run.",
  };
  const commandHelpMap: Record<JobCommand, string> = {
    build: "Build new work from your description.",
    improve: "Iterate on an existing run (refine, fix bugs, extend feature).",
    certify: "Verify an existing run against acceptance criteria.",
  };

  useEffect(() => {
    setTargetConfirmed(false);
  }, [project?.path]);

  useEffect(() => {
    if (!certificationPolicyAllowed(command, subcommand, certification)) {
      setCertification("");
    }
  }, [certification, command, subcommand]);

  // Sync the <details> open state when the user clicks the "Edit" link in
  // the summary. The native attribute change has to land on the DOM node so
  // the disclosure widget actually toggles open without a re-render race.
  useEffect(() => {
    const el = advancedRef.current;
    if (!el) return;
    if (advancedOpen && !el.open) el.open = true;
  }, [advancedOpen]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (intentRequired) {
      setStatus(`${intentLabelMap[command]} is required.`);
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
        provider,
        model,
        effort,
        certification,
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

  const dirtyPreview = dirtyFiles.slice(0, 5);
  const dirtyOverflow = Math.max(0, dirtyFiles.length - dirtyPreview.length);

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
        <div className="job-summary" data-testid="job-dialog-summary" aria-label="Run summary">
          <strong>Will run with:</strong>
          <span data-testid="job-dialog-summary-text">{summary}</span>
          <button
            type="button"
            className="job-summary-edit"
            data-testid="job-dialog-summary-edit"
            onClick={() => {
              setAdvancedOpen(true);
              advancedRef.current?.scrollIntoView({block: "nearest"});
            }}
          >Edit</button>
        </div>
        <label>Command
          <select data-testid="job-command-select" value={command} onChange={(event) => setCommand(event.target.value as JobCommand)}>
            <option value="build">Build</option>
            <option value="improve">Improve</option>
            <option value="certify">Certify</option>
          </select>
          <span className="field-hint" data-testid="job-command-help">{commandHelpMap[command]}</span>
        </label>
        <div className={`target-guard ${project?.dirty ? "target-dirty" : ""}`} aria-label="Target project">
          <strong>Target project</strong>
          <dl>
            <dt>Path</dt><dd title={project?.path || ""}>{project?.path || "loading"}</dd>
            <dt>Branch</dt><dd>{project?.branch || "-"}</dd>
            <dt>State</dt><dd>{project ? project.dirty ? "dirty" : "clean" : "unknown"}</dd>
          </dl>
          <p>This job can create temporary git worktrees and modify files under this folder.</p>
          {targetNeedsConfirmation && (
            <>
              {dirtyPreview.length ? (
                <div className="target-dirty-files" data-testid="job-dialog-dirty-files" aria-label="Uncommitted files">
                  <strong>Uncommitted changes ({dirtyFiles.length})</strong>
                  <ul>
                    {dirtyPreview.map((path) => <li key={path}>{path}</li>)}
                    {dirtyOverflow > 0 && <li>+{dirtyOverflow} more</li>}
                  </ul>
                  <span>Commit, stash, or revert these before queueing if they shouldn&apos;t affect this job.</span>
                </div>
              ) : null}
              <label className="check-label target-confirm">
                <input
                  checked={targetConfirmed}
                  data-testid="target-project-confirm"
                  type="checkbox"
                  onChange={(event) => setTargetConfirmed(event.target.checked)}
                />
                I understand this dirty project may affect the queued work
              </label>
            </>
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
        <label>{intentLabelMap[command]}
          <textarea
            value={intent}
            data-testid="job-dialog-intent"
            rows={5}
            placeholder={intentPlaceholderMap[command]}
            aria-describedby={submitDisabled ? "jobDialogValidationHint" : undefined}
            aria-invalid={intentRequired ? true : undefined}
            onChange={(event) => setIntent(event.target.value)}
          />
        </label>
        {submitDisabled && !submitting && (
          <p id="jobDialogValidationHint" className="job-dialog-validation" data-testid="job-dialog-validation-hint" aria-live="polite">
            {intentRequired
              ? `Describe the requested outcome (${intentLabelMap[command].toLowerCase()}) to enable queueing.`
              : targetNeedsConfirmation && !targetConfirmed
              ? "Confirm the dirty target project above to enable queueing."
              : "Submit is disabled."}
          </p>
        )}
        <details
          className="job-advanced"
          ref={advancedRef}
          open={advancedOpen}
          onToggle={(event) => setAdvancedOpen((event.target as HTMLDetailsElement).open)}
        >
          <summary>Advanced options</summary>
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
          <button
            className="primary"
            type="submit"
            data-testid="job-dialog-submit-button"
            disabled={submitDisabled}
            aria-busy={submitting}
            title={!submitting && submitDisabled ? (
              intentRequired
                ? `Describe the requested outcome (${intentLabelMap[command].toLowerCase()}) to enable queueing.`
                : "Confirm the dirty target project above."
            ) : undefined}
          >
            {submitting ? <><Spinner /> Queueing…</> : "Queue job"}
          </button>
        </footer>
      </form>
    </div>
  );
}

/**
 * Build the human-readable "Will run with: …" line shown above Advanced
 * options. Mirrors the precedence the queue payload builder uses: the user
 * override wins, otherwise we fall back to the project's effective defaults
 * coming from otto.yaml. mc-audit codex-first-time-user.md #2.
 */
function jobRunSummary({command, subcommand, project, provider, model, effort, certification}: {
  command: JobCommand;
  subcommand: ImproveSubcommand;
  project: StateResponse["project"] | undefined;
  provider: string;
  model: string;
  effort: string;
  certification: CertificationPolicy;
}): string {
  const defaults = project?.defaults;
  const providerLabel = provider || defaults?.provider || "default provider";
  const modelLabel = model.trim() || defaults?.model || "default model";
  const effortLabel = effort || defaults?.reasoning_effort || "default effort";
  const verificationLabel = describeVerificationPolicy(command, subcommand, certification, project);
  return `${providerLabel} · ${modelLabel} · effort=${effortLabel} · verification=${verificationLabel}`;
}

function describeVerificationPolicy(
  command: JobCommand,
  subcommand: ImproveSubcommand,
  certification: CertificationPolicy,
  project: StateResponse["project"] | undefined,
): string {
  if (certification === "skip") return "skipped";
  if (certification) return certification;
  if (command === "improve" && subcommand === "feature") return "hillclimb";
  if (command === "improve" && subcommand === "target") return "target";
  if (command === "improve" && subcommand === "bugs") return "thorough (improve default)";
  const defaults = project?.defaults;
  if (defaults?.skip_product_qa) return "skipped (project default)";
  return defaults?.certifier_mode || "fast";
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
          <button className={confirmClass} type="button" data-testid="confirm-dialog-confirm-button" disabled={pending} aria-busy={pending} onClick={onConfirm}>
            {pending ? <><Spinner /> Working…</> : confirm.confirmLabel}
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
  primary: "new" | "start" | "land" | "diagnostics";
  working: number;
  needsAction: number;
  ready: number;
  // True when the project has no completed runs and nothing in flight — this
  // is the very first time the user opens this project. The MissionFocus
  // component reads this to flip the primary CTA from "New job" to
  // "Start first build". mc-audit codex-first-time-user.md #6.
  firstRun: boolean;
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
      firstRun: false,
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
      firstRun: false,
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
      firstRun: false,
    };
  }
  if (needsAction) {
    return {
      kicker: "Attention",
      title: `${needsAction} task${needsAction === 1 ? "" : "s"} need action`,
      body: "Open blocked work to inspect the failure, stale run, missing branch, or recovery action.",
      tone: "warning",
      primary: "diagnostics",
      working,
      needsAction,
      ready,
      firstRun: false,
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
      firstRun: false,
    };
  }
  if (queued && data.watcher.health.state !== "running") {
    return {
      kicker: "Queue",
      title: `${queued} queued task${queued === 1 ? "" : "s"} waiting`,
      body: "Start the watcher to run the queued job.",
      tone: "info",
      primary: "start",
      working,
      needsAction,
      ready,
      firstRun: false,
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
      firstRun: false,
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
      firstRun: false,
    };
  }
  return {
    kicker: "Start",
    title: "Start your first build",
    body: "Describe what you want Otto to build. Otto will plan, code, and verify it inside an isolated git worktree, then surface logs, diffs, and a result for review.",
    tone: "neutral",
    primary: "new",
    working,
    needsAction,
    ready,
    firstRun: true,
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
    const card = boardTaskFromLanding(item, runId, !data.landing.merge_blocked);
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

function boardTaskFromLanding(item: LandingItem, runId: string | null, mergeAllowed: boolean): BoardTask {
  const stage = boardStageForLanding(item, mergeAllowed);
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
    reason: boardReasonForLanding(item, mergeAllowed),
    source: "landing",
  };
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

function boardReasonForLanding(item: LandingItem, mergeAllowed: boolean): string {
  if (item.landing_state === "ready" && !mergeAllowed) return "Repository cleanup required before landing.";
  if (item.landing_state === "ready") return `${changeLine(item)} changed; ${proofLine(item)} recorded.`;
  if (item.landing_state === "merged") return item.merge_id ? `Landed by ${item.merge_id}.` : "Already landed.";
  if (item.queue_status === "queued") return "Waiting for the watcher.";
  if (item.queue_status === "initializing") return "Child process started; waiting for Otto session readiness.";
  if (["starting", "running", "terminating"].includes(item.queue_status)) return "Task is still in flight.";
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
  return `${Math.round(seconds / 3600)}h`;
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

function isWaitingLandingItem(item: LandingItem): boolean {
  return item.landing_state === "blocked" && ["queued", "starting", "initializing", "running", "terminating"].includes(item.queue_status);
}

function providerLine(detail: RunDetail): string {
  return [detail.provider, detail.model, detail.reasoning_effort].filter(Boolean).join(" / ") || "-";
}

function detailStatusLabel(detail: RunDetail): string {
  const readiness = detail.review_packet.readiness.state;
  if (readiness === "blocked" || readiness === "merged") return readiness;
  return detail.display_status || "-";
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

// Build the merge confirm dialog body from the most recent diff fetch.
// We spell out branch + target with their captured SHAs so the operator
// can compare against the diff metadata header before clicking through —
// this is the human half of the diff-freshness contract.
function mergeConfirmationBody(diff: DiffResponse): string {
  const target = diff.target || "target";
  const branch = diff.branch || "branch";
  const targetSha = diff.target_sha ? diff.target_sha.slice(0, 7) : null;
  const branchSha = diff.branch_sha ? diff.branch_sha.slice(0, 7) : null;
  const branchPart = branchSha ? `${branch} @ ${branchSha}` : branch;
  const targetPart = targetSha ? `${target} @ ${targetSha}` : target;
  const lead = `Land branch ${branchPart} into target ${targetPart}?`;
  return `${lead} This is the diff you reviewed.`;
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
  const existing = packet.evidence.filter(isReadableArtifact).length;
  if (!packet.evidence.length) return "-";
  if (!existing) return "not attached";
  return `${existing}/${packet.evidence.length}`;
}

function preferredProofArtifact(artifacts: ArtifactRef[]): ArtifactRef | null {
  const existing = artifacts.filter(isReadableArtifact);
  if (!existing.length) return null;
  const preferredLabels = ["summary", "queue manifest", "manifest", "intent", "primary log"];
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

/**
 * Human-readable reason that the Diff control is disabled. Used for `title=`
 * on every Diff button so operators are not left guessing why the control is
 * grey (mc-audit microinteractions C4).
 */
function diffDisabledReason(detail: RunDetail | null): string {
  if (!detail) return "Select a run to view its diff.";
  const packet = detail.review_packet;
  if (packet.changes.diff_error) return `Diff failed: ${packet.changes.diff_error}`;
  if (!packet.changes.branch) return "Diff is unavailable until the task creates a branch.";
  if (packet.readiness.state === "in_progress") return "Diff is unavailable while the run is still in progress.";
  return "Diff is not available for this run.";
}

function isReadableArtifact(artifact: ArtifactRef): boolean {
  return artifact.exists && artifact.kind !== "directory";
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
  if (/(\bfatal\b|\berror\b|traceback|exception|\bfailed\b|\bfail\b|exit code [1-9])/.test(clean)) return "log-line-error";
  if (/(\bwarn\b|warning|blocked|stale|retry|skipped|caution)/.test(clean)) return "log-line-warn";
  if (/(\bpass\b|passed|success|completed|ready|done)/.test(clean)) return "log-line-success";
  if (/(story_result|story result|pytest|npm|uv run|\btest\b|collecting|running|\[build\]|\[certify\]|\[merge\]|\[queue\]|\binfo\b)/.test(clean)) return "log-line-info";
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
