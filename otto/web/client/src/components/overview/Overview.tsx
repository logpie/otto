import {Spinner} from "../Spinner";
import {FocusMetric, OverviewMetric, ProjectStatCard} from "../MicroComponents";
import type {StateResponse} from "../../types";
import {formatDuration, storyTotalsFromLanding, tokenBreakdownLine} from "../../utils/format";
import {activeRunSummary, canMerge, canResolveRelease, canStartWatcher, missionFocus, workflowHealth} from "../../utils/missionControl";
import type {ResultBannerState} from "../../uiTypes";

export function OperationalOverview({data, lastError, resultBanner, onDismissError, onDismissResult}: {
  data: StateResponse | null;
  lastError: string | null;
  resultBanner: ResultBannerState | null;
  onDismissError: () => void;
  onDismissResult: () => void;
}) {
  const health = workflowHealth(data);
  return (
    <div className="overview" role="region" aria-labelledby="missionOverviewHeading">
      <h2 id="missionOverviewHeading" className="sr-only">Mission overview</h2>
      <div className="overview-strip">
        <OverviewMetric label="Active" value={String(health.active)} tone={health.active ? "info" : "neutral"} />
        <OverviewMetric label="Needs attention" value={String(health.needsAttention)} tone={health.needsAttention ? "danger" : "neutral"} />
        <OverviewMetric label="Ready" value={String(health.ready)} tone={health.ready ? "success" : "neutral"} />
        <OverviewMetric label="Repository" value={health.repositoryLabel} tone={health.repositoryTone} />
        <OverviewMetric label="Watcher" value={health.watcherLabel} tone={health.watcherTone} />
        <OverviewMetric label="Runtime" value={health.runtimeLabel} tone={health.runtimeTone} />
      </div>
      {lastError && (
        <div className="status-banner error">
          <strong>Last error</strong>
          <span>{lastError}</span>
          <button type="button" onClick={onDismissError}>Dismiss</button>
        </div>
      )}
      {resultBanner && (
        <div className={`status-banner ${resultBanner.severity === "error" ? "error" : "warning"}`}>
          <strong>{resultBanner.title}</strong>
          <span>{resultBanner.body}</span>
          <button type="button" onClick={onDismissResult}>Dismiss</button>
        </div>
      )}
      {data?.runtime.issues.length ? <RuntimeWarnings data={data} /> : null}
    </div>
  );
}

export function ProjectOverview({data}: {data: StateResponse | null}) {
  const stats = data?.project_stats;
  const historyItems = data?.history.items || [];
  const historyCount = stats?.history_count ?? data?.history.total_rows ?? historyItems.length;
  const successCount = stats?.success_count ?? historyItems.filter((item) => item.terminal_outcome === "success").length;
  const failedCount = stats?.failed_count ?? historyItems.filter((item) => item.terminal_outcome === "failed").length;
  const totalTasks = data?.landing.counts.total || 0;
  const landingStories = storyTotalsFromLanding(data?.landing.items || []);
  const storiesPassed = stats?.stories_tested ? stats.stories_passed : landingStories.passed;
  const storiesTested = stats?.stories_tested ? stats.stories_tested : landingStories.tested;
  const storyValue = storiesTested ? `${storiesPassed}/${storiesTested}` : "-";
  return (
    <section className="panel project-overview" aria-labelledby="projectOverviewHeading">
      <div className="panel-heading">
        <div>
          <h2 id="projectOverviewHeading">Project Overview</h2>
          <p className="panel-subtitle">Work, review readiness, and token spend for this project.</p>
        </div>
      </div>
      <div className="project-stat-grid">
        {/* "Current work" tile dropped — same counts are owned by the Task
            Board (kanban column counters). mc-audit redesign §3b W4.3. */}
        <ProjectStatCard
          label="Run history"
          value={`${historyCount} runs`}
          detail={`${totalTasks} tracked tasks · ${successCount} success · ${failedCount} failed`}
          tone={failedCount ? "warning" : "neutral"}
        />
        <ProjectStatCard
          label="Tokens"
          value={stats?.token_display || "-"}
          detail={tokenBreakdownLine(stats?.token_usage)}
          tone={stats?.total_tokens ? "info" : "neutral"}
        />
        <ProjectStatCard
          label="Runtime"
          value={stats?.duration_display || "-"}
          detail="Completed + active run time"
          tone={stats?.total_duration_s ? "info" : "neutral"}
        />
        <ProjectStatCard
          label="Stories"
          value={storyValue}
          detail={storiesTested ? "Certified" : "No evidence yet"}
          tone={storiesTested ? (storiesPassed === storiesTested ? "success" : "warning") : "neutral"}
        />
      </div>
    </section>
  );
}

export function RuntimeWarnings({data}: {data: StateResponse}) {
  const top = data.runtime.issues.slice(0, 3);
  const bannerTone = top.some((issue) => issue.severity === "error") ? "error" : "warning";
  const backlog = data.runtime.command_backlog;
  // mc-audit codex-first-time-user #26: surface user-facing labels in the
  // runtime banner instead of internal terms ("malformed" → "unreadable").
  const suffix = [
    backlog.pending ? `${backlog.pending} pending` : "",
    backlog.processing ? `${backlog.processing} processing` : "",
    backlog.malformed ? `${backlog.malformed} unreadable` : "",
  ].filter(Boolean).join(" / ");
  return (
    <div className={`status-banner ${bannerTone} runtime-banner`}>
      <strong>Runtime</strong>
      <span title={top.map((issue) => `${issue.label}: ${issue.detail}`).join("\n")}>
        {top.map((issue) => `${issue.label}: ${issue.next_action}`).join(" | ")}
      </span>
      <span className="runtime-backlog">{suffix || data.runtime.status}</span>
    </div>
  );
}

export function MissionFocus({data, lastError, resultBanner, watcherPending, landPending, onNewJob, onStartWatcher, onLandReady, onRecoverLanding, onAbortMerge, onResolveRelease, onOpenDiagnostics, onDismissError, onDismissResult}: {
  data: StateResponse | null;
  lastError: string | null;
  resultBanner: ResultBannerState | null;
  watcherPending: boolean;
  landPending: boolean;
  onNewJob: () => void;
  onStartWatcher: () => void;
  onLandReady: () => void;
  onRecoverLanding: () => void;
  onAbortMerge: () => void;
  onResolveRelease: () => void;
  onOpenDiagnostics: () => void;
  onDismissError: () => void;
  onDismissResult: () => void;
}) {
  const focus = missionFocus(data);
  const activeSummary = activeRunSummary(data);
  return (
    <section className={`mission-focus focus-${focus.tone}`} data-testid="mission-focus" aria-labelledby="missionFocusHeading">
      <div className="focus-copy">
        <span>{focus.kicker}</span>
        <h2 id="missionFocusHeading">{focus.title}</h2>
        <p>{focus.body}</p>
        {activeSummary ? (
          <div className="focus-live-summary" aria-label="Active worker summary">
            <span className="task-live-dot" aria-hidden="true" />
            <strong>{activeSummary.label}</strong>
            <span>{activeSummary.detail}</span>
          </div>
        ) : null}
      </div>
      <div className="focus-actions">
        {focus.primary === "land" && (
          <>
            {canResolveRelease(data) ? (
              <button type="button" disabled={landPending} onClick={onResolveRelease}>Resolve release issues</button>
            ) : null}
            <button className="primary" type="button" data-testid="mission-land-ready-button" disabled={!canMerge(data?.landing) || landPending} aria-busy={landPending} onClick={onLandReady}>{landPending ? <><Spinner /> Landing…</> : "Land all ready"}</button>
          </>
        )}
        {focus.primary === "start" && (
          <button
            className="primary"
            type="button"
            data-testid="mission-start-watcher-button"
            disabled={!canStartWatcher(data) || watcherPending}
            aria-busy={watcherPending}
            // mc-audit live W11-IMPORTANT-3: when the button is disabled the
            // user still hovers asking "why?". Surface the supervisor's
            // start_blocked_reason / next_action via title so the disabled
            // state is self-explanatory ("watcher already running", "no
            // queued tasks", etc.) instead of generic.
            title={
              watcherPending
                ? "Starting watcher…"
                : (data?.runtime.supervisor.start_blocked_reason
                    || data?.watcher.health.next_action
                    || "Start the watcher process to run queued jobs.")
            }
            onClick={onStartWatcher}
          >
            {watcherPending
              ? <><Spinner /> Starting…</>
              : (Number(data?.watcher.counts.queued || 0) > 0 ? "Start queued job" : "Start watcher")}
          </button>
        )}
        {focus.primary === "diagnostics" && (
          <button className="primary" type="button" onClick={onOpenDiagnostics}>Open health</button>
        )}
        {focus.primary === "recover" && (
          <>
            <button className="primary" type="button" onClick={onResolveRelease}>Resolve release issues</button>
            <button type="button" onClick={onRecoverLanding}>Recover landing</button>
            <button type="button" onClick={onAbortMerge}>Abort merge</button>
          </>
        )}
        {focus.primary === "resolve" && (
          <button className="primary" type="button" disabled={!canResolveRelease(data)} onClick={onResolveRelease}>Resolve release issues</button>
        )}
        {focus.primary === "new" && (
          <button className="primary" type="button" data-testid="mission-new-job-button" onClick={onNewJob}>{focus.firstRun ? "Start first build" : "New job"}</button>
        )}
        {focus.primary !== "new" && <button type="button" onClick={onNewJob}>New job</button>}
      </div>
      {/* Banner metrics dropped — same numbers are visible in the Task
          Board column counters directly below this banner. mc-audit
          redesign §3b W4.2. The numbers stay accessible via the
          aria-label below for SR users.
          The banner now collapses to copy + actions only when idle. */}
      {(focus.working > 0 || focus.needsAction > 0 || focus.ready > 0) && (
        <div className="focus-metrics" aria-label="Queue summary">
          <FocusMetric label="Queued/running" value={String(focus.working)} />
          <FocusMetric label="Needs action" value={String(focus.needsAction)} />
          <FocusMetric label="Ready" value={String(focus.ready)} />
        </div>
      )}
      {lastError && (
        <div className="status-banner error">
          <strong>Last error</strong>
          <span>{lastError}</span>
          <button type="button" onClick={onDismissError}>Dismiss</button>
        </div>
      )}
      {resultBanner && (
        <div className={`status-banner ${resultBanner.severity === "error" ? "error" : "warning"}`}>
          <strong>{resultBanner.title}</strong>
          <span>{resultBanner.body}</span>
          <button type="button" onClick={onDismissResult}>Dismiss</button>
        </div>
      )}
      {data?.runtime.issues.length ? <RuntimeWarnings data={data} /> : null}
    </section>
  );
}
