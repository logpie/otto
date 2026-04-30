import {HealthCard} from "../MicroComponents";
import type {AutopilotDecision, AutopilotEvent, AutopilotIncident, AutopilotMode, StateResponse} from "../../types";
import {formatDuration} from "../../utils/format";
import {commandBacklogLine} from "../../utils/missionControl";

export function SystemHealth({
  data,
  autopilotPending = false,
  onAutopilotTick,
  onAutopilotApprove,
  onAutopilotEmergencyStop,
}: {
  data: StateResponse | null;
  autopilotPending?: boolean;
  onAutopilotTick?: () => void;
  onAutopilotApprove?: (decisionId: string) => void;
  onAutopilotEmergencyStop?: () => void;
}) {
  const runtime = data?.runtime;
  const watcher = data?.watcher.health;
  const backlog = runtime?.command_backlog;
  const queueFile = runtime?.files.queue;
  const stateFile = runtime?.files.state;
  const dirty = data?.landing.dirty_files || [];
  const autopilot = data?.autopilot;
  const autopilotOwnsTaskAttention = Boolean(
    autopilot && (
      autopilot.pending_decisions.some(isTaskRecoveryItem) ||
      autopilot.incidents.some(isTaskRecoveryItem)
    ),
  );
  const autopilotOwnsQueuePause = Boolean(
    autopilot && (
      autopilot.pending_decisions.some((decision) => decisionIncludesAction(decision, "start_watcher")) ||
      (autopilot.decisions || []).some((decision) => decisionIncludesAction(decision, "start_watcher")) ||
      autopilot.incidents.some((incident) => incident.action === "start_watcher")
    ),
  );
  const manualIssueCount = (runtime?.issues || []).filter((issue) => !isSuppressedRuntimeIssue(issue, {
    taskAttention: autopilotOwnsTaskAttention,
    queuePause: autopilotOwnsQueuePause,
  })).length;
  const commandCount = backlog?.items.length || 0;
  return (
    <>
      <AutopilotPanel
        autopilot={autopilot}
        pending={autopilotPending}
        prominent
        onTick={onAutopilotTick}
        onApprove={onAutopilotApprove}
        onEmergencyStop={onAutopilotEmergencyStop}
      />
      <section className="panel system-health" aria-labelledby="systemHealthHeading">
        <div className="panel-heading">
          <div>
            <h2 id="systemHealthHeading">System Health</h2>
            <p className="panel-subtitle">Queue runner, repository, and recovery state.</p>
          </div>
        </div>
        <div className="system-health-grid">
          <HealthCard
            title="Watcher"
            status={watcher?.state || "unknown"}
            detail={watcher ? `${watcher.blocking_pid ? `pid ${watcher.blocking_pid}` : "no pid"} · ${watcher.heartbeat_age_s === null || watcher.heartbeat_age_s === undefined ? "no heartbeat" : `${formatDuration(watcher.heartbeat_age_s)} heartbeat`}` : "No watcher data yet"}
            next={watcher?.next_action || "Refresh state"}
            tone={watcher?.state === "running" ? "success" : watcher?.state === "stale" ? "warning" : "neutral"}
          />
          <HealthCard
            title="Queue files"
            status={`${backlog?.pending || 0} pending`}
            detail={`${backlog?.processing || 0} processing · ${backlog?.malformed || 0} unreadable`}
            next={queueFile?.error || stateFile?.error || "Files OK."}
            tone={backlog?.malformed ? "danger" : backlog?.pending || backlog?.processing ? "info" : "neutral"}
          />
          <HealthCard
            title="Repository"
            status={data?.landing.merge_blocked ? "blocked" : data?.project.dirty ? "dirty" : "clean"}
            detail={dirty.length ? dirty.slice(0, 3).join(", ") : `branch ${data?.project.branch || "-"}`}
            next={data?.landing.merge_blocked ? "Commit or stash local changes." : "OK."}
            tone={data?.landing.merge_blocked ? "danger" : data?.project.dirty ? "warning" : "success"}
          />
          <HealthCard
            title="Runtime owner"
            status={runtime?.supervisor.mode || "unknown"}
            detail={runtime?.supervisor.stop_target_pid ? `stop target pid ${runtime.supervisor.stop_target_pid}` : runtime?.supervisor.start_blocked_reason || "No stop target."}
            next={runtime?.supervisor.can_start ? "Ready to start." : runtime?.supervisor.can_stop ? "Stop available." : runtime?.supervisor.start_blocked_reason || "No action available."}
            tone={runtime?.issues.some((issue) => issue.severity === "error") ? "danger" : runtime?.issues.length ? "warning" : "neutral"}
          />
        </div>
        {(commandCount || manualIssueCount) ? <RecoveryActions data={data} suppress={{taskAttention: autopilotOwnsTaskAttention, queuePause: autopilotOwnsQueuePause}} /> : null}
      </section>
    </>
  );
}

function isTaskRecoveryItem(item: {action?: string | null; run_id?: string | null; task_id?: string | null; kind?: string | null}): boolean {
  if (item.run_id || item.task_id) {
    return item.action === "requeue" || item.action === "pilot_triage" || item.action === "human_required";
  }
  return String(item.kind || "").startsWith("landing_") || String(item.kind || "").startsWith("run_");
}

function decisionIncludesAction(decision: AutopilotDecision, action: string): boolean {
  return decision.action === action || (decision.includes_actions || []).includes(action) || (decision.chain_actions || []).includes(action);
}

function isSuppressedRuntimeIssue(issue: {label?: string; key?: string}, suppress: {taskAttention?: boolean; queuePause?: boolean}): boolean {
  const label = String(issue.label || "").toLowerCase();
  const key = String(issue.key || "").toLowerCase();
  if (suppress.taskAttention && (label === "tasks need attention" || (key.includes("task") && key.includes("attention")))) {
    return true;
  }
  if (suppress.queuePause && (label === "queued work is paused" || (key.includes("queue") && key.includes("paused")))) {
    return true;
  }
  return false;
}

function AutopilotPanel({
  autopilot,
  pending,
  prominent = false,
  onTick,
  onApprove,
  onEmergencyStop,
}: {
  autopilot: StateResponse["autopilot"] | undefined;
  pending: boolean;
  prominent?: boolean;
  onTick?: (() => void) | undefined;
  onApprove?: ((decisionId: string) => void) | undefined;
  onEmergencyStop?: (() => void) | undefined;
}) {
  const mode = autopilot?.mode || "off";
  const incidents = autopilot?.incidents || [];
  const pendingDecisions = autopilot?.pending_decisions || [];
  const decisions = autopilot?.decisions || pendingDecisions;
  const activeDecisions = decisions.filter((decision) => isActiveDecision(decision));
  const runningDecisions = activeDecisions.filter((decision) => decision.status === "running");
  const blockedDecisions = decisions.filter((decision) => !isActiveDecision(decision));
  const events = collapseAutopilotEvents(autopilot?.recent_events || []);
  const health = autopilot?.health || "loading";
  const modeTone = autopilotModeTone(mode);
  const decisionIncidentIds = new Set(decisions.map((decision) => decision.incident_id).filter((id): id is string => Boolean(id)));
  const visibleIncidents = incidents.filter((incident) => !decisionIncidentIds.has(incident.id));
  const primaryDecision = activeDecisions[0] || blockedDecisions[0];
  const primaryIncident = visibleIncidents[0];
  const latestEvent = events[0];
  const hiddenItemCount = Math.max(0, activeDecisions.length + visibleIncidents.length - 1);
  const hasAutomaticRecovery = Boolean(primaryDecision || primaryIncident);
  const actionBudget = autopilot?.budgets.actions_limit_per_hour
    ? `${autopilot.budgets.actions_used_last_hour}/${autopilot.budgets.actions_limit_per_hour} actions this hour`
    : "Budget loading";
  const pilotBudget = autopilot?.budgets.pilot_calls_limit_per_hour
    ? `${autopilot.budgets.pilot_calls_used_last_hour}/${autopilot.budgets.pilot_calls_limit_per_hour} pilot calls`
    : "Pilot budget loading";
  const pilotAgent = formatPilotAgent(autopilot?.pilot_agent);
  if (!hasAutomaticRecovery && !pending) return null;
  const modeLabel = autopilotModeLabel(mode);
  return (
    <div className={`autopilot-panel${prominent ? " panel autopilot-panel-primary" : ""}`} data-testid="autopilot-panel">
      <div className="autopilot-panel-header">
        <div>
          <h3>Autopilot</h3>
          <p>{autopilotStatusText(mode, activeDecisions.length, runningDecisions.length, blockedDecisions.length, visibleIncidents.length, latestEvent, pending)}</p>
        </div>
        <div className="autopilot-controls">
          <span className={`autopilot-mode-pill pill-tone-${modeTone}`}>
            <span className={`watcher-dot tone-${modeTone}`} aria-hidden="true" />
            {modeLabel}
          </span>
          <button type="button" onClick={onTick} disabled={pending || !onTick} data-testid="autopilot-scan-button">
            {pending ? "Scanning..." : "Scan now"}
          </button>
          <button type="button" className="danger quiet" onClick={onEmergencyStop} disabled={pending || mode === "off" || !onEmergencyStop}>
            Turn off
          </button>
        </div>
      </div>
      {hasAutomaticRecovery ? (
        <div className="autopilot-action">
          <h4>{primaryDecision && isActiveDecision(primaryDecision) ? "Recommended action" : "Needs review"}</h4>
          {primaryDecision ? (
            <AutopilotDecisionCard
              decision={primaryDecision}
              pending={pending}
              onApprove={onApprove}
            />
          ) : primaryIncident ? (
            <AutopilotIncidentCard incident={primaryIncident} />
          ) : null}
          {hiddenItemCount > 0 ? <p className="autopilot-more">{hiddenItemCount} more recovery item{hiddenItemCount === 1 ? "" : "s"} waiting.</p> : null}
        </div>
      ) : null}
      <details className="autopilot-details">
        <summary>Policy and limits</summary>
        <dl>
          <div><dt>Mode</dt><dd>{modeLabel}</dd></div>
          <div><dt>Pilot</dt><dd>{pilotAgent}</dd></div>
          <div><dt>Safety limits</dt><dd>{actionBudget} · {pilotBudget}</dd></div>
          <div><dt>Last checked</dt><dd>{autopilot?.last_tick_at ? shortTime(autopilot.last_tick_at) : "Never"}</dd></div>
        </dl>
        <p>State: {health}</p>
        {!hasAutomaticRecovery && latestEvent ? <p>{eventTitle(latestEvent)}</p> : null}
      </details>
    </div>
  );
}

function AutopilotDecisionCard({decision, pending, onApprove}: {
  decision: AutopilotDecision;
  pending: boolean;
  onApprove?: ((decisionId: string) => void) | undefined;
}) {
  const running = decision.status === "running";
  const actionable = decision.status === "pending";
  const label = decisionButtonLabel(decision);
  const planSteps = decision.plan_steps || [];
  return (
    <article className={`autopilot-card severity-${decision.severity}`}>
      <div className="autopilot-card-copy">
        <strong>{decision.title || decision.action_label || "Recovery action"}</strong>
        <p>{decision.rationale || decision.reason || "Autopilot recommends this recovery action."}</p>
        {planSteps.length ? (
          <ol className="autopilot-plan-steps" aria-label="Recovery plan">
            {planSteps.map((step, index) => (
              <li key={`${step.action}-${index}`}>
                <span>{step.label}</span>
                {step.detail ? <small>{step.detail}</small> : null}
              </li>
            ))}
          </ol>
        ) : (
          <small>{decisionExplanation(decision)}</small>
        )}
      </div>
      {running ? (
        <button type="button" disabled data-testid="autopilot-approve-button">
          Diagnosing...
        </button>
      ) : actionable ? (
        <button
          type="button"
          className="primary"
          onClick={() => onApprove?.(decision.id)}
          disabled={pending || !onApprove}
          data-testid="autopilot-approve-button"
        >
          {label}
        </button>
      ) : (
        <span className="autopilot-card-status">{blockedDecisionLabel(decision)}</span>
      )}
    </article>
  );
}

function AutopilotIncidentCard({incident}: {incident: AutopilotIncident}) {
  return (
    <article className={`autopilot-card severity-${incident.severity}`}>
      <strong>{incident.title}</strong>
      <p>{incident.detail}</p>
      <small>{incident.action === "human_required" ? "Needs manual review" : "Scan for a recovery action"}</small>
    </article>
  );
}

function isActiveDecision(decision: AutopilotDecision): boolean {
  return decision.status === "pending" || decision.status === "running";
}

function collapseAutopilotEvents(events: AutopilotEvent[]): AutopilotEvent[] {
  const terminalPilotDecisionIds = new Set(
    events
      .filter((event) => event.kind === "pilot.noop" || event.kind === "pilot.failed")
      .map(eventDecisionId)
      .filter((id): id is string => Boolean(id)),
  );
  return events.filter((event) => {
    if (!terminalPilotDecisionIds.has(eventDecisionId(event) || "")) return true;
    return event.kind !== "pilot.requested" && event.kind !== "pilot.completed";
  });
}

function eventDecisionId(event: AutopilotEvent): string | null {
  if (event.decision_id) return event.decision_id;
  const decision = event.details?.decision;
  if (!decision || typeof decision !== "object") return null;
  const value = (decision as Record<string, unknown>).id;
  return typeof value === "string" ? value : null;
}

function eventTitle(event: AutopilotEvent): string {
  if (event.kind === "pilot.noop") return "Pilot: no action recommended";
  if (event.kind === "pilot.failed") return "Pilot: diagnosis failed";
  return event.message;
}

function pilotPlanText(event: AutopilotEvent, key: "reason" | "required_verification"): string {
  const plan = event.details?.pilot_plan;
  if (!plan || typeof plan !== "object") return "";
  const value = (plan as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

function decisionButtonLabel(decision: AutopilotDecision): string {
  if (decision.action === "pilot_triage") return "Ask Pilot";
  if ((decision.plan_steps || []).length > 1) return "Approve plan";
  if (decision.action === "start_watcher") return "Start queue runner";
  if (decision.action === "stop_watcher") return "Stop queue runner";
  if (decision.action === "requeue") return "Requeue task";
  return decision.action_label || "Approve action";
}

function decisionExplanation(decision: AutopilotDecision): string {
  if (!isActiveDecision(decision)) {
    if (decision.action === "requeue" && String(decision.reason || "").toLowerCase().includes("already tried")) {
      return "Review the latest retry before starting another one.";
    }
    if (decision.action === "human_required") return "Open the affected run and choose the next action.";
    return "Autopilot will not run this automatically.";
  }
  if (decision.action === "pilot_triage" && decision.status === "running") return "Pilot is checking run state and choosing the smallest safe next step.";
  if (decision.action === "pilot_triage") return "Starts a lightweight Pilot diagnosis. Results appear here after refresh.";
  if (decision.action === "start_watcher") return "Starts queue processing for queued tasks.";
  if (decision.action === "requeue") return "Creates a fresh queued run from the original task definition.";
  return decision.action_label || decision.action;
}

function blockedDecisionLabel(decision: AutopilotDecision): string {
  if (decision.status === "failed") return "Action failed";
  if (decision.action === "human_required") return "Review needed";
  if (decision.status === "blocked" && String(decision.reason || "").toLowerCase().includes("already tried")) return "Already tried";
  if (decision.status === "blocked") return "Not automatic";
  return "No action available";
}

function autopilotStatusText(mode: AutopilotMode | string, decisionCount: number, runningCount: number, blockedCount: number, incidentCount: number, latestEvent: AutopilotEvent | undefined, pending: boolean): string {
  if (mode === "off") return "Off. It will not recover stuck work.";
  if (pending) return "Scanning for recoverable problems.";
  if (runningCount > 0) return "Pilot is diagnosing the selected recovery.";
  if (decisionCount > 0) {
    return mode === "assisted"
      ? `${decisionCount} proposed action${decisionCount === 1 ? "" : "s"} waiting for approval.`
      : `${decisionCount} recovery action${decisionCount === 1 ? "" : "s"} waiting.`;
  }
  if (blockedCount > 0) return `${blockedCount} issue${blockedCount === 1 ? " was" : "s were"} already checked.`;
  if (incidentCount > 0) return `${incidentCount} issue${incidentCount === 1 ? " needs" : "s need"} attention.`;
  if (latestEvent?.kind === "pilot.noop") return "Last Pilot check found nothing to recover.";
  return "No recovery action pending.";
}

function autopilotModeLabel(mode: AutopilotMode | string): string {
  if (mode === "assisted") return "Ask first";
  if (mode === "full") return "Auto";
  return "Off";
}

function autopilotModeTone(mode: AutopilotMode | string): "neutral" | "info" | "success" | "warning" {
  if (mode === "full") return "info";
  if (mode === "assisted") return "neutral";
  return "neutral";
}

function formatPilotAgent(agent: StateResponse["autopilot"]["pilot_agent"] | undefined): string {
  if (!agent) return "Pilot agent loading";
  return `Pilot ${agent.agent_type} · ${agent.provider} · ${agent.model} · ${agent.reasoning_effort}`;
}

function shortTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function RecoveryActions({data, suppress = {}}: {data: StateResponse | null; suppress?: {taskAttention?: boolean; queuePause?: boolean}}) {
  const issues = (data?.runtime.issues || []).filter((issue) => !isSuppressedRuntimeIssue(issue, suppress));
  const commands = data?.runtime.command_backlog.items || [];
  const showSubheadings = commands.length > 0 && issues.length > 0;
  return (
    <section className="recovery-actions" aria-labelledby="recoveryActionsHeading">
      <h3 id="recoveryActionsHeading">Manual attention</h3>
      <div className="diagnostics-summary-body">
        {commands.length ? (
          <div>
            {showSubheadings ? <h3 data-testid="diagnostics-pending-commands-heading">Pending Commands</h3> : null}
            {commands.map((command, index) => (
              <details className={`diagnostic-card command-${command.state}`} key={`${command.command_id || command.run_id || "command"}-${index}`}>
                <summary>
                  <span>{command.state}</span>
                  <strong>{command.kind || "queued action"}</strong>
                  <small>{commandBacklogLine(command)}</small>
                </summary>
                <p>{command.run_id || command.task_id || command.command_id || "target unknown"}</p>
                <em>{commandBacklogLine(command)}</em>
              </details>
            ))}
          </div>
        ) : null}
        {issues.length ? (
          <div>
            {showSubheadings ? <h3 data-testid="diagnostics-system-issues-heading">System Issues</h3> : null}
            {issues.slice(0, 4).map((issue, index) => (
              <details className={`diagnostic-card severity-${issue.severity}`} key={`${issue.label}-${index}`} open={issue.severity === "error"}>
                <summary>
                  <span>{issue.severity}</span>
                  <strong>{issue.label}</strong>
                  <small>{issue.next_action}</small>
                </summary>
                <p>{issue.detail}</p>
                <em>{issue.next_action}</em>
              </details>
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function DiagnosticsSummary({data, onSelect}: {data: StateResponse | null; onSelect: (runId: string) => void}) {
  void onSelect;
  return <RecoveryActions data={data} />;
}
