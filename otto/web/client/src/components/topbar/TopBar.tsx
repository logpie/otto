import {canStartWatcher, canStopWatcher, startWatcherTooltip} from "../../App";
import {BrandMark} from "../BrandMark";
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
  const watcherState = watcher?.health.state || "stopped";
  const watcherTone: PillTone = watcherState === "running" ? "success" : watcherState === "stale" ? "warning" : "neutral";
  const watcherLabel = (() => {
    if (watcherState === "running") return "Running";
    if (watcherState === "stale") return "Stale";
    return "Stopped";
  })();
  const heartbeat = watcher?.health.heartbeat_age_s;
  const heartbeatHint = heartbeat === null || heartbeat === undefined ? "" : `${Math.round(heartbeat)}s ago`;
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
            title={projectsState?.launcher_enabled ? "Switch project" : "Project switching disabled"}
          >
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
            title={project.dirty ? "Local changes present" : "Repository clean"}
          >
            <span className={`watcher-dot tone-${project.dirty ? "warning" : "success"}`} aria-hidden="true" />
            {project.dirty ? "Dirty" : "Clean"}
          </span>
        ) : null}
        <button
          type="button"
          className={`topbar-watcher pill-tone-${watcherTone}`}
          data-testid={watcherState === "running" ? "stop-watcher-button" : "start-watcher-button"}
          disabled={watcherPending || (watcherState === "running" ? !canStopWatcher(data) : !canStartWatcher(data))}
          aria-busy={watcherPending}
          title={watcherState === "running" ? watcher?.health.next_action || "Stop watcher" : startWatcherTooltip(data)}
          onClick={watcherState === "running" ? onStopWatcher : onStartWatcher}
        >
          <span className={`watcher-dot tone-${watcherTone}`} aria-hidden="true" />
          Watcher: {watcherLabel}
          {heartbeatHint && watcherState === "running" ? <em>· {heartbeatHint}</em> : null}
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
