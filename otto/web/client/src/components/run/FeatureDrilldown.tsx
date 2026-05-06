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
// The drilldown must not expose fake controls. Group-level logs/diffs are
// real endpoints; feature-only evidence actions render only when evidence
// exists in the proof packet.

import {useState} from "react";
import type { FeatureView, FeatureVerdict, GroupStatus, RunView } from "../../types/run";
import { Badge, Pill, type PillTone } from "./Pill";

interface Props {
  feature: FeatureView;
  view: RunView;
  onBack: () => void;
}

interface FeatureState {
  label: string;
  tone: "ok" | "warn" | "fail" | "pending" | "info";
  pillTone: PillTone;
}

interface LogsPayload {
  logs: Array<{label: string; path: string; text: string; truncated: boolean}>;
  empty: boolean;
}

interface DiffPayload {
  branch: string;
  diff: string;
  truncated: boolean;
  error: string | null;
}

type ResourceState =
  | {kind: "logs"; loading: boolean; error: string | null; payload: LogsPayload | null}
  | {kind: "diff"; loading: boolean; error: string | null; payload: DiffPayload | null}
  | null;

function featureState(verdict: FeatureVerdict | null, buildStatus: GroupStatus): FeatureState {
  if (verdict === "passed") return {label: "passed", tone: "ok", pillTone: "ok"};
  if (verdict === "partial") return {label: "partial", tone: "warn", pillTone: "warn"};
  if (verdict === "missing") return {label: "missing", tone: "info", pillTone: "info"};
  if (verdict === "blocked" || verdict === "failed") {
    return {label: verdict, tone: "fail", pillTone: "error"};
  }
  if (buildStatus === "passing" || buildStatus === "landed") {
    return {label: "built", tone: "ok", pillTone: "ok"};
  }
  if (buildStatus === "in_progress") {
    return {label: "building", tone: "info", pillTone: "info"};
  }
  if (buildStatus === "blocked" || buildStatus === "failed_scope") {
    return {label: "blocked", tone: "fail", pillTone: "error"};
  }
  return {label: "waiting", tone: "pending", pillTone: "muted"};
}

export function FeatureDrilldown({ feature, view, onBack }: Props) {
  const findingsForFeature = view.findings.filter(
    (f) => f.feature_id === feature.id,
  );
  const groupForFeature = view.groups.find((g) => g.id === feature.group_id);
  const sessionId = view.meta.session_id;
  const state = featureState(feature.verdict, feature.build_status);
  const [resource, setResource] = useState<ResourceState>(null);

  async function openGroupResource(kind: "logs" | "diff") {
    if (!groupForFeature) return;
    setResource({kind, loading: true, error: null, payload: null});
    const endpoint = `/api/run-view/${encodeURIComponent(sessionId)}/groups/${encodeURIComponent(groupForFeature.id)}/${kind}`;
    try {
      const resp = await fetch(endpoint);
      if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      if (kind === "logs") {
        const payload = await resp.json() as LogsPayload;
        setResource({kind, loading: false, error: null, payload});
      } else {
        const payload = await resp.json() as DiffPayload;
        setResource({kind, loading: false, error: null, payload});
      }
    } catch (error) {
      setResource({
        kind,
        loading: false,
        error: (error as Error).message || String(error),
        payload: null,
      });
    }
  }

  return (
    <article
      className={`feature-drilldown ${state.tone}`}
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
          tone={state.pillTone}
          className="verdict-pill"
          testId="feature-drilldown-verdict"
        >
          {state.label}
        </Pill>
      </header>

      {groupForFeature ? (
        <div className="feature-drilldown-actions" data-testid="feature-drilldown-actions">
          <button
            type="button"
            className="feature-action-button"
            data-testid="feature-action-group-diff"
            onClick={() => void openGroupResource("diff")}
          >
            Group diff
          </button>
          <button
            type="button"
            className="feature-action-button"
            data-testid="feature-action-group-logs"
            onClick={() => void openGroupResource("logs")}
          >
            Group logs
          </button>
        </div>
      ) : null}

      {resource ? <FeatureResourcePanel resource={resource} /> : null}

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

function FeatureResourcePanel({resource}: {resource: ResourceState}) {
  if (!resource) return null;
  const title = resource.kind === "logs" ? "Group logs" : "Group diff";
  return (
    <section className="feature-resource-panel" data-testid={`feature-resource-${resource.kind}`}>
      <h3>{title}</h3>
      {resource.loading ? <p>Loading {title.toLowerCase()}...</p> : null}
      {resource.error ? <p role="alert">Failed to load {title.toLowerCase()}: {resource.error}</p> : null}
      {!resource.loading && !resource.error && resource.kind === "logs" && (
        <FeatureLogs payload={resource.payload as LogsPayload | null} />
      )}
      {!resource.loading && !resource.error && resource.kind === "diff" && (
        <FeatureDiff payload={resource.payload as DiffPayload | null} />
      )}
    </section>
  );
}

function FeatureLogs({payload}: {payload: LogsPayload | null}) {
  if (!payload || payload.empty || payload.logs.length === 0) {
    return <p>No group logs have been written yet.</p>;
  }
  return (
    <div className="feature-resource-list">
      {payload.logs.slice(0, 4).map((log) => (
        <details key={log.path} className="feature-resource-log">
          <summary>{log.label}{log.truncated ? " · truncated" : ""}</summary>
          <pre>{log.text}</pre>
        </details>
      ))}
    </div>
  );
}

function FeatureDiff({payload}: {payload: DiffPayload | null}) {
  if (!payload) return <p>No group diff is available yet.</p>;
  if (payload.error) return <p role="alert">{payload.error}</p>;
  if (!payload.diff) return <p>No group diff has been recorded yet.</p>;
  return (
    <>
      <p className="run-resource-note">
        {payload.branch ? `Branch ${payload.branch}` : "Group branch"}
        {payload.truncated ? " · truncated" : ""}
      </p>
      <pre className="run-diff-text">{payload.diff}</pre>
    </>
  );
}
