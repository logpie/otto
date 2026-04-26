import type {ApiErrorBody, CertificationPolicy, ExecutionMode, ImproveSubcommand, JobCommand, OutcomeFilter, PlanningMode, QueuePayload, RunTypeFilter} from "./types";

export interface StateQuery {
  type: RunTypeFilter;
  outcome: OutcomeFilter;
  query: string;
  activeOnly: boolean;
}

export class ApiError extends Error {
  status: number;
  severity: string;

  constructor(message: string, status: number, severity = "error") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.severity = severity;
  }
}

export function stateQueryParams(query: StateQuery): URLSearchParams {
  const params = new URLSearchParams();
  params.set("type", query.type);
  params.set("outcome", query.outcome);
  params.set("query", query.query);
  params.set("active_only", query.activeOnly ? "true" : "false");
  return params;
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
    throw new ApiError(body.message || `HTTP ${response.status}`, response.status, body.severity || "error");
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
