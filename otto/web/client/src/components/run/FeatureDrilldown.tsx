// FeatureDrilldown — per-Feature detail view (research §7 + §4).
//
// Surfaces what FeatureList collapses: full description, acceptance
// criterion, evidence kinds, audit honesty fields, evidence refs with
// links to artifacts, and severity-classified findings (`critical`,
// `important`, `polish`) that target this Feature.
//
// Mounted by RunViewPage when the user clicks a Feature row. A "Back"
// link lets the user return to the drawer without a full reload.

import type {
  FeatureView,
  FindingView,
  RunView,
} from "../../types/run";

interface Props {
  feature: FeatureView;
  view: RunView;
  onBack: () => void;
}

function verdictTone(verdict: FeatureView["verdict"]): "ok" | "warn" | "fail" | "pending" | "info" {
  if (verdict === null) return "pending";
  if (verdict === "passed") return "ok";
  if (verdict === "partial") return "warn";
  if (verdict === "missing") return "info";
  return "fail";
}

function severityTone(severity: FindingView["severity"]): "fail" | "warn" | "info" {
  if (severity === "critical") return "fail";
  if (severity === "important") return "warn";
  return "info";
}

export function FeatureDrilldown({ feature, view, onBack }: Props) {
  const findingsForFeature = view.findings.filter(
    (f) => f.feature_id === feature.id,
  );
  const groupForFeature = view.groups.find((g) => g.id === feature.group_id);

  return (
    <article
      className={`feature-drilldown ${verdictTone(feature.verdict)}`}
      data-testid="feature-drilldown"
    >
      <header className="feature-drilldown-header">
        <button
          type="button"
          className="back-link"
          data-testid="feature-drilldown-back"
          onClick={onBack}
        >
          ← Back to run
        </button>
        <h2 data-testid="feature-drilldown-name">{feature.name}</h2>
        <span
          className="verdict-pill"
          data-testid="feature-drilldown-verdict"
        >
          {feature.verdict ?? "pending"}
        </span>
      </header>

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
              <li key={`${ref.kind}-${ref.path}-${i}`}>
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
                className={`finding ${severityTone(f.severity)}`}
                data-testid="feature-drilldown-finding"
              >
                <span className="severity-pill">{f.severity}</span>
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
