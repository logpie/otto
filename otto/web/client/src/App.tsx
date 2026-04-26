import {FormEvent, useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent, ReactNode} from "react";
import {ApiError, api, buildQueuePayload, friendlyApiMessage, runDetailUrl, stateQueryParams} from "./api";
import {Spinner} from "./components/Spinner";
import {useInFlight} from "./hooks/useInFlight";
import {useDebouncedValue} from "./hooks/useDebouncedValue";
import {useCrossTabChannel} from "./hooks/useCrossTabChannel";
import type {
  ActionResult,
  ActionState,
  ArtifactContentResponse,
  ArtifactRef,
  CertificationPolicy,
  CertificationRound,
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
  ProofReportInfo,
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
  // Plain-text body. Always populated so existing callers keep working;
  // structured callers can also provide `bodyContent` for a richer render.
  body: string;
  // Optional rich body (scrollable list, dl, etc). Renders BELOW `body`.
  bodyContent?: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  // When set, the confirm button stays disabled until the user ticks the
  // checkbox. Used for high-blast-radius bulk operations like "Land 7 tasks"
  // (mc-audit codex-destructive-action-safety #1).
  requireCheckbox?: {label: string} | undefined;
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

// Sortable columns for the History table. `null` means "no sort applied"
// (the user has cycled past desc back to default). We keep this small —
// extra columns can be added by extending the union and historyComparators.
type HistorySortColumn = "outcome" | "run" | "summary" | "duration" | "usage";
type HistorySortDir = "asc" | "desc";

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
  // Heavy-user paper-cut #1 (filters): persist the four toolbar filters in
  // the URL so a power-user can refresh, back-button, and share filtered
  // views with a teammate. Defaults match `defaultFilters` and are omitted
  // from the URL when at default to keep links clean.
  filterType: RunTypeFilter;
  filterOutcome: OutcomeFilter;
  filterQuery: string;
  filterActiveOnly: boolean;
  // Heavy-user paper-cut #2 (history sort): URL keys `?hs=col&hd=desc`.
  // Null means "no sort override" — the server's natural order wins.
  historySort: HistorySortColumn | null;
  historySortDir: HistorySortDir | null;
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
  // mc-audit info-density #2: typed chip data so the card renders distinct
  // labelled chips instead of a row of unlabeled pills. `null` means the
  // chip is suppressed (rather than rendering "-" placeholder).
  storiesPassed?: number | null;
  storiesTested?: number | null;
  costDisplay?: string | null;
  durationDisplay?: string | null;
}

const defaultFilters: Filters = {
  type: "all",
  outcome: "all",
  query: "",
  activeOnly: false,
};

// Allowed values for the URL-persisted filter params. We validate on read so
// a hand-crafted URL with `?ft=banana` doesn't crash the SPA — invalid values
// silently fall back to "all" / defaultFilters.
const RUN_TYPE_VALUES: readonly RunTypeFilter[] = ["all", "build", "improve", "certify", "merge", "queue"];
const OUTCOME_VALUES: readonly OutcomeFilter[] = ["all", "success", "failed", "interrupted", "cancelled", "removed", "other"];
const HISTORY_SORT_COLUMNS: readonly HistorySortColumn[] = ["outcome", "run", "summary", "duration", "usage"];

function defaultRouteState(): RouteState {
  return {
    viewMode: "tasks",
    selectedRunId: null,
    historyPage: 1,
    historyPageSize: null,
    filterType: defaultFilters.type,
    filterOutcome: defaultFilters.outcome,
    filterQuery: defaultFilters.query,
    filterActiveOnly: defaultFilters.activeOnly,
    historySort: null,
    historySortDir: null,
  };
}

function readRouteState(): RouteState {
  if (typeof window === "undefined") return defaultRouteState();
  const params = new URLSearchParams(window.location.search);
  const ft = params.get("ft");
  const fo = params.get("fo");
  const fq = params.get("fq");
  const fa = params.get("fa");
  const hs = params.get("hs");
  const hd = params.get("hd");
  const filterType = (RUN_TYPE_VALUES as readonly string[]).includes(ft || "")
    ? (ft as RunTypeFilter)
    : defaultFilters.type;
  const filterOutcome = (OUTCOME_VALUES as readonly string[]).includes(fo || "")
    ? (fo as OutcomeFilter)
    : defaultFilters.outcome;
  const historySort = (HISTORY_SORT_COLUMNS as readonly string[]).includes(hs || "")
    ? (hs as HistorySortColumn)
    : null;
  const historySortDir = hd === "asc" || hd === "desc" ? hd : null;
  return {
    viewMode: params.get("view") === "diagnostics" ? "diagnostics" : "tasks",
    selectedRunId: params.get("run") || null,
    historyPage: parseHistoryPageParam(params.get("hp")),
    historyPageSize: parseHistoryPageSizeParam(params.get("ps")),
    filterType,
    filterOutcome,
    filterQuery: fq || defaultFilters.query,
    filterActiveOnly: fa === "true" ? true : defaultFilters.activeOnly,
    // sort col without dir or vice-versa is invalid — drop both.
    historySort: historySort && historySortDir ? historySort : null,
    historySortDir: historySort && historySortDir ? historySortDir : null,
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
  // Heavy-user paper-cut #1: filter params. Omit when at default so the
  // typical URL stays uncluttered. Query string lives at `fq` (not `q`)
  // because `q` is too generic and would collide with future search needs.
  if (route.filterType && route.filterType !== defaultFilters.type) {
    url.searchParams.set("ft", route.filterType);
  } else {
    url.searchParams.delete("ft");
  }
  if (route.filterOutcome && route.filterOutcome !== defaultFilters.outcome) {
    url.searchParams.set("fo", route.filterOutcome);
  } else {
    url.searchParams.delete("fo");
  }
  if (route.filterQuery && route.filterQuery !== defaultFilters.query) {
    url.searchParams.set("fq", route.filterQuery);
  } else {
    url.searchParams.delete("fq");
  }
  if (route.filterActiveOnly) {
    url.searchParams.set("fa", "true");
  } else {
    url.searchParams.delete("fa");
  }
  // Heavy-user paper-cut #2: history sort. Both keys present-or-absent.
  if (route.historySort && route.historySortDir) {
    url.searchParams.set("hs", route.historySort);
    url.searchParams.set("hd", route.historySortDir);
  } else {
    url.searchParams.delete("hs");
    url.searchParams.delete("hd");
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next === current) return;
  const method = mode === "replace" ? "replaceState" : "pushState";
  window.history[method]({otto: true}, "", next);
}

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
  const [toast, setToast] = useState<ToastState | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
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
  // the current page — it is documented in the followups that a server-side
  // sort would be more honest for "cost desc across all 200+ rows", but
  // page-local sort already covers the common "I'm scanning this page for
  // the priciest run" case. Refs let writeRouteState read the current value
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

  // mc-audit microinteractions I8: pause-on-hover + manual dismiss for toasts.
  // Track the auto-dismiss timer in a ref so mouseenter/mouseleave can
  // cancel/restart it without losing track of the current toast. dismissToast
  // gives the close-button a no-arg handler.
  const toastTimerRef = useRef<number | null>(null);
  const TOAST_DURATION_MS = 3200;
  const dismissToast = useCallback(() => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
    setToast(null);
  }, []);
  const scheduleToastDismiss = useCallback((duration: number) => {
    if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => {
      toastTimerRef.current = null;
      setToast(null);
    }, duration);
  }, []);
  const pauseToastDismiss = useCallback(() => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
  }, []);
  const resumeToastDismiss = useCallback(() => {
    // Restart with a full duration so the user gets a fresh window after
    // their hover is over.
    scheduleToastDismiss(TOAST_DURATION_MS);
  }, [scheduleToastDismiss]);
  const showToast = useCallback((message: string, severity: ToastState["severity"] = "information") => {
    if (severity === "error") setLastError(message);
    setToast({message, severity});
    scheduleToastDismiss(TOAST_DURATION_MS);
  }, [scheduleToastDismiss]);
  // Cleanup any pending timer on unmount so we don't fire setToast after
  // the component is gone.
  useEffect(() => () => {
    if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current);
  }, []);

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
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [jobOpen, confirm, paletteOpen]);

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
    const isMerge = action === "merge";
    const isCleanup = action === "cleanup";
    const isCancel = action === "cancel";
    // For merge actions we forward the SHAs from the most-recent diff
    // fetch so the server can refuse to merge code that differs from
    // what the operator reviewed (CRITICAL diff-freshness contract). The
    // confirm body is rebuilt to spell out exactly which branch+SHA is
    // about to land into which target+SHA.
    const liveDiff = isMerge ? diffContent : null;
    const requestPayload: Record<string, string> = {};
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
            showToast("Cancel did not take effect — reverted.", "warning");
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
          body: action === "start" ? JSON.stringify({concurrent: 2}) : "{}",
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
          ? {label: "Yes, stop the watcher now."}
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
    <div className="app-shell" data-mc-shell="ready">
      {/* Skip link must be the first focusable element so a single Tab from
          page load lands on it. Visually hidden until focused. mc-audit a11y
          A11Y-08, K-09. */}
      <a href="#main-content" className="skip-link" data-testid="skip-link">
        Skip to main content
      </a>
      {connectionBanner}
      <InertEffect active={sidebarInert} selector=".sidebar" />
      <InertEffect active={mainContentInert} selector=".main-shell-content" />
      <InertEffect active={inspectorInert} selector="[data-mc-inspector]" />
      <LiveRegion message={liveAnnouncement} />
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">O</div>
          <div>
            <h1>Otto</h1>
            <p>Mission Control</p>
          </div>
        </div>
        <ProjectMeta project={project} watcher={watcher} landing={landing} active={active} firstRun={isProjectFirstRun(data)} />
        {projectsState?.launcher_enabled && (
          <button type="button" data-testid="switch-project-button" onClick={() => void switchProject()}>Switch project</button>
        )}
        <button className="primary" type="button" data-testid="new-job-button" onClick={openJobDialog}>New job</button>
        <button type="button" data-testid="start-watcher-button" disabled={!canStartWatcher(data) || watcherInFlight.pending} aria-describedby="watcher-action-hint" aria-busy={watcherInFlight.pending} title={startWatcherTooltip(data)} onClick={() => void runWatcherAction("start")}>{watcherInFlight.pending ? <><Spinner /> Starting…</> : "Start watcher"}</button>
        <button type="button" data-testid="stop-watcher-button" disabled={!canStopWatcher(data) || watcherInFlight.pending} aria-describedby="watcher-action-hint" aria-busy={watcherInFlight.pending} title={watcher?.health.next_action || ""} onClick={() => void runWatcherAction("stop")}>{watcherInFlight.pending ? <><Spinner /> Stopping…</> : "Stop watcher"}</button>
        <p id="watcher-action-hint" className="sidebar-hint">{watcherHint}</p>
      </aside>

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
                selectedQueuedTaskId={selectedQueuedTask?.id || null}
                onSelect={selectRun}
                onSelectQueued={selectQueuedTask}
                onCancelRun={(runId, taskTitle) => void runActionForRun(
                  runId,
                  "cancel",
                  actionConfirmationBody("cancel", `Cancel ${taskTitle}`),
                  "Cancel",
                )}
                onClearFilters={() => updateFilters(defaultFilters)}
                onNewJob={openJobDialog}
              />
              <RecentActivity events={data?.events} history={data?.history.items || []} selectedRunId={selectedRunId} onSelect={selectRun} />
            </div>
            <RunDetailPanel
              detail={detail}
              landing={landing}
              inspectorOpen={inspectorOpen}
              queuedTask={selectedQueuedTask}
              watcherRunning={data?.watcher.health.state === "running"}
              onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
              onShowProof={showProof}
              onShowLogs={showLogs}
              onShowDiff={showDiff}
              onShowArtifacts={showArtifacts}
              onLoadArtifact={(index) => void loadArtifact(index)}
              onStartWatcher={() => void runWatcherAction("start")}
            />
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
                onRunAction={(action, label) => detail && void runActionForRun(detail.run_id, action, actionConfirmationBody(action, label), label)}
                onShowProof={showProof}
                onShowLogs={showLogs}
                onShowDiff={showDiff}
                onShowArtifacts={showArtifacts}
                onLoadArtifact={(index) => void loadArtifact(index)}
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
      <ToastDisplay
        toast={toast}
        onMouseEnter={pauseToastDismiss}
        onMouseLeave={resumeToastDismiss}
        onDismiss={dismissToast}
      />
    </div>
  );
}

/**
 * mc-audit microinteractions I8: shared toast renderer with hover-to-pause
 * and a manual ✕ dismiss button. Lives outside the App component so both
 * the launcher view (line ~1470) and the workspace view (line ~1690) can
 * use the same markup without duplication.
 */
function ToastDisplay({toast, onMouseEnter, onMouseLeave, onDismiss}: {
  toast: ToastState | null;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onDismiss: () => void;
}) {
  if (!toast) return null;
  return (
    <div
      id="toast"
      className={`visible toast-${toast.severity}`}
      role="status"
      aria-live="polite"
      data-testid="toast"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <span className="toast-message">{toast.message}</span>
      <button
        type="button"
        className="toast-close"
        data-testid="toast-close"
        aria-label="Dismiss notification"
        onClick={onDismiss}
      >×</button>
    </div>
  );
}

function ProjectMeta({project, watcher, landing, active, firstRun}: {
  project: StateResponse["project"] | undefined;
  watcher: WatcherInfo | undefined;
  landing: LandingState | undefined;
  active: number;
  // mc-audit codex-first-time-user #15: when the project has zero history AND
  // no live runs, hide the detailed Watcher/Heartbeat/In-flight/queued/ready/
  // landed counters and show a single-line "Project ready · No jobs yet"
  // summary so the first-run sidebar isn't a wall of internal vocabulary.
  firstRun: boolean;
}) {
  const counts = watcher?.counts || {};
  const health = watcher?.health;
  if (firstRun) {
    return (
      <dl className="project-meta project-meta-first-run" aria-label="Project metadata" data-testid="project-meta-first-run">
        <MetaItem label="Project" value={project?.name || "-"} />
        <MetaItem label="Branch" value={project?.branch || "-"} />
        <MetaItem
          label="Status"
          value={!project ? "Loading…" : "Project ready · No jobs yet"}
        />
      </dl>
    );
  }
  return (
    <dl className="project-meta" aria-label="Project metadata" data-testid="project-meta-full">
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
            All projects live under this directory. Otto manages projects in isolated git worktrees so it never touches your other repos on this machine. The repo that launched Mission Control is intentionally excluded — pick or create a project below to start.
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
      <div className="view-tabs" role="group" aria-label="Mission Control views">
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
      <div className="filters" role="group" aria-label="Run filters">
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
    <div className="overview" role="region" aria-labelledby="missionOverviewHeading">
      <h2 id="missionOverviewHeading" className="sr-only">Mission overview</h2>
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
    </div>
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
  // mc-audit codex-first-time-user #26: surface user-facing labels in the
  // runtime banner instead of internal terms ("malformed" → "unreadable").
  const suffix = [
    backlog.pending ? `${backlog.pending} pending` : "",
    backlog.processing ? `${backlog.processing} processing` : "",
    backlog.malformed ? `${backlog.malformed} unreadable` : "",
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
          {/* mc-audit codex-first-time-user #26: avoid internal vocabulary
              ("operator actions / command backlog / runtime issues") in the
              diagnostics surfaces — translate to user-facing copy. */}
          <p className="panel-subtitle" data-testid="diagnostics-subtitle">System issues and pending commands surface here so you can act on them.</p>
        </div>
        <span className="pill" title="Pending commands, system issues, and review items." aria-label={`${diagnosticCount} diagnostic items`}>{diagnosticCount}</span>
      </div>
      <div className="diagnostics-summary-body">
        <div>
          <h3 data-testid="diagnostics-pending-commands-heading">Pending Commands</h3>
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
        </div>
        <div>
          <h3 data-testid="diagnostics-system-issues-heading">System Issues</h3>
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
          )) : <div className="diagnostic-empty">No system issues.</div>}
        </div>
        <div className="wide-diagnostics-section">
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
        </div>
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
    <section className={`mission-focus focus-${focus.tone}`} data-testid="mission-focus" aria-labelledby="missionFocusHeading">
      <div className="focus-copy">
        <span>{focus.kicker}</span>
        <h2 id="missionFocusHeading">{focus.title}</h2>
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
            // mc-audit live W11-IMPORTANT-3: when the button is disabled the
            // user still hovers asking "why?". Surface the supervisor's
            // start_blocked_reason / next_action via title so the disabled
            // state is self-explanatory ("watcher already running", "no
            // queued tasks", etc.) instead of generic.
            title={
              watcherPending
                ? "Starting watcher…"
                : (data?.runtime.supervisor.start_blocked_reason
                    || data?.watcher.health.next_action
                    || "Start the watcher process to run queued jobs.")
            }
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

function TaskBoard({data, filters, selectedRunId, selectedQueuedTaskId, onSelect, onSelectQueued, onCancelRun, onClearFilters, onNewJob}: {
  data: StateResponse | null;
  filters: Filters;
  selectedRunId: string | null;
  // mc-audit codex-first-time-user #19: BoardTask.id of a queued-but-no-runId
  // card the user has selected. Drives the `selected` highlight on the card
  // since `selectedRunId` is null in that mode.
  selectedQueuedTaskId?: string | null;
  onSelect: (runId: string) => void;
  onSelectQueued?: (task: BoardTask) => void;
  // mc-audit W8-CRITICAL-1: forwarded to TaskCard so each in-flight row
  // renders its own discoverable Cancel button. Without this, keyboard
  // users walk past the row into inspector tabstops and accidentally
  // fire `review-next-action-button`.
  onCancelRun?: (runId: string, taskTitle: string) => void;
  onClearFilters?: () => void;
  onNewJob?: () => void;
}) {
  const columns = taskBoardColumns(data, filters);
  // mc-audit error-empty-states #11: distinguish *why* the board is empty.
  // Without this, an empty filter result reads as "No work queued" — telling
  // the user to queue more work when they actually need to clear filters.
  const emptyReason = computeBoardEmptyReason(data, filters, columns);
  return (
    <section className="panel task-board-panel" data-testid="task-board" aria-labelledby="taskBoardHeading">
      <div className="panel-heading">
        <div>
          <h2 id="taskBoardHeading">Task Board</h2>
          <p className="panel-subtitle">{taskBoardSubtitle(data, filters)}</p>
        </div>
      </div>
      {emptyReason && emptyReason !== "has-tasks" ? (
        <div
          className={`task-board-empty task-board-empty-${emptyReason}`}
          data-testid="task-board-empty"
          data-empty-reason={emptyReason}
          role="status"
        >
          {emptyReason === "loading" && <span>Loading tasks…</span>}
          {emptyReason === "no-project" && <span>No project selected.</span>}
          {emptyReason === "filtered-empty" && (
            <>
              <span>No matching tasks.</span>
              {onClearFilters && (
                <button
                  type="button"
                  className="task-board-empty-action"
                  data-testid="task-board-empty-clear-filters"
                  onClick={onClearFilters}
                >Clear filters</button>
              )}
            </>
          )}
          {emptyReason === "true-empty" && (
            <>
              <span>No work queued. Start a build to populate the board.</span>
              {onNewJob && (
                <button
                  type="button"
                  className="task-board-empty-action primary"
                  data-testid="task-board-empty-queue-job"
                  onClick={onNewJob}
                >Queue job</button>
              )}
            </>
          )}
        </div>
      ) : null}
      <div className="task-board">
        {columns.map((column) => (
          <div className="task-column" key={column.stage}>
            <header>
              <span>{column.title}</span>
              <strong>{column.items.length}</strong>
            </header>
            <div className="task-list">
              {column.items.length ? column.items.map((task) => {
                const cardSelected = Boolean(
                  (task.runId && task.runId === selectedRunId)
                  || (!task.runId && selectedQueuedTaskId && task.id === selectedQueuedTaskId)
                );
                return (
                  <TaskCard
                    key={`${task.source}-${task.id}`}
                    task={task}
                    selected={cardSelected}
                    onSelect={onSelect}
                    {...(onSelectQueued ? {onSelectQueued} : {})}
                    {...(onCancelRun ? {onCancelRun} : {})}
                  />
                );
              }) : (
                <div className="task-empty">{columnEmptyCopy(column, emptyReason)}</div>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// mc-audit error-empty-states #11: classify *why* the task board is empty.
// "filtered-empty" lets the UI offer "Clear filters" instead of misleading
// "No work queued." copy when the user actually has tasks but they're hidden.
export type BoardEmptyReason =
  | "has-tasks"
  | "loading"
  | "no-project"
  | "filtered-empty"
  | "true-empty";

export function filtersAreActive(filters: Filters): boolean {
  return (
    filters.type !== "all"
    || filters.outcome !== "all"
    || filters.query.trim() !== ""
    || filters.activeOnly
  );
}

export function computeBoardEmptyReason(
  data: StateResponse | null,
  filters: Filters,
  columns: ReadonlyArray<{items: ReadonlyArray<unknown>}>,
): BoardEmptyReason {
  if (!data) return "loading";
  if (!data.project) return "no-project";
  const visible = columns.reduce((sum, column) => sum + column.items.length, 0);
  if (visible > 0) return "has-tasks";
  // No visible tasks. Are the filters hiding them?
  if (filtersAreActive(filters)) {
    // Recompute against default filters: if there ARE tasks then, filters
    // are doing the hiding.
    const allColumns = taskBoardColumns(data);
    const allCount = allColumns.reduce((sum, column) => sum + column.items.length, 0);
    if (allCount > 0) return "filtered-empty";
  }
  return "true-empty";
}

function columnEmptyCopy(
  column: {empty: string},
  reason: BoardEmptyReason | null,
): string {
  if (reason === "filtered-empty") return "No matching tasks.";
  return column.empty;
}

// mc-audit W8-CRITICAL-1: statuses for which an in-flight task can still
// be cancelled via the watcher. Mirrors the backend's cancel-eligible set
// so the per-row Cancel button doesn't render for already-finished work.
// `terminating` is intentionally excluded — it's already cancelling.
const CANCELLABLE_TASK_STATUSES = new Set([
  "queued",
  "starting",
  "initializing",
  "running",
]);

function TaskCard({task, selected, onSelect, onSelectQueued, onCancelRun}: {
  task: BoardTask;
  selected: boolean;
  onSelect: (runId: string) => void;
  // mc-audit codex-first-time-user #19: handler for cards without a runId
  // (queued, never picked up). Without this the card's button is disabled
  // and the user can't open Details. We invoke onSelectQueued so the
  // detail panel can render a "waiting for watcher" placeholder.
  onSelectQueued?: (task: BoardTask) => void;
  // mc-audit W8-CRITICAL-1: per-row cancel affordance for in-flight tasks.
  // Without this, a keyboard user pressing Tab from a queued/running card
  // walks past the card straight into the inspector's
  // `review-next-action-button` (which can fire merge / next-step
  // actions). Pressing Enter there is data-loss-adjacent — the user
  // thinks they cancelled, the system thinks they approved the next
  // step. We put a Cancel target directly in the focus chain after the
  // row's main button, with a stable testid (`task-card-cancel-<id>`)
  // that cannot collide with inspector tabstops.
  onCancelRun?: (runId: string, taskTitle: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const selectTask = () => {
    if (task.runId) {
      onSelect(task.runId);
    } else if (onSelectQueued) {
      onSelectQueued(task);
    }
  };
  // mc-audit info-density #2: render typed chips with explicit kind labels
  // (files / stories / cost / time) instead of a row of unlabeled pills.
  // Each chip is suppressed when the underlying value is null/missing — no
  // "-" placeholder leakage.
  const chips = computeTaskChips(task);
  const isQueuedNoRun = !task.runId;
  // mc-audit W8-CRITICAL-1: render the per-row Cancel button only when
  // the task has a runId AND its status is cancel-eligible. The handler
  // routes through `runActionForRun` so the same confirm-and-POST flow
  // as the inspector cancel button is used — the row-level affordance
  // exists purely to give keyboard users a stable, discoverable focus
  // target adjacent to the card.
  const cancellable = Boolean(task.runId)
    && Boolean(onCancelRun)
    && CANCELLABLE_TASK_STATUSES.has(String(task.status || "").toLowerCase());
  return (
    <article
      className={`task-card task-${task.stage} ${selected ? "selected" : ""}`}
      // W10-CRITICAL-1/2: expose the run id (and the queue task id) on the
      // rendered card so cross-tab/UI tests can identify "the row for run
      // X" without relying on text scraping. The TaskCard renders the
      // *title*, not the run_id, so test harnesses scanning textContent
      // for a run id never match — they need a stable attribute hook.
      data-run-id={task.runId || undefined}
      data-task-id={task.id}
      data-stage={task.stage}
    >
      <button
        className="task-card-main"
        type="button"
        // Card is enabled even without runId so the user can open the
        // queued-task placeholder. Only disable as a last resort: no runId
        // AND no onSelectQueued handler wired (legacy callers).
        disabled={isQueuedNoRun && !onSelectQueued}
        data-testid={testIdForTask(task.id)}
        data-queued-no-run={isQueuedNoRun ? "true" : undefined}
        aria-pressed={selected}
        aria-label={isQueuedNoRun
          ? `${task.title}: ${task.status} (waiting for watcher)`
          : `${task.title}: ${task.status}`}
        onClick={selectTask}
      >
        <span className="task-card-top">
          {/* mc-audit info-density #3: tone classes so ready/blocked/running/
              failed/cancelled scan distinctly. Without these, every badge is
              the same gray pill. */}
          <span
            className={`task-status status-tone-${statusTone(task.status, task.stage)}`}
            data-status-tone={statusTone(task.status, task.stage)}
          >
            {/* mc-audit visual-coherence F10 — colour-blind safety: glyph
                prefix so the tone isn't conveyed by colour alone. */}
            <span className="status-icon" aria-hidden="true">{toneIcon(statusTone(task.status, task.stage))}</span>
            {" "}
            {task.status}
          </span>
          <span className="task-card-cta">{task.stage === "ready" ? "Review" : "Details"}</span>
        </span>
        <strong className="task-title" title={task.title}>{task.title}</strong>
        <span className="task-card-meta" data-testid={`${testIdForTask(task.id)}-chips`}>
          {chips.map((chip) => (
            <span
              key={chip.kind}
              className={`task-chip task-chip-${chip.kind}`}
              data-chip-kind={chip.kind}
              title={chip.tooltip || chip.label}
            >
              <span className="task-chip-icon" aria-hidden="true">{chip.icon}</span>
              {" "}
              {chip.label}
            </span>
          ))}
        </span>
      </button>
      {/* mc-audit W8-CRITICAL-1: per-row Cancel button for in-flight runs.
          This button is a SIBLING of `task-card-main`, NOT nested inside
          it — that gives it its own tab stop so a keyboard user pressing
          Tab from the row's main button lands on a discoverable cancel
          target instead of walking past the row into the inspector's
          `review-next-action-button`. The click handler routes through
          `onCancelRun` which calls `runActionForRun(runId, "cancel", …)`
          and triggers the same confirm dialog as the inspector. We
          stop event propagation defensively so the click cannot bubble
          to ancestors, and we fire ONLY the cancel POST — never an
          inspector / next-action POST. */}
      {cancellable && task.runId && (
        <button
          className="task-card-cancel"
          type="button"
          data-testid={`task-card-cancel-${task.id.replace(/[^a-zA-Z0-9_-]+/g, "-")}`}
          aria-label={`Cancel task ${task.title}`}
          title={`Cancel task ${task.title}`}
          onClick={(event) => {
            event.stopPropagation();
            if (task.runId && onCancelRun) onCancelRun(task.runId, task.title);
          }}
        >
          Cancel
        </button>
      )}
      <button
        className="task-card-toggle"
        type="button"
        aria-expanded={expanded}
        aria-controls={`${testIdForTask(task.id)}-drawer`}
        data-testid={`${testIdForTask(task.id)}-toggle`}
        onClick={() => setExpanded((value) => !value)}
      >
        {/* mc-audit microinteractions I5: chevron rotates 90deg on toggle so
            the user gets a visual affordance even before the height
            transition runs. The arrow points right when collapsed and down
            when expanded. */}
        <span className="task-card-toggle-chevron" aria-hidden="true" data-testid={`${testIdForTask(task.id)}-toggle-chevron`}>›</span>
        {expanded ? "Less" : "More"}
      </button>
      {/* mc-audit microinteractions I5: keep the drawer mounted and animate
          its grid-template-rows from 0fr → 1fr so the layout shift is
          visible (≤200ms). The inner div needs `min-height: 0; overflow:
          hidden` so the row animation actually clips. The transition is
          neutralised under prefers-reduced-motion (see styles.css). */}
      <div
        className="task-card-drawer-wrap"
        data-expanded={expanded ? "true" : "false"}
        data-testid={`${testIdForTask(task.id)}-drawer-wrap`}
      >
        <div
          className="task-card-drawer"
          id={`${testIdForTask(task.id)}-drawer`}
          aria-hidden={!expanded}
        >
          <p title={task.summary}>{shortText(task.summary, 220)}</p>
          <dl>
            <dt>Branch</dt><dd title={task.branch || ""}>{task.branch || "no branch"}</dd>
            <dt>Reason</dt><dd>{task.reason}</dd>
          </dl>
        </div>
      </div>
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
          {/* mc-audit codex-first-time-user #27: rewrite jargon-heavy subtitle
              ("queue, watcher, land, and run outcomes") into user-facing copy
              that describes what the panel actually shows. */}
          <p className="panel-subtitle" data-testid="activity-subtitle">Recent job activity, approvals, merges, and errors appear here.</p>
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
                aria-selected={item.run_id === selectedRunId}
              >
                <td className={`status-${item.display_status}`} aria-label={item.overlay?.reason || item.display_status}>{item.display_status.toUpperCase()}</td>
                <td>
                  <button
                    type="button"
                    className="row-link"
                    data-testid={`live-row-activator-${item.run_id}`}
                    aria-label={`Open live run ${item.display_id || item.run_id}`}
                    title={item.run_id}
                    onClick={() => onSelect(item.run_id)}
                  >{item.display_id || item.run_id}</button>
                </td>
                <td>
                  <span className="cell-overflow" aria-label={item.branch_task || ""}>{item.branch_task || "-"}</span>
                </td>
                <td>{item.elapsed_display || "-"}</td>
                <td>{item.cost_display || "-"}</td>
                <td>
                  <span className="cell-overflow" aria-label={runEventText(item, landingByTask)}>{runEventText(item, landingByTask)}</span>
                </td>
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
  sortColumn,
  sortDir,
  onSelect,
  onChangePage,
  onChangePageSize,
  onCycleSort,
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
  // Heavy-user paper-cut #2: which column the user clicked, and the direction.
  // Both null → no sort applied (server natural order wins).
  sortColumn: HistorySortColumn | null;
  sortDir: HistorySortDir | null;
  onSelect: (runId: string) => void;
  onChangePage: (nextPage: number) => void;
  onChangePageSize: (nextSize: number) => void;
  onCycleSort: (column: HistorySortColumn) => void;
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

  // Heavy-user paper-cut #2: apply local sort when a column is selected.
  // Sort is page-local (we sort the rows the server gave us); a true
  // server-side sort across all 200+ rows is in the followups.
  const sortedItems = useMemo(
    () => sortHistoryItems(items, sortColumn, sortDir),
    [items, sortColumn, sortDir],
  );

  const sortIndicator = (col: HistorySortColumn): string => {
    if (sortColumn !== col || !sortDir) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  };
  const ariaSort = (col: HistorySortColumn): "ascending" | "descending" | "none" => {
    if (sortColumn !== col || !sortDir) return "none";
    return sortDir === "asc" ? "ascending" : "descending";
  };
  const renderSortableTh = (col: HistorySortColumn, label: string) => (
    <th
      aria-sort={ariaSort(col)}
      data-testid={`history-th-${col}`}
      className={`history-th-sortable ${sortColumn === col && sortDir ? "active" : ""}`}
    >
      <button
        type="button"
        className="history-sort-button"
        data-testid={`history-sort-${col}`}
        aria-label={`Sort by ${label} (${sortColumn === col && sortDir ? sortDir : "asc"} on click)`}
        onClick={() => onCycleSort(col)}
      >
        {label}{sortIndicator(col)}
      </button>
    </th>
  );

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
              {renderSortableTh("outcome", "Outcome")}
              {renderSortableTh("run", "Run")}
              {renderSortableTh("summary", "Summary")}
              {renderSortableTh("duration", "Duration")}
              {renderSortableTh("usage", "Usage")}
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
            ) : sortedItems.length ? sortedItems.map((item) => (
              <tr
                key={item.run_id}
                className={item.run_id === selectedRunId ? "selected" : ""}
                aria-selected={item.run_id === selectedRunId}
              >
                <td className={`status-${(item.terminal_outcome || item.status || "").toLowerCase()}`}>{item.outcome_display || "-"}</td>
                <td>
                  <button
                    type="button"
                    className="row-link"
                    data-testid={`history-row-activator-${item.run_id}`}
                    aria-label={`Open history run ${item.queue_task_id || item.run_id}`}
                    title={item.run_id}
                    onClick={() => onSelect(item.run_id)}
                  >{item.queue_task_id || item.run_id}</button>
                </td>
                <td>
                  <span className="cell-overflow" aria-label={item.summary || ""}>{item.summary || "-"}</span>
                </td>
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

/**
 * Heavy-user paper-cut #2: page-local sort. We sort the rows we already
 * have (the current paginated slice) — server-side sort across all rows is
 * a followup. Comparators are domain-aware: cost/duration use numeric
 * values from the API (cost_usd / duration_s), not the display strings,
 * so "$2" doesn't sort ahead of "$10".
 */
function sortHistoryItems(
  items: HistoryItem[],
  column: HistorySortColumn | null,
  dir: HistorySortDir | null,
): HistoryItem[] {
  if (!column || !dir || items.length < 2) return items;
  const factor = dir === "asc" ? 1 : -1;
  const comparators: Record<HistorySortColumn, (a: HistoryItem, b: HistoryItem) => number> = {
    outcome: (a, b) => safeCompareString(a.outcome_display || a.terminal_outcome || a.status, b.outcome_display || b.terminal_outcome || b.status),
    run: (a, b) => safeCompareString(a.queue_task_id || a.run_id, b.queue_task_id || b.run_id),
    summary: (a, b) => safeCompareString(a.summary, b.summary),
    duration: (a, b) => safeCompareNumber(a.duration_s, b.duration_s),
    usage: (a, b) => safeCompareNumber(a.cost_usd, b.cost_usd),
  };
  const cmp = comparators[column];
  // Slice so we never mutate the caller's array — React identity matters
  // for memoization and for the test harness that snapshots `items`.
  return [...items].sort((a, b) => cmp(a, b) * factor);
}

function safeCompareString(a: string | null | undefined, b: string | null | undefined): number {
  const av = (a || "").toLowerCase();
  const bv = (b || "").toLowerCase();
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
}

function safeCompareNumber(a: number | null | undefined, b: number | null | undefined): number {
  const av = typeof a === "number" && Number.isFinite(a) ? a : Number.NEGATIVE_INFINITY;
  const bv = typeof b === "number" && Number.isFinite(b) ? b : Number.NEGATIVE_INFINITY;
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
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
          // mc-audit codex-first-time-user #26: replace "malformed event rows"
          // with user-facing "unreadable log entries".
          <div className="timeline-warning" data-testid="timeline-malformed-warning">Skipped {malformed} unreadable log entr{malformed === 1 ? "y" : "ies"}.</div>
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

function RunDetailPanel({detail, landing, inspectorOpen, queuedTask, watcherRunning, onRunAction, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadArtifact, onStartWatcher}: {
  detail: RunDetail | null;
  landing: LandingState | undefined;
  inspectorOpen: boolean;
  // mc-audit codex-first-time-user #19: when a queued card without a runId
  // is selected, render a placeholder explaining that the task is waiting
  // for the watcher and what the user can do (start the watcher, etc.).
  queuedTask?: BoardTask | null;
  watcherRunning?: boolean;
  onRunAction: (action: string, label?: string) => void;
  onShowProof: () => void;
  onShowLogs: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
  onLoadArtifact: (index: number) => void;
  onStartWatcher?: () => void;
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
                  <dt>Type</dt><dd data-testid="run-detail-type">{domainLabel(detail.domain)} / {detail.run_type}</dd>
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
          {/* When the inspector is open, the fixed-position inspector overlay
              covers this row of shortcut buttons. Leaving them in the DOM
              causes Playwright (and any script-driven click) to resolve them
              as visible while the actual click is intercepted by the
              overlay — see mc-audit W13-CRITICAL-1. The inspector ships its
              own tablist (Result / Code changes / Logs / Artifacts) so
              hiding these shortcuts while the inspector is open is the
              correct UX too. */}
          {!inspectorOpen && (
            <div className="detail-inspector-actions" role="group" aria-label="Evidence shortcuts">
              <button className="primary" type="button" data-testid="open-proof-button" onClick={onShowProof}>Review result</button>
              <button type="button" data-testid="open-diff-button" disabled={!canShowDiff(detail)} title={canShowDiff(detail) ? "" : diffDisabledReason(detail)} onClick={onShowDiff}>Code changes</button>
              <button type="button" data-testid="open-logs-button" onClick={onShowLogs}>Logs</button>
              <button type="button" data-testid="open-artifacts-button" onClick={onShowArtifacts}>Artifacts</button>
            </div>
          )}
        </>
      ) : queuedTask ? (
        <div className="detail-body empty queued-task-detail" data-testid="run-detail-queued" data-queued-task-id={queuedTask.id}>
          <h3>{queuedTask.title}</h3>
          <p className="queued-task-subtitle">
            <strong>Waiting for watcher</strong> — this task is queued but no
            run has started yet. Logs, diffs, and proof become available once
            the watcher picks it up.
          </p>
          <dl className="queued-task-meta">
            <dt>Status</dt><dd>{queuedTask.status}</dd>
            {queuedTask.branch && (<><dt>Branch</dt><dd title={queuedTask.branch}>{queuedTask.branch}</dd></>)}
            <dt>Reason</dt><dd>{queuedTask.reason}</dd>
            {queuedTask.summary && (<><dt>Intent</dt><dd>{shortText(queuedTask.summary, 240)}</dd></>)}
          </dl>
          <p className="queued-task-next-action">
            <strong>Next:</strong>{" "}
            {watcherRunning
              ? "Watcher is running — task should pick up shortly."
              : "Start the watcher to dispatch this queued task."}
          </p>
          {!watcherRunning && onStartWatcher && (
            <button
              type="button"
              className="primary"
              data-testid="run-detail-queued-start-watcher"
              onClick={onStartWatcher}
            >Start watcher</button>
          )}
        </div>
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
  // WAI-ARIA tablist pattern: roving tabindex + arrow keys + Home/End. Tabs
  // that are disabled (Code changes, when diff isn't available) skip in
  // arrow rotation. mc-audit a11y A11Y-03, K-04.
  const tabModes = useMemo<InspectorMode[]>(() => ["proof", "diff", "logs", "artifacts"], []);
  const tabHandlers: Record<InspectorMode, () => void> = {
    proof: onShowProof,
    diff: onShowDiff,
    logs: onShowLogs,
    artifacts: onShowArtifacts,
  };
  const tabLabels: Record<InspectorMode, string> = {
    proof: "Result",
    diff: "Code changes",
    logs: "Logs",
    artifacts: "Artifacts",
  };
  const tabDisabled = (m: InspectorMode): boolean => m === "diff" && !canShowDiff(detail);
  const onTabKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const key = event.key;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(key)) return;
    event.preventDefault();
    const enabled = tabModes.filter((m) => !tabDisabled(m));
    if (!enabled.length) return;
    const currentIndex = enabled.indexOf(mode);
    let nextIndex = 0;
    if (key === "Home") nextIndex = 0;
    else if (key === "End") nextIndex = enabled.length - 1;
    else if (key === "ArrowLeft") nextIndex = ((currentIndex < 0 ? 0 : currentIndex) - 1 + enabled.length) % enabled.length;
    else if (key === "ArrowRight") nextIndex = ((currentIndex < 0 ? -1 : currentIndex) + 1) % enabled.length;
    const nextMode = enabled[nextIndex];
    if (!nextMode) return;
    tabHandlers[nextMode]();
    window.requestAnimationFrame(() => {
      const root = inspectorRef.current;
      if (!root) return;
      const target = root.querySelector<HTMLButtonElement>(`[data-tab-id="${nextMode}"]`);
      target?.focus();
    });
  };
  return (
    <section
      ref={inspectorRef}
      className="run-inspector"
      role="dialog"
      aria-modal="true"
      aria-labelledby="runInspectorHeading"
      data-testid="run-inspector"
      data-mc-inspector="true"
      tabIndex={-1}
    >
      <div className="run-inspector-heading">
        <div>
          <h2 id="runInspectorHeading">{detail.title || detail.run_id}</h2>
          <p>{detailStatusLabel(detail)} review</p>
        </div>
        <div className="detail-tabs" role="tablist" aria-label="Evidence view" onKeyDown={onTabKeyDown}>
          {tabModes.map((m) => {
            const isSelected = mode === m;
            const isDisabled = tabDisabled(m);
            return (
              <button
                key={m}
                id={`run-inspector-tab-${m}`}
                data-tab-id={m}
                className={`tab ${isSelected ? "active" : ""}`}
                type="button"
                role="tab"
                aria-selected={isSelected}
                aria-controls="run-inspector-panel"
                tabIndex={isSelected ? 0 : -1}
                disabled={isDisabled}
                title={isDisabled ? diffDisabledReason(detail) : ""}
                onClick={tabHandlers[m]}
              >
                {tabLabels[m]}
              </button>
            );
          })}
        </div>
        <button type="button" data-testid="close-inspector-button" onClick={onClose}>Close inspector</button>
      </div>
      <div
        className="run-inspector-body"
        id="run-inspector-panel"
        role="tabpanel"
        aria-labelledby={`run-inspector-tab-${mode}`}
      >
        {mode === "proof" ? (
          <ProofPane detail={detail} proofArtifactIndex={proofArtifactIndex} proofContent={proofContent} onShowDiff={onShowDiff} onLoadProofArtifact={onLoadProofArtifact} />
        ) : mode === "diff" ? (
          <DiffPane diff={diffContent} onRefresh={onRefreshDiff} />
        ) : mode === "logs" ? (
          <LogPane logState={logState} runActive={detail.active} onRetry={onShowLogs} />
        ) : (
          <ArtifactPane
            runId={detail.run_id}
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

  // Heavy-user paper-cut #6 (log search). Local state — match index advances
  // through the highlighted regions; Enter / Shift+Enter step through them;
  // Cmd-F / `/` focuses the search box. The search box is only meaningful
  // when there's text to search, so we render it inside the populated body.
  const [search, setSearch] = useState("");
  const [matchIdx, setMatchIdx] = useState(0);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  // Reset selection when the query changes; keep when the buffer grows so
  // an active highlight doesn't snap back to 0 every poll tick.
  useEffect(() => {
    setMatchIdx(0);
  }, [search]);
  const matchCount = useMemo(() => {
    if (!search || !text) return 0;
    const needle = search.toLowerCase();
    const haystack = text.toLowerCase();
    let count = 0;
    let cursor = 0;
    while (cursor < haystack.length) {
      const found = haystack.indexOf(needle, cursor);
      if (found < 0) break;
      count += 1;
      cursor = found + Math.max(1, needle.length);
    }
    return count;
  }, [text, search]);
  const focusSearch = useCallback(() => {
    searchInputRef.current?.focus();
    searchInputRef.current?.select();
  }, []);
  const stepMatch = useCallback((dir: 1 | -1) => {
    if (!matchCount) return;
    setMatchIdx((prev) => (prev + dir + matchCount) % matchCount);
  }, [matchCount]);
  // Local Cmd-F / "/" interception. Only when this LogPane is mounted +
  // the inspector body has focus — we attach the listener on the
  // container so it doesn't fight global Cmd-K. Plain `/` only triggers
  // when the user is NOT typing in another input.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onKey = (event: KeyboardEvent) => {
      const cmdF = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f";
      if (cmdF) {
        event.preventDefault();
        focusSearch();
        return;
      }
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        focusSearch();
      }
    };
    container.addEventListener("keydown", onKey);
    return () => container.removeEventListener("keydown", onKey);
  }, [focusSearch]);

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
  } else if (search) {
    body = (
      <pre
        className="log-pane log-content"
        tabIndex={0}
        aria-label="Run log output"
        data-testid="run-log-pane"
      >{renderLogTextWithHighlight(text, search, matchIdx)}</pre>
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
    <div className="log-viewer" ref={containerRef}>
      <div className="log-toolbar">
        <strong>Run logs</strong>
        <span data-testid="log-pane-status">{headerStatus}</span>
        {droppedNote && (
          <span className="log-elided" data-testid="log-pane-elided">{droppedNote}</span>
        )}
      </div>
      {/* Heavy-user paper-cut #6: in-pane search. Always visible whenever
          there's a populated log buffer so the user doesn't have to discover
          a hidden affordance. We hide it when the body is in an empty/error
          state — there's nothing to search and the input would be confusing. */}
      {(status === "ok" || (text && status !== "missing" && status !== "error")) && (
        <div className="log-search" data-testid="log-search">
          <input
            ref={searchInputRef}
            value={search}
            type="search"
            placeholder="Search log (Cmd-F / /)"
            data-testid="log-search-input"
            aria-label="Search within log"
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                stepMatch(event.shiftKey ? -1 : 1);
              } else if (event.key === "Escape") {
                event.preventDefault();
                setSearch("");
                searchInputRef.current?.blur();
              }
            }}
          />
          <span className="log-search-count" data-testid="log-search-count">
            {search
              ? matchCount
                ? `${matchIdx + 1} / ${matchCount}`
                : "0 matches"
              : ""}
          </span>
          <button
            type="button"
            data-testid="log-search-prev"
            disabled={!matchCount}
            aria-label="Previous match"
            onClick={() => stepMatch(-1)}
          >Prev</button>
          <button
            type="button"
            data-testid="log-search-next"
            disabled={!matchCount}
            aria-label="Next match"
            onClick={() => stepMatch(1)}
          >Next</button>
        </div>
      )}
      {body}
    </div>
  );
}

/**
 * Heavy-user paper-cut #6: render log text with `<mark>`-wrapped matches.
 * The Nth match (0-indexed by `activeMatchIdx`) gets an `active` class so
 * Prev/Next nav can scroll the focused match into view. We don't replace
 * the existing line-classification (`renderLogText`) — instead we
 * pre-segment the text by match boundaries and run each segment through
 * the same line splitter. ANSI color is dropped inside highlighted
 * segments to keep the implementation simple; the test suite asserts
 * `<mark>` presence not ANSI nesting.
 */
function renderLogTextWithHighlight(text: string, needle: string, activeMatchIdx: number) {
  if (!needle) return renderLogText(text);
  const lower = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let matchIndex = 0;
  let key = 0;
  while (cursor < text.length) {
    const found = lower.indexOf(lowerNeedle, cursor);
    if (found < 0) {
      nodes.push(<span key={`s-${key++}`}>{text.slice(cursor)}</span>);
      break;
    }
    if (found > cursor) {
      nodes.push(<span key={`s-${key++}`}>{text.slice(cursor, found)}</span>);
    }
    const segment = text.slice(found, found + needle.length);
    const isActive = matchIndex === activeMatchIdx;
    nodes.push(
      <mark
        key={`m-${key++}`}
        className={`log-search-match ${isActive ? "active" : ""}`}
        data-testid={isActive ? "log-search-match-active" : "log-search-match"}
        ref={isActive ? (el) => {
          if (el && typeof el.scrollIntoView === "function") {
            el.scrollIntoView({block: "center", inline: "nearest"});
          }
        } : undefined}
      >{segment}</mark>,
    );
    cursor = found + Math.max(1, needle.length);
    matchIndex += 1;
  }
  return nodes;
}

/**
 * True when the keydown event target is a text-entry surface — input,
 * textarea, contentEditable. Used by global hotkeys (`/`, Cmd-K) to stay
 * out of the user's way while they're typing.
 */
function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
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
  const rounds = packet.certification.rounds || [];
  const proofReport = packet.certification.proof_report;
  const proofChecks = packet.failure ? packet.checks.filter((check) => check.key !== "run" && check.key !== "landing") : packet.checks;
  return (
    <div className="proof-pane" data-testid="proof-pane">
      <div className="proof-summary" aria-labelledby="proofHeading">
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
      </div>
      <ProofProvenance proofReport={proofReport} runId={detail.run_id} />
      <div className="proof-section" aria-labelledby="proofNextHeading">
        <h3 id="proofNextHeading">Next action</h3>
        <p>{packet.readiness.next_step}</p>
        <div className="proof-report-actions">
          {proofReport?.html_url ? (
            <a href={proofReport.html_url} target="_blank" rel="noreferrer" data-testid="proof-report-link">Open HTML proof report</a>
          ) : (
            <span>No HTML proof report is linked for this run.</span>
          )}
        </div>
      </div>
      {rounds.length > 1 && <CertificationRoundTabs rounds={rounds} />}
      {packet.failure && (
        <div className="proof-section proof-failure" aria-labelledby="proofFailureHeading">
          <h3 id="proofFailureHeading">What failed</h3>
          <FailureSummary failure={packet.failure} showExcerpt />
        </div>
      )}
      <div className="proof-section" aria-labelledby="proofChecksHeading">
        <h3 id="proofChecksHeading">Verification</h3>
        {proofChecks.length ? (
          <div className="proof-checks">
            {proofChecks.map((check) => (
              <div className={`review-check check-${check.status}`} key={check.key}>
                <span>
                  <span className="status-icon" aria-hidden="true">{checkStatusIcon(check.status)}</span>
                  {" "}
                  {checkStatusLabel(check.status)}
                </span>
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
      </div>
      <div className="proof-section" aria-labelledby="proofStoriesHeading">
        <h3 id="proofStoriesHeading">Stories tested</h3>
        {stories.length ? (
          <div className="proof-stories" data-testid="proof-story-list">
            {stories.map((story) => (
              <article className={`proof-story story-${storyStatusClass(story.status)}`} key={story.id || story.title}>
                <span>
                  <span className="status-icon" aria-hidden="true">{storyStatusIcon(story.status)}</span>
                  {" "}
                  {storyStatusLabel(story.status)}
                </span>
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
      </div>
      <div className="proof-section" aria-labelledby="proofFilesHeading">
        <h3 id="proofFilesHeading">Changed files</h3>
        {changedFiles.length ? (
          <ul className="proof-files">
            {changedFiles.map((path) => <li key={path}>{path}</li>)}
            {packet.changes.truncated && <li>more files not shown</li>}
          </ul>
        ) : (
          <p>No changed files reported yet.</p>
        )}
      </div>
      <div className="proof-section" aria-labelledby="proofDiffHeading">
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
      </div>
      <div className="proof-section" aria-labelledby="proofArtifactsHeading">
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
      </div>
      <ProofEvidenceContent
        runId={detail.run_id}
        artifactIndex={proofArtifactIndex}
        content={proofContent}
      />
    </div>
  );
}

/**
 * Provenance card for the proof-of-work file backing the drawer.
 *
 * Cluster-evidence-trustworthiness #3: surfaces the recorded run_id /
 * branch / head_sha / sha256 / file mtime so the operator can confirm
 * the rendered evidence belongs to this run. When the proof's
 * ``run_id`` does not match the run record being viewed (likely a
 * stale or mis-routed file) we render a prominent warning rather than
 * silently rendering somebody else's evidence.
 */
function ProofProvenance({proofReport, runId}: {proofReport: ProofReportInfo; runId: string}) {
  if (!proofReport || !proofReport.available) return null;
  const sha = proofReport.sha256 ? proofReport.sha256.slice(0, 12) : null;
  const mismatch = proofReport.run_id_matches === false;
  const branch = proofReport.branch;
  const head = proofReport.head_sha ? proofReport.head_sha.slice(0, 7) : null;
  return (
    <div className="proof-section proof-provenance" data-testid="proof-provenance" aria-label="Proof of work provenance">
      {mismatch && (
        <div className="proof-provenance-warning" data-testid="proof-provenance-mismatch" role="alert">
          ⚠ Proof report records run {proofReport.run_id || "unknown"}, but this view is run {runId}. The evidence below may not belong to this run.
        </div>
      )}
      <dl className="proof-provenance-meta">
        {proofReport.generated_at && <><dt>Generated</dt><dd data-testid="proof-generated-at">{proofReport.generated_at}</dd></>}
        {proofReport.file_mtime && <><dt>File mtime</dt><dd data-testid="proof-file-mtime">{proofReport.file_mtime}</dd></>}
        {proofReport.run_id && <><dt>Run id</dt><dd data-testid="proof-run-id">{proofReport.run_id}</dd></>}
        {proofReport.session_id && <><dt>Session</dt><dd data-testid="proof-session-id">{proofReport.session_id}</dd></>}
        {branch && <><dt>Branch</dt><dd data-testid="proof-branch">{branch}</dd></>}
        {head && <><dt>HEAD</dt><dd data-testid="proof-head-sha" title={proofReport.head_sha || ""}>{head}</dd></>}
        {sha && <><dt>SHA-256</dt><dd data-testid="proof-sha256" title={proofReport.sha256 || ""}>{sha}</dd></>}
      </dl>
    </div>
  );
}

/**
 * Per-round certification tabs.
 *
 * Cluster-evidence-trustworthiness #4: surface ``round_history`` from
 * the proof-of-work so a multi-round cert (where round 1 found bugs
 * and round 2 passed after a fix) shows verdict, counts, durations,
 * and per-round diagnosis instead of collapsing to the final state.
 */
function CertificationRoundTabs({rounds}: {rounds: CertificationRound[]}) {
  const [activeRound, setActiveRound] = useState<number>(rounds[rounds.length - 1]?.round ?? 1);
  const active = rounds.find((entry) => entry.round === activeRound) || rounds[rounds.length - 1];
  return (
    <div className="proof-section proof-rounds" data-testid="proof-round-tabs" aria-labelledby="proofRoundsHeading">
      <h3 id="proofRoundsHeading">Certify rounds</h3>
      <div className="proof-round-tablist" role="tablist">
        {rounds.map((round) => {
          const label = `Round ${round.round ?? "?"}`;
          const verdictClass = round.verdict.toLowerCase() === "passed" ? "passed" : round.verdict.toLowerCase() === "failed" ? "failed" : "unknown";
          return (
            <button
              key={`round-${round.round}`}
              type="button"
              role="tab"
              aria-selected={round.round === active?.round}
              data-testid={`proof-round-tab-${round.round}`}
              className={`proof-round-tab proof-round-${verdictClass} ${round.round === active?.round ? "active" : ""}`}
              onClick={() => setActiveRound(round.round ?? 1)}
            >
              <strong>{label}</strong>
              <span>{round.verdict.toUpperCase()}</span>
              {round.duration_human && <small>{round.duration_human}</small>}
            </button>
          );
        })}
      </div>
      {active && (
        <div className="proof-round-detail" data-testid={`proof-round-detail-${active.round}`}>
          <dl className="proof-round-meta">
            <dt>Verdict</dt><dd data-testid="proof-round-verdict">{active.verdict}</dd>
            {active.stories_tested != null && (<><dt>Stories</dt><dd data-testid="proof-round-stories">{active.passed_count ?? 0} passed / {active.failed_count ?? 0} failed / {active.warn_count ?? 0} warn / {active.stories_tested} tested</dd></>)}
            {active.duration_human && (<><dt>Duration</dt><dd data-testid="proof-round-duration">{active.duration_human}</dd></>)}
            {active.cost_usd != null && (<><dt>Cost</dt><dd data-testid="proof-round-cost">${active.cost_usd.toFixed(2)}{active.cost_estimated ? " (est)" : ""}</dd></>)}
          </dl>
          {active.diagnosis && <p className="proof-round-diagnosis" data-testid="proof-round-diagnosis">{active.diagnosis}</p>}
          {active.failing_story_ids.length > 0 && (
            <div className="proof-round-stories-list">
              <strong>Failing:</strong>
              <ul>{active.failing_story_ids.map((id) => <li key={id} data-testid={`proof-round-failing-${id}`}>{id}</li>)}</ul>
            </div>
          )}
          {active.fix_commits.length > 0 && (
            <div className="proof-round-fix-commits">
              <strong>Fix commits:</strong>
              <ul>{active.fix_commits.map((commit) => <li key={commit}><code>{commit}</code></li>)}</ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Render the active proof artifact: text, image, video, or "no preview".
 *
 * Cluster-evidence-trustworthiness #6: previously every artifact was
 * decoded as UTF-8 and shoved into a ``<pre>``. Server-side MIME
 * detection now tells us whether the body is previewable text. When it
 * is not, we route image/video MIMEs to ``<img>``/``<video>`` against
 * the raw artifact endpoint and otherwise show a "no text preview;
 * download artifact" message with the size + MIME so the operator can
 * decide what to do.
 */
function ProofEvidenceContent({runId, artifactIndex, content}: {
  runId: string;
  artifactIndex: number | null;
  content: ArtifactContentResponse | null;
}) {
  const previewable = content ? content.previewable !== false : true;
  const mime = content?.mime_type || "";
  const sizeBytes = content?.size_bytes ?? 0;
  const artifactIsLog = isLogArtifact(content?.artifact || null);
  const proofContentText = content?.content || "";
  const compact = compactLongText(artifactIsLog ? proofContentText : formatArtifactContent(proofContentText), 20000);
  const rawUrl = artifactIndex != null ? `/api/runs/${encodeURIComponent(runId)}/artifacts/${artifactIndex}/raw` : null;
  return (
    <div className="proof-section proof-content" aria-labelledby="proofContentHeading">
      <div className="proof-content-heading">
        <div>
          <h3 id="proofContentHeading">Evidence content</h3>
          <p>{content?.artifact.label || "Loading selected evidence artifact"}</p>
          {mime && <small data-testid="proof-evidence-mime">{mime}{sizeBytes > 0 ? ` · ${humanBytes(sizeBytes)}` : ""}</small>}
        </div>
        {(content?.truncated || compact.truncated) && previewable ? <span>truncated</span> : null}
      </div>
      {!content ? (
        <pre className={artifactIsLog ? "log-content" : ""} tabIndex={0} aria-label="Selected evidence content">Loading evidence content...</pre>
      ) : !previewable ? (
        rawUrl && mime.startsWith("image/") ? (
          <a href={rawUrl} target="_blank" rel="noreferrer">
            <img src={rawUrl} alt={content.artifact.label} data-testid="proof-evidence-image" className="proof-evidence-image" />
          </a>
        ) : rawUrl && mime.startsWith("video/") ? (
          <video controls data-testid="proof-evidence-video" className="proof-evidence-video">
            <source src={rawUrl} type={mime} />
          </video>
        ) : (
          <div className="proof-evidence-binary" data-testid="proof-evidence-no-preview">
            <p>No text preview for {mime || "this artifact"}.</p>
            {rawUrl && <a href={rawUrl} target="_blank" rel="noreferrer" download data-testid="proof-evidence-download">Download artifact</a>}
          </div>
        )
      ) : (
        <pre className={artifactIsLog ? "log-content" : ""} tabIndex={0} aria-label="Selected evidence content">
          {compact.text ? (artifactIsLog ? renderLogText(compact.text) : compact.text) : "(empty)"}
        </pre>
      )}
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
        <span title={`${diff.branch || "-"} → ${diff.target}`}>{diff.branch || "-"} → {diff.target}</span>
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
    <div className={`review-packet review-${packet.readiness.tone || "info"}`}>
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
                <span>
                  <span className="status-icon" aria-hidden="true">{checkStatusIcon(check.status)}</span>
                  {" "}
                  {checkStatusLabel(check.status)}
                </span>
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
    </div>
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
      <div className="action-bar" role="group" aria-label="Advanced run actions">
        {visible.map((action) => {
          const name = actionName(action.key);
          const disabled = !action.enabled || (action.key === "m" && mergeBlocked);
          const title = action.key === "m" && mergeBlocked ? "Commit, stash, or revert local project changes before merging." : action.reason || action.preview || "";
          return (
            <button
              key={action.key}
              type="button"
              data-testid={`advanced-action-${name}`}
              disabled={disabled}
              title={title}
              onClick={() => onRunAction(name, action.label)}
            >
              {reviewActionLabel(action.label)}
            </button>
          );
        })}
      </div>
    </details>
  );
}

function ArtifactPane({artifacts, selectedArtifactIndex, artifactContent, onLoadArtifact, onBack, runId}: {
  artifacts: ArtifactRef[];
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  onLoadArtifact: (index: number) => void;
  onBack: () => void;
  runId: string;
}) {
  if (selectedArtifactIndex !== null) {
    const previewable = artifactContent ? artifactContent.previewable !== false : true;
    const mime = artifactContent?.mime_type || "";
    const sizeBytes = artifactContent?.size_bytes ?? 0;
    const artifactIsLog = isLogArtifact(artifactContent?.artifact || null);
    const rawContent = artifactContent?.content || "No content.";
    const compact = compactLongText(artifactIsLog ? rawContent : formatArtifactContent(rawContent), 20000);
    const rawUrl = `/api/runs/${encodeURIComponent(runId)}/artifacts/${selectedArtifactIndex}/raw`;
    return (
      <div className="artifact-pane">
        <button type="button" onClick={onBack}>Back to artifacts</button>
        <div className="artifact-meta">
          {artifactContent?.artifact.label || "artifact"} {(artifactContent?.truncated || compact.truncated) && previewable ? "(truncated)" : ""}
          {mime && <small data-testid="artifact-mime">{` · ${mime}${sizeBytes > 0 ? ` · ${humanBytes(sizeBytes)}` : ""}`}</small>}
        </div>
        {!artifactContent ? (
          <pre tabIndex={0}>Loading…</pre>
        ) : !previewable ? (
          mime.startsWith("image/") ? (
            <a href={rawUrl} target="_blank" rel="noreferrer">
              <img src={rawUrl} alt={artifactContent.artifact.label} data-testid="artifact-image" className="artifact-image" />
            </a>
          ) : mime.startsWith("video/") ? (
            <video controls data-testid="artifact-video" className="artifact-video"><source src={rawUrl} type={mime} /></video>
          ) : (
            <div className="artifact-binary" data-testid="artifact-no-preview">
              <p>No text preview for {mime || "this artifact"}.</p>
              <a href={rawUrl} target="_blank" rel="noreferrer" download data-testid="artifact-download">Download artifact</a>
            </div>
          )
        ) : (
          <pre className={artifactIsLog ? "log-content" : ""} tabIndex={0} aria-label="Artifact content">
            {artifactIsLog ? renderLogText(compact.text) : compact.text}
          </pre>
        )}
      </div>
    );
  }
  if (!artifacts.length) return <div className="artifact-pane">No artifacts.</div>;
  return (
    <div className="artifact-pane artifact-list">
      {artifacts.map((artifact) => (
        <button
          key={artifact.index}
          type="button"
          disabled={!isReadableArtifact(artifact)}
          onClick={() => onLoadArtifact(artifact.index)}
          title={artifactProvenanceTooltip(artifact)}
          data-testid={`artifact-list-item-${artifact.index}`}
        >
          <strong>{artifact.label}</strong>
          <span>{artifactKindLabel(artifact)}</span>
          <small className="artifact-provenance">
            {artifact.size_bytes != null && <span data-testid={`artifact-size-${artifact.index}`}>{humanBytes(artifact.size_bytes)}</span>}
            {artifact.mtime && <span data-testid={`artifact-mtime-${artifact.index}`}>{artifact.mtime}</span>}
            {artifact.sha256 && <span data-testid={`artifact-sha-${artifact.index}`}>{artifact.sha256.slice(0, 12)}</span>}
          </small>
        </button>
      ))}
    </div>
  );
}

function artifactProvenanceTooltip(artifact: ArtifactRef): string {
  const parts: string[] = [artifact.path];
  if (artifact.size_bytes != null) parts.push(`${artifact.size_bytes.toLocaleString()} bytes`);
  if (artifact.mtime) parts.push(`mtime ${artifact.mtime}`);
  if (artifact.sha256) parts.push(`sha256 ${artifact.sha256}`);
  return parts.join("\n");
}

// W3-CRITICAL-1: a single prior-run candidate the JobDialog's "Refine which
// run?" dropdown can show. The label is what the operator sees; run_id is
// what the server uses to look up the prior branch.
interface PriorRunOption {
  run_id: string;
  branch: string;
  label: string;
}

/**
 * Build the list of prior runs the operator can pick to iterate on.
 *
 * Inputs:
 * - ``landingItems`` — terminal-ready queue runs that are still on their
 *   build branch (they haven't been merged yet). These are the natural
 *   "refine the most recent thing" candidates.
 * - ``historyItems`` — every terminal run we know about. We keep only
 *   ``build`` and ``improve`` runs that succeeded and have a recorded
 *   branch. ``certify`` runs land no branch; ``merge`` runs are not a
 *   useful base.
 *
 * Output: deduped by run_id, sorted "freshest first" by relying on the
 * caller's order (history is newest-first and landing.items are queued
 * order). Keep at most 25 — improve is point-in-time, the operator
 * doesn't need a 200-row select.
 *
 * Why not just expose all history? Because picking a 6-month-old build to
 * "improve" is almost always operator error — a long-tail dropdown invites
 * mistakes. 25 is enough to reach back through a normal day's work.
 */
function collectPriorRunOptions(
  landingItems: LandingItem[],
  historyItems: HistoryItem[],
): PriorRunOption[] {
  const seen = new Set<string>();
  const options: PriorRunOption[] = [];

  // Landing items first — they're the freshest and explicitly "ready to
  // land", which means the branch is on disk and uncollided.
  for (const item of landingItems) {
    if (item.landing_state !== "ready") continue;
    const runId = (item.run_id || "").trim();
    const branch = (item.branch || "").trim();
    if (!runId || !branch || seen.has(runId)) continue;
    seen.add(runId);
    options.push({
      run_id: runId,
      branch,
      label: priorRunLabel({
        summary: item.summary,
        branch,
        task_id: item.task_id,
        run_id: runId,
        when: null,
      }),
    });
  }

  // History items: terminal-success build/improve only.
  for (const row of historyItems) {
    if (row.terminal_outcome !== "success") continue;
    const familyOk = row.command === "build"
      || row.command === "improve"
      || row.command?.startsWith("improve.");
    if (!familyOk) continue;
    const runId = (row.run_id || "").trim();
    const branch = (row.branch || "").trim();
    if (!runId || !branch || seen.has(runId)) continue;
    seen.add(runId);
    options.push({
      run_id: runId,
      branch,
      label: priorRunLabel({
        summary: row.summary || row.intent || "",
        branch,
        task_id: row.queue_task_id,
        run_id: runId,
        when: row.completed_at_display,
      }),
    });
    if (options.length >= 25) break;
  }

  return options;
}

function priorRunLabel(args: {
  summary: string | null;
  branch: string;
  task_id: string | null;
  run_id: string;
  when: string | null;
}): string {
  const summary = (args.summary || "").trim();
  const trimmed = summary.length > 60 ? summary.slice(0, 57) + "…" : summary;
  const headline = trimmed || args.task_id || args.branch || args.run_id;
  const suffix = args.when ? ` · ${args.when}` : "";
  return `${headline} (${args.branch})${suffix}`;
}

function JobDialog({project, dirtyFiles, priorRunOptions, onClose, onQueued, onError}: {
  project: StateResponse["project"] | undefined;
  dirtyFiles: string[];
  // W3-CRITICAL-1: list of prior runs the operator can iterate on. Sourced
  // from the parent (landing.items + history.items, filtered to terminal
  // success runs with a recorded branch). When empty, the dialog tells the
  // operator there's nothing to improve and disables Submit for command=
  // "improve" so the silent-fork-from-main bug cannot recur.
  priorRunOptions: PriorRunOption[];
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
  // W3-CRITICAL-1: which prior run the improve job should iterate on.
  // Auto-selects the freshest option when the dropdown becomes available
  // so the most-likely choice is one click away. Empty string means "no
  // selection" — Submit stays disabled so the server never silently falls
  // back to main.
  const [priorRunId, setPriorRunId] = useState<string>("");
  const priorRunOptionsAvailable = priorRunOptions.length > 0;
  // For non-improve commands the dropdown isn't rendered — treat it as
  // satisfied so it doesn't block the submit button.
  const priorRunMissing =
    command === "improve" && (!priorRunOptionsAvailable || !priorRunId.trim());
  // Whether the Advanced section should be programmatically opened. The
  // pre-submit summary "Edit" link sets this so users get one-click access
  // to the provider/model/effort fields without scrolling through Otto
  // jargon. mc-audit codex-first-time-user.md #2.
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const advancedRef = useRef<HTMLDetailsElement | null>(null);
  // mc-audit codex-destructive-action-safety #7: cost-incurring jobs queue
  // immediately on Submit and the watcher dispatches with no cancel window.
  // Add a 3-second grace banner with a [Cancel] button between submit and
  // the actual POST. The form/dialog stay editable during the grace period.
  const [pendingSeconds, setPendingSeconds] = useState<number | null>(null);
  const pendingTimerRef = useRef<number | null>(null);
  const pendingTickRef = useRef<number | null>(null);
  const pendingCancelledRef = useRef<boolean>(false);
  const dialogRef = useDialogFocus<HTMLFormElement>(onClose, submitting);
  const targetNeedsConfirmation = Boolean(project?.dirty);
  // ALL commands now require a non-empty intent (or focus). Codex flagged
  // that improve/certify could queue blank — equivalent to "do something
  // unspecified", which is never what a user means. mc-audit
  // codex-first-time-user.md #8.
  const intentRequired = !intent.trim();
  const submitDisabled =
    submitting
    || intentRequired
    || (targetNeedsConfirmation && !targetConfirmed)
    || priorRunMissing;

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

  // W3-CRITICAL-1: when the operator switches to "improve" and there is
  // exactly one obvious prior run (or the previously-picked id is no
  // longer in the list), auto-select the freshest. The list is sorted
  // most-recent-first by collectPriorRunOptions, so options[0] is the
  // last terminal-success build/improve.
  useEffect(() => {
    if (command !== "improve") return;
    const first = priorRunOptions[0];
    if (!first) {
      if (priorRunId) setPriorRunId("");
      return;
    }
    const stillValid = priorRunOptions.some((option) => option.run_id === priorRunId);
    if (!stillValid) {
      setPriorRunId(first.run_id);
    }
  }, [command, priorRunOptions, priorRunId]);

  // Sync the <details> open state when the user clicks the "Edit" link in
  // the summary. The native attribute change has to land on the DOM node so
  // the disclosure widget actually toggles open without a re-render race.
  useEffect(() => {
    const el = advancedRef.current;
    if (!el) return;
    if (advancedOpen && !el.open) el.open = true;
  }, [advancedOpen]);

  // mc-audit codex-destructive-action-safety #7: clear timers on unmount so a
  // half-finished grace countdown can't fire after the dialog closes.
  useEffect(() => {
    return () => {
      if (pendingTimerRef.current !== null) {
        window.clearTimeout(pendingTimerRef.current);
        pendingTimerRef.current = null;
      }
      if (pendingTickRef.current !== null) {
        window.clearInterval(pendingTickRef.current);
        pendingTickRef.current = null;
      }
    };
  }, []);

  async function performQueue(): Promise<void> {
    setStatus("queueing");
    try {
      const priorRunForPayload = command === "improve" ? priorRunId.trim() : "";
      const payloadArgs: Parameters<typeof buildQueuePayload>[0] = {
        command,
        subcommand,
        intent: intent.trim(),
        taskId: taskId.trim(),
        after,
        provider,
        model,
        effort,
        certification,
      };
      if (priorRunForPayload) payloadArgs.priorRunId = priorRunForPayload;
      const payload = buildQueuePayload(payloadArgs);
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

  function cancelGraceWindow(): void {
    pendingCancelledRef.current = true;
    if (pendingTimerRef.current !== null) {
      window.clearTimeout(pendingTimerRef.current);
      pendingTimerRef.current = null;
    }
    if (pendingTickRef.current !== null) {
      window.clearInterval(pendingTickRef.current);
      pendingTickRef.current = null;
    }
    setPendingSeconds(null);
    setSubmitting(false);
    setStatus("Queueing cancelled. Edit and resubmit if you still want to queue.");
  }

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
    if (priorRunMissing) {
      setStatus(
        priorRunOptionsAvailable
          ? "Select a prior run for the improve job to iterate on."
          : "No prior runs to improve. Run a build first."
      );
      return;
    }
    // mc-audit codex-destructive-action-safety #7: 3-second grace window.
    // Show a banner with countdown; user can hit [Cancel] to abort before
    // the POST fires. Form fields stay editable so the user can fix a typo
    // and resubmit. After the grace expires (and no cancel), POST fires.
    setSubmitting(true);
    pendingCancelledRef.current = false;
    setPendingSeconds(3);
    setStatus("Queueing in 3s — click Cancel to keep editing.");
    pendingTickRef.current = window.setInterval(() => {
      setPendingSeconds((prev) => {
        if (prev === null) return null;
        const next = Math.max(0, prev - 1);
        if (next > 0) {
          setStatus(`Queueing in ${next}s — click Cancel to keep editing.`);
        }
        return next;
      });
    }, 1000);
    pendingTimerRef.current = window.setTimeout(() => {
      if (pendingTickRef.current !== null) {
        window.clearInterval(pendingTickRef.current);
        pendingTickRef.current = null;
      }
      pendingTimerRef.current = null;
      setPendingSeconds(null);
      if (pendingCancelledRef.current) return;
      void performQueue();
    }, 3000);
  }

  const dirtyPreview = dirtyFiles.slice(0, 5);
  const dirtyOverflow = Math.max(0, dirtyFiles.length - dirtyPreview.length);

  // mc-audit live W11-CRITICAL-2: clicking the backdrop dismisses the dialog
  // (standard modal UX). Without this, a dialog whose Submit was silently
  // rejected (e.g. dirty-target guard) appears closed-but-stuck — the
  // backdrop intercepts every subsequent page click. Skip dismissal while a
  // POST is mid-flight or a grace window is counting down so the user can
  // never lose an in-flight job by missing the dialog and hitting backdrop.
  const onBackdropClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (submitting) return;
    onClose();
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onBackdropClick}>
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
        <div className={`target-guard ${project?.dirty ? "target-dirty" : ""}`} role="group" aria-label="Target project">
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
        {command === "improve" && (
          <label>Prior run
            {priorRunOptionsAvailable ? (
              <>
                <select
                  data-testid="job-prior-run-select"
                  value={priorRunId}
                  onChange={(event) => setPriorRunId(event.target.value)}
                >
                  {priorRunOptions.map((option) => (
                    <option key={option.run_id} value={option.run_id}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <span className="field-hint">
                  Improve iterates on this run&apos;s branch — its files are pre-loaded
                  into the worktree, so the agent extends the prior work instead
                  of starting from scratch.
                </span>
              </>
            ) : (
              <span
                className="field-hint"
                data-testid="job-prior-run-empty"
              >
                No prior runs to improve. Run a build first, then come back.
              </span>
            )}
          </label>
        )}
        <label>{intentLabelMap[command]}
          <textarea
            value={intent}
            data-testid="job-dialog-intent"
            rows={5}
            placeholder={intentPlaceholderMap[command]}
            aria-describedby={submitDisabled && !submitting && pendingSeconds === null ? "jobDialogValidationHint" : undefined}
            aria-invalid={intentRequired ? true : undefined}
            onChange={(event) => setIntent(event.target.value)}
            onKeyDown={(event) => {
              // W8-IMPORTANT-1: documented power-user shortcut. Cmd+Enter
              // (mac) / Ctrl+Enter (linux/windows) submits the dialog from
              // the textarea — universal in code-gen tools and in MC's own
              // accelerator catalogue. The default `<textarea>` swallows
              // Enter as a newline, so we intercept here. The submit goes
              // through the form's onSubmit so the validation gating (grace
              // window, dirty-target confirm, prior-run requirement) stays
              // in one place.
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                if (submitDisabled) return;
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
        </label>
        {submitDisabled && !submitting && pendingSeconds === null && (
          <p id="jobDialogValidationHint" className="job-dialog-validation" data-testid="job-dialog-validation-hint" aria-live="polite">
            {intentRequired
              ? `Describe the requested outcome (${intentLabelMap[command].toLowerCase()}) to enable queueing.`
              : targetNeedsConfirmation && !targetConfirmed
              ? "Confirm the dirty target project above to enable queueing."
              : priorRunMissing
              ? (priorRunOptionsAvailable
                  ? "Select the prior run for the improve job to iterate on."
                  : "No prior runs to improve. Run a build first, then come back.")
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
        {pendingSeconds !== null && (
          <div
            className="job-grace-banner"
            data-testid="job-grace-banner"
            role="status"
            aria-live="polite"
          >
            <span>
              Queueing in <strong data-testid="job-grace-countdown">{pendingSeconds}s</strong>… edit fields above or cancel to abort.
            </span>
            <button
              type="button"
              className="job-grace-cancel"
              data-testid="job-grace-cancel-button"
              onClick={cancelGraceWindow}
            >
              Cancel
            </button>
          </div>
        )}
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
                : priorRunMissing
                ? (priorRunOptionsAvailable
                    ? "Select the prior run for the improve job to iterate on."
                    : "No prior runs to improve. Run a build first, then come back.")
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

function ConfirmDialog({confirm, pending, error, checkboxAck, onChangeCheckboxAck, onCancel, onConfirm}: {
  confirm: ConfirmState;
  pending: boolean;
  error: string | null;
  checkboxAck: boolean;
  onChangeCheckboxAck: (next: boolean) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const isDanger = confirm.tone === "danger";
  const confirmClass = isDanger ? "danger-button" : "primary";
  const dialogRef = useDialogFocus<HTMLDivElement>(onCancel, pending);
  const blockedByCheckbox = Boolean(confirm.requireCheckbox) && !checkboxAck;
  const submitDisabled = pending || blockedByCheckbox;

  // mc-audit live W11-CRITICAL-2: clicking the backdrop dismisses the
  // confirm dialog (matches Escape behaviour). Skip while a confirm POST is
  // pending so a stray click can never abandon an in-flight action.
  const onBackdropClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (pending) return;
    onCancel();
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onBackdropClick}>
      <div
        ref={dialogRef}
        className={`confirm-dialog${isDanger ? " confirm-dialog-danger" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirmHeading"
        aria-describedby="confirmBody"
        data-tone={isDanger ? "danger" : "primary"}
        tabIndex={-1}
      >
        <header>
          <h2 id="confirmHeading">{confirm.title}</h2>
          {/* mc-audit microinteractions I6: For danger-tone confirms, drop
              the header Close affordance — two close paths (header × +
              footer Cancel) dilute focus and neither emphasises the safe
              choice. Non-danger confirms keep the header Close so they
              match JobDialog's affordance set. */}
          {!isDanger && (
            <button
              type="button"
              data-testid="confirm-dialog-header-close"
              disabled={pending}
              onClick={onCancel}
            >Close</button>
          )}
        </header>
        <div id="confirmBody" className="confirm-body">
          {confirm.body && <p className="confirm-body-text">{confirm.body}</p>}
          {confirm.bodyContent}
        </div>
        {confirm.requireCheckbox && (
          <label className="confirm-ack" data-testid="confirm-dialog-ack">
            <input
              type="checkbox"
              data-testid="confirm-dialog-ack-checkbox"
              checked={checkboxAck}
              disabled={pending}
              onChange={(event) => onChangeCheckboxAck(event.target.checked)}
            />
            <span>{confirm.requireCheckbox.label}</span>
          </label>
        )}
        {error && (
          <div
            className="confirm-error"
            data-testid="confirm-dialog-error"
            role="alert"
            aria-live="assertive"
          >
            <strong>Action did not complete</strong>
            <span>{error}</span>
          </div>
        )}
        <footer>
          {/* mc-audit microinteractions I6: in danger flows the Cancel
              button receives a "safe choice" emphasis (outline + bold) so
              a panicked user can spot the abort path at-a-glance. The
              confirm button still carries the red CTA. */}
          <button
            type="button"
            className={isDanger ? "confirm-dialog-cancel cancel-emphasis" : "confirm-dialog-cancel"}
            data-testid="confirm-dialog-cancel-button"
            disabled={pending}
            onClick={onCancel}
          >Cancel</button>
          <button
            className={confirmClass}
            type="button"
            data-testid="confirm-dialog-confirm-button"
            disabled={submitDisabled}
            aria-busy={pending}
            title={blockedByCheckbox ? "Tick the acknowledgement above to enable this action." : undefined}
            onClick={onConfirm}
          >
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
      body: "Start the watcher to apply pending commands.",
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
    // mc-audit info-density #6: surface a headline for the *hottest* active
    // run instead of a bare count. Pick the most recently active live item
    // (sort by elapsed_s ascending — newest first; fall back to first
    // working board card). Headline format:
    //   "<task-id> · <branch> · <elapsed> · <cost> · <last event>"
    // Each segment is omitted if missing, so the line never reads as "·· ·".
    const liveActive = data.live.items
      .filter((item) => item.active || ["starting", "running", "initializing", "terminating"].includes(item.display_status))
      .slice()
      .sort((a, b) => Number(a.elapsed_s || 0) - Number(b.elapsed_s || 0));
    const hottest = liveActive[0];
    const headline = hottest
      ? [
          hottest.queue_task_id || hottest.display_id || hottest.run_id,
          hottest.branch || null,
          hottest.elapsed_display || null,
          hottest.cost_display && hottest.cost_display !== "…" ? hottest.cost_display : null,
          hottest.overlay?.reason || hottest.last_event || null,
        ]
          .filter((segment): segment is string => Boolean(segment && segment.trim()))
          .join(" · ")
      : null;
    const titleSuffix = working === 1 ? "" : "s";
    return {
      kicker: "Working",
      title: headline || `${working} task${titleSuffix} in flight`,
      body: working > 1
        ? `${working} tasks active. Review packets will update as tasks finish.`
        : "Run is active. Review packet will update when it finishes.",
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
    storiesPassed: item.stories_passed,
    storiesTested: item.stories_tested,
    costDisplay: typeof item.cost_usd === "number" ? `$${item.cost_usd.toFixed(2)}` : null,
    durationDisplay: typeof item.duration_s === "number" ? formatDuration(item.duration_s) : null,
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
    storiesPassed: null,
    storiesTested: null,
    costDisplay: item.cost_display && item.cost_display !== "-" ? item.cost_display : null,
    durationDisplay: item.elapsed_display && item.elapsed_display !== "-" ? item.elapsed_display : null,
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

// mc-audit info-density #2: typed chip data for the task card meta row.
// Each chip carries a `kind` (used for both data-chip-kind attribute + CSS
// selector), a glyph icon for at-a-glance scanning, a label, and an optional
// tooltip with extra context. Chips are suppressed when the underlying value
// is null — never render a "-" placeholder.
export interface TaskChip {
  kind: "files" | "stories" | "cost" | "time" | "status";
  icon: string;
  label: string;
  tooltip?: string;
}

export function computeTaskChips(task: BoardTask): TaskChip[] {
  const chips: TaskChip[] = [];
  // Files chip — only when we have a real number to show. Pending/landed
  // states without a count are NOT shown as "-"; they're suppressed entirely.
  if (typeof task.changedFileCount === "number") {
    chips.push({
      kind: "files",
      icon: "📄",
      label: `${task.changedFileCount} file${task.changedFileCount === 1 ? "" : "s"}`,
      tooltip: "Files changed in this task's branch",
    });
  }
  // Stories chip — only when at least one story has been tested.
  const tested = Number(task.storiesTested || 0);
  const passed = Number(task.storiesPassed || 0);
  if (tested > 0) {
    chips.push({
      kind: "stories",
      icon: passed === tested ? "✓" : "•",
      label: `${passed}/${tested} stories`,
      tooltip: passed === tested ? "All certifier stories passed" : "Certifier story results",
    });
  }
  // Cost chip — only when a non-null cost display is available.
  if (task.costDisplay && task.costDisplay !== "-" && task.costDisplay.trim() !== "") {
    chips.push({
      kind: "cost",
      icon: "$",
      label: task.costDisplay.replace(/^\$/, ""),
      tooltip: "Estimated provider cost for this task",
    });
  }
  // Time chip — only when we have a duration.
  if (task.durationDisplay && task.durationDisplay !== "-" && task.durationDisplay.trim() !== "") {
    chips.push({
      kind: "time",
      icon: "⏱",
      label: task.durationDisplay,
      tooltip: "Wall-clock duration",
    });
  }
  // Fallback: if no concrete chips, surface the human-readable change line
  // ("diff pending" / "not built yet") as a single status chip so the row is
  // not visually empty. This preserves the prior behavior for queued tasks
  // without losing the typed-chip look elsewhere.
  if (chips.length === 0) {
    chips.push({
      kind: "status",
      icon: "·",
      label: taskChangeLine(task),
      tooltip: "Task status",
    });
  }
  return chips;
}

// mc-audit info-density #3: map a task's status string + stage to a tone
// keyword so the CSS can colour ready/running/failed/cancelled etc. with
// distinct hues. Tone values:
//   success — ready/landed/done/merged
//   running — starting/running/initializing/terminating/in flight
//   warning — blocked/queued/waiting/paused/interrupted/stale
//   danger  — failed/cancelled/removed/error
//   neutral — anything else
// mc-audit visual-coherence F10 — tone-to-glyph map for status badges so
// signal isn't carried by colour alone. ✓ pass-like, ⚠ warning,
// ✗ danger, ● running (filled circle reads as "in flight"),
// · neutral. Marked aria-hidden where applied; the surrounding label
// already communicates the status to screen readers.
export function toneIcon(tone: "success" | "running" | "warning" | "danger" | "neutral" | string): string {
  switch (tone) {
    case "success":
      return "✓";
    case "warning":
      return "⚠";
    case "danger":
      return "✗";
    case "running":
      return "●";
    default:
      return "·";
  }
}

export function statusTone(
  status: string,
  stage: BoardStage,
): "success" | "running" | "warning" | "danger" | "neutral" {
  const lower = status.toLowerCase();
  if (["ready", "landed", "merged", "done", "success"].some((value) => lower.includes(value))) {
    return "success";
  }
  if (["failed", "cancelled", "canceled", "removed", "error"].some((value) => lower.includes(value))) {
    return "danger";
  }
  if (["starting", "running", "initializing", "terminating", "in flight", "in_flight"].some((value) => lower.includes(value))) {
    return "running";
  }
  if (["queued", "waiting", "paused", "interrupted", "stale", "blocked"].some((value) => lower.includes(value))) {
    return "warning";
  }
  // Stage-based fallback for empty/odd statuses.
  if (stage === "ready") return "success";
  if (stage === "attention") return "danger";
  if (stage === "working") return "running";
  if (stage === "landed") return "success";
  return "neutral";
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
  if (!total) {
    // mc-audit error-empty-states #11: don't say "No work queued" when the
    // user just has filters hiding their work. Pick wording that matches
    // the empty reason.
    if (filtersAreActive(filters)) {
      const all = taskBoardColumns(data).reduce((sum, column) => sum + column.items.length, 0);
      if (all > 0) return `No tasks match the active filters (${all} hidden).`;
    }
    return "No work queued.";
  }
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

/**
 * W9-IMPORTANT-2: client-side dedupe of stale live entries.
 *
 * The backend's live → history transition is not atomic — a run that has
 * reached terminal can briefly appear in BOTH ``live[]`` and ``history[]``.
 * Without this guard, the UI shows the same run twice (once "running" in
 * the live pane, once "success" in the history pane) until the next poll.
 *
 * The fix is purely cosmetic: prefer the history record (which carries
 * ``terminal_outcome``) and drop the matching live row. We never mutate
 * the server response — the result is a shallow-cloned ``StateResponse``
 * with ``live.items`` filtered. ``visibleRunIds`` and other consumers
 * see the dedup'd shape automatically because ``data`` is the only
 * source of truth flowing into the renderers.
 */
export function dedupeLiveAgainstHistory(data: StateResponse): StateResponse {
  const terminalHistoryIds = new Set<string>();
  for (const item of data.history.items) {
    if (item.terminal_outcome && item.run_id) {
      terminalHistoryIds.add(item.run_id);
    }
  }
  if (terminalHistoryIds.size === 0) return data;
  const filtered = data.live.items.filter((item) => !terminalHistoryIds.has(item.run_id));
  if (filtered.length === data.live.items.length) return data;
  return {
    ...data,
    live: {
      ...data.live,
      items: filtered,
      // Re-derive counts so headline numbers don't disagree with the
      // rendered list. ``active_count`` reflects truly-active rows, so
      // recomputing from the filtered list is the correct floor.
      total_count: filtered.length,
      active_count: filtered.filter((item) => item.active).length,
    },
  };
}

function refreshIntervalMs(data: StateResponse | null): number {
  return Math.max(700, Math.min(5000, Number(data?.live.refresh_interval_s || 1.5) * 1000));
}

// mc-audit live W11-IMPORTANT-2: standalone `otto build` registers as
// `domain="atomic"` in the live registry, but every user-facing label calls
// it "build". Alias the domain at the UI surface so external automation +
// the type filter share consistent terminology with what the user sees.
// Other domains (queue, merge, supervisor) pass through unchanged.
export function domainLabel(domain: string | null | undefined): string {
  if (!domain) return "-";
  if (domain === "atomic") return "build";
  return domain;
}

function activeCount(watcher?: WatcherInfo): number {
  const counts = watcher?.counts || {};
  return Number(counts.running || 0)
    + Number(counts.initializing || 0)
    + Number(counts.starting || 0)
    + Number(counts.terminating || 0);
}

// mc-audit codex-first-time-user #15: a project is on its first-run path when
// it has zero history, zero live runs, no landing items, and no queued tasks.
// In that state the sidebar collapses Watcher / Heartbeat / In-flight /
// queued/ready/landed counters into a single "Project ready · No jobs yet"
// summary. As soon as ANY of those counters fills, the full dashboard returns.
export function isProjectFirstRun(data: StateResponse | null | undefined): boolean {
  if (!data) return false;
  const historyItems = Number(data.history?.items?.length || 0);
  const totalRows = Number(data.history?.total_rows || 0);
  const liveCount = Number(data.live?.total_count || data.live?.items?.length || 0);
  const landingItems = Number(data.landing?.items?.length || 0);
  const queued = Number(data.watcher?.counts?.queued || 0);
  const running = activeCount(data.watcher);
  return historyItems === 0
    && totalRows === 0
    && liveCount === 0
    && landingItems === 0
    && queued === 0
    && running === 0;
}

function canStartWatcher(data?: StateResponse | null): boolean {
  const queued = Number(data?.watcher.counts.queued || 0);
  const backlog = Number(data?.runtime.command_backlog.pending || 0) + Number(data?.runtime.command_backlog.processing || 0);
  return Boolean(data?.runtime.supervisor.can_start && (queued > 0 || backlog > 0));
}

// W3-IMPORTANT-6: when the Start watcher button is disabled because the
// watcher is already running, the previous tooltip leaked the Stop button's
// next_action ("Stop watcher to pause queue dispatch.") onto the Start
// control. That read as "do this to start" — the opposite of what the
// disabled state meant. Compose a Start-specific tooltip so the title and
// the visible label agree.
function startWatcherTooltip(data?: StateResponse | null): string {
  const blocked = data?.runtime.supervisor.start_blocked_reason || "";
  if (blocked) return blocked;
  if (data?.watcher.health.state === "running") return "Watcher already running.";
  if (data?.watcher.health.state === "stale") return "Stop the stale watcher before starting another one.";
  // Falls back to the shared next_action only when the action is actually
  // about starting (state is "stopped" or unknown).
  return data?.watcher.health.next_action || "";
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
  // mc-audit live W11-IMPORTANT-1: prefer the server-side cross-domain
  // active count so atomic-domain runs (standalone otto build) flow through
  // the diagnostics overview.
  const active = data?.live?.active_count ?? activeCount(data?.watcher);
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

/**
 * Toggle the `inert` attribute on every element matching `selector`. Used
 * to make sidebars / main content / inspector quiet for AT and keyboard
 * users when an overlay (inspector / job dialog / confirm dialog) takes
 * focus. Replacing the previous `aria-hidden` toggle pattern, which broke
 * when the inspector itself was a child of `<main>` (mc-audit a11y A11Y-01,
 * A11Y-02). `inert` removes the entire subtree from focus, click, and AT
 * tree — exactly the semantics we want.
 */
function InertEffect({active, selector}: {active: boolean; selector: string}) {
  useEffect(() => {
    if (typeof document === "undefined") return;
    const nodes = Array.from(document.querySelectorAll<HTMLElement>(selector));
    if (!nodes.length) return;
    const previous = nodes.map((node) => node.hasAttribute("inert"));
    if (active) {
      for (const node of nodes) node.setAttribute("inert", "");
    } else {
      for (const node of nodes) node.removeAttribute("inert");
    }
    return () => {
      nodes.forEach((node, idx) => {
        if (previous[idx]) node.setAttribute("inert", "");
        else node.removeAttribute("inert");
      });
    };
  }, [active, selector]);
  return null;
}

/**
 * Polite singleton aria-live region. mc-audit a11y A11Y-10.
 */
function LiveRegion({message}: {message: string}) {
  return (
    <div
      id="mc-live-region"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="sr-only"
      data-testid="mc-live-region"
    >
      {message}
    </div>
  );
}

/**
 * Per-view document.title. mc-audit a11y A11Y-09.
 */
function useDocumentTitle({viewMode, selectedRunId, selectedDetail, inspectorOpen, inspectorMode}: {
  viewMode: ViewMode;
  selectedRunId: string | null;
  selectedDetail: RunDetail | null;
  inspectorOpen: boolean;
  inspectorMode: InspectorMode;
}) {
  useEffect(() => {
    if (typeof document === "undefined") return;
    const base = "Otto Mission Control";
    let prefix = viewMode === "diagnostics" ? "Diagnostics" : "Tasks";
    if (selectedRunId && selectedDetail) {
      const intent = selectedDetail.title || selectedDetail.run_id;
      const truncated = intent.length > 60 ? `${intent.slice(0, 57)}...` : intent;
      prefix = truncated;
      if (inspectorOpen) {
        const tabLabel = {proof: "Result", diff: "Code changes", logs: "Logs", artifacts: "Artifacts"}[inspectorMode];
        prefix = `${truncated} - ${tabLabel}`;
      }
    }
    document.title = `${prefix} · ${base}`;
  }, [viewMode, selectedRunId, selectedDetail, inspectorOpen, inspectorMode]);
}

/**
 * Live-region message generator. mc-audit a11y A11Y-10.
 */
function useLiveAnnouncement({viewMode, selectedRunId, inspectorOpen, inspectorMode}: {
  viewMode: ViewMode;
  selectedRunId: string | null;
  inspectorOpen: boolean;
  inspectorMode: InspectorMode;
}): string {
  return useMemo(() => {
    const parts: string[] = [];
    parts.push(`Viewing ${viewMode === "diagnostics" ? "Diagnostics" : "Tasks"}`);
    if (selectedRunId) {
      parts.push(`run ${selectedRunId}`);
      if (inspectorOpen) {
        const tabLabel = {proof: "Result", diff: "Code changes", logs: "Logs", artifacts: "Artifacts"}[inspectorMode];
        parts.push(`${tabLabel} tab`);
      }
    }
    return parts.join(", ");
  }, [viewMode, selectedRunId, inspectorOpen, inspectorMode]);
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

// --------------------------------------------------------------------------
// Destructive-action confirm helpers (mc-audit Phase 4 cluster H).
// Findings closed:
//   #1 Land all ready: scrollable enumeration + checkbox gate
//   #2 Single merge: task id, branch->target, file count, files preview
//   #3 Cleanup: status-specific irreversible copy, mentions worktree
//   #4 Cancel: task id + SIGTERM 30s + work-loss warning
//   #5 Watcher stop: pid + running/queued/backlog counts
// --------------------------------------------------------------------------

function BulkLandingConfirmList({items, target}: {
  items: LandingItem[];
  target: string;
}) {
  if (!items.length) return null;
  return (
    <div
      className="confirm-bulk-list"
      data-testid="confirm-bulk-list"
      role="region"
      aria-label="Tasks to land"
    >
      <ul>
        {items.map((item) => {
          const branch = item.branch || "(no branch)";
          const fileCount = Number(item.changed_file_count || 0);
          const previewFiles = (item.changed_files || []).slice(0, 3);
          const overflow = Math.max(0, fileCount - previewFiles.length);
          return (
            <li
              key={item.task_id}
              className="confirm-bulk-row"
              data-testid={`confirm-bulk-row-${item.task_id}`}
            >
              <div className="confirm-bulk-row-head">
                <strong>{item.task_id}</strong>
                <span>
                  <code>{branch}</code> &rarr; <code>{target}</code>
                </span>
                <span className="confirm-bulk-row-count">
                  {fileCount} file{fileCount === 1 ? "" : "s"}
                </span>
              </div>
              {previewFiles.length > 0 && (
                <ul className="confirm-bulk-row-files">
                  {previewFiles.map((path) => <li key={path}><code>{path}</code></li>)}
                  {overflow > 0 && <li className="muted">+{overflow} more</li>}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function SingleMergeConfirmDetails({detail, diff}: {
  detail: RunDetail | null;
  diff: DiffResponse | null;
}) {
  if (!detail) return null;
  const packet = detail.review_packet;
  const taskId = detail.queue_task_id || detail.run_id;
  const branch = packet.changes.branch || diff?.branch || detail.branch || "(no branch)";
  const target = packet.changes.target || diff?.target || "main";
  const fileCount = Number(packet.changes.file_count || diff?.file_count || 0);
  const files = (packet.changes.files && packet.changes.files.length
    ? packet.changes.files
    : diff?.files || []).slice(0, 5);
  const overflow = Math.max(0, fileCount - files.length);
  return (
    <div
      className="confirm-merge-details"
      data-testid="confirm-merge-details"
      role="region"
      aria-label="Merge details"
    >
      <dl>
        <dt>Task</dt>
        <dd data-testid="confirm-merge-task-id"><code>{taskId}</code></dd>
        <dt>Branch</dt>
        <dd>
          <code>{branch}</code> &rarr; <code>{target}</code>
        </dd>
        <dt>Files</dt>
        <dd data-testid="confirm-merge-file-count">{fileCount} file{fileCount === 1 ? "" : "s"}</dd>
      </dl>
      {files.length > 0 && (
        <ul className="confirm-merge-files" data-testid="confirm-merge-files">
          {files.map((path) => <li key={path}><code>{path}</code></li>)}
          {overflow > 0 && <li className="muted">+{overflow} more</li>}
        </ul>
      )}
    </div>
  );
}

function describeCleanupConfirm(detail: RunDetail | null): {
  title: string;
  body: string;
  confirmLabel: string;
} {
  if (!detail) {
    return {
      title: "Cleanup",
      body: "Remove this run record? This cannot be undone from Mission Control.",
      confirmLabel: "Cleanup",
    };
  }
  const status = String(detail.status || "").toLowerCase();
  const queuedStatuses = new Set(["queued", "pending", "waiting", "starting"]);
  const isQueued = queuedStatuses.has(status);
  if (isQueued) {
    const taskId = detail.queue_task_id || detail.run_id;
    return {
      title: "Remove queued task",
      body: `Remove queued task ${taskId} from the queue? It will not run; this cannot be undone from Mission Control.`,
      confirmLabel: "Remove queued task",
    };
  }
  const worktreeName = (detail.worktree || "").split("/").pop() || detail.worktree || detail.run_id;
  return {
    title: "Remove run + cleanup",
    body: `Remove run record and cleanup worktree ${worktreeName}? This cannot be undone from Mission Control.`,
    confirmLabel: "Remove and cleanup",
  };
}

function describeCancelConfirm(detail: RunDetail | null, runId: string): {
  title: string;
  body: string;
  confirmLabel: string;
} {
  const taskId = detail?.queue_task_id || runId;
  const body = `Cancel task ${taskId}. Otto will signal the agent to stop. If it doesn't acknowledge within 30s, Mission Control will terminate the process. Work in progress may be lost.`;
  return {
    title: "Cancel task",
    body,
    confirmLabel: "Cancel task",
  };
}

function describeWatcherStopConfirm(data: StateResponse | null): {
  body: string;
  detail: ReactNode;
  requireAck: boolean;
} {
  if (!data) {
    return {
      body: "Stop the queue watcher?",
      detail: null,
      requireAck: false,
    };
  }
  const counts = data.watcher.counts || {};
  const pid = data.watcher.health.blocking_pid || data.watcher.health.watcher_pid;
  const running = Number(counts.running || 0) + Number(counts.terminating || 0);
  const queued = Number(counts.queued || 0);
  const backlog = Number(data.runtime.command_backlog.pending || 0)
    + Number(data.runtime.command_backlog.processing || 0);
  const pidText = pid ? `pid ${pid}` : "process";
  const body = `Stop watcher (${pidText}).`;
  const requireAck = running > 0 || queued > 0 || backlog > 0;
  const detail = (
    <ul
      className="confirm-watcher-stop"
      data-testid="confirm-watcher-stop-detail"
      aria-label="Watcher stop impact"
    >
      <li>
        <strong data-testid="confirm-watcher-stop-pid">{pidText}</strong>
      </li>
      <li>
        <span data-testid="confirm-watcher-stop-running">
          {running} running task{running === 1 ? "" : "s"} may be interrupted.
        </span>
      </li>
      <li>
        <span data-testid="confirm-watcher-stop-queued">
          {queued} queued task{queued === 1 ? "" : "s"}
        </span>
        {" and "}
        <span data-testid="confirm-watcher-stop-backlog">
          {backlog} pending command{backlog === 1 ? "" : "s"}
        </span>
        {" will wait until you restart the watcher."}
      </li>
    </ul>
  );
  return {body, detail, requireAck};
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

/**
 * Overlay optimistic run states onto the live-run items returned by
 * /api/state. Used by mc-audit microinteractions I4 to flip a row's
 * displayed status to "cancelling" the moment the operator confirms a
 * cancel, instead of waiting for the next /api/state poll. The original
 * server item is otherwise untouched.
 */
function applyOptimisticRunStates(
  items: LiveRunItem[],
  overlays: Record<string, "cancelling">,
): LiveRunItem[] {
  if (!items.length || !Object.keys(overlays).length) return items;
  return items.map((item) => {
    const overlay = overlays[item.run_id];
    if (!overlay) return item;
    return {
      ...item,
      display_status: overlay,
      active: true,
      last_event: "cancelling",
    };
  });
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
  // mc-audit codex-first-time-user #26: "malformed" → "unreadable" in user-facing copy.
  const malformed = events.malformed_count ? ` / ${events.malformed_count} unreadable` : "";
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

// mc-audit visual-coherence F10 — status badges must NOT rely on colour
// alone. Prefix every check/story badge with a glyph so deuteranopia /
// protanopia users (~5% of men) can still tell pass/warn/fail apart.
// The icon char is exposed via a `.status-icon` span so test harnesses
// and styling can target it independently of the text label.
function checkStatusIcon(status: string): string {
  return {
    pass: "✓",
    warn: "⚠",
    fail: "✗",
    pending: "…",
    info: "i",
  }[status] || "i";
}

function storyStatusIcon(status: string): string {
  return {
    pass: "✓",
    warn: "⚠",
    fail: "✗",
    skipped: "–",
    unknown: "i",
  }[status] || "i";
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

// ---------------------------------------------------------------------------
// Heavy-user paper-cut #3 — notifications when long runs finish
// ---------------------------------------------------------------------------

let _notificationPermissionRequested = false;

/**
 * Request notification permission on the user's first interaction. Browsers
 * only allow `Notification.requestPermission()` to fire from a user gesture,
 * so we tie this to "open job dialog" / "start watcher" — natural moments
 * the user has signalled intent to track work.
 *
 * Idempotent: only fires once per page load. Gracefully degrades when the
 * Notification API is unavailable (older browsers, secure-context-required
 * pages) — caller flow is unaffected.
 */
function requestNotificationPermissionOnce(): void {
  if (_notificationPermissionRequested) return;
  if (typeof window === "undefined") return;
  const notif = (window as unknown as {Notification?: typeof Notification}).Notification;
  if (!notif || typeof notif.requestPermission !== "function") return;
  if (notif.permission !== "default") {
    _notificationPermissionRequested = true;
    return;
  }
  _notificationPermissionRequested = true;
  try {
    const result = notif.requestPermission();
    // Some older browsers return undefined (callback-only). Both are safe.
    if (result && typeof (result as Promise<NotificationPermission>).then === "function") {
      void (result as Promise<NotificationPermission>).catch(() => {/* user denied; degrade silently */});
    }
  } catch {
    // Some embedded browsers throw on requestPermission — never rethrow.
  }
}

/**
 * Track the live run set across polls; when a previously-live run drops out
 * (i.e. transitioned to a terminal state) AND the tab is currently hidden,
 * fire a Notification. The set lives in a ref so we re-compute the diff in
 * place rather than re-creating subscriptions per poll. We also reset the
 * baseline when the project changes (different `data.project.path`) so a
 * project switch isn't mis-read as "all runs just finished."
 */
function useNotificationsOnRunFinish(data: StateResponse | null): void {
  const previousLiveIdsRef = useRef<Set<string>>(new Set());
  const previousProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const projectPath = data?.project?.path || null;
    if (previousProjectRef.current !== projectPath) {
      // Project changed (or first hydration). Snapshot the current live set
      // as the new baseline; do NOT fire notifications for "runs that
      // disappeared" because they belonged to the previous project.
      previousProjectRef.current = projectPath;
      const initial = new Set<string>();
      for (const item of data?.live.items || []) initial.add(item.run_id);
      previousLiveIdsRef.current = initial;
      return;
    }
    const currentIds = new Set<string>();
    for (const item of data?.live.items || []) currentIds.add(item.run_id);
    const finished: string[] = [];
    for (const prevId of previousLiveIdsRef.current) {
      if (!currentIds.has(prevId)) finished.push(prevId);
    }
    previousLiveIdsRef.current = currentIds;
    if (!finished.length) return;
    const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
    if (!hidden) return;
    const notif = (window as unknown as {Notification?: typeof Notification}).Notification;
    if (!notif) return;
    if (notif.permission !== "granted") return;
    try {
      // One notification per finished batch — multiple completions in a
      // single poll fire one consolidated message rather than spamming.
      const body = finished.length === 1
        ? `Run ${finished[0]} finished.`
        : `${finished.length} runs finished.`;
      // eslint-disable-next-line no-new -- side-effect intentional
      new notif("Otto: build completed", {body});
    } catch {
      // Some browsers throw if the page lifecycle frame doesn't permit it.
    }
  }, [data]);
}

// ---------------------------------------------------------------------------
// Heavy-user paper-cut #4 — Cmd-K command palette
// ---------------------------------------------------------------------------

interface CommandPaletteProps {
  projects: ManagedProjectInfo[];
  currentPath: string | null;
  onSelect: (path: string | null) => void;
  onClose: () => void;
}

/**
 * Quick project switcher. Cmd-K opens, type to fuzzy-filter (substring on
 * name + path), Up/Down (or j/k) to navigate, Enter to select, Escape to
 * close. The current project is rendered with a "current" badge and is
 * non-selectable to avoid an accidental no-op switch.
 *
 * Modal isolation: we render inside `.modal-backdrop` and use
 * `useDialogFocus`, mirroring JobDialog/ConfirmDialog. The `inertSiblings`
 * pattern at the App shell respects this: when paletteOpen is true the
 * sidebar/main go inert, so click-through and tab traversal stay inside.
 */
function CommandPalette({projects, currentPath, onSelect, onClose}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const dialogRef = useDialogFocus<HTMLDivElement>(onClose, false);
  const filtered = useMemo(() => filterPalette(projects, query), [projects, query]);
  // Keep the highlight pinned in range when the filter narrows.
  useEffect(() => {
    setHighlight(0);
  }, [query]);
  const moveHighlight = useCallback((dir: 1 | -1) => {
    if (!filtered.length) return;
    setHighlight((prev) => (prev + dir + filtered.length) % filtered.length);
  }, [filtered.length]);
  const onBackdropClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    onClose();
  };
  return (
    <div className="modal-backdrop" role="presentation" onClick={onBackdropClick} data-testid="command-palette-backdrop">
      <div
        ref={dialogRef}
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-labelledby="commandPaletteHeading"
        data-testid="command-palette"
        tabIndex={-1}
      >
        <header>
          <h2 id="commandPaletteHeading" className="sr-only">Command palette</h2>
          <input
            value={query}
            type="search"
            placeholder="Switch project — type to filter"
            data-testid="command-palette-input"
            aria-label="Filter projects"
            autoFocus
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" || (event.key === "j" && (event.ctrlKey || event.metaKey))) {
                event.preventDefault();
                moveHighlight(1);
              } else if (event.key === "ArrowUp" || (event.key === "k" && (event.ctrlKey || event.metaKey))) {
                event.preventDefault();
                moveHighlight(-1);
              } else if (event.key === "Enter") {
                event.preventDefault();
                const target = filtered[highlight];
                if (!target) return;
                if (target.path === currentPath) return; // no-op for current
                onSelect(target.path);
              }
              // Escape is handled by useDialogFocus.
            }}
          />
        </header>
        <ul className="command-palette-list" data-testid="command-palette-list" role="listbox" aria-label="Recent projects">
          {filtered.length === 0 && (
            <li
              className="command-palette-empty"
              data-testid="command-palette-empty"
            >No projects match.</li>
          )}
          {filtered.map((project, idx) => {
            const isCurrent = project.path === currentPath;
            const isHighlighted = idx === highlight;
            return (
              <li
                key={project.path}
                role="option"
                aria-selected={isHighlighted}
                className={`command-palette-row ${isHighlighted ? "highlighted" : ""} ${isCurrent ? "current" : ""}`}
                data-testid={`command-palette-row-${project.path}`}
              >
                <button
                  type="button"
                  className="command-palette-row-button"
                  data-testid={`command-palette-select-${project.path}`}
                  disabled={isCurrent}
                  onMouseEnter={() => setHighlight(idx)}
                  onClick={() => {
                    if (isCurrent) return;
                    onSelect(project.path);
                  }}
                >
                  <strong>{project.name}</strong>
                  <code>{project.path}</code>
                  {isCurrent && <span className="command-palette-badge">current</span>}
                </button>
              </li>
            );
          })}
        </ul>
        <footer className="command-palette-footer">
          <span>↑/↓ to navigate · Enter to switch · Esc to close</span>
        </footer>
      </div>
    </div>
  );
}

/**
 * Substring-everywhere fuzzy filter for the palette. We compare against
 * lowercased name + path so a query like "kanb" matches "kanban-portal" or
 * a project under `~/projects/kanban`. Order is preserved (no scoring) —
 * keeps things predictable for muscle-memory users who recognise the order
 * of their last 5 projects.
 */
function filterPalette(projects: ManagedProjectInfo[], query: string): ManagedProjectInfo[] {
  const trimmed = query.trim().toLowerCase();
  if (!trimmed) return projects;
  return projects.filter((project) => {
    const haystack = `${project.name || ""} ${project.path || ""}`.toLowerCase();
    return haystack.includes(trimmed);
  });
}
