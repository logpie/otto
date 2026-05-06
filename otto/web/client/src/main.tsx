import React, {useCallback, useEffect, useState} from "react";
import {createRoot} from "react-dom/client";
import {api} from "./api";
import {AppShell} from "./components/AppShell";
import {ProjectLauncher} from "./components/launcher/ProjectLauncher";
import {RunListLanding} from "./components/run/RunListLanding";
import {RunViewPage} from "./components/run/RunViewPage";
import {SpecDiffPage} from "./components/spec/SpecDiffPage";
import {SpecReviewPage} from "./components/spec/SpecReviewPage";
import type {ProjectInfo, ProjectMutationResponse, ProjectsResponse} from "./types";
import "./styles.css";

const root = document.querySelector("#root");

if (!root) {
  throw new Error("Mission Control root element is missing");
}

// Phase C.4 (tick 65): legacy <App/> Mission Control inspector deleted
// alongside the legacy `/api/runs/<id>/...` backend surface. Default
// landing now lists sessions from `/api/run-view`; clicking a session
// mounts <RunViewPage/> via the `?view=run-view&session=<id>` URL.
//
//   (no params)                              → <RunListLanding/>
//   ?view=run-view&session=<session_id>      → <RunViewPage/>
//   ?view=spec-review&spec=<spec_id>         → <SpecReviewPage/>
//   ?view=spec-diff&session=<session_id>     → <SpecDiffPage/> (wireframe 4d)
interface ShellRouteProps {
  projectName?: string | null;
  projectBranch?: string | null;
  projectDirty?: boolean;
  project?: ProjectInfo | null;
  launcherEnabled?: boolean;
  onSwitchProject?: () => void;
}

// Cluster A (R2-B6/B9/B10/B11/B20/B30/B31): every top-level route is
// wrapped in <AppShell/> so the topbar branding, "Back to runs"
// affordance, page-padding, and min-height-fills-viewport are
// consistent across landing, run-view, spec-review, spec-diff, and
// the 404 fallback. Without the shell each route felt like a
// disconnected fragment.
function renderRoute(shellProps: ShellRouteProps = {}) {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  const sessionId = params.get("session");
  const specId = params.get("spec");

  if (view === "run-view" && sessionId) {
    // R3-B25: pass the full session id as the tooltip target so users
    // can hover the truncated "...abc123" label and recover the full id
    // without copying from the URL bar.
    return (
      <AppShell
        {...shellProps}
        showBackToRuns
        pageLabel={`Run detail · ${shortSession(sessionId)}`}
        pageLabelTitle={`Run detail · ${sessionId}`}
      >
        <RunViewPage sessionId={sessionId} />
      </AppShell>
    );
  }
  if (view === "spec-review" && specId) {
    return (
      <AppShell
        {...shellProps}
        showBackToRuns
        pageLabel="Spec review"
        pageLabelTitle={`Spec review · ${specId}`}
      >
        <SpecReviewPage specId={specId} />
      </AppShell>
    );
  }
  if (view === "spec-diff" && sessionId) {
    return (
      <AppShell
        {...shellProps}
        showBackToRuns
        pageLabel="Spec diff"
        pageLabelTitle={`Spec diff · ${sessionId}`}
      >
        <SpecDiffPage sessionId={sessionId} />
      </AppShell>
    );
  }
  return (
    <AppShell {...shellProps}>
      <RunListLanding project={shellProps.project ?? undefined} />
    </AppShell>
  );
}

function RootRouter() {
  const [projectsState, setProjectsState] = useState<ProjectsResponse | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [refreshPending, setRefreshPending] = useState(false);

  const refreshProjects = useCallback(async () => {
    setRefreshPending(true);
    try {
      const body = await api<ProjectsResponse>("/api/projects");
      setProjectsState(body);
      setProjectsError(null);
    } catch (error) {
      setProjectsError((error as Error).message || String(error));
    } finally {
      setRefreshPending(false);
    }
  }, []);

  useEffect(() => {
    void refreshProjects();
  }, [refreshProjects]);

  async function createProject(name: string) {
    const body = await api<ProjectMutationResponse>("/api/projects/create", {
      method: "POST",
      body: JSON.stringify({name}),
    });
    window.history.replaceState({}, "", "/");
    applyProjectMutation(body);
  }

  async function selectProject(path: string) {
    const body = await api<ProjectMutationResponse>("/api/projects/select?include_projects=false", {
      method: "POST",
      body: JSON.stringify({path}),
    });
    window.history.replaceState({}, "", "/");
    applyProjectMutation(body);
  }

  async function switchProject() {
    const body = await api<ProjectMutationResponse>("/api/projects/clear?include_projects=false", {
      method: "POST",
      body: JSON.stringify({}),
    });
    window.history.replaceState({}, "", "/");
    applyProjectMutation(body);
  }

  function applyProjectMutation(body: ProjectMutationResponse) {
    setProjectsState((previous) => {
      if (previous === null) return previous;
      return {
        ...previous,
        current: body.project ?? body.current ?? null,
        projects: body.projects ?? previous.projects,
      };
    });
  }

  if (projectsState === null) {
    return (
      <div className="app-shell boot-loading" data-testid="boot-loading">
        <div className="boot-loading-panel">
          <div className="boot-loading-mark" aria-hidden>o</div>
          <p>{projectsError ? `Failed to load projects: ${projectsError}` : "Loading Mission Control..."}</p>
        </div>
      </div>
    );
  }

  if (projectsState.launcher_enabled && projectsState.current === null) {
    return (
      <ProjectLauncher
        projectsState={projectsState}
        refreshStatus={refreshPending ? "refreshing" : "idle"}
        refreshPending={refreshPending}
        onCreate={createProject}
        onSelect={selectProject}
        onRefresh={() => void refreshProjects()}
      />
    );
  }

  return renderRoute({
    projectName: projectsState.current?.name ?? null,
    projectBranch: projectsState.current?.branch ?? null,
    projectDirty: Boolean(projectsState.current?.dirty),
    project: projectsState.current,
    launcherEnabled: projectsState.launcher_enabled,
    onSwitchProject: switchProject,
  });
}

// Truncate session ids in the page-label so the topbar reads cleanly
// regardless of full id length. Format: <date>-<HHMMSS>-<6hex>; the
// trailing hex is the most useful disambiguator at a glance.
//
// R3-B49: always truncate to "...<last 6 chars>" — even short or
// otherwise-shaped ids (e.g. the "DOES-NOT-EXIST" 404 fixture).
//
// R4-B4: when the trimmed tail starts with a hyphen / dot / underscore
// (e.g. "DOES-NOT-EXIST" → "-EXIST"), the resulting "...-EXIST" reads
// as awkward leading punctuation. Strip leading non-alphanumerics from
// the tail so we always render "...EXIST" / "...abc123" — no orphan
// separator hugging the ellipsis.
function shortSession(id: string): string {
  const tail = id.slice(-6).replace(/^[^A-Za-z0-9]+/, "");
  return `...${tail}`;
}

createRoot(root).render(
  <React.StrictMode>
    <RootRouter />
  </React.StrictMode>,
);
