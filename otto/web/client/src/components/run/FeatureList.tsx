// FeatureList — primary surface of the RunDrawer (research §3 atomic units:
// Feature = unit of value; this is what the user reads first).
//
// Each row: verdict glyph + name + 1-line detail (truncated). Click expands
// for evidence drilldown (deferred to a separate FeatureDrilldown component).

import type { FeatureView, FeatureVerdict } from "../../types/run";

interface Props {
  features: FeatureView[];
  onSelect?: (featureId: string) => void;
}

function verdictGlyph(verdict: FeatureVerdict | null): string {
  if (verdict === null) return "○";
  if (verdict === "passed") return "✓";
  if (verdict === "partial") return "⚠";
  if (verdict === "missing") return "?";
  return "✗"; // blocked / failed
}

function verdictTone(verdict: FeatureVerdict | null): "ok" | "warn" | "fail" | "pending" | "info" {
  if (verdict === null) return "pending";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  if (verdict === "missing") return "info";
  return "fail";
}

export function FeatureList({ features, onSelect }: Props) {
  if (features.length === 0) {
    return <p className="empty">No features in this run.</p>;
  }
  return (
    <ul className="feature-list" data-testid="feature-list">
      {features.map((f) => (
        <li
          key={f.id}
          className={`feature-row ${verdictTone(f.verdict)}`}
          data-testid={`feature-${f.id}`}
          onClick={() => onSelect?.(f.id)}
        >
          <span className="verdict-glyph" aria-hidden>
            {verdictGlyph(f.verdict)}
          </span>
          <span className="feature-name">{f.name}</span>
          {f.evidence_completeness !== "full" && (
            <span className="completeness-badge" title="Evidence not full">
              {f.evidence_completeness}
            </span>
          )}
          {f.multi_actor_required && (
            <span className="multi-actor-badge" title="Requires multiple actors">
              multi-actor
            </span>
          )}
          {f.description && (
            <span className="feature-detail" title={f.description}>
              {f.description}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

export default FeatureList;
