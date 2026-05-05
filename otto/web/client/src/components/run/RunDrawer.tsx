// RunDrawer — the new design's MC drawer (research §7 + wireframes screen 3).
//
// Top-level layout:
//   - VerdictHeader (outcome + intent + counts + wall+cost)
//   - Run-level action bar: Pause / Resume / Cancel (A7)
//   - FeatureList (primary surface)
//   - Guardrails (pinned negative scope)
//   - GroupList (secondary, collapsed by default; per-Group Abort buttons)
//   - StageTimeline (Compile → Build → Audit → Render → Land)
//
// Per the user directive (priority on Feature, not Group), FeatureList
// is the primary surface; GroupList is one click below.

import { useCallback, useState } from "react";
import type { RunView } from "../../types/run";
import { FeatureList } from "./FeatureList";
import { GroupList } from "./GroupList";
import { Guardrails } from "./Guardrails";
import { StageTimeline } from "./StageTimeline";
import { VerdictHeader } from "./VerdictHeader";

interface Props {
  view: RunView;
  onSelectFeature?: (featureId: string) => void;
  onAfterAction?: () => void;
}

const TERMINAL_VERDICTS = new Set(["passed", "partial", "blocked"]);

async function postSessionAction(
  sessionId: string,
  path: string,
  body: Record<string, unknown> = {},
): Promise<{ ok: boolean; message: string | null }> {
  const resp = await fetch(`/api/run-view/${encodeURIComponent(sessionId)}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: { ok?: boolean; message?: string | null } = {};
  try {
    data = (await resp.json()) as typeof data;
  } catch {
    /* non-JSON response; fall through with HTTP-only signal */
  }
  return {
    ok: Boolean(data.ok ?? resp.ok),
    message: data.message ?? null,
  };
}

export function RunDrawer({ view, onSelectFeature, onAfterAction }: Props) {
  const [pending, setPending] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const inFlight = view.verdict === null || !TERMINAL_VERDICTS.has(view.verdict);
  const sessionId = view.meta.session_id;

  const onPause = useCallback(async () => {
    setPending("pause");
    const { ok, message } = await postSessionAction(sessionId, "/actions/pause");
    setBanner(message ?? (ok ? "Run paused" : "Pause failed"));
    setPending(null);
    onAfterAction?.();
  }, [sessionId, onAfterAction]);

  const onResume = useCallback(async () => {
    setPending("resume");
    const { ok, message } = await postSessionAction(sessionId, "/actions/resume");
    setBanner(message ?? (ok ? "Run resumed" : "Resume failed"));
    setPending(null);
    onAfterAction?.();
  }, [sessionId, onAfterAction]);

  const onAbortGroup = useCallback(
    async (groupId: string) => {
      setPending(`abort:${groupId}`);
      const { ok, message } = await postSessionAction(
        sessionId,
        `/groups/${encodeURIComponent(groupId)}/abort`,
        { reason: "operator abort" },
      );
      setBanner(message ?? (ok ? `Group ${groupId} aborted` : "Abort failed"));
      setPending(null);
      onAfterAction?.();
    },
    [sessionId, onAfterAction],
  );

  return (
    <article className="run-drawer" data-testid="run-drawer">
      <VerdictHeader view={view} />
      {inFlight && (
        <div className="run-action-bar" data-testid="run-action-bar">
          <button
            type="button"
            className="run-action-pause"
            data-testid="run-action-pause"
            onClick={onPause}
            disabled={pending === "pause"}
          >
            Pause
          </button>
          <button
            type="button"
            className="run-action-resume"
            data-testid="run-action-resume"
            onClick={onResume}
            disabled={pending === "resume"}
          >
            Resume
          </button>
          {banner && (
            <span
              className="run-action-banner"
              data-testid="run-action-banner"
            >
              {banner}
            </span>
          )}
        </div>
      )}
      <section className="feature-section">
        <h3>Features</h3>
        <FeatureList
          features={view.features}
          {...(onSelectFeature ? { onSelect: onSelectFeature } : {})}
        />
      </section>
      <Guardrails guardrails={view.guardrails} />
      <section className="group-section">
        <GroupList
          groups={view.groups}
          {...(inFlight
            ? {
                onAbort: onAbortGroup,
                pendingAbortId: pending?.startsWith("abort:")
                  ? pending.slice("abort:".length)
                  : null,
              }
            : {})}
        />
      </section>
      <section className="stage-section">
        <h3>Stages</h3>
        <StageTimeline stages={view.stages} />
      </section>
    </article>
  );
}

export default RunDrawer;
