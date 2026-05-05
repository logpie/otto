// RunDrawer — the new design's MC drawer (research §7 + wireframes screen 3).
//
// Top-level layout:
//   - VerdictHeader (outcome + intent + counts + wall+cost)
//   - FeatureList (primary surface)
//   - Guardrails (pinned negative scope)
//   - GroupList (secondary, collapsed by default)
//   - StageTimeline (Compile → Build → Audit → Render → Land)
//
// Per the user directive (priority on Feature, not Group), FeatureList
// is the primary surface; GroupList is one click below.

import type { RunView } from "../../types/run";
import { FeatureList } from "./FeatureList";
import { GroupList } from "./GroupList";
import { Guardrails } from "./Guardrails";
import { StageTimeline } from "./StageTimeline";
import { VerdictHeader } from "./VerdictHeader";

interface Props {
  view: RunView;
  onSelectFeature?: (featureId: string) => void;
}

export function RunDrawer({ view, onSelectFeature }: Props) {
  return (
    <article className="run-drawer" data-testid="run-drawer">
      <VerdictHeader view={view} />
      <section className="feature-section">
        <h3>Features</h3>
        <FeatureList
          features={view.features}
          {...(onSelectFeature ? { onSelect: onSelectFeature } : {})}
        />
      </section>
      <Guardrails guardrails={view.guardrails} />
      <section className="group-section">
        <GroupList groups={view.groups} />
      </section>
      <section className="stage-section">
        <h3>Stages</h3>
        <StageTimeline stages={view.stages} />
      </section>
    </article>
  );
}

export default RunDrawer;
