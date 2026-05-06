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

import type { ReactNode } from "react";

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
}

export function AppShell({
  children,
  showBackToRuns = false,
  pageLabel = null,
  pageLabelTitle = null,
}: Props) {
  return (
    <div className="otto-app-shell" data-testid="otto-app-shell">
      <header className="otto-app-shell-topbar" data-testid="otto-app-shell-topbar">
        <div className="otto-app-shell-brand">
          <a
            className="otto-app-shell-brand-link"
            href="/"
            aria-label="Otto Mission Control — back to runs"
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
        {showBackToRuns ? (
          <a
            className="otto-app-shell-back-link"
            href="/"
            data-testid="otto-app-shell-back-to-runs"
          >
            ← Back to runs
          </a>
        ) : null}
      </header>
      <main className="otto-app-shell-main" data-testid="otto-app-shell-main">
        {children}
      </main>
    </div>
  );
}

export default AppShell;
