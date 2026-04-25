import type {ApiErrorBody, CertificationPolicy, ImproveSubcommand, JobCommand, OutcomeFilter, QueuePayload, RunTypeFilter} from "./types";

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
  provider: string;
  model: string;
  effort: string;
  certification: CertificationPolicy;
}): QueuePayload {
  const payload: QueuePayload = {extra_args: []};
  const after = splitCsv(args.after);
  if (args.taskId) payload.as = args.taskId;
  if (after.length) payload.after = after;
  if (args.provider) payload.extra_args.push("--provider", args.provider);
  if (args.model) payload.extra_args.push("--model", args.model);
  if (args.effort) payload.extra_args.push("--effort", args.effort);
  const certificationFlag = certificationArg(args.command, args.subcommand, args.certification);
  if (certificationFlag) payload.extra_args.push(certificationFlag);

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

function certificationArg(command: JobCommand, subcommand: ImproveSubcommand, certification: CertificationPolicy): string | null {
  if (!certification) return null;
  if (certification === "skip") return command === "build" ? "--no-qa" : null;
  if (command === "build" || command === "certify" || (command === "improve" && subcommand === "bugs")) {
    return `--${certification}`;
  }
  return null;
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}
