// StageTimeline — horizontal stage progression (research §6 pipeline stages).
// Compile → [Spec review] → Build → Audit → Render → Land.

import type { StageView, StageStatus } from "../../types/run";

interface Props {
  stages: StageView[];
}

function statusClass(status: StageStatus): string {
  switch (status) {
    case "done":
      return "done";
    case "active":
      return "active";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    default:
      return "pending";
  }
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

export function StageTimeline({ stages }: Props) {
  if (stages.length === 0) {
    return <p className="empty">No stage data.</p>;
  }
  return (
    <ol className="stage-timeline" data-testid="stage-timeline">
      {stages.map((s) => (
        <li
          key={s.name}
          className={`stage ${statusClass(s.status)}`}
          data-testid={`stage-${s.name}`}
        >
          <span className="stage-name">{s.name}</span>
          <span className="stage-status">{s.status}</span>
          <span className="stage-duration">{formatDuration(s.duration_s)}</span>
          {s.cost_usd !== null && (
            <span className="stage-cost">${s.cost_usd.toFixed(2)}</span>
          )}
        </li>
      ))}
    </ol>
  );
}

export default StageTimeline;
