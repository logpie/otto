import React from "react";
import {createRoot} from "react-dom/client";
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

function renderRoute() {
  if (view === "run-view" && sessionId) {
    return <RunViewPage sessionId={sessionId} />;
  }
  if (view === "spec-review" && specId) {
    return <SpecReviewPage specId={specId} />;
  }
  if (view === "spec-diff" && sessionId) {
    return <SpecDiffPage sessionId={sessionId} />;
  }
  return <RunListLanding />;
}

createRoot(root).render(
  <React.StrictMode>
    {renderRoute()}
  </React.StrictMode>,
);
