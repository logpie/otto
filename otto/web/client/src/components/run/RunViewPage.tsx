// RunViewPage — top-level page wrapper that fetches a RunView for a given
// session_id, renders the RunDrawer, and handles per-Feature drilldown.
//
// Parent prop `onSelectFeature` is preserved as an optional escape hatch
// (e.g. to navigate the host frame when the page is embedded). When the
// parent doesn't supply it, RunViewPage manages its own drilldown state:
// clicking a Feature row swaps to <FeatureDrilldown/>, "Back" returns
// to the drawer.

import { useState } from "react";
import { FeatureDrilldown } from "./FeatureDrilldown";
import { RunDrawer } from "./RunDrawer";
import { useRunView } from "./useRunView";

interface Props {
  sessionId: string;
  onSelectFeature?: (featureId: string) => void;
}

export function RunViewPage({ sessionId, onSelectFeature }: Props) {
  const { data, loading, error, errorStatus, reload } = useRunView(sessionId);
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(null);

  if (loading && !data) {
    return (
      <div className="run-view-loading" data-testid="run-view-loading">
        <p>Loading run {sessionId}…</p>
      </div>
    );
  }
  if (error) {
    // B19 — friendly 404 page. For 404 we know the run doesn't exist
    // (or never did); a "Retry" button just hammers the same dead URL.
    // Surface a "Back to runs" primary action instead. For non-404
    // errors (network / 5xx) we keep a "Try anyway" secondary that
    // calls reload — those failures are usually transient.
    const isNotFound = errorStatus === 404;
    if (isNotFound) {
      // R3-B50: AppShell topbar already renders "← Back to runs" on
      // every non-landing route. Drop the in-card duplicate so the
      // 404 surface stays focused on the explanation; navigation
      // belongs to the chrome.
      return (
        <div
          className="run-view-error run-view-not-found"
          data-testid="run-view-not-found"
          role="alert"
        >
          <h2>Run not found</h2>
          <p>
            Session ID <code>{sessionId}</code> doesn{"’"}t exist. It may
            have been deleted, or the URL is wrong.
          </p>
          {/* R3-B52: tell the user concretely what to do next.
              Session ids are auto-generated, so a typo is the most
              common cause; copying from the browser's URL bar back
              into a known-good list (Runs landing) recovers fast. */}
          <p className="run-view-not-found-hint">
            Check the URL — session ids look like{" "}
            <code>2026-05-05-141532-a1b2c3</code>. Or pick a session from
            the <a href="/">Runs list</a>.
          </p>
        </div>
      );
    }
    return (
      <div className="run-view-error" data-testid="run-view-error" role="alert">
        <h2>Couldn{"’"}t load this run</h2>
        <p className="run-view-error-detail">{error}</p>
        <div className="run-view-error-actions">
          <a className="primary-action" href="/" data-testid="run-view-back-to-runs">
            Back to runs
          </a>
          <button
            type="button"
            className="secondary-action"
            onClick={reload}
            data-testid="run-view-try-anyway"
          >
            Try anyway
          </button>
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="run-view-empty" data-testid="run-view-empty">
        <p>No run selected.</p>
      </div>
    );
  }

  // If the parent supplied onSelectFeature, defer to it (host-frame
  // navigation). Otherwise, render the drilldown inline.
  if (onSelectFeature) {
    return (
      <RunDrawer
        view={data}
        onSelectFeature={onSelectFeature}
        onAfterAction={reload}
      />
    );
  }

  if (selectedFeatureId !== null) {
    const feature = data.features.find((f) => f.id === selectedFeatureId);
    if (feature) {
      return (
        <FeatureDrilldown
          feature={feature}
          view={data}
          onBack={() => setSelectedFeatureId(null)}
        />
      );
    }
    // Feature id no longer exists in the view (e.g. after spec edit);
    // silently fall through to the drawer.
  }

  return (
    <RunDrawer
      view={data}
      onSelectFeature={(id) => setSelectedFeatureId(id)}
      onAfterAction={reload}
    />
  );
}

export default RunViewPage;
