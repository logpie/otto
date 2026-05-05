// VerdictHeader — top of the RunDrawer (research §7 + wireframes screen 3).
// Shows: outcome pill, intent line, verdict counts, quality score, wall+cost.
//
// Post-RUA round 1 (B1, B2, B5): the row is laid out via flex/gap so the
// metrics are visually separated; baked-in leading/trailing whitespace
// has been stripped from the labels (CSS gap handles separation, not
// the strings). The outcome chip routes through the scoped <Pill>.

import type { RunVerdict, RunView } from "../../types/run";
import { Pill, type PillTone } from "./Pill";

interface Props {
  view: RunView;
}

function verdictTone(verdict: RunVerdict | null): "ok" | "warn" | "fail" | "pending" {
  if (verdict === null) return "pending";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  return "fail";
}

function verdictPillTone(verdict: RunVerdict | null): PillTone {
  if (verdict === null) return "info";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  return "error";
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function formatCost(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

export function VerdictHeader({ view }: Props) {
  const tone = verdictTone(view.verdict);
  const passedFeatures = view.features.filter((f) => f.verdict === "passed").length;
  const totalFeatures = view.features.length;
  const criticalCount = view.findings.filter((f) => f.severity === "critical").length;
  const label = view.verdict ?? view.status;

  return (
    <header className={`run-drawer-header ${tone}`} data-testid="verdict-header">
      <div className="outcome-line">
        <Pill
          tone={verdictPillTone(view.verdict)}
          className="outcome-pill"
          testId="outcome-pill"
        >
          {label}
        </Pill>
        <span className="intent-text" title={view.intent}>
          {view.intent}
        </span>
      </div>
      <dl className="metrics" data-testid="metrics">
        <div className="metric">
          <dt>Features</dt>
          <dd className="features">
            <span className="metric-num">{passedFeatures}</span>
            <span className="metric-sep" aria-hidden>/</span>
            <span className="metric-num">{totalFeatures}</span>
          </dd>
        </div>
        {view.findings.length > 0 && (
          <div className="metric">
            <dt>Quality</dt>
            <dd className="quality">
              <span className="metric-num">{criticalCount}</span>
              <span className="metric-unit">critical</span>
            </dd>
          </div>
        )}
        <div className="metric">
          <dt>Wall</dt>
          <dd className="wall">{formatDuration(view.wall_s)}</dd>
        </div>
        <div className="metric">
          <dt>Cost</dt>
          <dd className="cost">{formatCost(view.cost_usd)}</dd>
        </div>
      </dl>
    </header>
  );
}

export default VerdictHeader;
