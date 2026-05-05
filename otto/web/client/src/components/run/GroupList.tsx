// GroupList — secondary surface of the RunDrawer (research §3: Group is
// dispatch unit, surfaced one click below the FeatureList primary view).

import type { GroupView } from "../../types/run";

interface Props {
  groups: GroupView[];
}

export function GroupList({ groups }: Props) {
  if (groups.length === 0) {
    return <p className="empty">No groups dispatched.</p>;
  }
  return (
    <details className="group-list" data-testid="group-list">
      <summary>
        {groups.length} group{groups.length === 1 ? "" : "s"}
      </summary>
      <ul>
        {groups.map((g) => (
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
          </li>
        ))}
      </ul>
    </details>
  );
}

export default GroupList;
