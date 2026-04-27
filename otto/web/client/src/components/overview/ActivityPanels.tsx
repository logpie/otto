import type {HistoryItem, LandingState, LiveRunItem, StateResponse} from "../../types";
import {formatEventTime, usageLine} from "../../utils/format";
import {runEventText} from "../../utils/missionControl";

export function RecentRunsPanel({items, totalRows, selectedRunId, onSelect}: {
  items: HistoryItem[];
  totalRows: number;
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  return (
    <section className="panel recent-runs-panel" aria-labelledby="recentRunsHeading">
      <div className="panel-heading">
        <div>
          <h2 id="recentRunsHeading">Recent Runs</h2>
          <p className="panel-subtitle">Outcome, duration, and token spend.</p>
        </div>
        <span className="pill">{totalRows}</span>
      </div>
      <div className="recent-run-list" role="list">
        {items.length ? items.map((item) => (
          <button
            className={`recent-run-row ${item.run_id === selectedRunId ? "selected" : ""}`}
            type="button"
            key={item.run_id}
            role="listitem"
            onClick={() => onSelect(item.run_id)}
          >
            <span className={`recent-run-outcome status-${(item.terminal_outcome || item.status || "").toLowerCase()}`}>
              {item.outcome_display || item.status}
            </span>
            <span className="recent-run-main">
              <strong title={item.queue_task_id || item.run_id}>{item.queue_task_id || item.run_id}</strong>
              <em title={item.summary || ""}>{item.summary || "-"}</em>
            </span>
            <span className="recent-run-usage">
              <strong>{item.duration_display || "-"}</strong>
              <em>{usageLine(item)}</em>
            </span>
          </button>
        )) : <div className="timeline-empty">No matching runs yet.</div>}
      </div>
    </section>
  );
}

export function RecentActivity({events, history, selectedRunId, onSelect}: {
  events: StateResponse["events"] | undefined;
  history: HistoryItem[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  // Collapse adjacent same-message lifecycle events (e.g. four
  // "watcher started"/"watcher stop requested" rows in 6 minutes) into a
  // single grouped row "watcher restarted 4×". mc-audit redesign §3b W4.4.
  const rawEvents = events?.items.slice(0, 24) || [];
  const collapsed: Array<{key: string; severity: string; message: string; created_at: string; count: number}> = [];
  for (const event of rawEvents) {
    const last = collapsed[collapsed.length - 1];
    const baseMsg = (event.message || "").replace(/\s+(started|stop requested|stopped)\s*$/i, "").trim();
    const isWatcher = /^watcher\b/i.test(event.message || "");
    if (isWatcher && last && last.message.startsWith("watcher") && last.severity === event.severity) {
      // Same-actor adjacency — collapse and keep the most recent timestamp.
      last.count += 1;
      last.created_at = event.created_at;
      if (last.count === 2) last.message = `${baseMsg} cycled`;
      continue;
    }
    collapsed.push({
      key: event.event_id || `${event.created_at}-${event.message}`,
      severity: event.severity,
      message: event.message,
      created_at: event.created_at,
      count: 1,
    });
    if (collapsed.length >= 4) break;
  }
  const recentEvents = collapsed.slice(0, 4);
  const recentHistory = history.slice(0, 4);
  return (
    <section className="panel activity-panel" aria-labelledby="activityHeading">
      <div className="panel-heading">
        <div>
          <h2 id="activityHeading">Recent Activity</h2>
          <p className="panel-subtitle" data-testid="activity-subtitle">Jobs, merges, and errors.</p>
        </div>
        <span className="pill">{(events?.total_count || 0) + history.length}</span>
      </div>
      <div className="activity-list">
        {recentEvents.map((event) => (
          <div className={`activity-item event-${event.severity}`} key={event.key}>
            <span>{event.severity}</span>
            <strong title={event.message}>
              {event.message}
              {event.count > 1 ? <em className="activity-count" aria-label={`${event.count} occurrences`}> ×{event.count}</em> : null}
            </strong>
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
            <strong title={item.summary || ""}>{item.queue_task_id || item.run_id}</strong>
            <time title="Run duration">{item.duration_display ? `${item.duration_display}` : "-"}</time>
          </button>
        ))}
        {!recentEvents.length && !recentHistory.length && <div className="timeline-empty">No activity yet.</div>}
      </div>
    </section>
  );
}

export function LiveRuns({items, landing, selectedRunId, onSelect}: {
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
                <td>{usageLine(item)}</td>
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
