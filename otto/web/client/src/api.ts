import type {ApiErrorBody, CertificationPolicy, ExecutionMode, ImproveSubcommand, JobCommand, OutcomeFilter, PlanningMode, QueuePayload, RunTypeFilter} from "./types";

export interface StateQuery {
  type: RunTypeFilter;
  outcome: OutcomeFilter;
  query: string;
  activeOnly: boolean;
  // 1-based page in the UI (server is 0-based — we subtract on send).
  // Optional so detail/non-history call sites can omit it.
  historyPage?: number;
  // Allowed values: 10 / 25 / 50 / 100. Anything else is rejected by the
  // server and falls back to the model default. See codex-state-management
  // #6 — the server already accepts these params, the client just never
  // sent them, leaving power users stuck on page 1.
  historyPageSize?: number;
}

export class ApiError extends Error {
  status: number;
  severity: string;
  // Original server-provided message preserved for tests/logging. The
  // `.message` is replaced with a friendlier mapping by `friendlyApiMessage`
  // when one of the recognized status codes hits.
  rawMessage: string;

  constructor(message: string, status: number, severity = "error") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.severity = severity;
    this.rawMessage = message;
  }
}

/**
 * Map common project-launcher / generic API failures to actionable copy.
 *
 * Pulled from mc-audit codex-first-time-user.md #24/#15/#16: launcher errors
 * surface raw `HTTP <status>` strings (or unhelpful server text) without
 * recovery hints. Mapping at the API boundary means every caller (project
 * create, project select, queue, watcher, ...) gets the same friendly copy
 * for free instead of duplicating switch-on-status in each component.
 */
export function friendlyApiMessage(status: number, raw: string, context?: {projectName?: string; projectPath?: string}): string {
  const trimmed = (raw || "").trim();
  const lowered = trimmed.toLowerCase();
  if (status === 409) {
    if (context?.projectName) {
      return `A project named "${context.projectName}" already exists. Choose a different name or open the existing one.`;
    }
    return trimmed || "That name or path is already in use.";
  }
  if (status === 400) {
    if (context?.projectPath || lowered.includes("not a git repository")) {
      const where = context?.projectPath || trimmed.replace(/^[^:]*:\s*/, "");
      return `${where || "That path"} isn't a valid git repo. Make sure it exists and contains a .git directory.`;
    }
    return trimmed || "Request was invalid.";
  }
  if (status === 403) {
    if (context?.projectPath) {
      return `Permission denied at ${context.projectPath}. Check directory permissions, or pick a different path.`;
    }
    return trimmed || "Permission denied.";
  }
  if (status === 404) {
    return trimmed || "The requested resource was not found.";
  }
  if (status >= 500) {
    return `Server error: ${trimmed || `HTTP ${status}`}. Try again or check the server log.`;
  }
  return trimmed || `HTTP ${status}`;
}

export function stateQueryParams(query: StateQuery): URLSearchParams {
  const params = new URLSearchParams();
  params.set("type", query.type);
  params.set("outcome", query.outcome);
  params.set("query", query.query);
  params.set("active_only", query.activeOnly ? "true" : "false");
  // The server uses 0-based history pages; the UI presents 1-based pages
  // because that's what humans expect. Translate at the boundary so the
  // rest of the front end can keep speaking 1-based.
  if (query.historyPage && query.historyPage > 1) {
    params.set("history_page", String(query.historyPage - 1));
  }
  if (query.historyPageSize && query.historyPageSize > 0) {
    params.set("history_page_size", String(query.historyPageSize));
  }
  return params;
}

/**
 * Per-run detail URL builder.
 *
 * The detail endpoint (`GET /api/runs/{run_id}`) is per-run — it must NOT
 * inherit state-pane filter params (`type` / `outcome` / `query` /
 * `active_only` / `history_page`). Earlier code reused `stateQueryParams`
 * here, which appended state-pane filters even for detail fetches. With
 * synthetic queue-compat run-ids (e.g. `queue-compat:<task>`) the resulting
 * URL is misrouted on the server and returns 404 (see live-findings
 * W2-IMPORTANT-1 / W13-IMPORTANT-1). The detail endpoint accepts only
 * `history_page_size` here so the inspector can show consistent paging if
 * a future detail-side history slice is added.
 */
export function runDetailUrl(runId: string, opts: {historyPageSize?: number} = {}): string {
  const params = new URLSearchParams();
  if (opts.historyPageSize && opts.historyPageSize > 0) {
    params.set("history_page_size", String(opts.historyPageSize));
  }
  const qs = params.toString();
  const base = `/api/runs/${encodeURIComponent(runId)}`;
  return qs ? `${base}?${qs}` : base;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const body = (data || {}) as ApiErrorBody;
    const raw = body.message || `HTTP ${response.status}`;
    const friendly = friendlyApiMessage(response.status, raw);
    throw new ApiError(friendly, response.status, body.severity || "error");
  }
  return data as T;
}

export function buildQueuePayload(args: {
  command: JobCommand;
  subcommand: ImproveSubcommand;
  intent: string;
  taskId: string;
  after: string;
  executionMode: ExecutionMode;
  provider: string;
  model: string;
  effort: string;
  buildProvider: string;
  buildModel: string;
  buildEffort: string;
  certifierProvider: string;
  certifierModel: string;
  certifierEffort: string;
  fixProvider: string;
  fixModel: string;
  fixEffort: string;
  certification: CertificationPolicy;
  planning: PlanningMode;
  specFilePath: string;
  priorRunId?: string;
}): QueuePayload {
  const payload: QueuePayload = {extra_args: []};
  const after = splitCsv(args.after);
  if (args.taskId) payload.as = args.taskId;
  if (after.length) payload.after = after;
  if (args.command !== "certify") {
    payload.extra_args.push(args.executionMode === "agentic" ? "--agentic" : "--split");
  }
  if (args.provider) payload.extra_args.push("--provider", args.provider);
  if (args.model) payload.extra_args.push("--model", args.model);
  if (args.effort) payload.extra_args.push("--effort", args.effort);
  if (args.command === "build" && args.executionMode === "split") {
    pushPhaseArgs(payload.extra_args, "build", args.buildProvider, args.buildModel, args.buildEffort);
  }
  if (args.command === "certify" || args.executionMode === "split") {
    pushPhaseArgs(payload.extra_args, "certifier", args.certifierProvider, args.certifierModel, args.certifierEffort);
  }
  if (args.command !== "certify" && args.executionMode === "split") {
    pushPhaseArgs(
      payload.extra_args,
      args.command === "improve" ? "improver" : "fix",
      args.fixProvider,
      args.fixModel,
      args.fixEffort,
    );
  }
  const certificationFlag = certificationArg(args.command, args.subcommand, args.certification);
  if (certificationFlag) payload.extra_args.push(certificationFlag);
  if (args.command === "build") {
    pushPlanningArgs(payload.extra_args, args.planning, args.specFilePath);
  }

  if (args.command === "build") {
    payload.intent = args.intent;
  } else if (args.command === "improve") {
    payload.subcommand = args.subcommand;
    if (args.intent) payload.focus = args.intent;
    // W3-CRITICAL-1: server uses prior_run_id to base the improve worktree
    // on the prior run's branch instead of forking from main and colliding
    // on the same files. Optional — server falls back to main when omitted.
    if (args.priorRunId) payload.prior_run_id = args.priorRunId;
  } else if (args.intent) {
    payload.intent = args.intent;
  }
  return payload;
}

function pushPhaseArgs(extraArgs: string[], phase: "build" | "certifier" | "fix" | "improver", provider: string, model: string, effort: string): void {
  if (provider) extraArgs.push(`--${phase}-provider`, provider);
  if (model) extraArgs.push(`--${phase}-model`, model);
  if (effort) extraArgs.push(`--${phase}-effort`, effort);
}

function certificationArg(command: JobCommand, subcommand: ImproveSubcommand, certification: CertificationPolicy): string | null {
  if (!certification) return null;
  if (certification === "skip") return command === "build" ? "--no-qa" : null;
  if (command === "build" || command === "certify" || (command === "improve" && subcommand === "bugs")) {
    return `--${certification}`;
  }
  return null;
}

function pushPlanningArgs(extraArgs: string[], planning: PlanningMode, specFilePath: string): void {
  if (planning === "spec-review") {
    extraArgs.push("--spec", "--spec-review-mode", "web");
  } else if (planning === "spec-auto") {
    extraArgs.push("--spec", "--yes");
  } else if (planning === "spec-file" && specFilePath) {
    extraArgs.push("--spec-file", specFilePath);
  }
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}
