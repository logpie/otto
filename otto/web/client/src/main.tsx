import React from "react";
import {createRoot} from "react-dom/client";
import {AppShell} from "./components/AppShell";
import {RunListLanding} from "./components/run/RunListLanding";
import {RunViewPage} from "./components/run/RunViewPage";
import {SpecDiffPage} from "./components/spec/SpecDiffPage";
import {SpecReviewPage} from "./components/spec/SpecReviewPage";
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
const params = new URLSearchParams(window.location.search);
const view = params.get("view");
const sessionId = params.get("session");
const specId = params.get("spec");

// Cluster A (R2-B6/B9/B10/B11/B20/B30/B31): every top-level route is
// wrapped in <AppShell/> so the topbar branding, "Back to runs"
// affordance, page-padding, and min-height-fills-viewport are
// consistent across landing, run-view, spec-review, spec-diff, and
// the 404 fallback. Without the shell each route felt like a
// disconnected fragment.
function renderRoute() {
  if (view === "run-view" && sessionId) {
    return (
      <AppShell showBackToRuns pageLabel={`Run detail · ${shortSession(sessionId)}`}>
        <RunViewPage sessionId={sessionId} />
      </AppShell>
    );
  }
  if (view === "spec-review" && specId) {
    return (
      <AppShell showBackToRuns pageLabel="Spec review">
        <SpecReviewPage specId={specId} />
      </AppShell>
    );
  }
  if (view === "spec-diff" && sessionId) {
    return (
      <AppShell showBackToRuns pageLabel="Spec diff">
        <SpecDiffPage sessionId={sessionId} />
      </AppShell>
    );
  }
  return (
    <AppShell>
      <RunListLanding />
    </AppShell>
  );
}

// Truncate session ids in the page-label so the topbar reads cleanly
// regardless of full id length. Format: <date>-<HHMMSS>-<6hex>; the
// trailing hex is the most useful disambiguator at a glance.
function shortSession(id: string): string {
  if (id.length <= 14) return id;
  const tail = id.slice(-6);
  return `…${tail}`;
}

createRoot(root).render(
  <React.StrictMode>
    {renderRoute()}
  </React.StrictMode>,
);
