import {canStartWatcher, canStopWatcher, effectiveWatcherState, startWatcherTooltip} from "../../utils/missionControl";
import {BrandMark} from "../BrandMark";
import {Spinner} from "../Spinner";
import type {PillTone} from "../Pill";
import type {ProjectsResponse, StateResponse, WatcherInfo} from "../../types";

/**
 * Slim horizontal header — replaces the previous vertical sidebar.
 * mc-audit redesign Phase C.
 */
export function TopBar({
  data, project, watcher, watcherPending, projectsState, onNewJob, onSwitchProject, onStartWatcher, onStopWatcher,
}: {
  data: StateResponse | null;
  project: StateResponse["project"] | undefined;
  watcher: WatcherInfo | undefined;
  watcherPending: boolean;
  projectsState: ProjectsResponse | null;
  onNewJob: () => void;
  onSwitchProject: () => void;
  onStartWatcher: () => void;
  onStopWatcher: () => void;
}) {
  const watcherState = effectiveWatcherState(watcher);
  const watcherTone: PillTone = watcherState === "running" ? "success" : watcherState === "stale" ? "warning" : "neutral";
  const startable = watcherState !== "running" && canStartWatcher(data);
  const heartbeat = watcher?.health.heartbeat_age_s;
  const heartbeatHint = heartbeat === null || heartbeat === undefined ? "" : `${Math.round(heartbeat)}s ago`;
  const heartbeatTitle = heartbeatHint ? ` Last heartbeat ${heartbeatHint}.` : "";
  const watcherActionLabel = (() => {
    if (watcherPending) return watcherState === "running" ? "Stopping runner..." : "Starting runner...";
    if (watcherState === "running") return "Queue running";
    if (watcherState === "stale") return "Queue runner stale";
    if (startable) return "Start queue runner";
    return "Queue idle";
  })();
  return (
    <header className="topbar" role="banner">
      <div className="topbar-brand">
        <BrandMark size={28} />
        <span className="brand-wordmark">otto</span>
      </div>
      <div className="topbar-context">
        {project ? (
          <button
            type="button"
            className="topbar-project"
            onClick={onSwitchProject}
            disabled={!projectsState?.launcher_enabled}
            data-testid="switch-project-button"
            aria-label={projectsState?.launcher_enabled ? "Open project launcher" : "Project switching disabled"}
            title={projectsState?.launcher_enabled ? "Open project launcher" : "Project switching disabled"}
          >
            {projectsState?.launcher_enabled ? <span className="topbar-project-switch">Projects</span> : null}
            <span className="topbar-project-name">{project.name || "Project"}</span>
            <span className="topbar-project-branch">{project.branch || "—"}</span>
            {project.dirty ? <span className="topbar-project-dirty" title="Local changes">●</span> : null}
            {projectsState?.launcher_enabled ? <span className="topbar-project-chevron" aria-hidden="true">▾</span> : null}
          </button>
        ) : null}
      </div>
      <div className="topbar-actions">
        {project ? (
          <span
            className={`topbar-status pill-tone-${project.dirty ? "warning" : "success"}`}
            title={project.dirty ? "Git working tree has uncommitted local changes." : "Git working tree is clean; no uncommitted local changes."}
          >
            <span className={`watcher-dot tone-${project.dirty ? "warning" : "success"}`} aria-hidden="true" />
            {project.dirty ? "Git dirty" : "Git clean"}
          </span>
        ) : null}
        <button
          type="button"
          className={`topbar-watcher pill-tone-${watcherTone} ${watcherState === "running" ? "is-live" : ""} ${startable ? "is-startable" : ""}`}
          data-testid={watcherState === "running" ? "stop-watcher-button" : "start-watcher-button"}
          disabled={watcherPending || (watcherState === "running" ? !canStopWatcher(data) : !canStartWatcher(data))}
          aria-busy={watcherPending}
          aria-label={
            watcherState === "running"
              ? "Queue runner is running. Click to pause queue processing."
              : watcherState === "stale"
              ? "Queue runner appears stale. Check Health or recover the runner."
              : watcherActionLabel
          }
          title={
            watcherPending
              ? watcherActionLabel
              : watcherState === "running"
              ? `Pause queue processing. Running tasks may continue until they reach a stop point.${heartbeatTitle}`
              : startWatcherTooltip(data) || "Start the queue runner to process queued jobs."
          }
          onClick={watcherState === "running" ? onStopWatcher : onStartWatcher}
        >
          {watcherPending ? <Spinner /> : <span className={`watcher-dot tone-${watcherTone}`} aria-hidden="true" />}
          {watcherActionLabel}
        </button>
        <button
          className="primary topbar-new-job"
          type="button"
          data-testid="new-job-button"
          onClick={onNewJob}
        >New job</button>
      </div>
    </header>
  );
}
