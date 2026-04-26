import {
  CANCELLABLE_TASK_STATUSES,
  canMerge,
  computeBoardEmptyReason,
  mergeButtonTitle,
  statusTone,
  taskBoardColumns,
  taskBoardSubtitle,
  testIdForTask,
} from "../../App";
import type {BoardTask, Filters} from "../../App";
import {shortText} from "../../utils/format";
import type {StateResponse} from "../../types";

/**
 * Single-table queue replacement for the kanban TaskBoard. Linear-style.
 * mc-audit redesign Phase C, refactored Phase G.
 */
export function TaskQueueList({
  data, filters, selectedRunId, selectedQueuedTaskId, onSelect, onSelectQueued, onLandReady, onCancelRun, onClearFilters, onNewJob,
}: {
  data: StateResponse | null;
  filters: Filters;
  selectedRunId: string | null;
  selectedQueuedTaskId?: string | null;
  onSelect: (runId: string) => void;
  onSelectQueued?: (task: BoardTask) => void;
  onLandReady?: () => void;
  onCancelRun?: (runId: string, taskTitle: string) => void;
  onClearFilters?: () => void;
  onNewJob?: () => void;
}) {
  const columns = taskBoardColumns(data, filters);
  const items: BoardTask[] = [];
  for (const stage of ["working", "attention", "ready", "landed"] as const) {
    const column = columns.find((c) => c.stage === stage);
    if (column) items.push(...column.items);
  }
  const readyCount = data?.landing.counts.ready || 0;
  const emptyReason = computeBoardEmptyReason(data, filters, columns);
  const totalLabel = items.length === 1 ? "1 task" : `${items.length} tasks`;
  const target = data?.landing.target || "main";
  return (
    <section className="queue-list" data-testid="task-board" aria-labelledby="taskBoardHeading">
      <header className="queue-list-head">
        <div>
          <h2 id="taskBoardHeading">Tasks</h2>
          <p>{items.length ? `${totalLabel} on ${target}` : taskBoardSubtitle(data, filters)}</p>
        </div>
        <div className="queue-list-actions">
          {readyCount > 0 && (
            <button
              className="primary"
              type="button"
              disabled={!onLandReady || !canMerge(data?.landing)}
              title={mergeButtonTitle(data?.landing)}
              onClick={onLandReady}
            >
              {readyCount === 1 ? "Land 1 ready" : `Land ${readyCount} ready`}
            </button>
          )}
        </div>
      </header>
      {emptyReason && emptyReason !== "has-tasks" ? (
        <div
          className={`queue-list-empty queue-list-empty-${emptyReason}`}
          data-testid="task-board-empty"
          data-empty-reason={emptyReason}
          role="status"
        >
          {emptyReason === "loading" && <span>Loading tasks…</span>}
          {emptyReason === "no-project" && <span>No project selected.</span>}
          {emptyReason === "filtered-empty" && (
            <>
              <span>No matching tasks.</span>
              {onClearFilters && (
                <button
                  type="button"
                  className="queue-list-empty-action"
                  data-testid="task-board-empty-clear-filters"
                  onClick={onClearFilters}
                >Clear filters</button>
              )}
            </>
          )}
          {emptyReason === "true-empty" && (
            <div className="queue-list-empty-hero">
              <strong>No tasks yet</strong>
              <p>Describe what you want Otto to build, certify, or improve.</p>
              {onNewJob && (
                <button
                  type="button"
                  className="primary"
                  data-testid="task-board-empty-queue-job"
                  onClick={onNewJob}
                >Queue your first job</button>
              )}
            </div>
          )}
        </div>
      ) : null}
      {items.length > 0 && (
        <div className="queue-list-table" role="table" aria-label="Tasks">
          <div className="queue-list-row queue-list-row-head" role="row">
            <span role="columnheader">Status</span>
            <span role="columnheader">Task</span>
            <span role="columnheader">Stories</span>
            <span role="columnheader">Files</span>
            <span role="columnheader">Time</span>
            <span role="columnheader" aria-label="Actions"></span>
          </div>
          {items.map((task) => (
            <TaskRow
              key={`${task.source}-${task.id}`}
              task={task}
              selected={Boolean(
                (task.runId && task.runId === selectedRunId)
                || (!task.runId && selectedQueuedTaskId && task.id === selectedQueuedTaskId)
              )}
              onSelect={onSelect}
              {...(onSelectQueued ? {onSelectQueued} : {})}
              {...(onCancelRun ? {onCancelRun} : {})}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function TaskRow({task, selected, onSelect, onSelectQueued, onCancelRun}: {
  task: BoardTask;
  selected: boolean;
  onSelect: (runId: string) => void;
  onSelectQueued?: (task: BoardTask) => void;
  onCancelRun?: (runId: string, taskTitle: string) => void;
}) {
  const isQueuedNoRun = !task.runId;
  const cancellable = Boolean(task.runId)
    && Boolean(onCancelRun)
    && CANCELLABLE_TASK_STATUSES.has(String(task.status || "").toLowerCase());
  const stageLabel = (() => {
    switch (task.stage) {
      case "attention": return "Needs action";
      case "working":   return task.active ? "Running" : "Queued";
      case "ready":     return "Ready";
      case "landed":    return "Landed";
      default:          return task.status;
    }
  })();
  const tone = statusTone(task.status, task.stage);
  const stories = task.storiesTested && task.storiesTested > 0
    ? `${task.storiesPassed || 0}/${task.storiesTested}`
    : "—";
  const files = typeof task.changedFileCount === "number" ? `${task.changedFileCount}` : "—";
  const time = task.elapsedDisplay || task.durationDisplay || "—";
  const onClick = () => {
    if (task.runId) onSelect(task.runId);
    else if (onSelectQueued) onSelectQueued(task);
  };
  return (
    <div
      className={`queue-list-row queue-list-row-task tone-${tone} ${selected ? "selected" : ""}`}
      role="row"
      data-run-id={task.runId || undefined}
      data-task-id={task.id}
      data-stage={task.stage}
    >
      <button
        type="button"
        className="queue-list-row-main"
        data-testid={testIdForTask(task.id)}
        data-queued-no-run={isQueuedNoRun ? "true" : undefined}
        aria-pressed={selected}
        aria-label={`${task.title}: ${task.status}`}
        disabled={isQueuedNoRun && !onSelectQueued}
        onClick={onClick}
      >
        <span className="queue-list-cell queue-list-cell-status" role="cell">
          <span className={`queue-list-status-dot tone-${tone}`} aria-hidden="true" />
          {stageLabel}
        </span>
        <span className="queue-list-cell queue-list-cell-task" role="cell">
          <strong title={task.title}>{task.title}</strong>
          {task.summary ? <em title={task.summary}>{shortText(task.summary, 90)}</em> : null}
        </span>
        <span className="queue-list-cell queue-list-cell-num" role="cell">{stories}</span>
        <span className="queue-list-cell queue-list-cell-num" role="cell">{files}</span>
        <span className="queue-list-cell queue-list-cell-num" role="cell">{time}</span>
      </button>
      <span className="queue-list-cell queue-list-cell-actions" role="cell">
        {cancellable && task.runId && (
          <button
            type="button"
            className="queue-list-row-cancel"
            data-testid={`task-card-cancel-${task.id.replace(/[^a-zA-Z0-9_-]+/g, "-")}`}
            aria-label={`Cancel task ${task.title}`}
            title={`Cancel task ${task.title}`}
            onClick={(event) => {
              event.stopPropagation();
              if (task.runId && onCancelRun) onCancelRun(task.runId, task.title);
            }}
          >Cancel</button>
        )}
      </span>
    </div>
  );
}
