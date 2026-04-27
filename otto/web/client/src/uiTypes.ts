import type {OutcomeFilter, RunBuildConfig, RunTypeFilter} from "./types";

export interface ToastState {
  message: string;
  severity: "information" | "warning" | "error";
}

export interface ResultBannerState {
  title: string;
  body: string;
  severity: ToastState["severity"];
}

export interface Filters {
  type: RunTypeFilter;
  outcome: OutcomeFilter;
  query: string;
  activeOnly: boolean;
}

export type ViewMode = "tasks" | "diagnostics";

export type BoardStage = "attention" | "working" | "ready" | "landed";
export type InspectorMode = "try" | "proof" | "logs" | "artifacts" | "diff";

export type HistorySortColumn = "outcome" | "run" | "summary" | "duration" | "usage";
export type HistorySortDir = "asc" | "desc";

export const DEFAULT_HISTORY_PAGE_SIZE = 25;
export const HISTORY_PAGE_SIZE_OPTIONS: readonly number[] = [10, 25, 50, 100];

export const defaultFilters: Filters = {
  type: "all",
  outcome: "all",
  query: "",
  activeOnly: false,
};

export interface BoardTask {
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
  storiesPassed?: number | null;
  storiesTested?: number | null;
  usageDisplay?: string | null;
  durationDisplay?: string | null;
}
