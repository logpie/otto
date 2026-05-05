// FeatureList — primary surface of the RunDrawer (research §3 atomic units:
// Feature = unit of value; this is what the user reads first).
//
// Each row: verdict glyph + name + 1-line detail (truncated). Click expands
// for evidence drilldown (deferred to a separate FeatureDrilldown component).
//
// Post-RUA round 1 (B1): rows lay out via flex/gap rather than baked-in
// whitespace; status renders through the scoped <Pill> component (B2).

import type { FeatureView, FeatureVerdict } from "../../types/run";
import { Pill, type PillTone } from "./Pill";

interface Props {
  features: FeatureView[];
  onSelect?: (featureId: string) => void;
}

// R2-B33: missing uses ⚠ (warning), not ?. Both `partial` and `missing`
// are warning states — keep ✓ for passed, ✗ for blocked. R2-B12: glyph
// color is tone-matched via .feature-row.<tone> selectors (green for
// passed, amber for partial+missing, red for blocked).
function verdictGlyph(verdict: FeatureVerdict | null): string {
  if (verdict === null) return "○";
  if (verdict === "passed") return "✓";
  if (verdict === "partial") return "⚠";
  if (verdict === "missing") return "⚠";
  return "✗"; // blocked / failed
}

function verdictTone(verdict: FeatureVerdict | null): "ok" | "warn" | "fail" | "pending" {
  if (verdict === null) return "pending";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  if (verdict === "missing") return "warn";
  return "fail";
}

function verdictPillTone(verdict: FeatureVerdict | null): PillTone {
  if (verdict === null) return "muted";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  if (verdict === "missing") return "warn";
  return "error";
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
          <Pill tone={verdictPillTone(f.verdict)} className="feature-verdict-pill">
            {f.verdict ?? "pending"}
          </Pill>
          {/* R2-B34: audit-context pills render as outlined-only so they
              don't read as verdict pills. The shared `run-pill-outlined`
              modifier strips the background fill and adds a neutral border. */}
          {f.evidence_completeness !== "full" &&
            f.evidence_completeness !== (f.verdict ?? "pending") && (
              <Pill
                tone="muted"
                title="Evidence not full"
                className="completeness-badge run-pill-outlined"
              >
                {f.evidence_completeness}
              </Pill>
            )}
          {f.multi_actor_required && (
            <Pill
              tone="muted"
              title="Requires multiple actors"
              className="multi-actor-badge run-pill-outlined"
            >
              multi-actor
            </Pill>
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
