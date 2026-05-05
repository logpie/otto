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
  const { data, loading, error, reload } = useRunView(sessionId);
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(null);

  if (loading && !data) {
    return (
      <div className="run-view-loading" data-testid="run-view-loading">
        <p>Loading run {sessionId}…</p>
      </div>
    );
  }
  if (error) {
    return (
      <div className="run-view-error" data-testid="run-view-error">
        <p>Failed to load run: {error}</p>
        <button type="button" onClick={reload}>
          Retry
        </button>
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
    return <RunDrawer view={data} onSelectFeature={onSelectFeature} />;
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
    />
  );
}

export default RunViewPage;
