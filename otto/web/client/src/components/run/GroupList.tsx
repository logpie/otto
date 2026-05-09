// GroupList — live dispatch surface of the RunDrawer. Groups are the unit
// Otto actually builds and merges; features inherit group state until audit
// produces per-feature verdicts.
//
// A7: while a run is in flight, each Group row carries an Abort button.
// Aborting writes a `group.aborted_by_user` event to the session journal;
// the build loop honors it on the next retry boundary and the merge queue
// skips the group as BLOCKED. Already-terminal groups (landed/blocked/
// redundant/failed_scope) hide the Abort button — there is nothing to abort.
//
// Post-RUA round 1 (B1, B2, B7): rows lay out via flex/gap (no baked
// whitespace), status renders through the scoped <Pill>, and per-Group
// wall + cost + [diff]/[logs] actions are surfaced.

import type { DispatchView, GroupStatus, GroupView } from "../../types/run";
import { Pill, type PillTone } from "./Pill";

interface Props {
  groups: GroupView[];
  dispatch?: DispatchView | undefined;
  onAbort?: (groupId: string) => void;
  onOpenDiff?: (groupId: string) => void;
  onOpenLogs?: (groupId: string) => void;
  pendingAbortId?: string | null;
}

const TERMINAL_GROUP_STATUSES = new Set([
  "landed",
  "degraded",
  "redundant",
  "blocked",
  "failed_scope",
]);

function statusTone(status: GroupStatus): PillTone {
  switch (status) {
    case "landed":
    case "passing":
      return "ok";
    case "degraded":
    case "redundant":
      return "warn";
    case "in_progress":
      return "info";
    case "blocked":
    case "failed_scope":
      return "error";
    default:
      return "muted";
  }
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function formatCost(usd: number): string {
  if (!Number.isFinite(usd) || usd < 0) return "—";
  return `$${usd.toFixed(2)}`;
}

export function GroupList({
  groups,
  dispatch,
  onAbort,
  onOpenDiff,
  onOpenLogs,
  pendingAbortId,
}: Props) {
  if (groups.length === 0) {
    return <p className="empty">No groups dispatched.</p>;
  }
  const ready = new Set(dispatch?.ready_group_ids ?? []);
  const waiting = new Set(dispatch?.waiting_group_ids ?? []);
  return (
    // R3-B29: <summary> is now styled as a section subheader so the
    // "▶ N groups" disclosure has the same visual weight as the
    // surrounding "Features" / "Stages" headings. The native <details>
    // marker is replaced by a custom chevron rendered via CSS.
    <details className="group-list" data-testid="group-list" open>
      <summary className="group-list-summary">
        <span className="group-list-summary-caret" aria-hidden>
          ▸
        </span>
        <span className="group-list-summary-label">
          Build groups
        </span>
        <span className="group-list-summary-count">
          {groups.length} group{groups.length === 1 ? "" : "s"}
        </span>
      </summary>
      {dispatch && (
        <div
          className="group-dispatch-status"
          data-testid="group-dispatch-status"
          title={dispatch.summary}
        >
          <span>
            Running {dispatch.running_group_ids.length}
            {dispatch.max_concurrent ? `/${dispatch.max_concurrent}` : ""}
          </span>
          <span>Ready now {dispatch.ready_group_ids.length}</span>
          <span>Waiting {dispatch.waiting_group_ids.length}</span>
          <span>Blocked {dispatch.blocked_group_ids.length}</span>
          {dispatch.parallelizable_group_ids.length > 1 && (
            <span>{dispatch.parallelizable_group_ids.length} can run now</span>
          )}
        </div>
      )}
      <ul>
        {groups.map((g) => {
          const canAbort =
            onAbort !== undefined && !TERMINAL_GROUP_STATUSES.has(g.status);
          const isPendingAbort = pendingAbortId === g.id;
          return (
            <li
              key={g.id}
              className={`group-row ${g.status}`}
              data-testid={`group-${g.id}`}
            >
              <span className="group-name">{g.name}</span>
              <Pill tone={statusTone(g.status)} className="group-status-pill">
                {g.status}
              </Pill>
              <span className="feature-count">
                {g.feature_ids.length} feature{g.feature_ids.length === 1 ? "" : "s"}
              </span>
              {g.dependencies.length > 0 && (
                <span className="group-deps" title={`Runs after ${g.dependencies.join(", ")}`}>
                  after {g.dependencies.join(", ")}
                </span>
              )}
              {ready.has(g.id) && (
                <Pill tone="info" className="group-ready-pill">
                  ready
                </Pill>
              )}
              {waiting.has(g.id) && (
                <span className="group-waiting" title="Waiting for dependency groups">
                  waiting
                </span>
              )}
              <span className="group-wall" title="Group wall time">
                wall {formatDuration(g.wall_s)}
              </span>
              <span className="group-cost" title="Group cost">
                cost {formatCost(g.cost_usd)}
              </span>
              {g.repair_attempts > 0 && (
                <Pill
                  tone="warn"
                  title="Group required repair"
                  className="repair-badge"
                >
                  {g.repair_attempts} repair{g.repair_attempts === 1 ? "" : "s"}
                </Pill>
              )}
              <span className="group-actions">
                <button
                  type="button"
                  className="group-action-button group-action-diff"
                  data-testid={`group-diff-${g.id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    onOpenDiff?.(g.id);
                  }}
                  title="Open current diff for this group"
                >
                  diff
                </button>
                <button
                  type="button"
                  className="group-action-button group-action-logs"
                  data-testid={`group-logs-${g.id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    onOpenLogs?.(g.id);
                  }}
                  title="Open logs for this group"
                >
                  logs
                </button>
                {canAbort && (
                  <button
                    type="button"
                    className="group-abort-button"
                    data-testid={`group-abort-${g.id}`}
                    onClick={() => onAbort?.(g.id)}
                    disabled={isPendingAbort}
                    title="Abort this group; merge queue will skip it."
                  >
                    Abort
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export default GroupList;
