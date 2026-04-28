import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode} from "react";
import {CommandList, ReviewDrawer, ReviewMetric} from "../MicroComponents";
import {useDialogFocus} from "../../hooks/useDialogFocus";
import {
  LOG_BUFFER_MAX_BYTES,
  LOG_POLL_BACKOFF_MS,
  LOG_POLL_BASE_MS,
  appendToLogBuffer,
  bytesToString,
  countLines,
  type LogState,
  type LogStatus,
} from "../../logBuffer";
import type {
  ActionState,
  ArtifactContentResponse,
  ArtifactRef,
  CertificationRound,
  DiffResponse,
  LandingState,
  ProductHandoff,
  ProofReportInfo,
  RunDetail,
} from "../../types";
import {
  formatCompactNumber,
  formatDiffTruncationBanner,
  formatDuration,
  formatRelativeFreshness,
  formatTechnicalIssue,
  humanBytes,
  shortText,
  storiesLine,
  tokenTotal,
} from "../../utils/format";
import {
  actionName,
  artifactKindLabel,
  canShowDiff,
  canTryProduct,
  certificationLine,
  checkStatusIcon,
  checkStatusLabel,
  compactLongText,
  detailStatusLabel,
  diffDisabledReason,
  domainLabel,
  flagsLine,
  formatArtifactContent,
  formatReviewText,
  isLogArtifact,
  isReadableArtifact,
  isRepositoryBlockedPacket,
  isReviewEvidenceArtifact,
  limitLine,
  productKindHint,
  productActionLabel,
  projectConfigLine,
  providerLine,
  renderDiffText,
  renderLogText,
  reviewActionLabel,
  shortPath,
  splitDiffIntoFiles,
  storyStatusClass,
  storyStatusIcon,
  storyStatusLabel,
  timeoutLine,
  planningLine,
  userVisibleDetailLine,
  isTypingTarget,
  agentsLine,
} from "../../utils/missionControl";
import type {BoardTask, InspectorMode} from "../../uiTypes";

export function RunDetailPanel({detail, logState, landing, inspectorOpen, queuedTask, loadingRunId, watcherRunning, onRunAction, onShowTryProduct, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadArtifact, onStartWatcher, onClose}: {
  detail: RunDetail | null;
  logState: LogState;
  landing: LandingState | undefined;
  inspectorOpen: boolean;
  queuedTask?: BoardTask | null;
  loadingRunId?: string | null;
  watcherRunning?: boolean;
  onRunAction: (action: string, label?: string) => void;
  onShowTryProduct: () => void;
  onShowProof: () => void;
  onShowLogs: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
  onLoadArtifact: (index: number) => void;
  onStartWatcher?: () => void;
  onClose?: () => void;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const [panelWidth, setPanelWidth] = useState<number>(() => {
    if (typeof window === "undefined") return 560;
    const saved = Number(window.localStorage.getItem("otto.runDetailWidth"));
    const maxWidth = Math.min(Math.max(420, window.innerWidth - 520), Math.max(440, window.innerWidth - 48));
    if (Number.isFinite(saved) && saved > 0) {
      return Math.min(Math.max(saved, 420), maxWidth);
    }
    return Math.min(560, maxWidth);
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("otto.runDetailWidth", String(Math.round(panelWidth)));
    document.documentElement.style.setProperty("--run-detail-width", `${Math.round(panelWidth)}px`);
    return () => {
      document.documentElement.style.removeProperty("--run-detail-width");
    };
  }, [panelWidth]);
  const startPanelResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (typeof window === "undefined") return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const startX = event.clientX;
    const startWidth = panelRef.current?.getBoundingClientRect().width || panelWidth;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "ew-resize";
    document.body.style.userSelect = "none";
    const onMove = (moveEvent: PointerEvent) => {
      const maxWidth = Math.min(Math.max(420, window.innerWidth - 520), Math.max(440, window.innerWidth - 48));
      const next = startWidth + startX - moveEvent.clientX;
      setPanelWidth(Math.min(Math.max(next, 420), maxWidth));
    };
    const onUp = () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, [panelWidth]);
  const panelStyle = {"--run-detail-width": `${Math.round(panelWidth)}px`} as CSSProperties;
  // Drawer-mode: only render when a task is selected. Replaces the inline
  // right-rail Review Packet that used to show "Already merged into main"
  // permanently. mc-audit redesign Phase C.
  if (!detail && !queuedTask && !loadingRunId) return null;
  const tryProductAvailable = canTryProduct(detail);
  const queuedRunWaiting = Boolean(
    detail
      && ["queued", "waiting", "pending"].includes(String(detail.display_status || detail.status || "").toLowerCase())
      && !watcherRunning
      && onStartWatcher,
  );
  const hideDuplicateProvisionalPlan = isLowValueVerificationPlan(detail, queuedRunWaiting);
  return (
    <>
      <div className="run-drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside ref={panelRef} className="detail run-drawer" style={panelStyle} aria-labelledby="detailHeading" data-testid="run-detail-panel">
        <div
          className="run-panel-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize run detail panel"
          title="Drag to resize panel"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            const step = event.shiftKey ? 80 : 24;
            setPanelWidth((current) => {
              const maxWidth = typeof window === "undefined"
                ? 1000
                : Math.min(Math.max(420, window.innerWidth - 520), Math.max(440, window.innerWidth - 48));
              const next = event.key === "ArrowLeft" ? current + step : current - step;
              return Math.min(Math.max(next, 420), maxWidth);
            });
          }}
          onPointerDown={startPanelResize}
        />
        <div className="panel-heading run-drawer-heading">
          <div>
            <h2 id="detailHeading">{detail || loadingRunId ? "Run detail" : "Queued task"}</h2>
            <span className="pill">{detail ? detailStatusLabel(detail) : "-"}</span>
          </div>
          {onClose ? (
            <button type="button" className="run-drawer-close" aria-label="Close" onClick={onClose}>×</button>
          ) : null}
        </div>
      {detail ? (
        <>
          <div className="detail-scroll">
            <RecoveryActionBar actions={detail.legal_actions || []} status={detail.display_status} onRunAction={onRunAction} />
            <ReviewPacket
              packet={detail.review_packet}
              detail={detail}
              onRunAction={onRunAction}
              onShowProof={onShowProof}
              onShowDiff={onShowDiff}
              onShowArtifacts={onShowArtifacts}
            />
            {detail.active && <LiveLogPreview logState={logState} onShowLogs={onShowLogs} />}
            {!hideDuplicateProvisionalPlan && <VerificationPlanPanel plan={detail.verification_plan} />}
            {queuedRunWaiting && (
              <div className="review-note recovery-note queued-start-note">
                <strong>Queue runner stopped</strong>
                <span>This task is waiting. Use the top-right queue runner control to start processing queued work.</span>
              </div>
            )}
            <PhaseTimeline phases={detail.phase_timeline || []} />
            <details className="detail-body detail-metadata">
              <summary>
                <span>Run metadata</span>
                <strong title={detail.run_id}>{detail.run_id}</strong>
              </summary>
              <div className="detail-metadata-content">
                <h3>{detail.title || detail.run_id}</h3>
                <dl>
                  <dt>Run</dt><dd>{detail.run_id}</dd>
                  <dt>Type</dt><dd data-testid="run-detail-type">{domainLabel(detail.domain)} / {detail.run_type}</dd>
                  <dt>Branch</dt><dd>{detail.branch || "-"}</dd>
                  <dt>Worktree</dt><dd>{detail.worktree || detail.cwd || "-"}</dd>
                  <dt>Provider</dt><dd>{providerLine(detail)}</dd>
                  <dt>Certification</dt><dd>{certificationLine(detail.build_config)}</dd>
                  <dt>Planning</dt><dd>{planningLine(detail.build_config) || "-"}</dd>
                  <dt>Timeouts</dt><dd>{timeoutLine(detail.build_config)}</dd>
                  <dt>Limits</dt><dd>{limitLine(detail.build_config)}</dd>
                  <dt>Run flags</dt><dd>{flagsLine(detail.build_config)}</dd>
                  <dt>Agents</dt><dd>{agentsLine(detail.build_config)}</dd>
                  <dt>Project</dt><dd>{projectConfigLine(detail.build_config)}</dd>
                  <dt>Artifacts</dt><dd>{detail.artifacts.length}</dd>
                  {detail.overlay && <><dt>Overlay</dt><dd>{detail.overlay.reason}</dd></>}
                  {detail.summary_lines.map((line, index) => <DetailLine key={`${line}-${index}`} line={line} />)}
                </dl>
              </div>
            </details>
            <ActionBar actions={detail.legal_actions || []} mergeBlocked={Boolean(landing?.merge_blocked)} onRunAction={onRunAction} />
          </div>
          {/* When the inspector is open, the fixed-position inspector overlay
              covers this row of shortcut buttons. Leaving them in the DOM
              causes Playwright (and any script-driven click) to resolve them
              as visible while the actual click is intercepted by the
              overlay — see mc-audit W13-CRITICAL-1. The inspector ships its
              own tablist (Review / Code changes / Logs / Artifacts) so
              hiding these shortcuts while the inspector is open is the
              correct UX too. */}
          {!inspectorOpen && (
            <div className="detail-inspector-actions" role="group" aria-label="Evidence shortcuts">
              {tryProductAvailable && <button className="primary" type="button" data-testid="open-try-product-button" onClick={onShowTryProduct}>{productActionLabel(detail)}</button>}
              <button type="button" data-testid="open-proof-button" onClick={onShowProof}>Proof</button>
              <button type="button" data-testid="open-diff-button" disabled={!canShowDiff(detail)} title={canShowDiff(detail) ? "" : diffDisabledReason(detail)} onClick={onShowDiff}>Code changes</button>
              <button type="button" data-testid="open-logs-button" onClick={onShowLogs}>Logs</button>
              <button type="button" data-testid="open-artifacts-button" onClick={onShowArtifacts}>Artifacts</button>
            </div>
          )}
        </>
      ) : queuedTask ? (
        <div className="detail-body empty queued-task-detail" data-testid="run-detail-queued" data-queued-task-id={queuedTask.id}>
          <h3>{queuedTask.title}</h3>
          <p className="queued-task-subtitle">
            <strong>Waiting for queue runner</strong> — this task is queued but no
            run has started yet. Logs, diffs, and proof become available once
            the queue runner picks it up.
          </p>
          <dl className="queued-task-meta">
            <dt>Status</dt><dd>{queuedTask.status}</dd>
            {queuedTask.branch && (<><dt>Branch</dt><dd title={queuedTask.branch}>{queuedTask.branch}</dd></>)}
            <dt>Reason</dt><dd>{queuedTask.reason}</dd>
            {queuedTask.summary && (<><dt>Intent</dt><dd>{shortText(queuedTask.summary, 240)}</dd></>)}
          </dl>
          <p className="queued-task-next-action">
            <strong>Next:</strong>{" "}
            {watcherRunning
              ? "Queue runner is running — task should pick up shortly."
              : "Start the queue runner from the top-right control."}
          </p>
        </div>
      ) : (
        <div className="detail-body empty run-detail-loading" data-testid="run-detail-loading">
          <h3>Loading run detail</h3>
          <p>{loadingRunId || "Selected run"}</p>
          <div className="run-detail-loading-bars" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
        </div>
      )}
      </aside>
    </>
  );
}

function CheckStatusBadge({status}: {status: string}) {
  const normalizedStatus = String(status || "").trim().toLowerCase();
  const icon = checkStatusIcon(normalizedStatus);
  if (!icon) {
    return <span>{checkStatusLabel(normalizedStatus)}</span>;
  }
  return (
    <span>
      <span className="status-icon" aria-hidden="true">{icon}</span>{" "}
      {checkStatusLabel(normalizedStatus)}
    </span>
  );
}

export function PhaseTimeline({phases}: {phases: RunDetail["phase_timeline"]}) {
  if (!phases.length) return null;
  // Filter out phases that are pure placeholders for run types where they
  // never run (e.g. merge runs always show 3 SKIPPED phases for build/
  // certify/fix — that is just visual noise). Show only phases that have
  // either run, are running, or are queued — i.e. status is meaningful.
  const visible = phases.filter((phase) => phase.status !== "skipped");
  if (!visible.length) return null;
  return (
    <section className="detail-body phase-timeline" aria-label="Execution phases">
      <div className="phase-timeline-heading">
        <h3>Execution</h3>
        <span>{visible.length} phase{visible.length === 1 ? "" : "s"}</span>
      </div>
      <div className="phase-timeline-list">
        {visible.map((phase) => (
          <article key={phase.phase} className={`phase-item phase-${phase.status}`}>
            <span className="phase-status">{phase.status}</span>
            <strong>{phase.label}</strong>
            <p>{phaseProviderLine(phase)}</p>
            <em>{phaseUsageLine(phase)}</em>
          </article>
        ))}
      </div>
    </section>
  );
}

export function VerificationPlanPanel({plan}: {plan: RunDetail["verification_plan"]}) {
  if (!plan) return null;
  const checks = plan.checks || [];
  const activeChecks = checks.filter((check) => String(check.status || "").toLowerCase() !== "skipped");
  const attentionChecks = activeChecks.filter((check) => isAttentionCheckStatus(check.status));
  const pendingChecks = activeChecks.filter((check) => isPendingCheckStatus(check.status));
  const meta = [
    verificationPolicyLabel(plan.policy),
    activeChecks.length ? `${activeChecks.length} check${activeChecks.length === 1 ? "" : "s"}` : "",
    attentionChecks.length ? `${attentionChecks.length} need review` : pendingChecks.length ? `${pendingChecks.length} pending` : "",
  ].filter(Boolean).join(" · ");
  return (
    <details className="verification-plan-panel" data-testid="verification-plan-panel" open={attentionChecks.length > 0 || undefined}>
      <summary>
        <span>Verification plan</span>
        <strong>{meta || "planned checks"}</strong>
      </summary>
      <div className="verification-plan-body">
        <div className="verification-plan-note">
          <strong>{verificationPolicyLabel(plan.policy)}</strong>
          <span>{verificationPolicyDescription(plan.policy)}</span>
        </div>
        {plan.reasons?.length ? (
          <ul className="verification-plan-reasons">
            {plan.reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        ) : null}
        {checks.length ? (
          <div className="verification-plan-checks" aria-label="Verification checks">
            {checks.map((check) => (
              <div className={`verification-check check-${String(check.status || "pending").toLowerCase()}`} key={check.id}>
                <CheckStatusBadge status={check.status || "pending"} />
                <div>
                  <strong>{check.label || check.id}</strong>
                  <p>
                    {check.action || "CHECK"}
                    {check.source ? ` · ${check.source}` : ""}
                    {check.reason ? ` · ${check.reason}` : ""}
                  </p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">No explicit checks recorded yet.</p>
        )}
        {!activeChecks.length && checks.length > 0 ? (
          <p className="muted">All checks are skipped by policy or scope.</p>
        ) : null}
      </div>
    </details>
  );
}

function isAttentionCheckStatus(status: string | null | undefined): boolean {
  return ["fail", "warn", "error", "danger", "flag_for_human"].includes(String(status || "").toLowerCase());
}

function isPendingCheckStatus(status: string | null | undefined): boolean {
  return ["pending", "running"].includes(String(status || "").toLowerCase());
}

function isLowValueVerificationPlan(detail: RunDetail | null, queuedRunWaiting: boolean): boolean {
  const plan = detail?.verification_plan;
  if (!plan) return false;
  const provisional = [
    plan.scope,
    plan.verification_level,
    plan.risk_level,
  ].some((value) => String(value || "").toLowerCase() === "provisional");
  if (queuedRunWaiting && provisional) return true;
  const planChecks = plan.checks || [];
  const reviewChecks = detail?.review_packet?.checks || [];
  if (!planChecks.length || !reviewChecks.length) return false;
  const fromReviewPacket = planChecks.every((check) => String(check.source || "").toLowerCase() === "review-packet");
  if (!fromReviewPacket) return false;
  const sameKeys = new Set(reviewChecks.map((check) => check.key));
  return planChecks.every((check) => sameKeys.has(check.id));
}

function verificationPolicyLabel(policy: string | null | undefined): string {
  switch (String(policy || "").toLowerCase()) {
    case "fast":
      return "Fast smoke";
    case "full":
      return "Full verification";
    case "skip":
      return "Verification skipped";
    case "smart":
      return "Smart verification";
    default:
      return "Verification";
  }
}

function verificationPolicyDescription(policy: string | null | undefined): string {
  switch (String(policy || "").toLowerCase()) {
    case "fast":
      return "Checks the core happy path only.";
    case "full":
      return "Runs the broadest available certification scope.";
    case "skip":
      return "Records that certification was intentionally skipped.";
    case "smart":
      return "Chooses checks based on changed code, risk, and available evidence.";
    default:
      return "Records what Otto plans to verify and what evidence it produced.";
  }
}

export function LiveLogPreview({logState, onShowLogs}: {logState: LogState; onShowLogs: () => void}) {
  const rawLines = logState.text
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim().length > 0);
  const preview = rawLines.slice(-6).join("\n");
  const displayLines = logState.totalLines > 0 ? logState.totalLines : countLines(logState.text);
  const status = describeLogHeader({
    runActive: true,
    status: logState.status,
    lastUpdatedAt: logState.lastUpdatedAt,
    pollIntervalMs: logState.pollIntervalMs,
    displayLines,
    totalBytes: logState.totalBytes,
  });
  return (
    <section className="live-log-preview" data-testid="run-detail-log-preview" aria-label="Live log preview">
      <div className="live-log-preview-head">
        <div>
          <strong>Live log</strong>
          <span>{status}</span>
        </div>
        <button type="button" data-testid="run-detail-open-logs-button" onClick={onShowLogs}>Open logs</button>
      </div>
      {preview ? (
        <pre className="live-log-preview-body">{renderLogText(preview)}</pre>
      ) : (
        <p>{logState.status === "loading" ? "Loading latest output..." : "Waiting for agent output."}</p>
      )}
    </section>
  );
}

export function phaseProviderLine(phase: RunDetail["phase_timeline"][number]): string {
  return [
    phase.provider || "provider default",
    phase.model || "model default",
    phase.reasoning_effort || "reasoning default",
  ].join(" / ");
}

export function phaseUsageLine(phase: RunDetail["phase_timeline"][number]): string {
  const parts = [
    typeof phase.duration_s === "number" ? formatDuration(phase.duration_s) : "",
    phase.rounds ? `${phase.rounds} round${phase.rounds === 1 ? "" : "s"}` : "",
    tokenTotal(phase.token_usage) ? `${formatCompactNumber(tokenTotal(phase.token_usage))} tokens` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "No usage recorded";
}

export function RunInspector({detail, mode, logState, selectedArtifactIndex, artifactContent, diffContent, onShowTryProduct, onShowProof, onShowLogs, onShowDiff, onShowArtifacts, onLoadArtifact, onRefreshDiff, onBackToArtifacts, onClose}: {
  detail: RunDetail;
  mode: InspectorMode;
  logState: LogState;
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  diffContent: DiffResponse | null;
  onShowTryProduct: () => void;
  onShowProof: () => void;
  onShowLogs: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
  onLoadArtifact: (index: number) => void;
  onRefreshDiff: () => void;
  onBackToArtifacts: () => void;
  onClose: () => void;
}) {
  const inspectorRef = useDialogFocus<HTMLElement>(onClose, false);
  const [inspectorWidth, setInspectorWidth] = useState<number>(() => {
    if (typeof window === "undefined") return 960;
    const saved = Number(window.localStorage.getItem("otto.inspectorWidth"));
    const maxWidth = Math.min(Math.max(560, window.innerWidth - 520), Math.max(560, window.innerWidth - 48));
    if (Number.isFinite(saved) && saved > 0) {
      return Math.min(Math.max(saved, 520), maxWidth);
    }
    return Math.min(960, maxWidth);
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("otto.inspectorWidth", String(Math.round(inspectorWidth)));
    document.documentElement.style.setProperty("--run-inspector-width", `${Math.round(inspectorWidth)}px`);
    return () => {
      document.documentElement.style.removeProperty("--run-inspector-width");
    };
  }, [inspectorWidth]);
  const activeMode: InspectorMode = mode;
  const tryProductAvailable = canTryProduct(detail);
  const effectiveMode: InspectorMode = activeMode === "try" && !tryProductAvailable ? "proof" : activeMode;
  // WAI-ARIA tablist pattern: roving tabindex + arrow keys + Home/End. Tabs
  // that are disabled (Code changes, when diff isn't available) skip in
  // arrow rotation. mc-audit a11y A11Y-03, K-04.
  const tabModes = useMemo<InspectorMode[]>(
    () => tryProductAvailable ? ["try", "proof", "diff", "logs", "artifacts"] : ["proof", "diff", "logs", "artifacts"],
    [tryProductAvailable],
  );
  const tabHandlers: Record<InspectorMode, () => void> = {
    try: onShowTryProduct,
    proof: onShowProof,
    diff: onShowDiff,
    logs: onShowLogs,
    artifacts: onShowArtifacts,
  };
  const tabLabels: Record<InspectorMode, string> = {
    try: "Product demo",
    proof: "Proof",
    diff: "Code changes",
    logs: "Logs",
    artifacts: "Artifacts",
  };
  const tabDisabled = (m: InspectorMode): boolean => m === "diff" && !canShowDiff(detail);
  const onTabKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const key = event.key;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(key)) return;
    event.preventDefault();
    const enabled = tabModes.filter((m) => !tabDisabled(m));
    if (!enabled.length) return;
    const focusedMode = (
      event.target instanceof HTMLElement
        ? event.target.getAttribute("data-tab-id")
        : ""
    ) as InspectorMode | "";
    const currentMode = focusedMode && enabled.includes(focusedMode) ? focusedMode : effectiveMode;
    const currentIndex = enabled.indexOf(currentMode);
    let nextIndex = 0;
    if (key === "Home") nextIndex = 0;
    else if (key === "End") nextIndex = enabled.length - 1;
    else if (key === "ArrowLeft") nextIndex = ((currentIndex < 0 ? 0 : currentIndex) - 1 + enabled.length) % enabled.length;
    else if (key === "ArrowRight") nextIndex = ((currentIndex < 0 ? -1 : currentIndex) + 1) % enabled.length;
    const nextMode = enabled[nextIndex];
    if (!nextMode) return;
    tabHandlers[nextMode]();
    const target = inspectorRef.current?.querySelector<HTMLButtonElement>(`[data-tab-id="${nextMode}"]`);
    target?.focus();
  };
  const startInspectorResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (typeof window === "undefined") return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = inspectorRef.current?.getBoundingClientRect().width || inspectorWidth;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "ew-resize";
    document.body.style.userSelect = "none";
    const onMove = (moveEvent: PointerEvent) => {
      const maxWidth = Math.min(Math.max(560, window.innerWidth - 520), Math.max(560, window.innerWidth - 48));
      const next = startWidth + startX - moveEvent.clientX;
      setInspectorWidth(Math.min(Math.max(next, 520), maxWidth));
    };
    const onUp = () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, [inspectorRef, inspectorWidth]);
  const inspectorStyle = {"--run-inspector-width": `${Math.round(inspectorWidth)}px`} as CSSProperties;
  return (
    <section
      ref={inspectorRef}
      className="run-inspector"
      style={inspectorStyle}
      role="dialog"
      aria-modal="true"
      aria-labelledby="runInspectorHeading"
      data-testid="run-inspector"
      data-mc-inspector="true"
      tabIndex={-1}
    >
      <div
        className="run-inspector-resize-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize inspector panel"
        title="Drag to resize inspector"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          const step = event.shiftKey ? 80 : 24;
          setInspectorWidth((current) => {
            const maxWidth = typeof window === "undefined"
              ? 1400
              : Math.min(Math.max(560, window.innerWidth - 520), Math.max(560, window.innerWidth - 48));
            const next = event.key === "ArrowLeft" ? current + step : current - step;
            return Math.min(Math.max(next, 520), maxWidth);
          });
        }}
        onPointerDown={startInspectorResize}
      />
      <div className="run-inspector-heading">
        <div>
          <h2 id="runInspectorHeading">{detail.title || detail.run_id}</h2>
          <p>{detailStatusLabel(detail)} review</p>
        </div>
        <div className="detail-tabs" role="tablist" aria-label="Evidence view" onKeyDown={onTabKeyDown}>
          {tabModes.map((m) => {
            const isSelected = effectiveMode === m;
            const isDisabled = tabDisabled(m);
            return (
              <button
                key={m}
                id={`run-inspector-tab-${m}`}
                data-tab-id={m}
                className={`tab ${isSelected ? "active" : ""}`}
                type="button"
                role="tab"
                aria-selected={isSelected}
                aria-controls="run-inspector-panel"
                tabIndex={isSelected ? 0 : -1}
                disabled={isDisabled}
                title={isDisabled ? diffDisabledReason(detail) : ""}
                onClick={tabHandlers[m]}
              >
                {tabLabels[m]}
              </button>
            );
          })}
        </div>
        <button type="button" data-testid="close-inspector-button" onClick={onClose}>Close inspector</button>
      </div>
      <div
        className="run-inspector-body"
        id="run-inspector-panel"
        role="tabpanel"
        aria-labelledby={`run-inspector-tab-${effectiveMode}`}
      >
        {effectiveMode === "try" ? (
          <ProductHandoffPane detail={detail} />
        ) : effectiveMode === "proof" ? (
          <ProofPane detail={detail} onShowDiff={onShowDiff} onShowArtifacts={onShowArtifacts} />
        ) : effectiveMode === "diff" ? (
          <DiffPane diff={diffContent} onRefresh={onRefreshDiff} />
        ) : effectiveMode === "logs" ? (
          <LogPane logState={logState} runActive={detail.active} onRetry={onShowLogs} />
        ) : (
          <ArtifactPane
            runId={detail.run_id}
            artifacts={detail.artifacts || []}
            selectedArtifactIndex={selectedArtifactIndex}
            artifactContent={artifactContent}
            onLoadArtifact={onLoadArtifact}
            onBack={onBackToArtifacts}
          />
        )}
      </div>
    </section>
  );
}

export function ProductHandoffPane({detail}: {detail: RunDetail}) {
  const handoff = productHandoffFor(detail);
  const hasLaunch = handoff.launch.length > 0;
  const hasReset = handoff.reset.length > 0;
  const hasSamples = handoff.sample_data.length > 0;
  const hasUrls = handoff.urls.length > 0;
  const hasTaskContext = Boolean(handoff.task_summary || handoff.task_flows.length || handoff.task_changed_files.length);
  const demo = detail.review_packet.certification.demo_evidence;
  const primaryDemo = demo?.primary_demo || null;
  const primaryDemoUrl = primaryDemo?.href ? proofAssetUrl(detail.run_id, primaryDemo.href) : "";
  const fallbackVideo = productDemoVideoArtifact(detail.artifacts);
  const screenshots = productScreenshotArtifacts(detail.artifacts).slice(0, 4);
  const proofReportUrl = detail.review_packet.certification.proof_report?.html_url || "";
  const certificationMode = detail.build_config?.certifier_mode || detail.certifier_mode || "unknown";
  const primaryIsVideo = Boolean(primaryDemo && isDemoVideo(primaryDemo));
  const primaryIsImage = Boolean(primaryDemo && isDemoImage(primaryDemo));
  return (
    <div className="product-handoff-pane" data-testid="product-handoff-pane">
      <section className="product-demo-section" aria-labelledby="productDemoHeading">
        <div>
          <span>{handoff.label}</span>
          <h3 id="productDemoHeading">Product demo</h3>
          <p>
            {primaryDemo
              ? demo?.demo_reason || "Task-specific proof media is available. Review it before reproducing the flow yourself."
              : fallbackVideo
                ? "A recorded proof clip is available. Use it to see the feature before reproducing it yourself."
                : demoUnavailableCopy(handoff, certificationMode, demo?.demo_reason || "")}
          </p>
        </div>
        {primaryDemo && primaryIsVideo ? (
          <video controls data-testid="product-demo-video" className="product-demo-video">
            <source src={primaryDemoUrl} type={demoVideoMimeType(primaryDemo)} />
          </video>
        ) : primaryDemo && primaryIsImage ? (
          <div className="product-demo-screenshots" aria-label="Visual proof screenshots">
            <a href={primaryDemoUrl} target="_blank" rel="noreferrer">
              <img src={primaryDemoUrl} alt={primaryDemo.name || "primary proof"} />
              <span>{primaryDemo.name || "primary proof"}</span>
            </a>
          </div>
        ) : fallbackVideo ? (
          <video controls data-testid="product-demo-video" className="product-demo-video">
            <source src={artifactRawUrl(detail.run_id, fallbackVideo.index)} type={videoMimeType(fallbackVideo)} />
          </video>
        ) : screenshots.length ? (
          <div className="product-demo-screenshots" aria-label="Visual proof screenshots">
            {screenshots.map((artifact) => (
              <a href={artifactRawUrl(detail.run_id, artifact.index)} target="_blank" rel="noreferrer" key={artifact.index}>
                <img src={artifactRawUrl(detail.run_id, artifact.index)} alt={artifact.label} />
                <span>{artifact.label}</span>
              </a>
            ))}
          </div>
        ) : (
          <div className="product-demo-empty" data-testid="product-demo-empty">
            <strong>No demo media recorded</strong>
            <span>{demo?.demo_reason || "That is expected for fast certification, CLI/API/library work, and test-only runs. The commands and proof report remain the audit trail."}</span>
          </div>
        )}
        <div className="product-demo-actions">
          {proofReportUrl ? <a href={proofReportUrl} target="_blank" rel="noreferrer">Open proof report</a> : null}
        </div>
      </section>

      <section className="product-handoff-hero" aria-labelledby="productHandoffHeading">
        <div>
          <span>Try it yourself</span>
          <h3 id="productHandoffHeading">{handoff.preview_label || "Preview product"}</h3>
          <p>{handoff.summary || productKindHint(handoff.kind)}</p>
          {handoff.preview_reason ? <p className="handoff-preview-reason">{handoff.preview_reason}</p> : null}
        </div>
        <dl>
          <dt>Root</dt>
          <dd title={handoff.root}>{shortPath(handoff.root)}</dd>
          <dt>Source</dt>
          <dd>{handoff.source_path ? `${handoff.source} · ${shortPath(handoff.source_path)}` : handoff.source}</dd>
        </dl>
      </section>

      {hasTaskContext && (
        <section className="product-handoff-section handoff-task-section" aria-labelledby="productTaskHeading">
          <div className="handoff-section-heading">
            <h3 id="productTaskHeading">This task</h3>
            <span>{[handoff.task_status, handoff.task_branch].filter(Boolean).join(" · ") || "task-specific"}</span>
          </div>
          {handoff.task_summary ? <p className="handoff-task-summary">{handoff.task_summary}</p> : null}
          {handoff.task_flows.length ? (
            <div className="handoff-flow-list">
              {handoff.task_flows.map((flow, index) => (
                <article className="handoff-flow" key={`${flow.title}-${index}`}>
                  <strong>{flow.title}</strong>
                  {flow.steps.length ? (
                    <ol>
                      {flow.steps.map((step) => <li key={step}>{step}</li>)}
                    </ol>
                  ) : null}
                </article>
              ))}
            </div>
          ) : null}
          {handoff.task_changed_files.length ? (
            <details className="handoff-files">
              <summary>Changed files <strong>{handoff.task_changed_files.length}</strong></summary>
              <ul>
                {handoff.task_changed_files.map((path) => <li key={path}>{path}</li>)}
              </ul>
            </details>
          ) : null}
        </section>
      )}

      <section className="product-handoff-section" aria-labelledby="productLaunchHeading">
        <div className="handoff-section-heading">
          <h3 id="productLaunchHeading">Launch</h3>
          <span>{hasLaunch ? `${handoff.launch.length} command${handoff.launch.length === 1 ? "" : "s"}` : "not declared"}</span>
        </div>
        {hasLaunch ? (
          <CommandList commands={handoff.launch} />
        ) : (
          <p>{productKindHint(handoff.kind)}</p>
        )}
        {hasUrls && (
          <div className="handoff-links" aria-label="Product URLs">
            {handoff.urls.map((url) => (
              <a href={url} target="_blank" rel="noreferrer" key={url}>{url}</a>
            ))}
          </div>
        )}
      </section>

      {handoff.try_flows.length ? (
        <section className="product-handoff-section" aria-labelledby="productFlowsHeading">
          <div className="handoff-section-heading">
            <h3 id="productFlowsHeading">Additional checks</h3>
            <span>{handoff.try_flows.length} flow{handoff.try_flows.length === 1 ? "" : "s"}</span>
          </div>
          <div className="handoff-flow-list">
            {handoff.try_flows.map((flow, index) => (
              <article className="handoff-flow" key={`${flow.title}-${index}`}>
                <strong>{flow.title}</strong>
                {flow.steps.length ? (
                  <ol>
                    {flow.steps.map((step) => <li key={step}>{step}</li>)}
                  </ol>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {hasSamples && (
        <section className="product-handoff-section" aria-labelledby="productSampleHeading">
          <div className="handoff-section-heading">
            <h3 id="productSampleHeading">Sample data</h3>
            <span>{handoff.sample_data.length} item{handoff.sample_data.length === 1 ? "" : "s"}</span>
          </div>
          <div className="handoff-samples">
            {handoff.sample_data.map((sample, index) => (
              <div key={`${sample.label}-${sample.value}-${index}`}>
                <span>{sample.label}</span>
                <strong>{sample.value}</strong>
                {sample.detail ? <p>{sample.detail}</p> : null}
              </div>
            ))}
          </div>
        </section>
      )}

      {(hasReset || handoff.notes.length > 0) && (
        <section className="product-handoff-section" aria-labelledby="productOpsHeading">
          <div className="handoff-section-heading">
            <h3 id="productOpsHeading">Reset and notes</h3>
            <span>{hasReset ? `${handoff.reset.length} reset command${handoff.reset.length === 1 ? "" : "s"}` : "notes"}</span>
          </div>
          {hasReset ? <CommandList commands={handoff.reset} /> : null}
          {handoff.notes.length ? (
            <ul className="handoff-notes">
              {handoff.notes.map((note) => <li key={note}>{note}</li>)}
            </ul>
          ) : null}
        </section>
      )}
    </div>
  );
}

function productDemoVideoArtifact(artifacts: ArtifactRef[]): ArtifactRef | null {
  const videos = artifacts.filter((artifact) => {
    const text = `${artifact.label} ${artifact.path}`.toLowerCase();
    return artifact.exists && (artifact.kind.toLowerCase() === "video" || /\.(webm|mp4|mov|m4v)$/i.test(text));
  });
  return videos.find((artifact) => /demo|recording|walkthrough|proof/.test(`${artifact.label} ${artifact.path}`.toLowerCase())) || videos[0] || null;
}

function productScreenshotArtifacts(artifacts: ArtifactRef[]): ArtifactRef[] {
  return artifacts.filter((artifact) => {
    const text = `${artifact.label} ${artifact.path}`.toLowerCase();
    return artifact.exists && (artifact.kind.toLowerCase() === "image" || /\.(png|jpe?g|gif|webp)$/i.test(text));
  });
}

function artifactRawUrl(runId: string, index: number): string {
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${index}/raw`;
}

function videoMimeType(artifact: ArtifactRef): string {
  const text = `${artifact.label} ${artifact.path}`.toLowerCase();
  if (text.endsWith(".mp4") || text.endsWith(".m4v")) return "video/mp4";
  if (text.endsWith(".mov")) return "video/quicktime";
  return "video/webm";
}

function proofAssetUrl(runId: string, href: string): string {
  return `/api/runs/${encodeURIComponent(runId)}/proof-assets/${encodeURIComponent(href)}`;
}

function isDemoVideo(item: {kind?: string; name?: string} | null): boolean {
  const text = `${item?.kind || ""} ${item?.name || ""}`.toLowerCase();
  return text.includes("video") || /\.(webm|mp4|mov|m4v)$/i.test(text);
}

function isDemoImage(item: {kind?: string; name?: string} | null): boolean {
  const text = `${item?.kind || ""} ${item?.name || ""}`.toLowerCase();
  return text.includes("image") || /\.(png|jpe?g|gif|webp)$/i.test(text);
}

function demoVideoMimeType(item: {name?: string}): string {
  const name = String(item.name || "").toLowerCase();
  if (name.endsWith(".mp4") || name.endsWith(".m4v")) return "video/mp4";
  if (name.endsWith(".mov")) return "video/quicktime";
  return "video/webm";
}

function demoUnavailableCopy(handoff: ProductHandoff, certificationMode: string, reason = ""): string {
  if (reason) return reason;
  const mode = certificationMode.toLowerCase();
  const kind = handoff.kind.toLowerCase();
  if (mode === "fast") {
    return "Fast certification does not record video. Use the flows below to reproduce the smoke-tested behavior.";
  }
  if (/(api|cli|library|worker|pipeline|service)/.test(kind)) {
    return "This product is best demonstrated through commands, requests, outputs, and files rather than video.";
  }
  return "No demo video was recorded for this run. Use the flows below to reproduce the feature and the proof report to audit what was verified.";
}

function ArtifactFrame({rawUrl, label, mime, testId}: {
  rawUrl: string;
  label: string;
  mime: string;
  testId: string;
}) {
  const isHtml = mime.toLowerCase().includes("html");
  return (
    <div className="artifact-frame-preview" data-testid={`${testId}-wrap`}>
      <div className="artifact-frame-actions">
        <span>{mime || "rendered artifact"}</span>
        <a href={rawUrl} target="_blank" rel="noreferrer" data-testid={`${testId}-open`}>Open in new tab</a>
      </div>
      <iframe
        src={rawUrl}
        title={label}
        data-testid={testId}
        className="artifact-frame"
        sandbox={isHtml ? "allow-same-origin allow-scripts" : undefined}
      />
    </div>
  );
}

export function productHandoffFor(detail: RunDetail): ProductHandoff {
  const handoff = detail.review_packet.product_handoff;
  if (handoff) return handoff;
  return {
    kind: "unknown",
    label: "Product handoff",
    source: "not declared",
    source_path: null,
    root: detail.worktree || detail.cwd || detail.project_dir || "",
    summary: "No product handoff was attached to this run.",
    preview_available: false,
    preview_label: "Preview product",
    preview_reason: "No product URL or launch command was recorded for this run.",
    task_summary: detail.summary_lines?.[0] || detail.title || detail.run_id,
    task_status: detail.display_status || detail.status || null,
    task_branch: detail.branch || null,
    task_changed_files: detail.review_packet.changes.files || [],
    task_flows: [],
    urls: [],
    launch: [],
    reset: [],
    try_flows: [],
    sample_data: [],
    notes: ["See README and logs to run."],
  };
}

export function LogPane({logState, runActive, onRetry}: {logState: LogState; runActive: boolean; onRetry: () => void}) {
  const {text: rawText, status, error, path, totalBytes, totalLines, droppedBytes, lastUpdatedAt, pollIntervalMs} = logState;
  // mc-audit redesign §5 W5.7: heartbeat filter + jump-to-verdict.
  // Heartbeats are progress-tick lines ("⋯ building… (40s) · …", repeated
  // every 20s). They drown out semantic events. Default-on for terminal
  // runs (the user is reviewing a finished log); off for active runs (the
  // user wants live ticks).
  const [hideHeartbeats, setHideHeartbeats] = useState<boolean>(!runActive);
  useEffect(() => {
    // When run flips active->inactive, default the filter ON to declutter
    // the post-run review. Don't flip the user's explicit choice while
    // active.
    if (!runActive) setHideHeartbeats(true);
  }, [runActive]);
  const text = useMemo(() => {
    if (!hideHeartbeats || !rawText) return rawText;
    // A heartbeat line is one starting with `[+H:MM] ⋯` (or just ⋯).
    // Strip them but keep newlines so layout doesn't collapse.
    return rawText
      .split("\n")
      .filter((line) => !/^\[\+\d+:\d+\]\s*⋯/.test(line))
      .join("\n");
  }, [rawText, hideHeartbeats]);
  // Display lines are derived from the unbounded `totalLines` counter so the
  // header reflects the *full* log size, not just what fits in the tail
  // buffer. We never re-split the buffer per render — that's the whole bug.
  const displayLines = totalLines > 0 ? totalLines : (text ? 1 : 0);
  const headerStatus = describeLogHeader({runActive, status, lastUpdatedAt, pollIntervalMs, displayLines, totalBytes});
  const droppedNote = droppedBytes > 0 ? `${humanBytes(droppedBytes)} earlier bytes elided` : null;

  // Heavy-user paper-cut #6 (log search). Local state — match index advances
  // through the highlighted regions; Enter / Shift+Enter step through them;
  // Cmd-F / `/` focuses the search box. The search box is only meaningful
  // when there's text to search, so we render it inside the populated body.
  const [search, setSearch] = useState("");
  const [matchIdx, setMatchIdx] = useState(0);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  // Reset selection when the query changes; keep when the buffer grows so
  // an active highlight doesn't snap back to 0 every poll tick.
  useEffect(() => {
    setMatchIdx(0);
  }, [search]);
  const matchCount = useMemo(() => {
    if (!search || !text) return 0;
    const needle = search.toLowerCase();
    const haystack = text.toLowerCase();
    let count = 0;
    let cursor = 0;
    while (cursor < haystack.length) {
      const found = haystack.indexOf(needle, cursor);
      if (found < 0) break;
      count += 1;
      cursor = found + Math.max(1, needle.length);
    }
    return count;
  }, [text, search]);
  const focusSearch = useCallback(() => {
    searchInputRef.current?.focus();
    searchInputRef.current?.select();
  }, []);
  const stepMatch = useCallback((dir: 1 | -1) => {
    if (!matchCount) return;
    setMatchIdx((prev) => (prev + dir + matchCount) % matchCount);
  }, [matchCount]);
  // Local Cmd-F / "/" interception. Only when this LogPane is mounted +
  // the inspector body has focus — we attach the listener on the
  // container so it doesn't fight global Cmd-K. Plain `/` only triggers
  // when the user is NOT typing in another input.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onKey = (event: KeyboardEvent) => {
      const cmdF = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f";
      if (cmdF) {
        event.preventDefault();
        focusSearch();
        return;
      }
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        focusSearch();
      }
    };
    container.addEventListener("keydown", onKey);
    return () => container.removeEventListener("keydown", onKey);
  }, [focusSearch]);

  // Empty/missing/error rendering — these states replace the bare "waiting
  // for output" placeholder with state-specific copy + a recovery action.
  let body: ReactNode;
  if (status === "missing") {
    body = (
      <div className="log-empty" data-testid="log-empty-missing">
        {path ? `No log file at ${path}.` : "Log will appear when the agent starts writing."}
      </div>
    );
  } else if (status === "error") {
    body = (
      <div className="log-empty log-error" data-testid="log-empty-error">
        <p>Could not read log{error ? `: ${error}` : "."}</p>
        <button type="button" data-testid="log-retry-button" onClick={onRetry}>Retry</button>
      </div>
    );
  } else if (!text) {
    body = (
      <div className="log-empty" data-testid="log-empty-waiting">
        {status === "loading" ? "Loading log…" : "Log will appear when the agent starts writing."}
      </div>
    );
  } else if (search) {
    body = (
      <pre
        className="log-pane log-content"
        tabIndex={0}
        aria-label="Run log output"
        data-testid="run-log-pane"
      >{renderLogTextWithHighlight(text, search, matchIdx)}</pre>
    );
  } else {
    body = (
      <pre
        className="log-pane log-content"
        tabIndex={0}
        aria-label="Run log output"
        data-testid="run-log-pane"
      >{renderLogText(text)}</pre>
    );
}

  const jumpToEnd = useCallback(() => {
    const pre = containerRef.current?.querySelector<HTMLElement>('[data-testid="run-log-pane"]');
    if (pre) pre.scrollTop = pre.scrollHeight;
  }, []);
  return (
    <div className="log-viewer" ref={containerRef}>
      <div className="log-toolbar">
        <strong>Run logs</strong>
        <span data-testid="log-pane-status">{headerStatus}</span>
        {droppedNote && (
          <span className="log-elided" data-testid="log-pane-elided">{droppedNote}</span>
        )}
        {rawText ? (
          <>
            <label className="log-toolbar-toggle" data-testid="log-toggle-heartbeats" title="Hide repeated progress ticks like '⋯ building… (40s)'">
              <input
                type="checkbox"
                checked={hideHeartbeats}
                onChange={(event) => setHideHeartbeats(event.target.checked)}
              />
              {" "}Hide heartbeats
            </label>
            <button
              type="button"
              className="log-toolbar-jump"
              data-testid="log-jump-to-end"
              onClick={jumpToEnd}
              title="Scroll to the verdict/end of the log"
            >Jump to end</button>
          </>
        ) : null}
      </div>
      {/* Heavy-user paper-cut #6: in-pane search. Always visible whenever
          there's a populated log buffer so the user doesn't have to discover
          a hidden affordance. We hide it when the body is in an empty/error
          state — there's nothing to search and the input would be confusing. */}
      {(status === "ok" || (text && status !== "missing" && status !== "error")) && (
        <div className="log-search" data-testid="log-search">
          <input
            ref={searchInputRef}
            value={search}
            type="search"
            placeholder="Search log (Cmd-F / /)"
            data-testid="log-search-input"
            aria-label="Search within log"
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                stepMatch(event.shiftKey ? -1 : 1);
              } else if (event.key === "Escape") {
                event.preventDefault();
                setSearch("");
                searchInputRef.current?.blur();
              }
            }}
          />
          <span className="log-search-count" data-testid="log-search-count">
            {search
              ? matchCount
                ? `${matchIdx + 1} / ${matchCount}`
                : "0 matches"
              : ""}
          </span>
          <button
            type="button"
            data-testid="log-search-prev"
            disabled={!matchCount}
            aria-label="Previous match"
            onClick={() => stepMatch(-1)}
          >Prev</button>
          <button
            type="button"
            data-testid="log-search-next"
            disabled={!matchCount}
            aria-label="Next match"
            onClick={() => stepMatch(1)}
          >Next</button>
        </div>
      )}
      {body}
    </div>
  );
}

export function renderLogTextWithHighlight(text: string, needle: string, activeMatchIdx: number) {
  if (!needle) return renderLogText(text);
  const lower = text.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let matchIndex = 0;
  let key = 0;
  while (cursor < text.length) {
    const found = lower.indexOf(lowerNeedle, cursor);
    if (found < 0) {
      nodes.push(<span key={`s-${key++}`}>{text.slice(cursor)}</span>);
      break;
    }
    if (found > cursor) {
      nodes.push(<span key={`s-${key++}`}>{text.slice(cursor, found)}</span>);
    }
    const segment = text.slice(found, found + needle.length);
    const isActive = matchIndex === activeMatchIdx;
    nodes.push(
      <mark
        key={`m-${key++}`}
        className={`log-search-match ${isActive ? "active" : ""}`}
        data-testid={isActive ? "log-search-match-active" : "log-search-match"}
        ref={isActive ? (el) => {
          if (el && typeof el.scrollIntoView === "function") {
            el.scrollIntoView({block: "center", inline: "nearest"});
          }
        } : undefined}
      >{segment}</mark>,
    );
    cursor = found + Math.max(1, needle.length);
    matchIndex += 1;
  }
  return nodes;
}

export function describeLogHeader({runActive, status, lastUpdatedAt, pollIntervalMs, displayLines, totalBytes}: {
  runActive: boolean;
  status: LogStatus;
  lastUpdatedAt: number | null;
  pollIntervalMs: number;
  displayLines: number;
  totalBytes: number;
}): string {
  if (runActive) {
    const cadence = (pollIntervalMs / 1000).toFixed(pollIntervalMs >= 10_000 ? 0 : 1);
    if (lastUpdatedAt === null) return `Live · polling every ${cadence}s`;
    const ageSec = Math.max(0, Math.round((Date.now() - lastUpdatedAt) / 1000));
    return `Live · polling every ${cadence}s · last update ${ageSec}s ago`;
  }
  if (status === "missing") return "No log file";
  if (displayLines === 0 && totalBytes === 0) return "waiting for output";
  return `Final · ${displayLines.toLocaleString()} line${displayLines === 1 ? "" : "s"} · ${humanBytes(totalBytes)}`;
}

export function ProofPane({detail, onShowDiff, onShowArtifacts}: {
  detail: RunDetail;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
}) {
  const packet = detail.review_packet;
  const changedFiles = packet.changes.files.slice(0, 10);
  const evidence = packet.evidence.filter(isReadableArtifact);
  const stories = packet.certification.stories || [];
  const rounds = packet.certification.rounds || [];
  const demoEvidence = packet.certification.demo_evidence;
  const proofReport = packet.certification.proof_report;
  const proofChecks = packet.failure ? packet.checks.filter((check) => check.key !== "run" && check.key !== "landing") : packet.checks;
  const visibleChecks = proofChecks.filter((check) => !["run", "landing"].includes(check.key)).slice(0, 5);
  const keyEvidenceCount = keyReviewEvidenceArtifacts(evidence).length;
  return (
    <div className="proof-pane" data-testid="proof-pane">
      <div className="proof-summary" aria-labelledby="proofHeading">
        <div>
          <span>{packet.readiness.label}</span>
          <h3 id="proofHeading">Review summary</h3>
          <p>{packet.headline}</p>
        </div>
        <div className="proof-metrics">
          <ReviewMetric label="Stories" value={storiesLine(packet)} />
          <ReviewMetric label="Changes" value={packet.changes.file_count ? `${packet.changes.file_count} file${packet.changes.file_count === 1 ? "" : "s"}` : "-"} />
          <ReviewMetric label="Demo" value={demoEvidenceLine(packet)} />
          <ReviewMetric label="Evidence" value={reviewEvidenceLine(packet)} />
        </div>
      </div>
      <DemoEvidenceSection detail={detail} demo={demoEvidence} />
      <div className="proof-section" aria-labelledby="proofNextHeading">
        <h3 id="proofNextHeading">Next action</h3>
        <p>{packet.readiness.next_step}</p>
        <div className="proof-report-actions">
          {proofReport?.html_url ? (
            <a href={proofReport.html_url} target="_blank" rel="noreferrer" data-testid="proof-report-link">Open HTML proof report</a>
          ) : (
            <span>No HTML proof report is linked for this run.</span>
          )}
          <button type="button" data-testid="proof-open-artifacts-button" onClick={onShowArtifacts}>
            {keyEvidenceCount ? "Open evidence artifacts" : "Open artifacts"}
          </button>
          {canShowDiff(detail) ? (
            <button type="button" data-testid="proof-open-diff-button" onClick={onShowDiff}>Open code diff</button>
          ) : null}
        </div>
      </div>
      {rounds.length > 1 && <CertificationRoundTabs rounds={rounds} />}
      {packet.failure && (
        <div className="proof-section proof-failure" aria-labelledby="proofFailureHeading">
          <h3 id="proofFailureHeading">What failed</h3>
          <FailureSummary failure={packet.failure} showExcerpt />
        </div>
      )}
      <div className="proof-section" aria-labelledby="proofChecksHeading">
        <h3 id="proofChecksHeading">Verification</h3>
        {visibleChecks.length ? (
          <div className="proof-checks">
            {visibleChecks.map((check) => (
              <div className={`review-check check-${String(check.status || "").trim().toLowerCase()}`} key={check.key}>
                <CheckStatusBadge status={check.status} />
                <div>
                  <strong>{check.label}</strong>
                  <p>{formatReviewText(check.detail)}</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p>No additional verification checks were recorded for this run.</p>
        )}
      </div>
      <div className="proof-section" aria-labelledby="proofStoriesHeading">
        <h3 id="proofStoriesHeading">Stories tested</h3>
        {stories.length ? (
          <div className="proof-stories" data-testid="proof-story-list">
            {stories.map((story) => (
              <article className={`proof-story story-${storyStatusClass(story.status)}`} key={story.id || story.title}>
                <span>
                  <span className="status-icon" aria-hidden="true">{storyStatusIcon(story.status)}</span>
                  {" "}
                  {storyStatusLabel(story.status)}
                </span>
                <div>
                  <strong>{story.title || story.id}</strong>
                  {story.detail ? <p>{formatReviewText(story.detail)}</p> : null}
                  <small>{[story.id, story.methodology, story.surface].filter(Boolean).join(" · ")}</small>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p>No per-story certification details were recorded. Open the HTML report or summary artifact if available.</p>
        )}
      </div>
      <div className="proof-section" aria-labelledby="proofFilesHeading">
        <h3 id="proofFilesHeading">What changed</h3>
        {changedFiles.length ? (
          <ul className="proof-files">
            {changedFiles.map((path) => <li key={path}>{path}</li>)}
            {packet.changes.truncated && <li>more files not shown</li>}
          </ul>
        ) : (
          <p>No changed files reported yet.</p>
        )}
      </div>
      {packet.changes.diff_error ? (
        <div className="proof-section proof-failure" aria-labelledby="proofDiffIssueHeading">
          <h3 id="proofDiffIssueHeading">Diff issue</h3>
          <p>{formatTechnicalIssue(packet.changes.diff_error)}</p>
        </div>
      ) : null}
      <details className="proof-section proof-provenance-details">
        <summary>
          <span>Proof metadata</span>
          <strong>{proofReport?.sha256 ? proofReport.sha256.slice(0, 12) : detail.run_id}</strong>
        </summary>
        <ProofProvenance proofReport={proofReport} runId={detail.run_id} />
      </details>
    </div>
  );
}

function DemoEvidenceSection({detail, demo}: {
  detail: RunDetail;
  demo: RunDetail["review_packet"]["certification"]["demo_evidence"];
}) {
  if (!demo) return null;
  const primary = demo.primary_demo;
  const primaryUrl = primary?.href ? proofAssetUrl(detail.run_id, primary.href) : "";
  const visibleStories = (demo.stories || []).slice(0, 8);
  const statusClass = demoStatusClass(demo.demo_status);
  return (
    <div className={`proof-section demo-evidence-section demo-${statusClass}`} aria-labelledby="demoEvidenceHeading" data-testid="demo-evidence-section">
      <div className="demo-evidence-head">
        <div>
          <h3 id="demoEvidenceHeading">Demo proof</h3>
          <p>{demo.demo_reason || "Structured demo proof was not recorded for this run."}</p>
        </div>
        <span className={`demo-status-pill demo-${statusClass}`}>
          {demoStatusLabel(demo.demo_status)} · {demoKindLabel(demo.app_kind)}
        </span>
      </div>
      {primary && primaryUrl ? (
        <div className="demo-primary-preview" data-testid="demo-primary-preview">
          <div>
            <strong>{primary.name || "Primary proof"}</strong>
            {primary.caption ? <span>{primary.caption}</span> : null}
          </div>
          {isDemoVideo(primary) ? (
            <video controls>
              <source src={primaryUrl} type={demoVideoMimeType(primary)} />
            </video>
          ) : isDemoImage(primary) ? (
            <a href={primaryUrl} target="_blank" rel="noreferrer">
              <img src={primaryUrl} alt={primary.name || "primary proof"} />
            </a>
          ) : (
            <a href={primaryUrl} target="_blank" rel="noreferrer">Open primary proof</a>
          )}
        </div>
      ) : null}
      {visibleStories.length ? (
        <div className="demo-story-grid" aria-label="Story proof coverage">
          {visibleStories.map((story) => (
            <article className="demo-story-proof" key={story.id || story.title}>
              <strong>{story.title || story.id}</strong>
              <span>{story.proof_level || "not recorded"}</span>
              <small>
                {[
                  story.needs_visual ? "visual expected" : "",
                  story.needs_file_validation ? "file validation expected" : "",
                  story.has_file_validation ? "file validated" : "",
                  story.has_text_evidence ? "notes" : "",
                ].filter(Boolean).join(" · ") || "no extra proof metadata"}
              </small>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function keyReviewEvidenceArtifacts(evidence: ArtifactRef[]): ArtifactRef[] {
  const seen = new Set<number>();
  return evidence.filter((artifact) => {
    if (!isReadableArtifact(artifact)) return false;
    if (seen.has(artifact.index)) return false;
    const text = `${artifact.label} ${artifact.path}`.toLowerCase();
    const isKeyArtifact = (
      isProofReportArtifact(artifact)
      || /proof|summary/i.test(`${artifact.label} ${artifact.path}`)
      || ["image", "video"].includes(artifact.kind.toLowerCase())
      || /\.(png|jpe?g|gif|webp|webm|mp4)$/i.test(text)
    );
    if (!isKeyArtifact) return false;
    seen.add(artifact.index);
    return true;
  });
}

function reviewEvidenceLine(packet: RunDetail["review_packet"]): string {
  if (packet.readiness.state === "in_progress") return "-";
  if (isRepositoryBlockedPacket(packet)) return "-";
  const readable = packet.evidence.filter(isReviewEvidenceArtifact).filter(isReadableArtifact);
  if (!readable.length) return "not attached";
  const keyCount = keyReviewEvidenceArtifacts(packet.evidence).length;
  if (!keyCount) return "attached";
  return `${keyCount} item${keyCount === 1 ? "" : "s"}`;
}

function demoEvidenceLine(packet: RunDetail["review_packet"]): string {
  const demo = packet.certification.demo_evidence;
  if (!demo) return "-";
  return demoStatusLabel(demo.demo_status);
}

function demoStatusLabel(status: string | null | undefined): string {
  switch (String(status || "").toLowerCase()) {
    case "strong":
      return "Strong";
    case "partial":
      return "Partial";
    case "missing":
      return "Missing";
    case "not_applicable":
      return "Not needed";
    default:
      return "Unknown";
  }
}

function demoStatusClass(status: string | null | undefined): string {
  const normalized = String(status || "").toLowerCase();
  if (["strong", "partial", "missing", "not_applicable"].includes(normalized)) {
    return normalized.replace("_", "-");
  }
  return "unknown";
}

function demoKindLabel(kind: string | null | undefined): string {
  switch (String(kind || "").toLowerCase()) {
    case "web":
      return "Web UI";
    case "mixed":
      return "Web + export";
    case "file_export":
      return "File/export";
    case "api":
      return "API";
    case "cli":
      return "CLI";
    case "library":
      return "Library";
    case "worker":
      return "Worker";
    default:
      return "Unknown";
  }
}

export function ProofProvenance({proofReport, runId}: {proofReport: ProofReportInfo; runId: string}) {
  if (!proofReport || !proofReport.available) return null;
  const sha = proofReport.sha256 ? proofReport.sha256.slice(0, 12) : null;
  const mismatch = proofReport.run_id_matches === false;
  const branch = proofReport.branch;
  const head = proofReport.head_sha ? proofReport.head_sha.slice(0, 7) : null;
  return (
    <div className="proof-provenance" data-testid="proof-provenance" aria-label="Proof of work provenance">
      {mismatch && (
        <div className="proof-provenance-warning" data-testid="proof-provenance-mismatch" role="alert">
          ⚠ Proof report records run {proofReport.run_id || "unknown"}, but this view is run {runId}. The evidence below may not belong to this run.
        </div>
      )}
      <dl className="proof-provenance-meta">
        {proofReport.generated_at && <><dt>Generated</dt><dd data-testid="proof-generated-at">{proofReport.generated_at}</dd></>}
        {proofReport.file_mtime && <><dt>File mtime</dt><dd data-testid="proof-file-mtime">{proofReport.file_mtime}</dd></>}
        {proofReport.run_id && <><dt>Run id</dt><dd data-testid="proof-run-id">{proofReport.run_id}</dd></>}
        {proofReport.session_id && <><dt>Session</dt><dd data-testid="proof-session-id">{proofReport.session_id}</dd></>}
        {branch && <><dt>Branch</dt><dd data-testid="proof-branch">{branch}</dd></>}
        {head && <><dt>HEAD</dt><dd data-testid="proof-head-sha" title={proofReport.head_sha || ""}>{head}</dd></>}
        {sha && <><dt>SHA-256</dt><dd data-testid="proof-sha256" title={proofReport.sha256 || ""}>{sha}</dd></>}
      </dl>
    </div>
  );
}

export function CertificationRoundTabs({rounds}: {rounds: CertificationRound[]}) {
  const [activeRound, setActiveRound] = useState<number>(rounds[rounds.length - 1]?.round ?? 1);
  const active = rounds.find((entry) => entry.round === activeRound) || rounds[rounds.length - 1];
  return (
    <div className="proof-section proof-rounds" data-testid="proof-round-tabs" aria-labelledby="proofRoundsHeading">
      <h3 id="proofRoundsHeading">Certify rounds</h3>
      <div className="proof-round-tablist" role="tablist">
        {rounds.map((round) => {
          const label = `Round ${round.round ?? "?"}`;
          const verdictClass = round.verdict.toLowerCase() === "passed" ? "passed" : round.verdict.toLowerCase() === "failed" ? "failed" : "unknown";
          return (
            <button
              key={`round-${round.round}`}
              type="button"
              role="tab"
              aria-selected={round.round === active?.round}
              data-testid={`proof-round-tab-${round.round}`}
              className={`proof-round-tab proof-round-${verdictClass} ${round.round === active?.round ? "active" : ""}`}
              onClick={() => setActiveRound(round.round ?? 1)}
            >
              <strong>{label}</strong>
              <span>{round.verdict.toUpperCase()}</span>
              {round.duration_human && <small>{round.duration_human}</small>}
            </button>
          );
        })}
      </div>
      {active && (
        <div className="proof-round-detail" data-testid={`proof-round-detail-${active.round}`}>
          <dl className="proof-round-meta">
            <dt>Verdict</dt><dd data-testid="proof-round-verdict">{active.verdict}</dd>
            {active.stories_tested != null && (<><dt>Stories</dt><dd data-testid="proof-round-stories">{active.passed_count ?? 0} passed / {active.failed_count ?? 0} failed / {active.warn_count ?? 0} warn / {active.stories_tested} tested</dd></>)}
            {active.duration_human && (<><dt>Duration</dt><dd data-testid="proof-round-duration">{active.duration_human}</dd></>)}
          </dl>
          {active.diagnosis && <p className="proof-round-diagnosis" data-testid="proof-round-diagnosis">{active.diagnosis}</p>}
          {active.failing_story_ids.length > 0 && (
            <div className="proof-round-stories-list">
              <strong>Failing:</strong>
              <ul>{active.failing_story_ids.map((id) => <li key={id} data-testid={`proof-round-failing-${id}`}>{id}</li>)}</ul>
            </div>
          )}
          {active.fix_commits.length > 0 && (
            <div className="proof-round-fix-commits">
              <strong>Fix commits:</strong>
              <ul>{active.fix_commits.map((commit) => <li key={commit}><code>{commit}</code></li>)}</ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function DiffPane({diff, onRefresh}: {diff: DiffResponse | null; onRefresh: () => void}) {
  // All hooks must run on every render — bail-out branches must come AFTER
  // the hook calls or React throws "Rendered more hooks than previous"
  // (#310). Order matters here.
  const sections = useMemo(() => splitDiffIntoFiles(diff?.text || "", diff?.files || []), [diff?.text, diff?.files]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  // Re-render the "captured X ago" relative time once a second so the
  // header doesn't lie when the operator stares at the panel.
  const [, setNowTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setNowTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    setSelectedPath(sections[0]?.path || null);
  }, [diff?.run_id, diff?.text, sections]);
  const command = diff?.command || null;
  const copyCommand = useCallback(() => {
    if (!command) return;
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(command);
    }
  }, [command]);
  const selected = sections.find((section) => section.path === selectedPath) || sections[0] || null;
  if (!diff) {
    return (
      <div className="diff-viewer" data-testid="diff-pane">
        <div className="diff-toolbar"><strong>Code diff</strong><span>loading</span></div>
        <pre className="diff-pane">Loading diff...</pre>
      </div>
    );
  }
  const targetShaShort = diff.target_sha ? diff.target_sha.slice(0, 7) : null;
  const branchShaShort = diff.branch_sha ? diff.branch_sha.slice(0, 7) : null;
  const mergeBaseShort = diff.merge_base ? diff.merge_base.slice(0, 7) : null;
  const ageLabel = diff.fetched_at ? formatRelativeFreshness(diff.fetched_at) : null;
  const truncationBanner = diff.truncated
    ? formatDiffTruncationBanner(diff)
    : null;
  return (
    <div className="diff-viewer" data-testid="diff-pane">
      <div className="diff-freshness" data-testid="diff-freshness">
        <div className="diff-freshness-meta">
          {ageLabel && <span data-testid="diff-fetched-at">Captured {ageLabel}</span>}
          {targetShaShort ? (
            <span data-testid="diff-target-sha" title={diff.target_sha || ""}>target {diff.target} @ {targetShaShort}</span>
          ) : (
            <span className="diff-warning" data-testid="diff-target-sha-missing">⚠ Could not resolve target SHA; diff may be stale.</span>
          )}
          {diff.branch && diff.branch !== diff.target ? (
            branchShaShort ? (
              <span data-testid="diff-branch-sha" title={diff.branch_sha || ""}>branch {diff.branch} @ {branchShaShort}</span>
            ) : (
              <span className="diff-warning" data-testid="diff-branch-sha-missing">⚠ Could not resolve branch SHA; diff may be stale.</span>
            )
          ) : null}
          {mergeBaseShort ? (
            <span data-testid="diff-merge-base" title={diff.merge_base || ""}>base {mergeBaseShort}</span>
          ) : null}
        </div>
        <button
          type="button"
          className="diff-refresh-button"
          data-testid="diff-refresh-button"
          onClick={onRefresh}
        >
          Refresh
        </button>
      </div>
      <div className="diff-toolbar">
        <strong>Code diff</strong>
        <span title={`${diff.branch || "branch"} → ${diff.target || "target"}`}>
          {diff.branch || "branch"} → {diff.target || "target"}
        </span>
      </div>
      {truncationBanner ? (
        <div className="diff-truncation" data-testid="diff-truncation">
          <span>{truncationBanner}</span>
          {diff.command ? (
            <button type="button" data-testid="diff-copy-command-button" onClick={copyCommand}>
              Copy diff command
            </button>
          ) : null}
        </div>
      ) : null}
      {diff.error ? <div className="diff-error">{formatTechnicalIssue(diff.error)}</div> : null}
      <div className="diff-layout">
        {sections.length ? (
          <nav className="diff-file-list" aria-label="Changed files in diff" data-testid="diff-file-list">
            {sections.map((section) => (
              <button
                className={section.path === selected?.path ? "selected" : ""}
                type="button"
                key={section.path}
                onClick={() => setSelectedPath(section.path)}
              >
                {section.path}
              </button>
            ))}
          </nav>
        ) : null}
        <div className="diff-file-view">
          <div className="diff-file-heading" data-testid="diff-selected-file">
            <strong>{selected?.path || "No changed file selected"}</strong>
            <span>{sections.length ? `${sections.length} file${sections.length === 1 ? "" : "s"}` : "empty diff"}</span>
          </div>
          <pre className="diff-pane" tabIndex={0} aria-label="Code diff output">{selected?.text ? renderDiffText(selected.text) : "No diff content."}</pre>
        </div>
      </div>
    </div>
  );
}

export function ReviewPacket({packet, detail, onRunAction, onShowProof, onShowDiff, onShowArtifacts}: {
  packet: RunDetail["review_packet"];
  detail: RunDetail;
  onRunAction: (action: string, label?: string) => void;
  onShowProof: () => void;
  onShowDiff: () => void;
  onShowArtifacts: () => void;
}) {
  const action = packet.next_action;
  const blockers = packet.readiness.blockers || [];
  const inProgress = packet.readiness.state === "in_progress";
  const artifactCount = packet.evidence.filter(isReadableArtifact).length;
  const showActionButton = Boolean(action.action_key);
  const hasFailure = Boolean(packet.failure);
  const attentionChecks = packet.checks.filter((check) => isAttentionCheckStatus(check.status));
  const pendingChecks = packet.checks.filter((check) => isPendingCheckStatus(check.status));
  const drawerChecks = attentionChecks.length ? attentionChecks : pendingChecks;
  const checksDefaultOpen = hasFailure || attentionChecks.length > 0;
  const checkSummary = attentionChecks.length
    ? `${attentionChecks.length} need review`
    : pendingChecks.length
      ? `${pendingChecks.length} pending`
    : `${packet.checks.length} recorded`;
  const showNextStep = packet.readiness.state !== "merged" || showActionButton || hasFailure || blockers.length > 0;
  const canOpenDiff = canShowDiff(detail);
  return (
    <div className={`review-packet review-${packet.readiness.tone || "info"}`}>
      <div className="review-head">
        <div>
          <span className="review-kicker">{packet.readiness.label}</span>
          <strong>{packet.headline}</strong>
          <span title={packet.summary}>{packet.summary}</span>
        </div>
        {showActionButton && (
          <button
            className={action.enabled ? "primary" : ""}
            type="button"
            data-testid="review-next-action-button"
            disabled={!action.enabled || !action.action_key}
            title={action.reason || ""}
            onClick={() => action.action_key && onRunAction(actionName(action.action_key), action.label)}
          >
            {reviewActionLabel(action.label)}
          </button>
        )}
      </div>
      {packet.failure && <FailureSummary failure={packet.failure} />}
      {showNextStep && (
        <div className="review-next-step">
          <strong>Next</strong>
          <span>{packet.readiness.next_step}</span>
        </div>
      )}
      {!hasFailure && blockers.length > 0 && (
        <ul className="review-blockers" aria-label="Review blockers">
          {blockers.map((blocker) => <li key={blocker}>{formatReviewText(blocker)}</li>)}
        </ul>
      )}
      <div className={`review-grid ${packet.readiness.state === "merged" || inProgress ? "review-grid-wide" : ""}`}>
        <ReviewMetric label="Stories" value={storiesLine(packet)} onClick={onShowProof} title="Open review result" testId="review-metric-stories" />
        <ReviewMetric
          label="Files"
          value={packet.changes.file_count ? `${packet.changes.file_count} file${packet.changes.file_count === 1 ? "" : "s"}` : "-"}
          onClick={canOpenDiff ? onShowDiff : undefined}
          title={canOpenDiff ? "Open code changes" : diffDisabledReason(detail)}
          testId="review-metric-files"
        />
        <ReviewMetric label="Proof" value={demoEvidenceLine(packet)} onClick={onShowProof} title="Open proof" testId="review-metric-proof" />
        {(packet.readiness.state === "merged" || inProgress) && (
          <ReviewMetric
            label="Artifacts"
            value={artifactCount ? `${artifactCount} file${artifactCount === 1 ? "" : "s"}` : "-"}
            onClick={artifactCount ? onShowArtifacts : undefined}
            title={artifactCount ? "Open artifact bundle" : "No artifacts are attached"}
            testId="review-metric-artifacts"
          />
        )}
      </div>
      {drawerChecks.length > 0 && (
        <ReviewDrawer title="Checks" meta={checkSummary} defaultOpen={checksDefaultOpen}>
          <div className="review-checklist" aria-label="Readiness checklist">
            {drawerChecks.map((check) => (
              <div className={`review-check check-${String(check.status || "").trim().toLowerCase()}`} key={check.key}>
                <CheckStatusBadge status={check.status} />
                <div>
                  <strong>{check.label}</strong>
                  <p>{formatReviewText(check.detail)}</p>
                </div>
              </div>
            ))}
          </div>
        </ReviewDrawer>
      )}
      {packet.changes.diff_error && <div className="review-note danger">{formatTechnicalIssue(packet.changes.diff_error)}</div>}
      {isRepositoryBlockedPacket(packet) && (
        <div className="review-note recovery-note">
          <strong>Recovery</strong>
          <span>Run git status --short, then commit, stash, or revert local project changes before landing.</span>
        </div>
      )}
    </div>
  );
}

export function FailureSummary({failure, showExcerpt = false}: {
  failure: NonNullable<RunDetail["review_packet"]["failure"]>;
  showExcerpt?: boolean;
}) {
  return (
    <div className="review-note danger failure-summary">
      <strong>Failure</strong>
      <span>{failure.reason || "Failure recorded."}</span>
      {showExcerpt && failure.excerpt ? (
        <pre className="log-content" tabIndex={0} aria-label="Failure log excerpt">{renderLogText(failure.excerpt)}</pre>
      ) : null}
    </div>
  );
}

export function DetailLine({line}: {line: string}) {
  const visibleLine = userVisibleDetailLine(line);
  if (!visibleLine) return null;
  const visibleSplit = visibleLine.indexOf(":");
  if (visibleSplit > 0 && visibleSplit < 24) {
    return (
      <>
        <dt>{visibleLine.slice(0, visibleSplit)}</dt>
        <dd>{visibleLine.slice(visibleSplit + 1).trim() || "-"}</dd>
      </>
    );
  }
  return (
    <>
      <dt>Info</dt>
      <dd>{visibleLine}</dd>
    </>
  );
}

export function RecoveryActionBar({actions, status, onRunAction}: {
  actions: ActionState[];
  status: string;
  onRunAction: (action: string, label?: string) => void;
}) {
  const recovery = pickRecoveryActions(actions, status);
  if (!recovery.length) return null;
  return (
    <div
      className="recovery-action-bar"
      data-testid="recovery-action-bar"
      role="toolbar"
      aria-label="Recovery actions"
    >
      {recovery.map((action, idx) => {
        const name = actionName(action.key);
        return (
          <button
            key={action.key}
            type="button"
            className={actionButtonClass(action, idx === 0)}
            data-testid={`recovery-action-${name}`}
            disabled={!action.enabled}
            title={action.reason || action.preview || ""}
            onClick={() => onRunAction(name, action.label)}
          >
            {reviewActionLabel(action.label)}
          </button>
        );
      })}
    </div>
  );
}

const RECOVERABLE_STATUSES = new Set([
  "failed",
  "cancelled",
  "interrupted",
  "stale",
  "paused",
  "needs_attention",
]);

const RECOVERY_ACTION_KEYS = ["R", "r", "x"];

export function pickRecoveryActions(actions: ActionState[], status: string | null | undefined): ActionState[] {
  const normalized = String(status || "").toLowerCase();
  if (!RECOVERABLE_STATUSES.has(normalized)) return [];
  // Honor the order in RECOVERY_ACTION_KEYS (Retry > Resume > Cleanup) so
  // the primary slot is the most-likely next step.
  const byKey = new Map<string, ActionState>();
  for (const action of actions) byKey.set(action.key, action);
  const result: ActionState[] = [];
  for (const key of RECOVERY_ACTION_KEYS) {
    const match = byKey.get(key);
    if (match) result.push(match);
  }
  return result;
}

export function ActionBar({actions, mergeBlocked, onRunAction}: {actions: ActionState[]; mergeBlocked: boolean; onRunAction: (action: string, label?: string) => void}) {
  const visible = actions.filter((action) => !["o", "e", "m", "M"].includes(action.key));
  if (!visible.length) return <div className="advanced-actions empty" aria-hidden="true" />;
  return (
    <details className="advanced-actions">
      <summary>Advanced run actions</summary>
      <div className="action-bar" role="group" aria-label="Advanced run actions">
        {visible.map((action) => {
          const name = actionName(action.key);
          const disabled = !action.enabled || (action.key === "m" && mergeBlocked);
          const title = action.key === "m" && mergeBlocked ? "Commit, stash, or revert local project changes before merging." : action.reason || action.preview || "";
          return (
            <button
              key={action.key}
              type="button"
              className={actionButtonClass(action)}
              data-testid={`advanced-action-${name}`}
              disabled={disabled}
              title={title}
              onClick={() => onRunAction(name, action.label)}
            >
              {reviewActionLabel(action.label)}
            </button>
          );
        })}
      </div>
    </details>
  );
}

function actionButtonClass(action: ActionState, primary = false): string {
  const normalized = `${actionName(action.key)} ${action.label}`.toLowerCase();
  if (normalized.includes("cancel") || normalized.includes("remove")) return "danger-button";
  if (primary) return "primary";
  return "";
}

export function ArtifactPane({artifacts, selectedArtifactIndex, artifactContent, onLoadArtifact, onBack, runId}: {
  artifacts: ArtifactRef[];
  selectedArtifactIndex: number | null;
  artifactContent: ArtifactContentResponse | null;
  onLoadArtifact: (index: number) => void;
  onBack: () => void;
  runId: string;
}) {
  if (selectedArtifactIndex !== null) {
    const selectedArtifact = artifacts.find((artifact) => artifact.index === selectedArtifactIndex) || null;
    const artifactRef = artifactContent?.artifact || selectedArtifact;
    const previewable = artifactContent ? artifactContent.previewable !== false : true;
    const mime = artifactContent?.mime_type || "";
    const sizeBytes = artifactContent?.size_bytes ?? 0;
    const artifactIsLog = isLogArtifact(artifactRef || null);
    const rawContent = artifactContent?.content || "No content.";
    const compact = compactLongText(artifactIsLog ? rawContent : formatArtifactContent(rawContent), 20000);
    const proofReportUrl = artifactRef && isProofReportArtifact(artifactRef)
      ? `/api/runs/${encodeURIComponent(runId)}/proof-report`
      : null;
    const rawUrl = proofReportUrl || `/api/runs/${encodeURIComponent(runId)}/artifacts/${selectedArtifactIndex}/raw`;
    const artifactLabel = artifactRef?.label || "artifact";
    const renderHtml = Boolean(proofReportUrl || (artifactContent && previewable && mime.toLowerCase().includes("html")));
    const renderPdf = Boolean(artifactContent && mime.toLowerCase() === "application/pdf");
    return (
      <div className="artifact-pane">
        <button type="button" onClick={onBack}>Back to artifacts</button>
        <div className="artifact-meta">
          {artifactLabel} {(artifactContent?.truncated || compact.truncated) && previewable && !proofReportUrl ? "(truncated)" : ""}
          {mime && <small data-testid="artifact-mime">{` · ${mime}${sizeBytes > 0 ? ` · ${humanBytes(sizeBytes)}` : ""}`}</small>}
        </div>
        {!artifactContent && !proofReportUrl ? (
          <pre tabIndex={0}>Loading…</pre>
        ) : renderHtml ? (
          <ArtifactFrame rawUrl={rawUrl} label={artifactLabel} mime={mime} testId="artifact-html-frame" />
        ) : !previewable ? (
          renderPdf ? (
            <ArtifactFrame rawUrl={rawUrl} label={artifactLabel} mime={mime} testId="artifact-pdf-frame" />
          ) : mime.startsWith("image/") ? (
            <a href={rawUrl} target="_blank" rel="noreferrer">
              <img src={rawUrl} alt={artifactLabel} data-testid="artifact-image" className="artifact-image" />
            </a>
          ) : mime.startsWith("video/") ? (
            <video controls data-testid="artifact-video" className="artifact-video"><source src={rawUrl} type={mime} /></video>
          ) : (
            <div className="artifact-binary" data-testid="artifact-no-preview">
              <p>No text preview for {mime || "this artifact"}.</p>
              <a href={rawUrl} target="_blank" rel="noreferrer" download data-testid="artifact-download">Download artifact</a>
            </div>
          )
        ) : (
          <pre className={artifactIsLog ? "log-content" : ""} tabIndex={0} aria-label="Artifact content">
            {artifactIsLog ? renderLogText(compact.text) : compact.text}
          </pre>
        )}
      </div>
    );
  }
  if (!artifacts.length) return <div className="artifact-pane">No artifacts.</div>;
  const groups = artifactGroups(artifacts);
  return (
    <div className="artifact-pane artifact-list artifact-list-grouped">
      {groups.map((group) => {
        const content = (
          <div className="artifact-group-grid">
            {group.items.map((artifact) => (
              <ArtifactButton key={artifact.index} artifact={artifact} onLoadArtifact={onLoadArtifact} />
            ))}
          </div>
        );
        if (group.defaultOpen) {
          return (
            <section className="artifact-group" key={group.key} data-artifact-group={group.key}>
              <header>
                <h3>{group.title}</h3>
                <span>{group.items.length}</span>
              </header>
              {content}
            </section>
          );
        }
        return (
          <details className="artifact-group" key={group.key} data-artifact-group={group.key}>
            <summary>
              <span>{group.title}</span>
              <strong>{group.items.length}</strong>
            </summary>
            {content}
          </details>
        );
      })}
    </div>
  );
}

function ArtifactButton({artifact, onLoadArtifact}: {
  artifact: ArtifactRef;
  onLoadArtifact: (index: number) => void;
}) {
  return (
    <button
      type="button"
      disabled={!isReadableArtifact(artifact)}
      onClick={() => onLoadArtifact(artifact.index)}
      title={artifactProvenanceTooltip(artifact)}
      data-testid={`artifact-list-item-${artifact.index}`}
    >
      <strong>{artifact.label}</strong>
      <span>{artifactKindLabel(artifact)}</span>
      <small className="artifact-provenance">
        {artifact.size_bytes != null && <span data-testid={`artifact-size-${artifact.index}`}>{humanBytes(artifact.size_bytes)}</span>}
        {artifact.mtime && <span data-testid={`artifact-mtime-${artifact.index}`}>{artifact.mtime}</span>}
        {artifact.sha256 && <span data-testid={`artifact-sha-${artifact.index}`}>{artifact.sha256.slice(0, 12)}</span>}
      </small>
    </button>
  );
}

function isProofReportArtifact(artifact: ArtifactRef): boolean {
  const text = `${artifact.label} ${artifact.path}`.toLowerCase();
  return text.includes("proof report") || text.includes("proof-report") || text.includes("proof-of-work.html");
}

type ArtifactGroup = {
  key: string;
  title: string;
  defaultOpen: boolean;
  items: ArtifactRef[];
};

function artifactGroups(artifacts: ArtifactRef[]): ArtifactGroup[] {
  const groups: ArtifactGroup[] = [
    {key: "review", title: "Proof packet", defaultOpen: true, items: []},
    {key: "visual", title: "Screenshots and media", defaultOpen: true, items: []},
    {key: "logs", title: "Logs", defaultOpen: false, items: []},
    {key: "internals", title: "Run internals", defaultOpen: false, items: []},
  ];
  const byKey = new Map(groups.map((group) => [group.key, group]));
  for (const artifact of artifacts) {
    const kind = artifact.kind.toLowerCase();
    const text = `${artifact.label} ${artifact.path}`.toLowerCase();
    let key = "internals";
    if (
      isProofReportArtifact(artifact)
      || kind === "html"
      || text.includes("proof markdown")
      || text.includes("summary")
      || text.includes("proof-of-work.md")
      || text.includes("proof-of-work.json")
    ) {
      key = "review";
    } else if (["image", "video"].includes(kind) || text.endsWith(".png") || text.endsWith(".webm") || text.endsWith(".pdf")) {
      key = "visual";
    } else if (isLogArtifact(artifact)) {
      key = "logs";
    }
    byKey.get(key)?.items.push(artifact);
  }
  return groups.filter((group) => group.items.length > 0);
}

export function artifactProvenanceTooltip(artifact: ArtifactRef): string {
  const parts: string[] = [artifact.path];
  if (artifact.size_bytes != null) parts.push(`${artifact.size_bytes.toLocaleString()} bytes`);
  if (artifact.mtime) parts.push(`mtime ${artifact.mtime}`);
  if (artifact.sha256) parts.push(`sha256 ${artifact.sha256}`);
  return parts.join("\n");
}
