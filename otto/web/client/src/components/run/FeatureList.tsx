// FeatureList — primary surface of the RunDrawer (research §3 atomic units:
// Feature = unit of value; this is what the user reads first).
//
// Each row: verdict glyph + name + 1-line detail (truncated). Click expands
// for evidence drilldown (deferred to a separate FeatureDrilldown component).
//
// Post-RUA round 1 (B1): rows lay out via flex/gap rather than baked-in
// whitespace; status renders through the scoped <Pill> component (B2).
//
// Post-RUA round 3:
//   - R3-B23: trailing chevron (▸) makes drilldown affordance discoverable.
//   - R3-B24: verdict-grouping subheads ("Passing (3)" / "Blocked (1)" /
//     etc.) when more than one verdict class is present.
//   - R3-B32: inline first-line preview of the first critical finding for
//     blocked features (truncated ~80 chars).
//   - R3-B33: red `[N critical]` badge next to the verdict pill when a
//     feature has ≥1 critical finding.

import type { FeatureView, FeatureVerdict, FindingView, GroupStatus } from "../../types/run";
import { Pill, type PillTone } from "./Pill";

interface Props {
  features: FeatureView[];
  // R3-B32 + R3-B33: critical-finding inline preview and count badge are
  // derived from the run-level findings list, so consumers must pass it.
  // Optional with a safe `[]` default to keep older call sites working.
  findings?: FindingView[];
  onSelect?: (featureId: string) => void;
}

// R2-B33: missing uses ⚠ (warning), not ?. Both `partial` and `missing`
// are warning states — keep ✓ for passed, ✗ for blocked. R2-B12: glyph
// color is tone-matched via .feature-row.<tone> selectors (green for
// passed, amber for partial+missing, red for blocked).
interface FeatureVisualState {
  glyph: string;
  tone: "ok" | "warn" | "fail" | "pending" | "info";
  pillTone: PillTone;
  label: string;
  bucket: VerdictBucket;
}

function buildStatusLabel(status: GroupStatus): string {
  if (status === "passing" || status === "landed") return "built";
  if (status === "degraded") return "degraded";
  if (status === "redundant") return "satisfied";
  if (status === "in_progress") return "building";
  if (status === "blocked" || status === "failed_scope") return "blocked";
  return "waiting";
}

function visualState(verdict: FeatureVerdict | null, buildStatus: GroupStatus): FeatureVisualState {
  if (verdict === "passed") {
    return { glyph: "✓", tone: "ok", pillTone: "ok", label: "passed", bucket: "passing" };
  }
  if (verdict === "partial" || verdict === "missing") {
    return { glyph: "⚠", tone: "warn", pillTone: "warn", label: verdict, bucket: "partial" };
  }
  if (verdict === "blocked" || verdict === "failed") {
    return { glyph: "✗", tone: "fail", pillTone: "error", label: verdict, bucket: "blocked" };
  }
  if (buildStatus === "passing" || buildStatus === "landed") {
    return { glyph: "✓", tone: "ok", pillTone: "ok", label: buildStatusLabel(buildStatus), bucket: "passing" };
  }
  if (buildStatus === "degraded") {
    return { glyph: "⚠", tone: "warn", pillTone: "warn", label: buildStatusLabel(buildStatus), bucket: "partial" };
  }
  if (buildStatus === "in_progress") {
    return { glyph: "●", tone: "info", pillTone: "info", label: buildStatusLabel(buildStatus), bucket: "building" };
  }
  if (buildStatus === "blocked" || buildStatus === "failed_scope") {
    return { glyph: "✗", tone: "fail", pillTone: "error", label: buildStatusLabel(buildStatus), bucket: "blocked" };
  }
  return { glyph: "○", tone: "pending", pillTone: "muted", label: buildStatusLabel(buildStatus), bucket: "pending" };
}

// R3-B24: classify each feature into one of four buckets so we can emit
// section subheads when more than one bucket has members. We surface
// "Passing", "Partial", "Blocked", "Pending" (covers null + missing).
type VerdictBucket = "passing" | "building" | "partial" | "blocked" | "pending";

const BUCKET_ORDER: VerdictBucket[] = ["blocked", "partial", "building", "passing", "pending"];
const BUCKET_LABEL: Record<VerdictBucket, string> = {
  passing: "Passing",
  building: "Building",
  partial: "Partial",
  blocked: "Blocked",
  pending: "Waiting",
};

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1).trimEnd() + "…";
}

export function FeatureList({ features, findings = [], onSelect }: Props) {
  if (features.length === 0) {
    return <p className="empty">No features in this run.</p>;
  }

  // R3-B33: precompute critical finding count per feature.
  const criticalsByFeature = new Map<string, FindingView[]>();
  for (const f of findings) {
    if (f.severity !== "critical") continue;
    if (!f.feature_id) continue;
    const list = criticalsByFeature.get(f.feature_id) ?? [];
    list.push(f);
    criticalsByFeature.set(f.feature_id, list);
  }

  // R3-B24: group features into buckets, preserving original order within
  // each bucket. Only emit subheads when more than one bucket is present.
  const buckets = new Map<VerdictBucket, FeatureView[]>();
  for (const f of features) {
    const b = visualState(f.verdict, f.build_status).bucket;
    const list = buckets.get(b) ?? [];
    list.push(f);
    buckets.set(b, list);
  }
  const presentBuckets = BUCKET_ORDER.filter((b) => (buckets.get(b)?.length ?? 0) > 0);
  const showSubheads = presentBuckets.length > 1;

  const renderRow = (f: FeatureView) => {
    const criticals = criticalsByFeature.get(f.id) ?? [];
    const criticalCount = criticals.length;
    const isBlocked = f.verdict === "blocked" || f.verdict === "failed";
    const inlineCritical =
      isBlocked && criticalCount > 0 ? truncate(criticals[0]!.text, 80) : null;
    const state = visualState(f.verdict, f.build_status);
    return (
      <li
        key={f.id}
        className={`feature-row ${state.tone}`}
        data-testid={`feature-${f.id}`}
        onClick={() => onSelect?.(f.id)}
      >
        <span className="feature-row-main">
          <span className="verdict-glyph" aria-hidden>
            {state.glyph}
          </span>
          <span className="feature-name">{f.name}</span>
          <Pill tone={state.pillTone} className="feature-verdict-pill">
            {state.label}
          </Pill>
          {/* R3-B33: critical-finding count badge (red bg) sits next to
              the verdict pill so it reads as severity, not metadata. */}
          {criticalCount > 0 && (
            <span
              className="feature-critical-badge"
              data-testid={`feature-critical-${f.id}`}
              title={`${criticalCount} critical ${criticalCount === 1 ? "finding" : "findings"}`}
            >
              {criticalCount} critical
            </span>
          )}
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
          {/* R3-B23: chevron makes drilldown affordance visible. The whole
              row is still clickable; the chevron is purely visual. */}
          <span className="feature-row-chevron" aria-hidden>
            ▸
          </span>
        </span>
        {/* R3-B32: when blocked + has criticals, surface the first finding
            inline (truncated) so triage doesn't require a click. */}
        {inlineCritical && (
          <span
            className="feature-row-critical-preview"
            data-testid={`feature-critical-preview-${f.id}`}
          >
            {inlineCritical}
          </span>
        )}
      </li>
    );
  };

  if (!showSubheads) {
    return (
      <ul className="feature-list" data-testid="feature-list">
        {features.map(renderRow)}
      </ul>
    );
  }

  return (
    <div className="feature-list-grouped" data-testid="feature-list">
      {presentBuckets.map((bucket) => {
        const items = buckets.get(bucket) ?? [];
        return (
          <section
            key={bucket}
            className={`feature-list-bucket feature-list-bucket--${bucket}`}
            data-testid={`feature-bucket-${bucket}`}
          >
            <h4 className="feature-list-subhead">
              {BUCKET_LABEL[bucket]} ({items.length})
            </h4>
            <ul className="feature-list">{items.map(renderRow)}</ul>
          </section>
        );
      })}
    </div>
  );
}

export default FeatureList;
