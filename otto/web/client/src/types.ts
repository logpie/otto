export type RunTypeFilter = "all" | "build" | "improve" | "certify" | "merge" | "queue";
export type OutcomeFilter = "all" | "success" | "failed" | "interrupted" | "cancelled" | "removed" | "other";
export type JobCommand = "build" | "improve" | "certify";
export type ImproveSubcommand = "bugs" | "feature" | "target";
export type CertificationPolicy = "" | "fast" | "standard" | "thorough" | "skip";
export type VerificationPolicy = "smart" | "fast" | "full" | "skip";
export type ExecutionMode = "split" | "agentic";
export type PlanningMode = "direct" | "spec-review" | "spec-auto" | "spec-file";

export interface ProjectDefaults {
  provider: string;
  model: string | null;
  reasoning_effort: string | null;
  certifier_mode: string;
  skip_product_qa: boolean;
  run_budget_seconds: number | null;
  spec_timeout: number | null;
  max_certify_rounds: number | null;
  max_turns_per_call: number | null;
  strict_mode: boolean;
  split_mode: boolean;
  allow_dirty_repo: boolean;
  default_branch: string | null;
  test_command: string | null;
  queue_concurrent: number | null;
  queue_task_timeout_s: number | null;
  queue_worktree_dir: string | null;
  queue_on_watcher_restart: string | null;
  queue_merge_certifier_mode: string | null;
  config_file_exists: boolean;
  config_error: string | null;
}

export interface AgentBuildConfig {
  provider: string | null;
  model: string | null;
  reasoning_effort: string | null;
}

export interface RunBuildConfig {
  command_family: "build" | "improve" | "certify" | null;
  provider: string | null;
  model: string | null;
  reasoning_effort: string | null;
  certifier_mode: string | null;
  skip_product_qa: boolean;
  certification: string;
  planning: "direct" | "spec_review" | "spec_auto" | "spec_file" | string;
  spec_file_path: string | null;
  run_budget_seconds: number | null;
  spec_timeout: number | null;
  max_certify_rounds: number | null;
  max_turns_per_call: number | null;
  strict_mode: boolean;
  split_mode: boolean;
  allow_dirty_repo: boolean;
  default_branch: string | null;
  test_command: string | null;
  queue: {
    concurrent: number | null;
    task_timeout_s: number | null;
    worktree_dir: string | null;
    on_watcher_restart: string | null;
    merge_certifier_mode: string | null;
  };
  agents: Record<"build" | "certifier" | "spec" | "fix", AgentBuildConfig>;
  config_file_exists: boolean;
  config_error: string | null;
}

export interface ProjectInfo {
  path: string;
  name: string;
  branch: string | null;
  dirty: boolean;
  head_sha: string | null;
  defaults?: ProjectDefaults;
}

export interface TokenUsage {
  input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  total_tokens?: number;
}

export interface ProjectStats {
  active_count: number;
  history_count: number;
  success_count: number;
  failed_count: number;
  total_duration_s: number;
  duration_display: string;
  reported_cost_usd: number | null;
  cost_display: string;
  token_usage: TokenUsage;
  total_tokens: number;
  token_display: string;
  stories_passed: number;
  stories_tested: number;
}

export interface ManagedProjectInfo extends ProjectInfo {
  managed?: boolean;
}

export interface ProjectsResponse {
  launcher_enabled: boolean;
  projects_root: string;
  current: ProjectInfo | null;
  projects: ManagedProjectInfo[];
}

export interface ProjectMutationResponse {
  ok: boolean;
  project?: ProjectInfo;
  current?: ProjectInfo | null;
  projects: ManagedProjectInfo[];
}

export interface WatcherInfo {
  alive: boolean;
  watcher: {pid?: number | null} | null;
  counts: Record<string, number>;
  health: WatcherHealth;
}

export interface WatcherHealth {
  state: "running" | "stale" | "stopped" | string;
  blocking_pid: number | null;
  watcher_pid: number | null;
  watcher_process_alive: boolean;
  lock_pid: number | null;
  lock_process_alive: boolean;
  heartbeat: string | null;
  heartbeat_age_s: number | null;
  started_at: string | null;
  log_path: string;
  next_action: string;
}

export interface RuntimeIssue {
  severity: "error" | "warning" | "info" | string;
  label: string;
  detail: string;
  next_action: string;
}

export interface RuntimeFileStatus {
  path: string;
  exists: boolean;
  size_bytes: number | null;
  mtime: string | null;
  error: string | null;
  line_count?: number;
  malformed_count?: number;
}

export interface RuntimeStatus {
  status: "healthy" | "attention" | string;
  generated_at: string;
  queue_tasks: number | null;
  state_tasks: number | null;
  command_backlog: {
    pending: number;
    processing: number;
    malformed: number;
    items: CommandBacklogItem[];
  };
  files: {
    queue: RuntimeFileStatus;
    state: RuntimeFileStatus;
    commands: RuntimeFileStatus;
    processing: RuntimeFileStatus;
  };
  supervisor: RuntimeSupervisor;
  issues: RuntimeIssue[];
}

export interface CommandBacklogItem {
  state: "pending" | "processing" | string;
  command_id: string | null;
  kind: string | null;
  run_id: string | null;
  task_id: string | null;
  requested_at: string | null;
  age_s: number | null;
}

export interface RuntimeSupervisor {
  mode: string;
  path: string;
  metadata: Record<string, unknown> | null;
  metadata_error: string | null;
  supervised_pid: number | null;
  matches_blocking_pid: boolean;
  can_start: boolean;
  can_stop: boolean;
  start_blocked_reason: string | null;
  stop_target_pid: number | null;
  watcher_log_path: string;
  web_log_exists: boolean;
  queue_lock_holder_pid: number | null;
}

export interface MissionEvent {
  schema_version: number;
  event_id: string;
  created_at: string;
  kind: string;
  severity: "error" | "warning" | "info" | "success" | string;
  message: string;
  run_id: string | null;
  task_id: string | null;
  actor: Record<string, unknown>;
  details: Record<string, unknown>;
}

export interface EventsState {
  path: string;
  items: MissionEvent[];
  total_count: number;
  malformed_count: number;
  limit: number;
  truncated: boolean;
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
  build_config: RunBuildConfig;
  queue_status: string;
  landing_state: "ready" | "merged" | "blocked" | string;
  label: string;
  merge_id: string | null;
  merge_status: string | null;
  merge_run_status: string | null;
  duration_s: number | null;
  cost_usd: number | null;
  token_usage?: TokenUsage;
  stories_passed: number | null;
  stories_tested: number | null;
  changed_file_count: number;
  changed_files: string[];
  diff_error: string | null;
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
  certifier_mode: string | null;
  skip_product_qa: boolean;
  build_config: RunBuildConfig;
  run_config?: RunBuildConfig;
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
  token_usage: TokenUsage;
  last_event: string;
  progress: string;
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
  token_usage: TokenUsage;
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
  // Cluster-evidence-trustworthiness #7: artifact provenance — the UI
  // surfaces these as tooltips + sortable columns so the operator can
  // spot stale or tampered files. Null when the file is missing or
  // exceeds the in-line hashing limit (16MB).
  size_bytes: number | null;
  mtime: string | null;
  sha256: string | null;
}

export interface PhaseTimelineItem {
  phase: string;
  label: string;
  status: string;
  duration_s: number | null;
  cost_usd: number | null;
  rounds: number | null;
  token_usage: TokenUsage;
  provider: string | null;
  model: string | null;
  reasoning_effort: string | null;
}

export interface VerificationCheck {
  id: string;
  label: string;
  action: string;
  status: "pending" | "running" | "pass" | "fail" | "warn" | "skipped" | "flag_for_human" | string;
  reason: string;
  source: string;
  evidence: string[];
  metadata: Record<string, unknown>;
}

export interface VerificationPlan {
  schema_version: number;
  scope: string;
  target: string;
  policy: VerificationPolicy | string;
  risk_level: string;
  verification_level: string;
  allow_skip: boolean;
  reasons: string[];
  checks: VerificationCheck[];
  metadata: Record<string, unknown>;
}

export interface RunDetail extends RunSummary {
  display_status: string;
  active: boolean;
  source: "live" | "history" | string;
  title: string;
  summary_lines: string[];
  overlay: Overlay | null;
  artifacts: ArtifactRef[];
  log_paths: string[];
  selected_log_index: number;
  selected_log_path: string | null;
  legal_actions: ActionState[];
  review_packet: ReviewPacket;
  verification_plan: VerificationPlan | null;
  phase_timeline: PhaseTimelineItem[];
  landing_state: string | null;
  merge_info?: Record<string, unknown> | null;
  record: Record<string, unknown>;
}

export interface CertificationRound {
  round: number | null;
  verdict: string;
  stories_tested: number | null;
  passed_count: number | null;
  failed_count: number | null;
  warn_count: number | null;
  failing_story_ids: string[];
  warn_story_ids: string[];
  diagnosis: string | null;
  duration_s: number | null;
  duration_human: string | null;
  cost_usd: number | null;
  cost_estimated: boolean;
  fix_commits: string[];
  fix_diff_stat: string | null;
  still_failing_after_fix: string[];
  subagent_errors: string[];
}

export interface DemoEvidenceItem {
  name: string;
  kind: string;
  href: string;
  caption: string;
}

export interface DemoEvidenceStory {
  id: string;
  title: string;
  status: string;
  needs_visual: boolean;
  needs_file_validation: boolean;
  has_text_evidence: boolean;
  has_file_validation: boolean;
  proof_level: string;
  visual_items: DemoEvidenceItem[];
}

export interface DemoEvidence {
  schema_version: number;
  app_kind: string;
  demo_required: boolean;
  demo_status: "strong" | "partial" | "missing" | "not_applicable" | "unknown" | string;
  demo_reason: string | null;
  primary_demo: DemoEvidenceItem | null;
  stories: DemoEvidenceStory[];
  counts: Record<string, number | string | boolean | null>;
}

export interface EvidenceGate {
  schema_version: number;
  status: "pass" | "warn" | "fail" | "not_applicable" | "unknown" | string;
  blocks_pass: boolean;
  reason: string;
}

export interface ProofReportInfo {
  json_path: string | null;
  html_path: string | null;
  html_url: string | null;
  available: boolean;
  // Cluster-evidence-trustworthiness #3: provenance threaded from the
  // proof-of-work.json. The client invalidates its cached content when
  // ``sha256`` or ``file_mtime`` changes, and warns when ``run_id``
  // does not match the run record being viewed (``run_id_matches`` is
  // false). ``run_id_matches === null`` means we have no proof-side
  // run_id to compare against (legacy report).
  generated_at: string | null;
  run_id: string | null;
  session_id: string | null;
  branch: string | null;
  head_sha: string | null;
  file_mtime: string | null;
  sha256: string | null;
  run_id_matches: boolean | null;
}

export interface ReviewPacket {
  headline: string;
  status: string;
  summary: string;
  product_handoff: ProductHandoff;
  readiness: {
    state: "ready" | "merged" | "blocked" | "in_progress" | "needs_attention" | string;
    label: string;
    tone: "success" | "info" | "warning" | "danger" | string;
    blockers: string[];
    next_step: string;
  };
  checks: Array<{
    key: string;
    label: string;
    status: "pass" | "warn" | "fail" | "pending" | "info" | string;
    detail: string;
  }>;
  next_action: {
    label: string;
    action_key: string | null;
    enabled: boolean;
    reason: string | null;
  };
  certification: {
    stories_passed: number | null;
    stories_tested: number | null;
    passed: boolean;
    summary_path: string | null;
    stories: Array<{
      id: string;
      title: string;
      status: "pass" | "warn" | "fail" | "skipped" | "unknown" | string;
      methodology: string;
      surface: string;
      detail: string;
    }>;
    // Cluster-evidence-trustworthiness #4: per-round history so the UI
    // can render round tabs with verdict + counts + diagnosis instead
    // of pretending every certification was a single round.
    rounds: CertificationRound[];
    demo_evidence: DemoEvidence;
    evidence_gate: EvidenceGate;
    proof_report: ProofReportInfo;
  };
  changes: {
    branch: string | null;
    target: string;
    merged: boolean;
    merge_id: string | null;
    file_count: number;
    files: string[];
    truncated: boolean;
    diff_command: string | null;
    diff_error: string | null;
  };
  evidence: ArtifactRef[];
  failure: {
    reason: string | null;
    last_event: string | null;
    excerpt?: string | null;
    source?: string | null;
  } | null;
}

export interface ProductCommand {
  label: string;
  command: string;
}

export interface ProductFlow {
  title: string;
  steps: string[];
}

export interface ProductSampleData {
  label: string;
  value: string;
  detail: string;
}

export interface ProductHandoff {
  kind: string;
  label: string;
  source: string;
  source_path: string | null;
  root: string;
  summary: string;
  preview_available: boolean;
  preview_label: string;
  preview_reason: string;
  task_summary: string;
  task_status: string | null;
  task_branch: string | null;
  task_changed_files: string[];
  task_flows: ProductFlow[];
  urls: string[];
  launch: ProductCommand[];
  reset: ProductCommand[];
  try_flows: ProductFlow[];
  sample_data: ProductSampleData[];
  notes: string[];
}

export interface StateResponse {
  project: ProjectInfo;
  project_stats: ProjectStats;
  watcher: WatcherInfo;
  runtime: RuntimeStatus;
  events: EventsState;
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
  total_bytes: number;
  eof: boolean;
}

export interface ArtifactContentResponse {
  artifact: ArtifactRef;
  content: string;
  truncated: boolean;
  // Cluster-evidence-trustworthiness #6: server-side MIME detection so
  // images/videos/PDFs render via their dedicated previewer (or as a
  // download link) instead of garbage-decoded text. ``previewable``
  // is true when the body was decoded as UTF-8 text.
  previewable?: boolean;
  mime_type?: string;
  size_bytes?: number;
}

export interface DiffResponse {
  run_id: string;
  branch: string | null;
  target: string;
  command: string | null;
  files: string[];
  file_count: number;
  text: string;
  error: string | null;
  truncated: boolean;
  // Freshness metadata: SHAs captured at fetch time so the operator can
  // tell whether the code about to be merged matches what they reviewed.
  // ``target_sha`` / ``branch_sha`` may be null when the underlying
  // ``git rev-parse`` failed (deleted branch, missing remote, etc.); the
  // failing lookups are recorded in ``errors`` so the UI can render a
  // targeted warning instead of a generic "diff unavailable".
  fetched_at: string;
  target_sha: string | null;
  branch_sha: string | null;
  merge_base: string | null;
  limit_chars: number;
  full_size_chars: number;
  shown_hunks: number;
  total_hunks: number;
  errors: string[];
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
  // W3-CRITICAL-1: improve must iterate on the prior run's branch (not fork
  // from main). The JobDialog populates this when command === "improve" and
  // the operator picks a prior run from the dropdown.
  prior_run_id?: string;
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
