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

import { useCallback, useEffect, useRef, useState } from "react";
import { ConfirmDialog, type ConfirmState } from "../ConfirmDialog";
import type { GroupView, RunView } from "../../types/run";
import { formatTokenSpend } from "../../utils/format";
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

interface ResourceSelection {
  kind: ResourcePanel;
  groupId: string | null;
}

const STAGE_LABELS: Record<string, string> = {
  compile: "Compile spec",
  spec_review: "Spec review",
  seed: "Prepare fixtures",
  build: "Build groups",
  audit: "Audit product",
  render: "Render proof",
  land: "Land on main",
};

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
  text?: string;
  group_id?: string;
  branch?: string;
  diff?: string;
  truncated?: boolean;
  error?: string | null;
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
  const [resourcePanel, setResourcePanel] = useState<ResourceSelection | null>(null);
  const inFlight = view.verdict === null && !TERMINAL_STATUSES.has(view.status);
  const isPaused = view.status === "paused" || view.control_plane?.status === "paused";
  const canPause = inFlight && !isPaused;
  const canResume = inFlight && isPaused;
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
    if (!view.meta.spec_path) return;
    window.location.href = `?view=spec-review&spec=${encodeURIComponent(sessionId)}`;
  }, [sessionId, view.meta.spec_path]);
  const openResource = useCallback((kind: ResourcePanel, groupId: string | null = null) => {
    setResourcePanel((current) =>
      current?.kind === kind && current.groupId === groupId ? null : {kind, groupId},
    );
  }, []);
  const onOpenLogs = useCallback(() => {
    openResource("logs");
  }, [openResource]);
  const onOpenArtifacts = useCallback(() => {
    openResource("artifacts");
  }, [openResource]);
  const onOpenDiff = useCallback(() => {
    openResource("diff");
  }, [openResource]);
  const onOpenGroupLogs = useCallback((groupId: string) => {
    openResource("logs", groupId);
  }, [openResource]);
  const onOpenGroupDiff = useCallback((groupId: string) => {
    openResource("diff", groupId);
  }, [openResource]);

  return (
    <article className="run-drawer" data-testid="run-drawer">
      <VerdictHeader view={view} />
      <CurrentWorkSummary
        view={view}
        onOpenLogs={onOpenGroupLogs}
        onOpenDiff={onOpenGroupDiff}
      />
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
          disabled={!view.meta.spec_path}
          title={view.meta.spec_path ? "Open the compiled spec" : "Spec is not available yet"}
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
        <RunResourcePanel
          key={`${resourcePanel.kind}:${resourcePanel.groupId ?? "all"}`}
          sessionId={sessionId}
          kind={resourcePanel.kind}
          groupId={resourcePanel.groupId}
          groupName={
            resourcePanel.groupId
              ? view.groups.find((group) => group.id === resourcePanel.groupId)?.name ?? null
              : null
          }
        />
      ) : null}
      {(canPause || canResume) && (
        <div className="run-action-bar" data-testid="run-action-bar">
          {canPause ? (
            <button
              type="button"
              className="run-action-pause"
              data-testid="run-action-pause"
              onClick={requestPause}
              disabled={pending === "pause"}
            >
              Pause
            </button>
          ) : null}
          {canResume ? (
            <button
              type="button"
              className="run-action-resume"
              data-testid="run-action-resume"
              onClick={onResume}
              disabled={pending === "resume"}
            >
              Resume
            </button>
          ) : null}
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

function CurrentWorkSummary({
  view,
  onOpenLogs,
  onOpenDiff,
}: {
  view: RunView;
  onOpenLogs: (groupId: string) => void;
  onOpenDiff: (groupId: string) => void;
}) {
  const activeGroups = view.groups.filter((group) => group.status === "in_progress");
  const displayGroups = activeGroups.length > 0 ? activeGroups : nextPendingGroups(view.groups);
  const activeStage = view.stages.find((stage) => stage.status === "active");
  const provider = view.provider ?? null;
  if (displayGroups.length === 0 && !activeStage && !provider) return null;
  const providerTokens = provider ? formatTokenSpend(provider.token_usage) : "";
  const changedFiles = provider?.diff_summary?.changed_files ?? [];
  return (
    <section className="current-work-panel" data-testid="current-work-panel" aria-label="Current work">
      <div className="current-work-heading">
        <h3>{activeGroups.length > 0 ? "Working now" : "Up next"}</h3>
        {activeStage ? (
          <span className="current-work-stage">
            Stage: {stageLabel(activeStage.name)}
          </span>
        ) : null}
      </div>
      {provider ? (
        <div className="current-work-provider" data-testid="current-work-provider">
          <span>
            {provider.provider}
            {provider.current_activity ? ` · ${provider.current_activity}` : ""}
            {provider.status ? ` · ${provider.status}` : ""}
          </span>
          <span>
            {providerTokens || "tokens pending"}
            {changedFiles.length > 0
              ? ` · ${changedFiles.length} file${changedFiles.length === 1 ? "" : "s"} changed`
              : ""}
          </span>
        </div>
      ) : null}
      {displayGroups.map((group) => {
        const features = view.features.filter((feature) => feature.group_id === group.id);
        return (
          <article key={group.id} className="current-work-group" data-testid={`current-work-group-${group.id}`}>
            <div className="current-work-group-main">
              <strong>{group.name}</strong>
              <span>{features.length} feature{features.length === 1 ? "" : "s"}</span>
            </div>
            {features.length > 0 ? (
              <p title={features.map((feature) => feature.name).join(", ")}>
                {features.slice(0, 3).map((feature) => feature.name).join(", ")}
                {features.length > 3 ? ` +${features.length - 3} more` : ""}
              </p>
            ) : null}
            <div className="current-work-actions">
              <button type="button" onClick={() => onOpenLogs(group.id)}>
                Logs
              </button>
              <button type="button" onClick={() => onOpenDiff(group.id)}>
                Diff
              </button>
            </div>
          </article>
        );
      })}
    </section>
  );
}

function nextPendingGroups(groups: GroupView[]): GroupView[] {
  const firstPending = groups.find((group) => group.status === "pending");
  return firstPending ? [firstPending] : [];
}

function stageLabel(name: string): string {
  return STAGE_LABELS[name] ?? name;
}

function RunResourcePanel({
  sessionId,
  kind,
  groupId,
  groupName,
}: {
  sessionId: string;
  kind: ResourcePanel;
  groupId: string | null;
  groupName: string | null;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const [payload, setPayload] = useState<LogsPayload | FilesPayload | DiffPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const endpoint = resourceEndpoint(sessionId, kind, groupId);
  const title = resourceTitle(kind, groupName ?? groupId);

  useEffect(() => {
    let cancelled = false;
    setPayload(null);
    setError(null);
    void (async () => {
      try {
        const resp = await fetch(endpoint);
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        const contentType = resp.headers.get("content-type") || "";
        let body: LogsPayload | FilesPayload | DiffPayload;
        if (kind === "diff" && !contentType.includes("application/json")) {
          body = {text: await resp.text()};
        } else {
          body = await resp.json() as LogsPayload | FilesPayload | DiffPayload;
        }
        if (!cancelled) setPayload(body);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [endpoint, kind]);

  useEffect(() => {
    panelRef.current?.scrollIntoView({block: "nearest"});
  }, [kind, groupId]);

  return (
    <section
      ref={panelRef}
      className="run-resource-panel"
      data-testid={`run-resource-panel-${kind}`}
    >
      <h3>{title}</h3>
      {error ? <p role="alert">Failed to load {title.toLowerCase()}: {error}</p> : null}
      {!payload && !error ? <p>Loading {title.toLowerCase()}...</p> : null}
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

function resourceEndpoint(sessionId: string, kind: ResourcePanel, groupId: string | null): string {
  const encodedSession = encodeURIComponent(sessionId);
  if (kind === "artifacts") return `/api/run-view/${encodedSession}/files`;
  if (!groupId) return `/api/run-view/${encodedSession}/${kind}`;
  const encodedGroup = encodeURIComponent(groupId);
  if (kind === "logs") {
    return `/api/run-view/${encodedSession}/groups/${encodedGroup}/logs`;
  }
  return `/api/run-view/${encodedSession}/groups/${encodedGroup}/diff`;
}

function resourceTitle(kind: ResourcePanel, scope: string | null): string {
  const base = kind === "logs" ? "Logs" : kind === "diff" ? "Diff" : "Artifacts";
  return scope ? `${base} for ${scope}` : base;
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
  const text = (payload.text ?? payload.diff ?? "").trimEnd();
  if (payload.error) {
    return <p role="alert">{payload.error}</p>;
  }
  if (!text) {
    return <p>No diff is available yet.</p>;
  }
  return (
    <>
      {(payload.branch || payload.group_id || payload.truncated) ? (
        <p className="run-resource-note">
          {payload.branch ? `Branch ${payload.branch}` : payload.group_id ? `Group ${payload.group_id}` : "Diff"}
          {payload.truncated ? " · truncated" : ""}
        </p>
      ) : null}
      <pre className="run-resource-diff">{text}</pre>
    </>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
