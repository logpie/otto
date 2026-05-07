// VerdictHeader — top of the RunDrawer (research §7 + wireframes screen 3).
// Shows: outcome pill, intent line, verdict counts, quality score, wall+cost.
//
// Post-RUA round 1 (B1, B2, B5): the row is laid out via flex/gap so the
// metrics are visually separated; baked-in leading/trailing whitespace
// has been stripped from the labels (CSS gap handles separation, not
// the strings). The outcome chip routes through the scoped <Pill>.
//
// Post-RUA round 2 (R2-B13): KPI numbers (Wall, Cost, Features) are
// rendered with `font-variant-numeric: tabular-nums` so columns don't
// jitter when stacked across runs. The base rule is on
// `.run-drawer-header .metric dd` in styles.css; an explicit rule on
// `.metric-num` and `.metric-val` defends against future markup changes
// where the dd wrapper may not be the immediate parent.

import { useState } from "react";
import type { RunVerdict, RunView } from "../../types/run";
import { formatTokenSpend, tokenBreakdownLine } from "../../utils/format";
import { Pill, type PillTone } from "./Pill";

interface Props {
  view: RunView;
}

// R3-B22: long intents truncate via CSS ellipsis on the header row,
// which hid context behind a tooltip-only affordance. Render an
// inline "View full intent ▸" toggle when the intent exceeds a
// reasonable inline-length budget; clicking expands the full text
// in-place so the user never has to hover/copy the title attribute.
const INTENT_INLINE_LIMIT = 120;

function truncateIntent(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1).trimEnd() + "…";
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

// R3-B26: relative-time formatter for the run's finished_at. Mirrors
// the SpecReviewPage formatter so spelling stays consistent ("4m ago",
// "2h ago"). Returns null for in-flight runs (caller suppresses).
const RTF =
  typeof Intl !== "undefined" && Intl.RelativeTimeFormat
    ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
    : null;

function formatRelative(iso: string | null): string | null {
  if (!iso) return null;
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return null;
  const diffMs = ts - Date.now();
  const absSec = Math.abs(diffMs) / 1000;
  if (!RTF) return iso;
  if (absSec < 45) return "just now";
  if (absSec < 60 * 60) return RTF.format(Math.round(diffMs / 60000), "minute");
  if (absSec < 60 * 60 * 24) return RTF.format(Math.round(diffMs / 3600000), "hour");
  if (absSec < 60 * 60 * 24 * 30) return RTF.format(Math.round(diffMs / 86400000), "day");
  if (absSec < 60 * 60 * 24 * 365)
    return RTF.format(Math.round(diffMs / (86400000 * 30)), "month");
  return RTF.format(Math.round(diffMs / (86400000 * 365)), "year");
}

export function VerdictHeader({ view }: Props) {
  const tone = verdictTone(view.verdict);
  const hasFeatureAudit = view.features.some((f) => f.verdict !== null);
  const passedFeatures = hasFeatureAudit
    ? view.features.filter((f) => f.verdict === "passed").length
    : view.features.filter((f) => f.build_status === "passing" || f.build_status === "landed").length;
  const totalFeatures = view.features.length;
  const completedGroups = view.groups.filter((g) => g.status === "passing" || g.status === "landed").length;
  const totalGroups = view.groups.length;
  const activeGroup = view.groups.find((g) => g.status === "in_progress") ?? null;
  const dispatch = view.dispatch;
  const criticalCount = view.findings.filter((f) => f.severity === "critical").length;
  const label = view.verdict ?? view.status;
  const [intentExpanded, setIntentExpanded] = useState(false);
  const intentRaw = view.intent ?? "";
  const intentNeedsToggle = intentRaw.length > INTENT_INLINE_LIMIT;
  const intentVisible =
    !intentNeedsToggle || intentExpanded
      ? intentRaw
      : truncateIntent(intentRaw, INTENT_INLINE_LIMIT);
  // R3-B26: human-readable "finished N ago" for terminal runs. We
  // suppress the timestamp on in-flight runs (finished_at is null
  // until the run lands) so the header doesn't show a stale or empty
  // value. The full ISO timestamp lives on the <time> element's
  // dateTime + title attributes for hover/inspection.
  const finishedAt = view.meta.finished_at;
  const finishedRelative = formatRelative(finishedAt);
  const tokenSpend = formatTokenSpend(view.token_usage);

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
        <span
          className={`intent-text${
            intentNeedsToggle && intentExpanded ? " intent-text--expanded" : ""
          }`}
          title={intentRaw}
          data-testid="intent-text"
        >
          {intentVisible}
        </span>
        {intentNeedsToggle && (
          <button
            type="button"
            className="intent-toggle"
            data-testid="intent-toggle"
            aria-expanded={intentExpanded}
            onClick={() => setIntentExpanded((v) => !v)}
          >
            {intentExpanded ? "Hide full intent ▾" : "View full intent ▸"}
          </button>
        )}
      </div>
      {finishedRelative && finishedAt && (
        <div
          className="run-drawer-finished-line"
          data-testid="run-drawer-finished-line"
        >
          Finished{" "}
          <time dateTime={finishedAt} title={finishedAt}>
            {finishedRelative}
          </time>
        </div>
      )}
      {view.control_plane?.conflict && (
        <div className="run-drawer-control-conflict" data-testid="run-control-plane-conflict">
          {view.control_plane.conflict_reason ||
            "Proof and queue control-plane status disagree."}
        </div>
      )}
      {!view.verdict && totalGroups > 0 && (
        <div className="run-drawer-active-line" data-testid="run-drawer-active-line">
          {dispatch && dispatch.running_group_ids.length > 0 ? (
            <>
              Running {dispatch.running_group_ids.length}
              {dispatch.max_concurrent ? `/${dispatch.max_concurrent}` : ""} groups
              {dispatch.ready_group_ids.length > 0
                ? ` · ${dispatch.ready_group_ids.length} ready`
                : ""}
              {dispatch.waiting_group_ids.length > 0
                ? ` · ${dispatch.waiting_group_ids.length} waiting on dependencies`
                : ""}
            </>
          ) : activeGroup ? (
            <>
              Building group: <strong>{activeGroup.name}</strong>
              {activeGroup.dependencies.length > 0
                ? ` · after ${activeGroup.dependencies.join(", ")}`
                : ""}
            </>
          ) : (
            <>Dispatching groups in dependency order.</>
          )}
        </div>
      )}
      <dl className="metrics" data-testid="metrics">
        {totalGroups > 0 && (
          <div className="metric">
            <dt>Groups</dt>
            <dd className="groups">
              <span className="metric-num">{completedGroups}</span>
              <span className="metric-sep" aria-hidden>/</span>
              <span className="metric-num">{totalGroups}</span>
            </dd>
          </div>
        )}
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
          <dd className="wall">
            <span className="metric-num">{formatDuration(view.wall_s)}</span>
          </dd>
        </div>
        <div className="metric">
          <dt>Cost</dt>
          <dd className="cost">
            <span className="metric-num">{formatCost(view.cost_usd)}</span>
          </dd>
        </div>
        {tokenSpend && (
          <div className="metric">
            <dt>Tokens</dt>
            <dd className="tokens" title={tokenBreakdownLine(view.token_usage)}>
              <span className="metric-num">{tokenSpend}</span>
            </dd>
          </div>
        )}
      </dl>
    </header>
  );
}

export default VerdictHeader;
