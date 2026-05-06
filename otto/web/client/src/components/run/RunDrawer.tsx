// RunDrawer — the new design's MC drawer (research §7 + wireframes screen 3).
//
// Top-level layout:
//   - VerdictHeader (outcome + intent + counts + wall+cost)
//   - Run-level action bar: Pause / Resume / Cancel (A7)
//   - GroupList (current dispatch state)
//   - FeatureList (value breakdown)
//   - Guardrails (pinned negative scope)
//   - StageTimeline (Compile → Build → Audit → Render → Land)
//
// During live builds, GroupList leads because groups are the actual dispatch
// unit. Features inherit group build state until Audit produces per-feature
// verdicts.
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

type ResourcePanel =
  | { kind: "logs" }
  | { kind: "files" }
  | { kind: "group-logs"; groupId: string }
  | { kind: "group-diff"; groupId: string };

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
  group_id: string;
  branch: string;
  diff: string;
  truncated: boolean;
  error: string | null;
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
    setResourcePanel((current) => (current?.kind === "logs" ? null : { kind: "logs" }));
  }, []);
  const onOpenFiles = useCallback(() => {
    setResourcePanel((current) => (current?.kind === "files" ? null : { kind: "files" }));
  }, []);
  const onOpenGroupLogs = useCallback((groupId: string) => {
    setResourcePanel((current) =>
      current?.kind === "group-logs" && current.groupId === groupId
        ? null
        : { kind: "group-logs", groupId },
    );
  }, []);
  const onOpenGroupDiff = useCallback((groupId: string) => {
    setResourcePanel((current) =>
      current?.kind === "group-diff" && current.groupId === groupId
        ? null
        : { kind: "group-diff", groupId },
    );
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
          data-testid="run-quick-action-files"
          onClick={onOpenFiles}
        >
          Files
        </button>
      </div>
      {resourcePanel ? (
        <RunResourcePanel sessionId={sessionId} target={resourcePanel} />
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
      {/* R3-B28: section dividers between Groups / Features / Guardrails /
          Stages — major drawer sections each carry a top border + breathing
          margin so the eye can find boundaries without reading headings. */}
      <section className="group-section run-drawer-section">
        <GroupList
          groups={view.groups}
          dispatch={view.dispatch}
          onOpenDiff={onOpenGroupDiff}
          onOpenLogs={onOpenGroupLogs}
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

function RunResourcePanel({sessionId, target}: {sessionId: string; target: ResourcePanel}) {
  const [result, setResult] = useState<{
    endpoint: string;
    payload: LogsPayload | FilesPayload | DiffPayload;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const endpoint = resourceEndpoint(sessionId, target);
  const title = resourceTitle(target);
  const testId = resourceTestId(target);
  const payload = result?.endpoint === endpoint ? result.payload : null;

  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setError(null);
    fetch(endpoint)
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        return resp.json() as Promise<LogsPayload | FilesPayload | DiffPayload>;
      })
      .then((body) => {
        if (!cancelled) setResult({ endpoint, payload: body });
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [endpoint]);

  return (
    <section className="run-resource-panel" data-testid={testId}>
      <h3>{title}</h3>
      {error ? <p role="alert">Failed to load {title.toLowerCase()}: {error}</p> : null}
      {!payload && !error ? <p>Loading {title.toLowerCase()}...</p> : null}
      {payload && (target.kind === "logs" || target.kind === "group-logs") ? (
        <LogsPanel payload={payload as LogsPayload} />
      ) : null}
      {payload && target.kind === "files" ? (
        <FilesPanel payload={payload as FilesPayload} />
      ) : null}
      {payload && target.kind === "group-diff" ? (
        <DiffPanel payload={payload as DiffPayload} />
      ) : null}
    </section>
  );
}

function resourceEndpoint(sessionId: string, target: ResourcePanel): string {
  const encodedSession = encodeURIComponent(sessionId);
  if (target.kind === "logs") return `/api/run-view/${encodedSession}/logs`;
  if (target.kind === "files") return `/api/run-view/${encodedSession}/files`;
  const encodedGroup = encodeURIComponent(target.groupId);
  if (target.kind === "group-logs") {
    return `/api/run-view/${encodedSession}/groups/${encodedGroup}/logs`;
  }
  return `/api/run-view/${encodedSession}/groups/${encodedGroup}/diff`;
}

function resourceTitle(target: ResourcePanel): string {
  if (target.kind === "logs") return "Logs";
  if (target.kind === "files") return "Session files";
  if (target.kind === "group-logs") return `Group logs: ${target.groupId}`;
  return `Group diff: ${target.groupId}`;
}

function resourceTestId(target: ResourcePanel): string {
  if (target.kind === "logs") return "run-resource-panel-logs";
  if (target.kind === "files") return "run-resource-panel-files";
  return `run-resource-panel-${target.kind}-${target.groupId}`;
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
    return <p>No session files have been written yet.</p>;
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
  if (payload.error) {
    return <p role="alert">{payload.error}</p>;
  }
  if (!payload.diff) {
    return <p>No diff has been recorded for this group yet.</p>;
  }
  return (
    <>
      <p className="run-resource-note">
        {payload.branch ? `Branch ${payload.branch}` : `Group ${payload.group_id}`}
        {payload.truncated ? " · truncated" : ""}
      </p>
      <pre className="run-diff-text">{payload.diff}</pre>
    </>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
