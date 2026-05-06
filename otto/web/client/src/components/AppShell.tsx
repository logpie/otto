// AppShell — shared page chrome for every top-level route.
//
// Provides a consistent top-bar (Otto Mission Control branding +
// optional "Back to runs" link when scoped to a single session/spec)
// and a main content container that owns horizontal padding +
// min-height so every route fills the viewport rather than reading
// as an unfinished fragment with massive empty whitespace.
//
// R2-B6 / R2-B20 / R2-B30: min-height: calc(100vh - topbar) on main.
// R2-B9: padding-inline on the main content container.
// R2-B10 / R2-B11: route-aware "← Back to runs" affordance.
// R2-B31: 404 page reuses this shell so it doesn't feel orphaned.

import type { MouseEvent, ReactNode } from "react";

interface Props {
  children: ReactNode;
  // When true, render a "← Back to runs" link in the top bar. Set on
  // every non-landing route so the user always has a one-click escape.
  showBackToRuns?: boolean;
  // Optional inline label rendered next to the brand (e.g. "Run detail
  // · abc123"). Lets per-page context surface in the chrome instead of
  // re-implementing a separate header inside each route component.
  pageLabel?: string | null;
  // R3-B25: optional full text for the page-label tooltip. Set when
  // `pageLabel` contains a truncated session id ("...abc123") so the
  // user can hover to recover the full id without selecting / copying
  // from the URL bar. Omit to fall back to `pageLabel` itself.
  pageLabelTitle?: string | null;
  projectName?: string | null;
  projectBranch?: string | null;
  projectDirty?: boolean;
  launcherEnabled?: boolean;
  onSwitchProject?: () => void;
}

export function AppShell({
  children,
  showBackToRuns = false,
  pageLabel = null,
  pageLabelTitle = null,
  projectName = null,
  projectBranch = null,
  projectDirty = false,
  launcherEnabled = false,
  onSwitchProject,
}: Props) {
  const canSwitchProject = launcherEnabled && onSwitchProject;
  const onBrandClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (!canSwitchProject) return;
    event.preventDefault();
    onSwitchProject();
  };
  const projectControl = projectName ? (
    canSwitchProject ? (
      <button
        type="button"
        className="otto-app-shell-project"
        data-testid="switch-project-button"
        aria-label="Open project launcher"
        title="Open project launcher"
        onClick={onSwitchProject}
      >
        <span className="otto-app-shell-project-prefix">Projects</span>
        <span className="otto-app-shell-project-name">{projectName}</span>
        {projectBranch ? (
          <span className="otto-app-shell-project-branch">{projectBranch}</span>
        ) : null}
        {projectDirty ? (
          <span className="otto-app-shell-project-dirty" title="Local changes" aria-label="Local changes" />
        ) : null}
        <span className="otto-app-shell-project-chevron" aria-hidden />
      </button>
    ) : (
      <span className="otto-app-shell-project otto-app-shell-project-static">
        <span className="otto-app-shell-project-name">{projectName}</span>
        {projectBranch ? (
          <span className="otto-app-shell-project-branch">{projectBranch}</span>
        ) : null}
      </span>
    )
  ) : null;

  return (
    <div className="otto-app-shell" data-testid="otto-app-shell">
      <header className="otto-app-shell-topbar" data-testid="otto-app-shell-topbar">
        <div className="otto-app-shell-brand">
          <a
            className="otto-app-shell-brand-link"
            href="/"
            aria-label={canSwitchProject ? "Otto Mission Control — open project launcher" : "Otto Mission Control — back to runs"}
            onClick={onBrandClick}
          >
            Otto Mission Control
          </a>
          {pageLabel ? (
            <span
              className="otto-app-shell-page-label"
              data-testid="otto-app-shell-page-label"
              title={pageLabelTitle ?? pageLabel}
            >
              <span className="otto-app-shell-page-label-sep" aria-hidden>
                ·
              </span>
              {pageLabel}
            </span>
          ) : null}
        </div>
        <div className="otto-app-shell-actions">
          {projectControl}
          {showBackToRuns ? (
            <a
              className="otto-app-shell-back-link"
              href="/"
              data-testid="otto-app-shell-back-to-runs"
            >
              ← Back to runs
            </a>
          ) : null}
        </div>
      </header>
      <main className="otto-app-shell-main" data-testid="otto-app-shell-main">
        {children}
      </main>
    </div>
  );
}

export default AppShell;
