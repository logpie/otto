// StageTimeline — horizontal stage progression (research §6 pipeline stages).
// Compile → [Spec review] → Prepare fixtures → Build → Audit → Render → Land.
//
// Post-RUA round 1 (B6): replaced the old numbered <ol> with a horizontal
// stepper. Each stage is a small block (status dot + name + plain-language
// description + duration + optional cost) separated by arrows.

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

function formatDuration(seconds: number | null, startedAt: string | null, status: StageStatus): string {
  if (seconds === null && status === "active" && startedAt) {
    const started = Date.parse(startedAt);
    if (Number.isFinite(started)) {
      seconds = Math.max(0, (Date.now() - started) / 1000);
    }
  }
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

// R3-B19: humanize raw stage tokens (e.g. "spec_review" → "Spec
// review"). The CSS `text-transform: capitalize` rule only fires on
// word boundaries, which leaves the underscore intact and renders
// as "Spec_review". Strip the underscore in JS so the visible label
// reads as ordinary prose.
function humanizeStage(raw: string): string {
  if (!raw) return raw;
  const spaced = raw.replace(/_/g, " ").trim();
  if (spaced.length === 0) return raw;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase();
}

const STAGE_COPY: Record<string, {label: string; description: string}> = {
  compile: {
    label: "Compile spec",
    description: "Turn intent into grouped work",
  },
  spec_review: {
    label: "Spec review",
    description: "Optional human approval gate",
  },
  seed: {
    label: "Prepare fixtures",
    description: "Set up audit/test data",
  },
  build: {
    label: "Build groups",
    description: "Run the feature agents",
  },
  audit: {
    label: "Audit product",
    description: "Verify behavior and quality",
  },
  render: {
    label: "Render proof",
    description: "Write review evidence",
  },
  land: {
    label: "Land on main",
    description: "Merge completed work",
  },
};

function stageCopy(raw: string, status: StageStatus): {label: string; description: string} {
  const copy = STAGE_COPY[raw] ?? {label: humanizeStage(raw), description: ""};
  if (raw === "spec_review" && status === "skipped") {
    return {...copy, description: "Manual review was not used"};
  }
  return copy;
}

export function StageTimeline({ stages }: Props) {
  if (stages.length === 0) {
    return <p className="empty">No stage data.</p>;
  }
  return (
    <ol className="stage-timeline" data-testid="stage-timeline">
      {stages.map((s, i) => {
        const copy = stageCopy(s.name, s.status);
        return (
          <li
            key={s.name}
            className={`stage stage-${statusClass(s.status)}`}
            data-testid={`stage-${s.name}`}
          >
            <span className="stage-block" title={copy.description}>
              <span className="stage-dot" aria-hidden>
                {statusGlyph(s.status)}
              </span>
              <span className="stage-copy">
                <span className="stage-name">{copy.label}</span>
                <span className="stage-description">{copy.description}</span>
              </span>
              <span className="stage-status">{s.status}</span>
              <span className="stage-duration">{formatDuration(s.duration_s, s.started_at, s.status)}</span>
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
        );
      })}
    </ol>
  );
}

export default StageTimeline;
