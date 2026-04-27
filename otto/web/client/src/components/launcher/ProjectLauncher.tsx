import {FormEvent, useEffect, useRef, useState} from "react";
import {ApiError, friendlyApiMessage} from "../../api";
import {errorMessage, watcherSummary} from "../../App";
import {BrandMark} from "../BrandMark";
import {Spinner} from "../Spinner";
import {LauncherExplainer} from "./LauncherExplainer";
import {MetaItem} from "../MicroComponents";
import type {LandingState, ManagedProjectInfo, ProjectsResponse, StateResponse, WatcherInfo} from "../../types";

/**
 * Sidebar metadata block (used in legacy launcher / future use).
 * mc-audit redesign Phase G.
 */
export function ProjectMeta({project, watcher, landing, active, firstRun}: {
  project: StateResponse["project"] | undefined;
  watcher: WatcherInfo | undefined;
  landing: LandingState | undefined;
  active: number;
  firstRun: boolean;
}) {
  const counts = watcher?.counts || {};
  const health = watcher?.health;
  void counts;
  void landing;
  if (firstRun) {
    return (
      <dl className="project-meta project-meta-first-run" aria-label="Project metadata" data-testid="project-meta-first-run">
        <MetaItem label="Project" value={project?.name || "-"} />
        <MetaItem label="Branch" value={project?.branch || "-"} />
        <MetaItem
          label="Status"
          value={!project ? "Loading…" : "Project ready · No jobs yet"}
        />
      </dl>
    );
  }
  return (
    <dl className="project-meta" aria-label="Project metadata" data-testid="project-meta-full">
      <MetaItem label="Project" value={project?.name || "-"} />
      <MetaItem label="Branch" value={project?.branch || "-"} />
      <MetaItem label="State" value={!project ? "unknown" : project.dirty ? "dirty" : "clean"} />
      <MetaItem
        label="Watcher"
        value={watcherSummary(watcher)}
        tooltip="The supervisor process that picks up queued tasks and dispatches them. Stop it to pause queue processing."
      />
      <MetaItem
        label="Heartbeat"
        value={health?.heartbeat_age_s === null || health?.heartbeat_age_s === undefined ? "-" : `${Math.round(health.heartbeat_age_s)}s ago`}
        tooltip="Seconds since the watcher last ticked. >15s = stale (likely crashed)."
      />
      <MetaItem
        label="In flight"
        value={String(active)}
        tooltip="Number of tasks currently being executed by the watcher."
      />
    </dl>
  );
}

/**
 * Centered hero launcher page — pick or create a managed project.
 * mc-audit redesign Phase E.
 */
export function ProjectLauncher({projectsState, refreshStatus, refreshPending, onCreate, onSelect, onRefresh}: {
  projectsState: ProjectsResponse;
  refreshStatus: string;
  refreshPending: boolean;
  onCreate: (name: string) => Promise<void>;
  onSelect: (path: string) => Promise<void>;
  onRefresh: () => void;
}) {
  void refreshStatus;
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");
  const [statusKind, setStatusKind] = useState<"info" | "error">("info");
  const [pending, setPending] = useState(false);
  const projects = projectsState.projects || [];
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (projects.length === 0) {
      nameInputRef.current?.focus();
    }
  }, [projects.length]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setStatus("Project name is required.");
      setStatusKind("error");
      return;
    }
    setPending(true);
    setStatus("Creating project");
    setStatusKind("info");
    try {
      await onCreate(trimmed);
      setName("");
      setStatus("");
    } catch (error) {
      setStatus(launcherErrorMessage(error, {projectName: trimmed}));
      setStatusKind("error");
    } finally {
      setPending(false);
    }
  }

  async function openProject(project: ManagedProjectInfo) {
    if (!project.path || pending) return;
    setPending(true);
    setStatus(`Opening ${project.name}`);
    setStatusKind("info");
    try {
      await onSelect(project.path);
      setStatus("");
    } catch (error) {
      setStatus(launcherErrorMessage(error, {projectPath: project.path}));
      setStatusKind("error");
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="launcher-page" aria-labelledby="projectLauncherHeading">
      <header className="launcher-hero">
        <BrandMark size={48} />
        <h1 id="projectLauncherHeading" className="launcher-hero-title">otto</h1>
        <p className="launcher-hero-tagline" data-testid="launcher-subhead">
          Describe a feature. Otto builds, verifies, and lands it in an isolated git worktree.
        </p>
        <button
          type="button"
          className="launcher-hero-refresh"
          data-testid="launcher-refresh-button"
          disabled={refreshPending}
          aria-busy={refreshPending}
          onClick={onRefresh}
          aria-label="Refresh project list"
          title="Refresh project list"
        >
          {refreshPending ? <Spinner /> : "↻"}
        </button>
      </header>

      <LauncherExplainer />

      {projects.length > 0 && (
        <div className="launcher-section">
          <div className="launcher-section-head">
            <h2>Open a project</h2>
            <span className="muted">{projects.length} {projects.length === 1 ? "project" : "projects"}</span>
          </div>
          <div className="project-list">
            {projects.map((project) => (
              <button className="project-row" type="button" key={project.path} disabled={pending} onClick={() => void openProject(project)}>
                <span className="project-row-mark" aria-hidden="true">
                  {(project.name || "?").charAt(0).toUpperCase()}
                </span>
                <span className="project-row-main">
                  <strong>{project.name}</strong>
                  <code title={project.path}>{project.path}</code>
                </span>
                <span className="project-row-meta">
                  <span className="project-row-branch" title="Branch">{project.branch || "-"}</span>
                  {project.dirty ? <span className="project-row-dirty" title="Local changes">●</span> : null}
                </span>
                <span className="project-row-arrow" aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className={`launcher-section launcher-create ${projects.length === 0 ? "launcher-create-hero" : ""}`}>
        <div className="launcher-section-head">
          <h2>{projects.length === 0 ? "Create your first project" : "Create new"}</h2>
        </div>
        <form className="launcher-create-form" onSubmit={(event) => void submit(event)}>
          <input
            ref={nameInputRef}
            value={name}
            data-testid="launcher-create-name-input"
            autoFocus
            type="text"
            placeholder="e.g. Expense approval portal"
            aria-label="Project name"
            onChange={(event) => setName(event.target.value)}
          />
          <button className="primary" type="submit" data-testid="launcher-create-submit" disabled={pending}>{pending ? <><Spinner /> Working…</> : "Create project"}</button>
        </form>
        {status ? (
          <p
            className={`launcher-status ${statusKind === "error" ? "launcher-status-error" : ""}`}
            data-testid="launcher-form-status"
            aria-live="polite"
          >{status}</p>
        ) : null}
        <p className="launcher-create-hint" data-testid="launcher-managed-root-help">
          Stored under <code title={projectsState.projects_root}>{projectsState.projects_root}</code>. Pick or create a managed project; other repos are intentionally excluded and are not affected.
        </p>
      </div>
    </section>
  );
}

export function launcherErrorMessage(error: unknown, context: {projectName?: string; projectPath?: string}): string {
  if (error instanceof ApiError) {
    return friendlyApiMessage(error.status, error.rawMessage, context);
  }
  return errorMessage(error);
}
