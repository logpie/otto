import {useState} from "react";
import type {MutableRefObject, ReactNode} from "react";
import type {DiffResponse, LandingItem, RunDetail, StateResponse, VerificationPolicy} from "../../types";

export function BulkLandingConfirmList({items, target, verificationPolicyRef}: {
  items: LandingItem[];
  target: string;
  verificationPolicyRef?: MutableRefObject<VerificationPolicy>;
}) {
  if (!items.length) return null;
  return (
    <div>
      {verificationPolicyRef && <VerificationPolicySelector policyRef={verificationPolicyRef} />}
      <div
        className="confirm-bulk-list"
        data-testid="confirm-bulk-list"
        role="region"
        aria-label="Tasks to land"
      >
        <ul>
          {items.map((item) => {
            const branch = item.branch || "(no branch)";
            const fileCount = Number(item.changed_file_count || 0);
            const previewFiles = (item.changed_files || []).slice(0, 3);
            const overflow = Math.max(0, fileCount - previewFiles.length);
            return (
              <li
                key={item.task_id}
                className="confirm-bulk-row"
                data-testid={`confirm-bulk-row-${item.task_id}`}
              >
                <div className="confirm-bulk-row-head">
                  <strong>{item.task_id}</strong>
                  <span>
                    <code>{branch}</code> &rarr; <code>{target}</code>
                  </span>
                  <span className="confirm-bulk-row-count">
                    {fileCount} file{fileCount === 1 ? "" : "s"}
                  </span>
                </div>
                {previewFiles.length > 0 && (
                  <ul className="confirm-bulk-row-files">
                    {previewFiles.map((path) => <li key={path}><code>{path}</code></li>)}
                    {overflow > 0 && <li className="muted">+{overflow} more</li>}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

export function SingleMergeConfirmDetails({detail, diff, verificationPolicyRef}: {
  detail: RunDetail | null;
  diff: DiffResponse | null;
  verificationPolicyRef?: MutableRefObject<VerificationPolicy>;
}) {
  if (!detail) return null;
  const packet = detail.review_packet;
  const taskId = detail.queue_task_id || detail.run_id;
  const branch = packet.changes.branch || diff?.branch || detail.branch || "(no branch)";
  const target = packet.changes.target || diff?.target || "main";
  const fileCount = Number(packet.changes.file_count || diff?.file_count || 0);
  const files = (packet.changes.files && packet.changes.files.length
    ? packet.changes.files
    : diff?.files || []).slice(0, 5);
  const overflow = Math.max(0, fileCount - files.length);
  return (
    <div>
      {verificationPolicyRef && <VerificationPolicySelector policyRef={verificationPolicyRef} />}
      <div
        className="confirm-merge-details"
        data-testid="confirm-merge-details"
        role="region"
        aria-label="Merge details"
      >
        <dl>
          <dt>Task</dt>
          <dd data-testid="confirm-merge-task-id"><code>{taskId}</code></dd>
          <dt>Branch</dt>
          <dd>
            <code>{branch}</code> &rarr; <code>{target}</code>
          </dd>
          <dt>Files</dt>
          <dd data-testid="confirm-merge-file-count">{fileCount} file{fileCount === 1 ? "" : "s"}</dd>
        </dl>
        {files.length > 0 && (
          <ul className="confirm-merge-files" data-testid="confirm-merge-files">
            {files.map((path) => <li key={path}><code>{path}</code></li>)}
            {overflow > 0 && <li className="muted">+{overflow} more</li>}
          </ul>
        )}
      </div>
    </div>
  );
}

function VerificationPolicySelector({policyRef}: {policyRef: MutableRefObject<VerificationPolicy>}) {
  const [policy, setPolicy] = useState<VerificationPolicy>(policyRef.current || "smart");
  const update = (next: VerificationPolicy) => {
    policyRef.current = next;
    setPolicy(next);
  };
  const selected = VERIFICATION_POLICY_OPTIONS.find((option) => option.value === policy);
  return (
    <div className="confirm-verification-policy" data-testid="confirm-verification-policy">
      <label htmlFor="landingVerificationPolicy">Verification</label>
      <select
        id="landingVerificationPolicy"
        data-testid="confirm-verification-policy-select"
        value={policy}
        onChange={(event) => update(event.target.value as VerificationPolicy)}
      >
        {VERIFICATION_POLICY_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
      <p>{selected?.description || ""}</p>
    </div>
  );
}

const VERIFICATION_POLICY_OPTIONS: Array<{value: VerificationPolicy; label: string; description: string}> = [
  {
    value: "smart",
    label: "Smart verification",
    description: "Default. Otto certifies the integration based on risk, changed files, and prior task evidence.",
  },
  {
    value: "fast",
    label: "Fast landing",
    description: "Pure git landing only. Use when you need speed and already trust the task proof.",
  },
  {
    value: "full",
    label: "Full verification",
    description: "Certify every merged story. Slower, useful for conflicts or high-risk releases.",
  },
  {
    value: "skip",
    label: "Skip verification",
    description: "Land without post-merge certification. Audit responsibility stays with the operator.",
  },
];

export function describeCleanupConfirm(detail: RunDetail | null): {
  title: string;
  body: string;
  confirmLabel: string;
} {
  if (!detail) {
    return {
      title: "Cleanup",
      body: "Remove this run record? This cannot be undone from Mission Control.",
      confirmLabel: "Cleanup",
    };
  }
  const status = String(detail.status || "").toLowerCase();
  const queuedStatuses = new Set(["queued", "pending", "waiting", "starting"]);
  const isQueued = queuedStatuses.has(status);
  if (isQueued) {
    const taskId = detail.queue_task_id || detail.run_id;
    return {
      title: "Remove queued task",
      body: `Remove queued task ${taskId} from the queue? It will not run; this cannot be undone from Mission Control.`,
      confirmLabel: "Remove queued task",
    };
  }
  const worktreeName = (detail.worktree || "").split("/").pop() || detail.worktree || detail.run_id;
  return {
    title: "Remove run + cleanup",
    body: `Remove run record and cleanup worktree ${worktreeName}? This cannot be undone from Mission Control.`,
    confirmLabel: "Remove and cleanup",
  };
}

export function describeCancelConfirm(detail: RunDetail | null, runId: string): {
  title: string;
  body: string;
  confirmLabel: string;
} {
  const taskId = detail?.queue_task_id || runId;
  const body = `Cancel task ${taskId}. Otto will signal the agent to stop. If it doesn't acknowledge within 30s, Mission Control will terminate the process. Work in progress may be lost.`;
  return {
    title: "Cancel task",
    body,
    confirmLabel: "Cancel task",
  };
}

export function describeWatcherStopConfirm(data: StateResponse | null): {
  body: string;
  detail: ReactNode;
  requireAck: boolean;
} {
  if (!data) {
    return {
      body: "Stop the queue runner?",
      detail: null,
      requireAck: false,
    };
  }
  const counts = data.watcher.counts || {};
  const pid = data.watcher.health.blocking_pid || data.watcher.health.watcher_pid;
  const running = Number(counts.running || 0) + Number(counts.terminating || 0);
  const queued = Number(counts.queued || 0);
  const backlog = Number(data.runtime.command_backlog.pending || 0)
    + Number(data.runtime.command_backlog.processing || 0);
  const pidText = pid ? `pid ${pid}` : "process";
  const body = `Stop queue runner (${pidText}).`;
  const requireAck = running > 0 || queued > 0 || backlog > 0;
  const detail = (
    <ul
      className="confirm-watcher-stop"
      data-testid="confirm-watcher-stop-detail"
      aria-label="Watcher stop impact"
    >
      <li>
        <strong data-testid="confirm-watcher-stop-pid">{pidText}</strong>
      </li>
      <li>
        <span data-testid="confirm-watcher-stop-running">
          {running} running task{running === 1 ? "" : "s"} may be interrupted.
        </span>
      </li>
      <li>
        <span data-testid="confirm-watcher-stop-queued">
          {queued} queued task{queued === 1 ? "" : "s"}
        </span>
        {" and "}
        <span data-testid="confirm-watcher-stop-backlog">
          {backlog} pending command{backlog === 1 ? "" : "s"}
        </span>
        {" will wait until you restart the watcher."}
      </li>
    </ul>
  );
  return {body, detail, requireAck};
}
