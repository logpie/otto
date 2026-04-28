import {HealthCard} from "../MicroComponents";
import type {CSSProperties} from "react";
import type {AutopilotDecision, AutopilotMode, StateResponse} from "../../types";
import {formatDuration} from "../../utils/format";
import {commandBacklogLine} from "../../utils/missionControl";

export function SystemHealth({
  data,
  autopilotPending = false,
  onAutopilotMode,
  onAutopilotTick,
  onAutopilotApprove,
  onAutopilotEmergencyStop,
}: {
  data: StateResponse | null;
  autopilotPending?: boolean;
  onAutopilotMode?: (mode: AutopilotMode) => void;
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
  return (
    <section className="panel system-health" aria-labelledby="systemHealthHeading">
      <div className="panel-heading">
        <div>
          <h2 id="systemHealthHeading">System Health</h2>
          <p className="panel-subtitle">Recovery view for queue, watcher, and repo.</p>
        </div>
        <span className={`pill health-${runtime?.status || "loading"}`}>{runtime?.status || "loading"}</span>
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
          detail={`${backlog?.processing || 0} processing · ${backlog?.malformed || 0} malformed`}
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
      <AutopilotPanel
        autopilot={autopilot}
        pending={autopilotPending}
        onMode={onAutopilotMode}
        onTick={onAutopilotTick}
        onApprove={onAutopilotApprove}
        onEmergencyStop={onAutopilotEmergencyStop}
      />
      <div className="system-issues" role="list" aria-label="Runtime issues">
        {runtime?.issues.length ? runtime.issues.map((issue, index) => (
          <div className={`system-issue severity-${issue.severity}`} role="listitem" key={`${issue.label}-${index}`}>
            <span>{issue.severity}</span>
            <strong>{issue.label}</strong>
            <p>{issue.detail}</p>
            <em>{issue.next_action}</em>
          </div>
        )) : <div className="diagnostic-empty">No runtime issues.</div>}
      </div>
    </section>
  );
}

function AutopilotPanel({
  autopilot,
  pending,
  onMode,
  onTick,
  onApprove,
  onEmergencyStop,
}: {
  autopilot: StateResponse["autopilot"] | undefined;
  pending: boolean;
  onMode?: ((mode: AutopilotMode) => void) | undefined;
  onTick?: (() => void) | undefined;
  onApprove?: ((decisionId: string) => void) | undefined;
  onEmergencyStop?: (() => void) | undefined;
}) {
  const mode = autopilot?.mode || "off";
  const incidents = autopilot?.incidents || [];
  const decisions = autopilot?.pending_decisions || [];
  const events = autopilot?.recent_events || [];
  const health = autopilot?.health || "loading";
  const tone = mode === "off" ? "neutral" : decisions.length || incidents.some((incident) => incident.severity === "error") ? "warning" : "success";
  const actionBudget = autopilot?.budgets.actions_limit_per_hour
    ? `${autopilot.budgets.actions_used_last_hour}/${autopilot.budgets.actions_limit_per_hour} actions this hour`
    : "Budget loading";
  const pilotBudget = autopilot?.budgets.pilot_calls_limit_per_hour
    ? `${autopilot.budgets.pilot_calls_used_last_hour}/${autopilot.budgets.pilot_calls_limit_per_hour} pilot calls`
    : "Pilot budget loading";
  return (
    <div className="autopilot-panel" data-testid="autopilot-panel">
      <div className="autopilot-panel-header">
        <div>
          <h3>Autopilot</h3>
          <p>{mode === "off" ? "Paused. It will not recover problems." : "Watching queue, runner, repo, and landing state."}</p>
        </div>
        <div className="autopilot-controls">
          <span className={`autopilot-mode-pill pill-tone-${tone} ${mode !== "off" ? "is-live" : ""}`}>
            <span className={`watcher-dot tone-${tone}`} aria-hidden="true" />
            {mode}
          </span>
          <select
            data-testid="autopilot-health-mode-select"
            value={mode}
            disabled={pending || !onMode}
            onChange={(event) => onMode?.(event.target.value as AutopilotMode)}
            aria-label="Autopilot mode"
          >
            <option value="off">Off</option>
            <option value="assisted">Assisted</option>
            <option value="full">Full auto</option>
          </select>
          <button type="button" onClick={onTick} disabled={pending || !onTick} data-testid="autopilot-scan-button">
            {pending ? "Scanning..." : "Scan now"}
          </button>
          <button type="button" className="danger quiet" onClick={onEmergencyStop} disabled={pending || mode === "off" || !onEmergencyStop}>
            Stop
          </button>
        </div>
      </div>
      <div className="autopilot-loop" aria-label="Autopilot loop">
        {["Observe", "Diagnose", mode === "full" ? "Recover" : "Ask", "Verify"].map((step, index) => (
          <span key={step} className={`autopilot-step ${mode !== "off" ? "is-live" : ""}`} style={{"--step-index": index} as CSSProperties}>
            {step}
          </span>
        ))}
      </div>
      <div className="autopilot-meta">
        <span>{health}</span>
        <span>{actionBudget}</span>
        <span>{pilotBudget}</span>
        <span>{autopilot?.last_tick_at ? `Last scan ${shortTime(autopilot.last_tick_at)}` : "No scan yet"}</span>
      </div>
      <div className="autopilot-body">
        <div className="autopilot-column">
          <h4>Current diagnosis</h4>
          {incidents.length ? incidents.slice(0, 4).map((incident) => (
            <article key={incident.id} className={`autopilot-card severity-${incident.severity}`}>
              <strong>{incident.title}</strong>
              <p>{incident.detail}</p>
              <small>{incident.action}</small>
            </article>
          )) : <div className="diagnostic-empty">No recovery issues detected.</div>}
        </div>
        <div className="autopilot-column">
          <h4>Needs approval</h4>
          {decisions.length ? decisions.slice(0, 4).map((decision) => (
            <AutopilotDecisionCard
              key={decision.id}
              decision={decision}
              pending={pending}
              onApprove={onApprove}
            />
          )) : <div className="diagnostic-empty">No approvals waiting.</div>}
        </div>
        <div className="autopilot-column">
          <h4>Timeline</h4>
          {events.length ? events.slice(0, 6).map((event) => (
            <article key={event.id} className={`autopilot-event severity-${event.severity}`}>
              <span>{shortTime(event.created_at)}</span>
              <strong>{event.message}</strong>
            </article>
          )) : <div className="diagnostic-empty">No Autopilot events yet.</div>}
        </div>
      </div>
    </div>
  );
}

function AutopilotDecisionCard({decision, pending, onApprove}: {
  decision: AutopilotDecision;
  pending: boolean;
  onApprove?: ((decisionId: string) => void) | undefined;
}) {
  return (
    <article className={`autopilot-card severity-${decision.severity}`}>
      <strong>{decision.title || decision.action_label || "Recovery action"}</strong>
      <p>{decision.rationale || decision.reason || "Autopilot recommends this recovery action."}</p>
      <small>{decision.action_label || decision.action}</small>
      <button
        type="button"
        onClick={() => onApprove?.(decision.id)}
        disabled={pending || !onApprove}
        data-testid="autopilot-approve-button"
      >
        Approve recovery
      </button>
    </article>
  );
}

function shortTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

export function DiagnosticsSummary({data, onSelect}: {data: StateResponse | null; onSelect: (runId: string) => void}) {
  const issues = data?.runtime.issues || [];
  const commands = data?.runtime.command_backlog.items || [];
  // Landing items dropped from Health — same list lives in the Tasks view
  // Task Board, and showing it here forces the user to triangulate.
  // mc-audit redesign §3b W4.7. `onSelect` is no longer needed but kept in
  // signature for callers.
  void onSelect;
  const diagnosticCount = issues.length + commands.length;
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
        {/* "Review and landing" list dropped — duplicates Tasks view's
            Task Board. mc-audit redesign §3b W4.7. */}
      </div>
    </section>
  );
}
