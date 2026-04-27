import {DEFAULT_HISTORY_PAGE_SIZE, defaultFilters, HISTORY_PAGE_SIZE_OPTIONS} from "./uiTypes";
import type {Filters, HistorySortColumn, HistorySortDir, ViewMode} from "./uiTypes";
import type {OutcomeFilter, RunTypeFilter} from "./types";

export interface RouteState {
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

// Allowed values for the URL-persisted filter params. We validate on read so
// a hand-crafted URL with `?ft=banana` doesn't crash the SPA — invalid values
// silently fall back to "all" / defaultFilters.
const RUN_TYPE_VALUES: readonly RunTypeFilter[] = ["all", "build", "improve", "certify", "merge", "queue"];
const OUTCOME_VALUES: readonly OutcomeFilter[] = ["all", "success", "failed", "interrupted", "cancelled", "removed", "other"];
const HISTORY_SORT_COLUMNS: readonly HistorySortColumn[] = ["outcome", "run", "summary", "duration", "usage"];

export function defaultRouteState(): RouteState {
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

export function readRouteState(): RouteState {
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

export function parseHistoryPageParam(raw: string | null): number {
  if (!raw) return 1;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1) return 1;
  return parsed;
}

export function parseHistoryPageSizeParam(raw: string | null): number | null {
  if (!raw) return null;
  const parsed = Number.parseInt(raw, 10);
  if (!HISTORY_PAGE_SIZE_OPTIONS.includes(parsed)) return null;
  return parsed;
}

export function writeRouteState(route: RouteState, mode: "push" | "replace"): void {
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
