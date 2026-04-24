export type RunTypeFilter = "all" | "build" | "improve" | "certify" | "merge" | "queue";
export type OutcomeFilter = "all" | "success" | "failed" | "interrupted" | "cancelled" | "removed";
export type JobCommand = "build" | "improve" | "certify";
export type ImproveSubcommand = "bugs" | "feature" | "target";

export interface ProjectInfo {
  path: string;
  name: string;
  branch: string | null;
  dirty: boolean;
  head_sha: string | null;
}

export interface WatcherInfo {
  alive: boolean;
  watcher: {pid?: number | null} | null;
  counts: Record<string, number>;
}

export interface LandingCounts {
  ready: number;
  merged: number;
  blocked: number;
  total: number;
}

export interface LandingCollision {
  left: string;
  right: string;
  files: string[];
  file_count: number;
}

export interface LandingItem {
  task_id: string;
  run_id: string | null;
  branch: string | null;
  worktree: string | null;
  summary: string | null;
  queue_status: string;
  landing_state: "ready" | "merged" | "blocked" | string;
  label: string;
  merge_id: string | null;
  merge_status: string | null;
  merge_run_status: string | null;
  duration_s: number | null;
  cost_usd: number | null;
  stories_passed: number | null;
  stories_tested: number | null;
}

export interface LandingState {
  target: string;
  items: LandingItem[];
  counts: LandingCounts;
  collisions: LandingCollision[];
  merge_blocked: boolean;
  merge_blockers: string[];
  dirty_files: string[];
}

export interface Overlay {
  level: string;
  label: string;
  reason: string;
  writer_alive: boolean;
}

export interface RunSummary {
  run_id: string;
  domain: string;
  run_type: string;
  command: string;
  display_name: string;
  status: string;
  terminal_outcome: string | null;
  project_dir: string;
  cwd: string | null;
  queue_task_id: string | null;
  merge_id: string | null;
  branch: string | null;
  worktree: string | null;
  provider: string | null;
  model: string | null;
  reasoning_effort: string | null;
  adapter_key: string;
  version: number;
}

export interface LiveRunItem extends RunSummary {
  display_status: string;
  active: boolean;
  display_id: string;
  branch_task: string;
  elapsed_s: number | null;
  elapsed_display: string;
  cost_usd: number | null;
  cost_display: string;
  last_event: string;
  row_label: string;
  overlay: Overlay | null;
}

export interface HistoryItem {
  run_id: string;
  domain: string;
  run_type: string;
  command: string;
  status: string;
  terminal_outcome: string | null;
  queue_task_id: string | null;
  merge_id: string | null;
  branch: string | null;
  worktree: string | null;
  summary: string;
  intent: string | null;
  completed_at_display: string;
  outcome_display: string;
  duration_s: number | null;
  duration_display: string;
  cost_usd: number | null;
  cost_display: string;
  resumable: boolean;
  adapter_key: string;
}

export interface ActionState {
  key: string;
  label: string;
  enabled: boolean;
  reason: string | null;
  preview: string;
}

export interface ArtifactRef {
  index: number;
  label: string;
  path: string;
  kind: string;
  exists: boolean;
}

export interface RunDetail extends RunSummary {
  display_status: string;
  active: boolean;
  source: Record<string, unknown>;
  title: string;
  summary_lines: string[];
  overlay: Overlay | null;
  artifacts: ArtifactRef[];
  log_paths: string[];
  selected_log_index: number;
  selected_log_path: string | null;
  legal_actions: ActionState[];
  record: Record<string, unknown>;
}

export interface StateResponse {
  project: ProjectInfo;
  watcher: WatcherInfo;
  landing: LandingState;
  live: {
    items: LiveRunItem[];
    total_count: number;
    active_count: number;
    refresh_interval_s: number;
  };
  history: {
    items: HistoryItem[];
    page: number;
    page_size: number;
    total_rows: number;
    total_pages: number;
  };
}

export interface LogsResponse {
  path: string | null;
  offset: number;
  next_offset: number;
  text: string;
  exists: boolean;
}

export interface ArtifactContentResponse {
  artifact: ArtifactRef;
  content: string;
  truncated: boolean;
}

export interface ActionResult {
  ok: boolean;
  message: string | null;
  severity: string;
  modal_title: string | null;
  modal_message: string | null;
  refresh: boolean;
  clear_banner: boolean;
}

export interface QueuePayload {
  intent?: string;
  focus?: string;
  subcommand?: ImproveSubcommand;
  as?: string;
  after?: string[];
  extra_args: string[];
}

export interface QueueResult {
  ok: boolean;
  message: string;
  task: Record<string, unknown>;
  warnings: string[];
  refresh: boolean;
}

export interface ApiErrorBody {
  ok?: boolean;
  message?: string;
  severity?: string;
}
