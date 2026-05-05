// FeatureDrilldown — per-Feature detail view (research §7 + §4).
//
// Surfaces what FeatureList collapses: full description, acceptance
// criterion, evidence kinds, audit honesty fields, evidence refs with
// links to artifacts, and severity-classified findings (`critical`,
// `important`, `polish`) that target this Feature.
//
// Mounted by RunViewPage when the user clicks a Feature row. A "Back"
// link lets the user return to the drawer without a full reload.
//
// Post-RUA round 1 (B8): added breadcrumb (Run › group › feature),
// per-Feature action stubs (Open evidence dir / Re-audit / Logs),
// and left-aligned the back button. Status pill + severity badges
// route through the scoped <Pill>/<Badge> primitives (B2, B3).

import type { FeatureView, FeatureVerdict, RunView } from "../../types/run";
import { Badge, Pill, type PillTone } from "./Pill";

interface Props {
  feature: FeatureView;
  view: RunView;
  onBack: () => void;
}

function verdictTone(verdict: FeatureVerdict | null): "ok" | "warn" | "fail" | "pending" | "info" {
  if (verdict === null) return "pending";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  if (verdict === "missing") return "info";
  return "fail";
}

function verdictPillTone(verdict: FeatureVerdict | null): PillTone {
  if (verdict === null) return "muted";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  if (verdict === "missing") return "info";
  return "error";
}

export function FeatureDrilldown({ feature, view, onBack }: Props) {
  const findingsForFeature = view.findings.filter(
    (f) => f.feature_id === feature.id,
  );
  const groupForFeature = view.groups.find((g) => g.id === feature.group_id);
  const sessionId = view.meta.session_id;

  return (
    <article
      className={`feature-drilldown ${verdictTone(feature.verdict)}`}
      data-testid="feature-drilldown"
    >
      <nav className="feature-drilldown-breadcrumb" aria-label="Breadcrumb">
        <button
          type="button"
          className="back-link feature-drilldown-back"
          data-testid="feature-drilldown-back"
          onClick={onBack}
        >
          ← Back to run
        </button>
        <span className="breadcrumb-trail" data-testid="feature-drilldown-breadcrumb">
          <a
            className="breadcrumb-link"
            href={`/?view=run-view&session=${encodeURIComponent(sessionId)}`}
            onClick={(e) => {
              e.preventDefault();
              onBack();
            }}
          >
            Run
          </a>
          <span className="breadcrumb-sep" aria-hidden>›</span>
          {groupForFeature ? (
            <a
              className="breadcrumb-link"
              href={`/?view=run-view&session=${encodeURIComponent(sessionId)}`}
              onClick={(e) => {
                e.preventDefault();
                onBack();
              }}
              data-testid="feature-drilldown-breadcrumb-group"
            >
              {groupForFeature.name}
            </a>
          ) : (
            <span className="breadcrumb-current">Ungrouped</span>
          )}
          <span className="breadcrumb-sep" aria-hidden>›</span>
          <span className="breadcrumb-current" data-testid="feature-drilldown-breadcrumb-feature">
            {feature.name}
          </span>
        </span>
      </nav>

      <header className="feature-drilldown-header">
        <h2 data-testid="feature-drilldown-name">{feature.name}</h2>
        <Pill
          tone={verdictPillTone(feature.verdict)}
          className="verdict-pill"
          testId="feature-drilldown-verdict"
        >
          {feature.verdict ?? "pending"}
        </Pill>
      </header>

      <div className="feature-drilldown-actions" data-testid="feature-drilldown-actions">
        <button
          type="button"
          className="feature-action-button"
          data-testid="feature-action-evidence"
          onClick={() => {
            // TODO: wire to /api/run-view/<sid>/features/<fid>/evidence-dir
            // eslint-disable-next-line no-console
            console.log("[FeatureDrilldown] open evidence dir stub", feature.id);
          }}
        >
          Open evidence dir
        </button>
        <button
          type="button"
          className="feature-action-button"
          data-testid="feature-action-reaudit"
          onClick={() => {
            // TODO: wire to /api/run-view/<sid>/features/<fid>/reaudit
            // eslint-disable-next-line no-console
            console.log("[FeatureDrilldown] re-audit stub", feature.id);
          }}
        >
          Re-audit just this Feature
        </button>
        <button
          type="button"
          className="feature-action-button"
          data-testid="feature-action-logs"
          onClick={() => {
            // TODO: wire to /api/run-view/<sid>/features/<fid>/logs
            // eslint-disable-next-line no-console
            console.log("[FeatureDrilldown] logs stub", feature.id);
          }}
        >
          Logs
        </button>
      </div>

      {feature.description && (
        <section className="feature-drilldown-description">
          <h3>Description</h3>
          <p>{feature.description}</p>
        </section>
      )}

      {feature.acceptance_detail && (
        <section className="feature-drilldown-acceptance">
          <h3>Acceptance</h3>
          <p>{feature.acceptance_detail}</p>
        </section>
      )}

      <section className="feature-drilldown-honesty">
        <h3>Audit context</h3>
        <dl>
          <dt>Evidence completeness</dt>
          <dd data-testid="feature-drilldown-completeness">
            {feature.evidence_completeness}
          </dd>
          <dt>Coverage confidence</dt>
          <dd data-testid="feature-drilldown-coverage">
            {feature.coverage_confidence}
          </dd>
          <dt>Multi-actor required</dt>
          <dd>{feature.multi_actor_required ? "yes" : "no"}</dd>
          <dt>Pre-merge audit</dt>
          <dd>{feature.audit_pre_merge ? "yes" : "no"}</dd>
          {groupForFeature && (
            <>
              <dt>Group</dt>
              <dd data-testid="feature-drilldown-group">
                {groupForFeature.name}
              </dd>
            </>
          )}
        </dl>
      </section>

      <section className="feature-drilldown-evidence-kinds">
        <h3>Evidence kinds</h3>
        {feature.evidence_kinds.length === 0 ? (
          <p className="empty">No evidence kinds declared.</p>
        ) : (
          <ul>
            {feature.evidence_kinds.map((k) => (
              <li key={k} className="evidence-kind-pill">
                {k}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="feature-drilldown-evidence-refs">
        <h3>Evidence collected</h3>
        {feature.evidence_refs.length === 0 ? (
          <p className="empty">No evidence collected yet.</p>
        ) : (
          <ul>
            {feature.evidence_refs.map((ref, i) => (
              <li key={`${ref.kind}-${ref.path}-${i}`} className="evidence-ref-row">
                <span className="evidence-kind">{ref.kind}</span>
                <code className="evidence-path">{ref.path}</code>
                <span className="evidence-summary">{ref.summary}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="feature-drilldown-findings">
        <h3>Findings</h3>
        {findingsForFeature.length === 0 ? (
          <p className="empty">No findings against this feature.</p>
        ) : (
          <ul>
            {findingsForFeature.map((f, i) => (
              <li
                key={`${f.severity}-${i}`}
                className={`finding finding-${f.severity}`}
                data-testid="feature-drilldown-finding"
              >
                <Badge severity={f.severity} testId="feature-drilldown-finding-severity" />
                <span className="finding-message">{f.text}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  );
}

export default FeatureDrilldown;
