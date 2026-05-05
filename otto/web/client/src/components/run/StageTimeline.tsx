// StageTimeline — horizontal stage progression (research §6 pipeline stages).
// Compile → [Spec review] → Build → Audit → Render → Land.
//
// Post-RUA round 1 (B6): replaced the old numbered <ol> with a horizontal
// stepper. Each stage is a small block (status dot + name + duration +
// optional cost) separated by → arrows. The container scrolls horizontally
// on narrow viewports rather than wrapping.

import type { StageStatus, StageView } from "../../types/run";

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

function statusGlyph(status: StageStatus): string {
  switch (status) {
    case "done":
      return "●";
    case "active":
      return "◐";
    case "failed":
      return "✗";
    case "skipped":
      return "–";
    default:
      return "○";
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
      {stages.map((s, i) => (
        <li
          key={s.name}
          className={`stage stage-${statusClass(s.status)}`}
          data-testid={`stage-${s.name}`}
        >
          <span className="stage-block">
            <span className="stage-dot" aria-hidden>
              {statusGlyph(s.status)}
            </span>
            <span className="stage-name">{s.name}</span>
            <span className="stage-status">{s.status}</span>
            <span className="stage-duration">{formatDuration(s.duration_s)}</span>
            {s.cost_usd !== null && (
              <span className="stage-cost">${s.cost_usd.toFixed(2)}</span>
            )}
          </span>
          {i < stages.length - 1 && (
            <span className="stage-arrow" aria-hidden>
              →
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

export default StageTimeline;
