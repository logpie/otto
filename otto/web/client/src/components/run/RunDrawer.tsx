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

import { useCallback, useEffect, useState } from "react";
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

const TERMINAL_STATUSES = new Set(["passed", "partial", "blocked", "landed", "interrupted", "aborted", "failed"]);

type ResourcePanel = "logs" | "artifacts" | "diff";

interface LogsPayload {
  logs: Array<{
    label: string;
    path: string;
    size_bytes: number;
    text: string;
    truncated: boolean;
  }>;
  empty: boolean;
}

interface FilesPayload {
  files: Array<{
    path: string;
    size_bytes: number;
    kind: string;
  }>;
  truncated: boolean;
}

interface DiffPayload {
  text: string;
}

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
  const [resourcePanel, setResourcePanel] = useState<ResourcePanel | null>(null);
  const inFlight = view.verdict === null && !TERMINAL_STATUSES.has(view.status);
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

  const onOpenProofPacket = useCallback(() => {
    if (!view.meta.proof_packet_html) return;
    window.open(
      `/api/run-view/${encodeURIComponent(sessionId)}/proof-packet.html`,
      "_blank",
      "noopener,noreferrer",
    );
  }, [sessionId, view.meta.proof_packet_html]);
  const onViewSpec = useCallback(() => {
    window.location.href = `?view=spec-review&spec=${encodeURIComponent(sessionId)}`;
  }, [sessionId]);
  const onOpenLogs = useCallback(() => {
    setResourcePanel((current) => (current === "logs" ? null : "logs"));
  }, []);
  const onOpenArtifacts = useCallback(() => {
    setResourcePanel((current) => (current === "artifacts" ? null : "artifacts"));
  }, []);
  const onOpenDiff = useCallback(() => {
    setResourcePanel((current) => (current === "diff" ? null : "diff"));
  }, []);

  return (
    <article className="run-drawer" data-testid="run-drawer">
      <VerdictHeader view={view} />
      <div className="run-quick-actions" data-testid="run-quick-actions">
        <button
          type="button"
          className="run-quick-action run-quick-action-primary"
          data-testid="run-quick-action-proof"
          onClick={onOpenProofPacket}
          disabled={!view.meta.proof_packet_html}
          title={view.meta.proof_packet_html ? "Open the rendered proof packet" : "Proof packet has not been produced yet"}
        >
          Open proof packet
        </button>
        <button
          type="button"
          className="run-quick-action"
          data-testid="run-quick-action-spec"
          onClick={onViewSpec}
        >
          View spec
        </button>
        <button
          type="button"
          className="run-quick-action"
          data-testid="run-quick-action-logs"
          onClick={onOpenLogs}
        >
          Logs
        </button>
        <button
          type="button"
          className="run-quick-action"
          data-testid="run-quick-action-artifacts"
          onClick={onOpenArtifacts}
        >
          Artifacts
        </button>
        <button
          type="button"
          className="run-quick-action"
          data-testid="run-quick-action-diff"
          onClick={onOpenDiff}
        >
          Diff
        </button>
      </div>
      {resourcePanel ? (
        <RunResourcePanel key={resourcePanel} sessionId={sessionId} kind={resourcePanel} />
      ) : null}
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
      {/* R3-B28: section dividers between Features / Guardrails / Groups /
          Stages — major drawer sections each carry a top border + breathing
          margin so the eye can find boundaries without reading headings. */}
      <section className="feature-section run-drawer-section">
        <h3>Features</h3>
        <FeatureList
          features={view.features}
          findings={view.findings}
          {...(onSelectFeature ? { onSelect: onSelectFeature } : {})}
        />
      </section>
      {view.guardrails.length > 0 && (
        <section className="guardrail-section run-drawer-section">
          <Guardrails guardrails={view.guardrails} />
        </section>
      )}
      <section className="group-section run-drawer-section">
        {/* R3-B29: GroupList's own <summary> is now styled to match the
            h3 weight of "Features" / "Stages", so it doubles as the
            section header. No additional <h3> needed here. */}
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
      <section className="stage-section run-drawer-section">
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

function RunResourcePanel({sessionId, kind}: {sessionId: string; kind: ResourcePanel}) {
  const [payload, setPayload] = useState<LogsPayload | FilesPayload | DiffPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPayload(null);
    setError(null);
    const endpoint = kind === "artifacts" ? "files" : kind;
    void (async () => {
      try {
        const resp = await fetch(`/api/run-view/${encodeURIComponent(sessionId)}/${endpoint}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        let body: LogsPayload | FilesPayload | DiffPayload;
        if (kind === "diff") {
          body = {text: await resp.text()};
        } else {
          body = await resp.json() as LogsPayload | FilesPayload;
        }
        if (!cancelled) setPayload(body);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, kind]);

  return (
    <section className="run-resource-panel" data-testid={`run-resource-panel-${kind}`}>
      <h3>{kind === "logs" ? "Logs" : kind === "diff" ? "Diff" : "Artifacts"}</h3>
      {error ? <p role="alert">Failed to load {kind}: {error}</p> : null}
      {!payload && !error ? <p>Loading {kind}...</p> : null}
      {payload && kind === "logs" ? (
        <LogsPanel payload={payload as LogsPayload} />
      ) : null}
      {payload && kind === "artifacts" ? (
        <FilesPanel payload={payload as FilesPayload} />
      ) : null}
      {payload && kind === "diff" ? (
        <DiffPanel payload={payload as DiffPayload} />
      ) : null}
    </section>
  );
}

function LogsPanel({payload}: {payload: LogsPayload}) {
  if (payload.empty || payload.logs.length === 0) {
    return <p>No session logs have been written yet.</p>;
  }
  return (
    <div className="run-resource-list">
      {payload.logs.map((log) => (
        <details key={log.path} className="run-resource-item" open={payload.logs.length === 1}>
          <summary>
            <span>{log.label}</span>
            <small>{formatBytes(log.size_bytes)}{log.truncated ? " · tail" : ""}</small>
          </summary>
          <pre>{log.text || "(empty)"}</pre>
        </details>
      ))}
    </div>
  );
}

function FilesPanel({payload}: {payload: FilesPayload}) {
  if (payload.files.length === 0) {
    return <p>No artifacts have been written yet.</p>;
  }
  return (
    <>
      <ul className="run-file-list">
        {payload.files.map((file) => (
          <li key={file.path}>
            <span>{file.path}</span>
            <small>{file.kind} · {formatBytes(file.size_bytes)}</small>
          </li>
        ))}
      </ul>
      {payload.truncated ? <p className="run-resource-note">Showing the first 300 files.</p> : null}
    </>
  );
}

function DiffPanel({payload}: {payload: DiffPayload}) {
  const text = payload.text.trimEnd();
  if (!text) {
    return <p>No diff is available yet.</p>;
  }
  return <pre className="run-resource-diff">{text}</pre>;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
