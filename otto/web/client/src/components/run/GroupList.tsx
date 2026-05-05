// GroupList — secondary surface of the RunDrawer (research §3: Group is
// dispatch unit, surfaced one click below the FeatureList primary view).
//
// A7: while a run is in flight, each Group row carries an Abort button.
// Aborting writes a `group.aborted_by_user` event to the session journal;
// the build loop honors it on the next retry boundary and the merge queue
// skips the group as BLOCKED. Already-terminal groups (landed/blocked/
// failed_scope) hide the Abort button — there is nothing to abort.

import type { GroupView } from "../../types/run";

interface Props {
  groups: GroupView[];
  onAbort?: (groupId: string) => void;
  pendingAbortId?: string | null;
}

const TERMINAL_GROUP_STATUSES = new Set([
  "landed",
  "blocked",
  "failed_scope",
]);

export function GroupList({ groups, onAbort, pendingAbortId }: Props) {
  if (groups.length === 0) {
    return <p className="empty">No groups dispatched.</p>;
  }
  return (
    <details className="group-list" data-testid="group-list">
      <summary>
        {groups.length} group{groups.length === 1 ? "" : "s"}
      </summary>
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
              <span className="group-status">{g.status}</span>
              <span className="feature-count">
                {g.feature_ids.length} feature{g.feature_ids.length === 1 ? "" : "s"}
              </span>
              {g.repair_attempts > 0 && (
                <span className="repair-badge" title="Group required repair">
                  {g.repair_attempts} repair{g.repair_attempts === 1 ? "" : "s"}
                </span>
              )}
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
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export default GroupList;
