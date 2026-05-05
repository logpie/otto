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
//
// Destructive actions (Pause and per-Group Abort) are gated behind a
// ConfirmDialog so a single mis-click cannot pause a run or throw away
// a Group's worktree work. Resume is non-destructive and fires inline.

import { useCallback, useState } from "react";
import { ConfirmDialog, type ConfirmState } from "../ConfirmDialog";
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
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [confirmPending, setConfirmPending] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const inFlight = view.verdict === null || !TERMINAL_VERDICTS.has(view.verdict);
  const sessionId = view.meta.session_id;

  const closeConfirm = useCallback(() => {
    if (confirmPending) return;
    setConfirm(null);
    setConfirmError(null);
  }, [confirmPending]);

  const runConfirm = useCallback(async () => {
    if (!confirm) return;
    setConfirmPending(true);
    setConfirmError(null);
    try {
      await confirm.onConfirm();
      setConfirm(null);
    } catch (err) {
      setConfirmError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfirmPending(false);
    }
  }, [confirm]);

  const requestPause = useCallback(() => {
    setConfirm({
      title: "Pause this run?",
      body: "Use Resume to continue.",
      confirmLabel: "Pause",
      tone: "primary",
      onConfirm: async () => {
        setPending("pause");
        try {
          const { ok, message } = await postSessionAction(sessionId, "/actions/pause");
          setBanner(message ?? (ok ? "Run paused" : "Pause failed"));
          if (!ok) throw new Error(message ?? "Pause failed");
        } finally {
          setPending(null);
          onAfterAction?.();
        }
      },
    });
  }, [sessionId, onAfterAction]);

  const onResume = useCallback(async () => {
    setPending("resume");
    const { ok, message } = await postSessionAction(sessionId, "/actions/resume");
    setBanner(message ?? (ok ? "Run resumed" : "Resume failed"));
    setPending(null);
    onAfterAction?.();
  }, [sessionId, onAfterAction]);

  const requestAbortGroup = useCallback(
    (groupId: string) => {
      setConfirm({
        title: `Abort Group ${groupId}?`,
        body: "Its worktree work will be lost.",
        confirmLabel: "Abort Group",
        tone: "danger",
        onConfirm: async () => {
          setPending(`abort:${groupId}`);
          try {
            const { ok, message } = await postSessionAction(
              sessionId,
              `/groups/${encodeURIComponent(groupId)}/abort`,
              { reason: "operator abort" },
            );
            setBanner(message ?? (ok ? `Group ${groupId} aborted` : "Abort failed"));
            if (!ok) throw new Error(message ?? "Abort failed");
          } finally {
            setPending(null);
            onAfterAction?.();
          }
        },
      });
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
            onClick={requestPause}
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
                onAbort: requestAbortGroup,
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
      {confirm && (
        <ConfirmDialog
          confirm={confirm}
          pending={confirmPending}
          error={confirmError}
          checkboxAck={false}
          onChangeCheckboxAck={() => {}}
          onCancel={closeConfirm}
          onConfirm={runConfirm}
        />
      )}
    </article>
  );
}

export default RunDrawer;
