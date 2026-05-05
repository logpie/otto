// VerdictHeader — top of the RunDrawer (research §7 + wireframes screen 3).
// Shows: outcome pill, intent line, verdict counts, quality score, wall+cost.

import type { RunView, RunVerdict } from "../../types/run";

interface Props {
  view: RunView;
}

function verdictTone(verdict: RunVerdict | null): "ok" | "warn" | "fail" | "pending" {
  if (verdict === null) return "pending";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  return "fail";
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

  return (
    <header className="run-drawer-header" data-testid="verdict-header">
      <div className="outcome-line">
        <span className={`outcome-pill ${tone}`} data-testid="outcome-pill">
          {view.verdict ?? view.status}
        </span>
        <span className="intent-text" title={view.intent}>
          {view.intent}
        </span>
      </div>
      <div className="metrics" data-testid="metrics">
        <span className="features">
          {passedFeatures}/{totalFeatures} features
        </span>
        {view.findings.length > 0 && (
          <span className="quality">
            quality:{" "}
            {view.findings.filter((f) => f.severity === "critical").length} critical
          </span>
        )}
        <span className="wall">wall {formatDuration(view.wall_s)}</span>
        <span className="cost">cost {formatCost(view.cost_usd)}</span>
      </div>
    </header>
  );
}

export default VerdictHeader;
