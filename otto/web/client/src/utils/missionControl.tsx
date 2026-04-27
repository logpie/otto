import {useEffect, useMemo, useRef} from "react";
import type {ReactNode} from "react";
import {ApiError} from "../api";
import type {
  ActionResult,
  AgentBuildConfig,
  ArtifactRef,
  CommandBacklogItem,
  DiffResponse,
  LandingItem,
  LandingState,
  LiveRunItem,
  OutcomeFilter,
  RunBuildConfig,
  RunDetail,
  StateResponse,
  WatcherInfo,
} from "../types";
import type {
  BoardStage,
  BoardTask,
  Filters,
  InspectorMode,
  ResultBannerState,
  ToastState,
  ViewMode,
} from "../uiTypes";
import {defaultFilters} from "../uiTypes";
import {
  capitalize,
  formatCompactNumber,
  formatDuration,
  formatTechnicalIssue,
  refreshLabel,
  shortText,
  titleCase,
  tokenTotal,
  usageLine,
} from "./format";

export type BoardEmptyReason =
  | "has-tasks"
  | "loading"
  | "no-project"
  | "filtered-empty"
  | "true-empty";

export function filtersAreActive(filters: Filters): boolean {
  return (
    filters.type !== "all"
    || filters.outcome !== "all"
    || filters.query.trim() !== ""
    || filters.activeOnly
  );
}

export function computeBoardEmptyReason(
  data: StateResponse | null,
  filters: Filters,
  columns: ReadonlyArray<{items: ReadonlyArray<unknown>}>,
): BoardEmptyReason {
  if (!data) return "loading";
  if (!data.project) return "no-project";
  const visible = columns.reduce((sum, column) => sum + column.items.length, 0);
  if (visible > 0) return "has-tasks";
  // No visible tasks. Are the filters hiding them?
  if (filtersAreActive(filters)) {
    // Recompute against default filters: if there ARE tasks then, filters
    // are doing the hiding.
    const allColumns = taskBoardColumns(data);
    const allCount = allColumns.reduce((sum, column) => sum + column.items.length, 0);
    if (allCount > 0) return "filtered-empty";
  }
  return "true-empty";
}

// mc-audit W8-CRITICAL-1: statuses for which an in-flight task can still
// be cancelled via the watcher. Mirrors the backend's cancel-eligible set
// so the per-row Cancel button doesn't render for already-finished work.
// `terminating` is intentionally excluded — it's already cancelling.
/**
 * Heavy-user paper-cut #2: page-local sort. We sort the rows we already
 * have (the current paginated slice) — server-side sort across all rows is
 * a followup. Comparators are domain-aware: token usage/duration use numeric
 * values from the API (token_usage / duration_s), not the display strings,
 * so "2K" doesn't sort ahead of "10K". USD is only a fallback for legacy
 * rows with no token usage.
 */
// EventTimeline moved to components/EventTimeline.tsx


// CommandList moved to components/MicroComponents.tsx

/**
 * Heavy-user paper-cut #6: render log text with `<mark>`-wrapped matches.
 * The Nth match (0-indexed by `activeMatchIdx`) gets an `active` class so
 * Prev/Next nav can scroll the focused match into view. We don't replace
 * the existing line-classification (`renderLogText`) — instead we
 * pre-segment the text by match boundaries and run each segment through
 * the same line splitter. ANSI color is dropped inside highlighted
 * segments to keep the implementation simple; the test suite asserts
 * `<mark>` presence not ANSI nesting.
 */
/**
 * True when the keydown event target is a text-entry surface — input,
 * textarea, contentEditable. Used by global hotkeys (`/`, Cmd-K) to stay
 * out of the user's way while they're typing.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
}

/**
 * Provenance card for the proof-of-work file backing the drawer.
 *
 * Cluster-evidence-trustworthiness #3: surfaces the recorded run_id /
 * branch / head_sha / sha256 / file mtime so the operator can confirm
 * the rendered evidence belongs to this run. When the proof's
 * ``run_id`` does not match the run record being viewed (likely a
 * stale or mis-routed file) we render a prominent warning rather than
 * silently rendering somebody else's evidence.
 */
/**
 * Per-round certification tabs.
 *
 * Cluster-evidence-trustworthiness #4: surface ``round_history`` from
 * the proof-of-work so a multi-round cert (where round 1 found bugs
 * and round 2 passed after a fix) shows verdict, counts, durations,
 * and per-round diagnosis instead of collapsing to the final state.
 */
/**
 * Render the active proof artifact: text, image, video, or "no preview".
 *
 * Cluster-evidence-trustworthiness #6: previously every artifact was
 * decoded as UTF-8 and shoved into a ``<pre>``. Server-side MIME
 * detection now tells us whether the body is previewable text. When it
 * is not, we route image/video MIMEs to ``<img>``/``<video>`` against
 * the raw artifact endpoint and otherwise show a "no text preview;
 * download artifact" message with the size + MIME so the operator can
 * decide what to do.
 */
// Render "Showing N hunks of M · X KB of Y MB" so the operator can tell
// how much of the diff is hidden by the 240k char slice. Falls back to a
// concise byte-only line when the server didn't report a hunk count.
// formatDiffTruncationBanner / formatRelativeFreshness extracted to
// utils/format.ts during mc-audit redesign Wave 8.

// ReviewMetric / ReviewDrawer moved to components/MicroComponents.tsx

/**
 * Surface the most-relevant recovery action (Retry / Resume / Cleanup /
 * Requeue) next to the run header for failed/paused/interrupted runs. The
 * full set still lives under "Advanced run actions" below — this bar is a
 * shortcut for the obvious-next-step. mc-audit codex-first-time-user.md #14.
 */
// W3-CRITICAL-1: a single prior-run candidate the JobDialog's "Refine which
// run?" dropdown can show. The label is what the operator sees; run_id is
// what the server uses to look up the prior branch.
/**
 * Build the list of prior runs the operator can pick to iterate on.
 *
 * Inputs:
 * - ``landingItems`` — terminal-ready queue runs that are still on their
 *   build branch (they haven't been merged yet). These are the natural
 *   "refine the most recent thing" candidates.
 * - ``historyItems`` — every terminal run we know about. We keep only
 *   ``build`` and ``improve`` runs that succeeded and have a recorded
 *   branch. ``certify`` runs land no branch; ``merge`` runs are not a
 *   useful base.
 *
 * Output: deduped by run_id, sorted "freshest first" by relying on the
 * caller's order (history is newest-first and landing.items are queued
 * order). Keep at most 25 — improve is point-in-time, the operator
 * doesn't need a 200-row select.
 *
 * Why not just expose all history? Because picking a 6-month-old build to
 * "improve" is almost always operator error — a long-tail dropdown invites
 * mistakes. 25 is enough to reach back through a normal day's work.
 */
/**
 * Build the human-readable "Will run with: …" line shown above Advanced
 * options. Mirrors the precedence the queue payload builder uses: the user
 * override wins, otherwise we fall back to the project's effective defaults
 * coming from otto.yaml. mc-audit codex-first-time-user.md #2.
 */
// configSourceLabel / titleCase moved to utils/format.ts (Wave 8).

// ConfirmDialog moved to components/ConfirmDialog.tsx

export function missionFocus(data: StateResponse | null): {
  kicker: string;
  title: string;
  body: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
  primary: "new" | "start" | "land" | "diagnostics" | "recover" | "resolve";
  working: number;
  needsAction: number;
  ready: number;
  // True when the project has no completed runs and nothing in flight — this
  // is the very first time the user opens this project. The MissionFocus
  // component reads this to flip the primary CTA from "New job" to
  // "Start first build". mc-audit codex-first-time-user.md #6.
  firstRun: boolean;
} {
  if (!data) {
    return {
      kicker: "Loading",
      title: "Reading project state",
      body: "Loading project state.",
      tone: "info",
      primary: "new",
      working: 0,
      needsAction: 0,
      ready: 0,
      firstRun: false,
    };
  }
  const columns = taskBoardColumns(data);
  const working = columns.find((column) => column.stage === "working")?.items.length || 0;
  const needsAction = columns.find((column) => column.stage === "attention")?.items.length || 0;
  const ready = columns.find((column) => column.stage === "ready")?.items.length || 0;
  const rawReady = data.landing.counts.ready || 0;
  const queued = data.watcher.counts.queued || 0;
  const commandBacklog = Number(data.runtime.command_backlog.pending || 0) + Number(data.runtime.command_backlog.processing || 0);
  const target = data.landing.target || "main";
  if (mergeRecoveryNeeded(data.landing)) {
    return {
      kicker: "Landing",
      title: "Landing needs recovery",
      body: "A previous landing left a partial merge. Recover will clean it up and retry.",
      tone: "danger",
      primary: "recover",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (commandBacklog && data.watcher.health.state !== "running") {
    return {
      kicker: "Commands",
      title: `${commandBacklog} command${commandBacklog === 1 ? "" : "s"} waiting`,
      body: "Start the watcher to run pending commands.",
      tone: "warning",
      primary: "start",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (data.landing.merge_blocked && rawReady) {
    const dirty = data.landing.dirty_files.slice(0, 3).join(", ");
    return {
      kicker: "Repository",
      title: "Cleanup required before landing",
      body: dirty ? `Local changes block landing: ${dirty}.` : "Local repository state blocks landing.",
      tone: "danger",
      primary: "diagnostics",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (needsAction) {
    if (supersededFailedTaskIds(data.landing).length) {
      return {
        kicker: "Release",
        title: `${needsAction} stale task${needsAction === 1 ? "" : "s"} can be cleaned`,
        body: "A failed attempt is superseded by landed work. Otto can clean the stale card and leave the board focused on current release state.",
        tone: "warning",
        primary: "resolve",
        working,
        needsAction,
        ready,
        firstRun: false,
      };
    }
    return {
      kicker: "Attention",
      title: `${needsAction} task${needsAction === 1 ? "" : "s"} need action`,
      body: "Open the failure or Health to investigate.",
      tone: "warning",
      primary: "diagnostics",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (ready) {
    return {
      kicker: "Review",
      title: `${ready} task${ready === 1 ? "" : "s"} ready to land`,
      body: `Review evidence and changed files, then land ready work into ${target}.`,
      tone: "success",
      primary: "land",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (queued && data.watcher.health.state !== "running") {
    return {
      kicker: "Queue",
      title: `${queued} queued task${queued === 1 ? "" : "s"} waiting`,
      body: "Start the watcher to run this task.",
      tone: "info",
      primary: "start",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (working) {
    // mc-audit info-density #6: surface a headline for the *hottest* active
    // run instead of a bare count. Pick the most recently active live item
    // (sort by elapsed_s ascending — newest first; fall back to first
    // working board card). Headline format:
    //   "<task-id> · <branch> · <elapsed> · <usage> · <last event>"
    // Each segment is omitted if missing, so the line never reads as "·· ·".
    const liveActive = data.live.items
      .filter((item) => item.active || ["starting", "running", "initializing", "terminating"].includes(item.display_status))
      .slice()
      .sort((a, b) => Number(a.elapsed_s || 0) - Number(b.elapsed_s || 0));
    const hottest = liveActive[0];
    const headline = hottest
      ? [
          hottest.queue_task_id || hottest.display_id || hottest.run_id,
          hottest.branch || null,
          hottest.elapsed_display || null,
          usageLine(hottest) !== "-" ? usageLine(hottest) : null,
          hottest.overlay?.reason || hottest.last_event || null,
        ]
          .filter((segment): segment is string => Boolean(segment && segment.trim()))
          .join(" · ")
      : null;
    const titleSuffix = working === 1 ? "" : "s";
    return {
      kicker: "Working",
      title: headline || `${working} task${titleSuffix} in flight`,
      body: working > 1
        ? `${working} tasks active. Review packets will update as tasks finish.`
        : "Run is active. Review packet will update when it finishes.",
      tone: "info",
      primary: "new",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  if (data.landing.counts.total || data.history.total_rows) {
    return {
      kicker: "Idle",
      title: "Idle",
      body: "Queue your next task.",
      tone: "neutral",
      primary: "new",
      working,
      needsAction,
      ready,
      firstRun: false,
    };
  }
  return {
    kicker: "Start",
    title: "Start your first build",
    body: "Describe what you want. Otto plans, codes, verifies, and shows the result for review.",
    tone: "neutral",
    primary: "new",
    working,
    needsAction,
    ready,
    firstRun: true,
  };
}

export function taskBoardColumns(data: StateResponse | null, filters: Filters = defaultFilters): Array<{
  stage: BoardStage;
  title: string;
  empty: string;
  items: BoardTask[];
}> {
  const columns: Array<{stage: BoardStage; title: string; empty: string; items: BoardTask[]}> = [
    {stage: "attention", title: "Needs action", empty: "No tasks.", items: []},
    {stage: "working", title: "In progress", empty: "No tasks.", items: []},
    {stage: "ready", title: "Ready to land", empty: "No tasks.", items: []},
    {stage: "landed", title: "Landed", empty: "Nothing landed yet.", items: []},
  ];
  if (!data) return columns;
  const liveByTask = new Map<string, LiveRunItem>();
  const cardsByKey = new Map<string, BoardTask>();
  for (const item of data.live.items) {
    if (item.queue_task_id) liveByTask.set(item.queue_task_id, item);
  }
  for (const item of data.landing.items) {
    const live = liveByTask.get(item.task_id);
    const runId = item.run_id || live?.run_id || null;
    const card = boardTaskFromLanding(item, runId, !data.landing.merge_blocked, live);
    cardsByKey.set(item.task_id, card);
  }
  for (const item of data.live.items) {
    const key = item.queue_task_id || item.run_id;
    if (cardsByKey.has(key)) continue;
    if (!item.queue_task_id && !item.active && !isAttentionStatus(item.display_status)) continue;
    cardsByKey.set(key, boardTaskFromLive(item));
  }
  for (const card of cardsByKey.values()) {
    if (!boardTaskMatchesFilters(card, filters)) continue;
    const column = columns.find((candidate) => candidate.stage === card.stage);
    column?.items.push(card);
  }
  for (const column of columns) {
    column.items.sort(compareBoardTasks);
  }
  return columns;
}

export function boardTaskMatchesFilters(task: BoardTask, filters: Filters): boolean {
  const query = filters.query.trim().toLowerCase();
  if (query) {
    const haystack = [task.id, task.title, task.summary, task.status, task.branch || "", task.reason, task.proof]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(query)) return false;
  }
  if (filters.activeOnly && task.stage !== "working") return false;
  if (filters.outcome !== "all" && !boardTaskMatchesOutcome(task, filters.outcome)) return false;
  return true;
}

export function boardTaskMatchesOutcome(task: BoardTask, outcome: OutcomeFilter): boolean {
  const status = task.status.toLowerCase();
  if (outcome === "success") return ["ready", "landed", "done", "success"].some((value) => status.includes(value));
  if (outcome === "failed") return status.includes("failed") || task.stage === "attention";
  if (outcome === "interrupted") return status.includes("interrupted") || status.includes("stale");
  if (outcome === "cancelled") return status.includes("cancelled");
  if (outcome === "removed") return status.includes("removed");
  if (outcome === "other") return !["ready", "landed", "done", "success", "failed", "interrupted", "stale", "cancelled", "removed"].some((value) => status.includes(value));
  return true;
}

export function boardTaskFromLanding(item: LandingItem, runId: string | null, mergeAllowed: boolean, live?: LiveRunItem): BoardTask {
  const stage = boardStageForLanding(item, mergeAllowed);
  const active = Boolean(live?.active) || isActiveQueueStatus(item.queue_status);
  return {
    id: item.task_id,
    runId,
    title: item.task_id || item.summary || "queue task",
    summary: item.summary || item.task_id || "",
    stage,
    status: boardStatusLabel(item, mergeAllowed),
    branch: item.branch,
    changedFileCount: item.changed_file_count,
    proof: proofLine(item),
    reason: boardReasonForLanding(item, mergeAllowed, live),
    active,
    elapsedDisplay: live?.elapsed_display || landingDurationDisplay(item),
    lastEvent: live?.last_event || null,
    progress: live?.progress || null,
    buildConfig: live?.build_config || item.build_config || null,
    source: "landing",
    storiesPassed: item.stories_passed,
    storiesTested: item.stories_tested,
    usageDisplay: usageLine(item) !== "-" ? usageLine(item) : null,
    durationDisplay: typeof item.duration_s === "number" ? formatDuration(item.duration_s) : null,
  };
}

export function landingDurationDisplay(item: LandingItem): string | null {
  if (!["done", "failed", "cancelled", "interrupted", "removed"].includes(item.queue_status)) return null;
  return typeof item.duration_s === "number" ? formatDuration(item.duration_s) : null;
}

export function boardTaskFromLive(item: LiveRunItem): BoardTask {
  const stage: BoardStage = item.active ? "working" : isAttentionStatus(item.display_status) ? "attention" : "working";
  return {
    id: item.queue_task_id || item.run_id,
    runId: item.run_id,
    title: item.queue_task_id || item.display_id || item.run_id,
    summary: item.command || item.last_event || item.run_id,
    stage,
    status: item.display_status,
    branch: item.branch,
    changedFileCount: null,
    proof: usageLine(item),
    reason: item.overlay?.reason || item.last_event || item.elapsed_display || item.display_status,
    active: item.active,
    elapsedDisplay: item.elapsed_display || null,
    lastEvent: item.last_event || null,
    progress: item.progress || null,
    buildConfig: item.build_config || null,
    source: "live",
    storiesPassed: null,
    storiesTested: null,
    usageDisplay: usageLine(item) !== "-" ? usageLine(item) : null,
    durationDisplay: item.elapsed_display && item.elapsed_display !== "-" ? item.elapsed_display : null,
  };
}

export function boardStageForLanding(item: LandingItem, mergeAllowed: boolean): BoardStage {
  if (item.landing_state === "merged") return "landed";
  if (item.landing_state === "ready") return mergeAllowed ? "ready" : "attention";
  if (isWaitingLandingItem(item)) return "working";
  return "attention";
}

export function boardStatusLabel(item: LandingItem, mergeAllowed: boolean): string {
  if (item.landing_state === "ready") return mergeAllowed ? "ready" : "blocked";
  if (item.landing_state === "merged") return "landed";
  return item.queue_status || item.landing_state || "blocked";
}

export function boardReasonForLanding(item: LandingItem, mergeAllowed: boolean, live?: LiveRunItem): string {
  if (item.landing_state === "ready" && !mergeAllowed) return "Repository cleanup required before landing.";
  if (item.landing_state === "ready") return `${changeLine(item)} changed; ${proofLine(item)} recorded.`;
  if (item.landing_state === "merged") return item.merge_id ? `Landed by ${item.merge_id}.` : "Already landed.";
  if (item.queue_status === "queued") return "Waiting for the watcher.";
  if (item.queue_status === "initializing") return "Child process started; waiting for Otto session readiness.";
  if (["starting", "running", "terminating"].includes(item.queue_status)) {
    if (live?.elapsed_display) return `Running for ${live.elapsed_display}.`;
    return "Task is still in flight.";
  }
  if (item.diff_error) return formatTechnicalIssue(item.diff_error);
  if (!item.branch) return "No branch is recorded.";
  if (["failed", "cancelled", "interrupted", "stale"].includes(item.queue_status)) return "Open the review packet for recovery actions.";
  return "Not ready to land yet.";
}

export function isActiveQueueStatus(status: string): boolean {
  return ["initializing", "starting", "running", "terminating"].includes(status);
}

export function liveEventLabel(task: BoardTask): string | null {
  const event = String(task.lastEvent || "").trim();
  if (!event || event === "-") return null;
  const normalized = event.toLowerCase();
  const status = task.status.toLowerCase();
  if (normalized === status || ["running", "queued", "starting", "initializing"].includes(normalized)) return null;
  return shortText(event, 56);
}

export function progressLabel(task: BoardTask): string | null {
  const progress = String(task.progress || "").trim();
  if (!task.active || !progress) return null;
  return shortText(progress, 110);
}

export function activeRunSummary(data: StateResponse | null): {label: string; detail: string} | null {
  if (!data) return null;
  const active = data.live.items.filter((item) => item.active);
  if (!active.length) return null;
  if (active.length === 1) {
    const item = active[0];
    if (!item) return null;
    const label = shortText(item.queue_task_id || item.display_id || item.run_id, 56);
    const status = titleCase(item.display_status || item.status || "running");
    return {label, detail: [status, item.elapsed_display].filter(Boolean).join(" · ")};
  }
  return {label: `${active.length} active runs`, detail: ""};
}

// mc-audit info-density #2: typed chip data for the task card meta row.
// Each chip carries a `kind` (used for both data-chip-kind attribute + CSS
// selector), a glyph icon for at-a-glance scanning, a label, and an optional
// tooltip with extra context. Chips are suppressed when the underlying value
// is null — never render a "-" placeholder.
// mc-audit info-density #3: map a task's status string + stage to a tone
// keyword so the CSS can colour ready/running/failed/cancelled etc. with
// distinct hues. Tone values:
//   success — ready/landed/done/merged
//   running — starting/running/initializing/terminating/in flight
//   warning — blocked/queued/waiting/paused/interrupted/stale
//   danger  — failed/cancelled/removed/error
//   neutral — anything else
// mc-audit visual-coherence F10 — tone-to-glyph map for status badges so
// signal isn't carried by colour alone. ✓ pass-like, ⚠ warning,
// ✗ danger, ● running (filled circle reads as "in flight"),
// · neutral. Marked aria-hidden where applied; the surrounding label
// already communicates the status to screen readers.
export function toneIcon(tone: "success" | "running" | "warning" | "danger" | "neutral" | string): string {
  switch (tone) {
    case "success":
      return "✓";
    case "warning":
      return "⚠";
    case "danger":
      return "✗";
    case "running":
      return "●";
    default:
      return "·";
  }
}

export function statusTone(
  status: string,
  stage: BoardStage,
): "success" | "running" | "warning" | "danger" | "neutral" {
  const lower = status.toLowerCase();
  if (["ready", "landed", "merged", "done", "success"].some((value) => lower.includes(value))) {
    return "success";
  }
  if (["failed", "cancelled", "canceled", "removed", "error"].some((value) => lower.includes(value))) {
    return "danger";
  }
  if (["starting", "running", "initializing", "terminating", "in flight", "in_flight"].some((value) => lower.includes(value))) {
    return "running";
  }
  if (["queued", "waiting", "paused", "interrupted", "stale", "blocked"].some((value) => lower.includes(value))) {
    return "warning";
  }
  // Stage-based fallback for empty/odd statuses.
  if (stage === "ready") return "success";
  if (stage === "attention") return "danger";
  if (stage === "working") return "running";
  if (stage === "landed") return "success";
  return "neutral";
}

export function compareBoardTasks(left: BoardTask, right: BoardTask): number {
  const stageOrder: Record<BoardStage, number> = {attention: 0, ready: 1, working: 2, landed: 3};
  const byStage = stageOrder[left.stage] - stageOrder[right.stage];
  if (byStage) return byStage;
  return left.title.localeCompare(right.title);
}

export function taskBoardSubtitle(data: StateResponse | null, filters: Filters = defaultFilters): string {
  if (!data) return "Loading tasks.";
  const total = taskBoardColumns(data, filters).reduce((sum, column) => sum + column.items.length, 0);
  if (!total) {
    // mc-audit error-empty-states #11: don't say "No work queued" when the
    // user just has filters hiding their work. Pick wording that matches
    // the empty reason.
    if (filtersAreActive(filters)) {
      const all = taskBoardColumns(data).reduce((sum, column) => sum + column.items.length, 0);
      if (all > 0) return `No tasks match the active filters (${all} hidden).`;
    }
    return "No work queued.";
  }
  const target = data.landing.target || "main";
  return `${total} task${total === 1 ? "" : "s"} on ${target}.`;
}

export function testIdForTask(taskId: string): string {
  return `task-card-${taskId.replace(/[^a-zA-Z0-9_-]+/g, "-")}`;
}

export function visibleRunIds(data: StateResponse): Set<string> {
  return new Set([
    ...data.live.items.map((item) => item.run_id),
    ...data.landing.items.map((item) => item.run_id).filter((value): value is string => Boolean(value)),
    ...data.history.items.map((item) => item.run_id),
  ]);
}

/**
 * W9-IMPORTANT-2: client-side dedupe of stale live entries.
 *
 * The backend's live → history transition is not atomic — a run that has
 * reached terminal can briefly appear in BOTH ``live[]`` and ``history[]``.
 * Without this guard, the UI shows the same run twice (once "running" in
 * the live pane, once "success" in the history pane) until the next poll.
 *
 * The fix is purely cosmetic: prefer the history record (which carries
 * ``terminal_outcome``) and drop the matching live row. We never mutate
 * the server response — the result is a shallow-cloned ``StateResponse``
 * with ``live.items`` filtered. ``visibleRunIds`` and other consumers
 * see the dedup'd shape automatically because ``data`` is the only
 * source of truth flowing into the renderers.
 */
export function dedupeLiveAgainstHistory(data: StateResponse): StateResponse {
  const terminalHistoryIds = new Set<string>();
  for (const item of data.history.items) {
    if (item.terminal_outcome && item.run_id) {
      terminalHistoryIds.add(item.run_id);
    }
  }
  if (terminalHistoryIds.size === 0) return data;
  const filtered = data.live.items.filter((item) => !terminalHistoryIds.has(item.run_id));
  if (filtered.length === data.live.items.length) return data;
  return {
    ...data,
    live: {
      ...data.live,
      items: filtered,
      // Re-derive counts so headline numbers don't disagree with the
      // rendered list. ``active_count`` reflects truly-active rows, so
      // recomputing from the filtered list is the correct floor.
      total_count: filtered.length,
      active_count: filtered.filter((item) => item.active).length,
    },
  };
}

export function refreshIntervalMs(data: StateResponse | null): number {
  return Math.max(700, Math.min(5000, Number(data?.live.refresh_interval_s || 1.5) * 1000));
}

// mc-audit live W11-IMPORTANT-2: standalone `otto build` registers as
// `domain="atomic"` in the live registry, but every user-facing label calls
// it "build". Alias the domain at the UI surface so external automation +
// the type filter share consistent terminology with what the user sees.
// Other domains (queue, merge, supervisor) pass through unchanged.
export function domainLabel(domain: string | null | undefined): string {
  if (!domain) return "-";
  if (domain === "atomic") return "build";
  return domain;
}

export function activeCount(watcher?: WatcherInfo): number {
  const counts = watcher?.counts || {};
  return Number(counts.running || 0)
    + Number(counts.initializing || 0)
    + Number(counts.starting || 0)
    + Number(counts.terminating || 0);
}

// mc-audit codex-first-time-user #15: a project is on its first-run path when
// it has zero history, zero live runs, no landing items, and no queued tasks.
// In that state the sidebar collapses Watcher / Heartbeat / In-flight /
// queued/ready/landed counters into a single "Project ready · No jobs yet"
// summary. As soon as ANY of those counters fills, the full dashboard returns.
export function isProjectFirstRun(data: StateResponse | null | undefined): boolean {
  if (!data) return false;
  const historyItems = Number(data.history?.items?.length || 0);
  const totalRows = Number(data.history?.total_rows || 0);
  const liveCount = Number(data.live?.total_count || data.live?.items?.length || 0);
  const landingItems = Number(data.landing?.items?.length || 0);
  const queued = Number(data.watcher?.counts?.queued || 0);
  const running = activeCount(data.watcher);
  return historyItems === 0
    && totalRows === 0
    && liveCount === 0
    && landingItems === 0
    && queued === 0
    && running === 0;
}

export function canStartWatcher(data?: StateResponse | null): boolean {
  const queued = Number(data?.watcher.counts.queued || 0);
  const backlog = Number(data?.runtime.command_backlog.pending || 0) + Number(data?.runtime.command_backlog.processing || 0);
  return Boolean(data?.runtime.supervisor.can_start && (queued > 0 || backlog > 0));
}

// W3-IMPORTANT-6: when the Start watcher button is disabled because the
// watcher is already running, the previous tooltip leaked the Stop button's
// next_action ("Stop watcher to pause queue dispatch.") onto the Start
// control. That read as "do this to start" — the opposite of what the
// disabled state meant. Compose a Start-specific tooltip so the title and
// the visible label agree.
export function startWatcherTooltip(data?: StateResponse | null): string {
  const blocked = data?.runtime.supervisor.start_blocked_reason || "";
  if (blocked) return blocked;
  if (data?.watcher.health.state === "running") return "Watcher already running.";
  if (data?.watcher.health.state === "stale") return "Stop the stale watcher before starting another one.";
  // Falls back to the shared next_action only when the action is actually
  // about starting (state is "stopped" or unknown).
  return data?.watcher.health.next_action || "";
}

export function canStopWatcher(data?: StateResponse | null): boolean {
  return Boolean(data?.runtime.supervisor.can_stop);
}

export function watcherControlHint(data?: StateResponse | null): string {
  if (!data) return "Loading watcher controls.";
  const queued = Number(data.watcher.counts.queued || 0);
  const backlog = Number(data.runtime.command_backlog.pending || 0) + Number(data.runtime.command_backlog.processing || 0);
  if (canStartWatcher(data)) {
    const work = [queued ? `${queued} queued` : "", backlog ? `${backlog} command${backlog === 1 ? "" : "s"}` : ""].filter(Boolean).join(" and ");
    return `Start watcher to process ${work}.`;
  }
  if (canStopWatcher(data)) return "Running. Stop to pause the queue.";
  if (data.runtime.supervisor.start_blocked_reason) return `Start unavailable: ${data.runtime.supervisor.start_blocked_reason}`;
  if (!queued && !backlog) return "Queue a job to start.";
  return data.watcher.health.next_action || "Watcher controls are unavailable.";
}

export function watcherSummary(watcher?: WatcherInfo): string {
  const health = watcher?.health;
  if (!health) return "stopped";
  // mc-audit redesign §5 W5.3: when the backend still reports "running" but
  // the heartbeat hasn't ticked in >15s, the supervisor process may have
  // crashed silently. Auto-flip the user-facing label to "stale" so the
  // sidebar can't lie. Backend "stale" already works; this guard catches
  // the gap between liveness and the next snapshot. */
  const stale = health.heartbeat_age_s !== null && health.heartbeat_age_s !== undefined && health.heartbeat_age_s > 15;
  if (health.state === "running" && stale) {
    return `stale pid ${health.blocking_pid || "-"} (${Math.round(health.heartbeat_age_s || 0)}s)`;
  }
  if (health.state === "running") return `running pid ${health.blocking_pid || "-"}`;
  if (health.state === "stale") return `stale pid ${health.blocking_pid || "-"}`;
  return "stopped";
}

export function commandBacklogLine(command: CommandBacklogItem): string {
  const id = command.command_id || "command id unknown";
  const target = command.run_id || command.task_id || command.command_id || "target unknown";
  const age = command.age_s === null || command.age_s === undefined ? "" : ` · ${formatDuration(command.age_s)} old`;
  return `${id} · ${target}${age}`;
}

// formatDuration / tokenBreakdownLine / usageLine / storyTotalsFromLanding /
// tokenTotal / formatCompactNumber moved to utils/format.ts
// during mc-audit redesign Wave 8.

export function workflowHealth(data: StateResponse | null): {
  active: number;
  needsAttention: number;
  ready: number;
  repositoryLabel: string;
  repositoryTone: "neutral" | "warning" | "danger";
  watcherLabel: string;
  watcherTone: "neutral" | "success" | "warning";
  runtimeLabel: string;
  runtimeTone: "neutral" | "warning" | "danger";
} {
  if (!data) {
    return {
      active: 0,
      needsAttention: 0,
      ready: 0,
      repositoryLabel: "unknown",
      repositoryTone: "warning",
      watcherLabel: "unknown",
      watcherTone: "warning",
      runtimeLabel: "loading",
      runtimeTone: "warning",
    };
  }
  // mc-audit live W11-IMPORTANT-1: prefer the server-side cross-domain
  // active count so atomic-domain runs (standalone otto build) flow through
  // the diagnostics overview.
  const active = data?.live?.active_count ?? activeCount(data?.watcher);
  const attentionKeys = new Set<string>();
  for (const item of data?.live.items || []) {
    if (isAttentionStatus(item.display_status)) attentionKeys.add(item.queue_task_id || item.run_id);
  }
  for (const item of data?.landing.items || []) {
    if (isAttentionStatus(item.queue_status)) attentionKeys.add(item.task_id);
  }
  const needsAttention = attentionKeys.size;
  const ready = data?.landing.counts.ready || 0;
  const repositoryLabel = data?.landing.merge_blocked
    ? "blocked"
    : data?.project.dirty
    ? "dirty"
    : "clean";
  const repositoryTone = data?.landing.merge_blocked ? "danger" : data?.project.dirty ? "warning" : "neutral";
  const watcherLabel = data?.watcher.health.state || "stopped";
  const watcherTone = data?.watcher.health.state === "running" ? "success" : ready || active || data?.watcher.health.state === "stale" ? "warning" : "neutral";
  const runtimeIssues = data?.runtime.issues.length || 0;
  const runtimeHasError = Boolean(data?.runtime.issues.some((issue) => issue.severity === "error"));
  const runtimeLabel = runtimeIssues ? `${runtimeIssues} issue${runtimeIssues === 1 ? "" : "s"}` : "healthy";
  const runtimeTone = runtimeHasError ? "danger" : runtimeIssues ? "warning" : "neutral";
  return {active, needsAttention, ready, repositoryLabel, repositoryTone, watcherLabel, watcherTone, runtimeLabel, runtimeTone};
}

export function selectOnKeyboard(event: {key: string; preventDefault: () => void}, onSelect: () => void) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  onSelect();
}

// InertEffect + LiveRegion moved to components/a11y.tsx

/**
 * Per-view document.title. mc-audit a11y A11Y-09.
 */
export function useDocumentTitle({viewMode, selectedRunId, selectedDetail, inspectorOpen, inspectorMode}: {
  viewMode: ViewMode;
  selectedRunId: string | null;
  selectedDetail: RunDetail | null;
  inspectorOpen: boolean;
  inspectorMode: InspectorMode;
}) {
  useEffect(() => {
    if (typeof document === "undefined") return;
    const base = "Otto Mission Control";
    let prefix = viewMode === "diagnostics" ? "Diagnostics" : "Tasks";
    if (selectedRunId && selectedDetail) {
      const intent = selectedDetail.title || selectedDetail.run_id;
      const truncated = intent.length > 60 ? `${intent.slice(0, 57)}...` : intent;
      prefix = truncated;
      if (inspectorOpen) {
        const tabLabel = {try: "Try product", proof: "Result", diff: "Code changes", logs: "Logs", artifacts: "Artifacts"}[inspectorMode];
        prefix = `${truncated} - ${tabLabel}`;
      }
    }
    document.title = `${prefix} · ${base}`;
  }, [viewMode, selectedRunId, selectedDetail, inspectorOpen, inspectorMode]);
}

/**
 * Live-region message generator. mc-audit a11y A11Y-10.
 */
export function useLiveAnnouncement({viewMode, selectedRunId, inspectorOpen, inspectorMode}: {
  viewMode: ViewMode;
  selectedRunId: string | null;
  inspectorOpen: boolean;
  inspectorMode: InspectorMode;
}): string {
  return useMemo(() => {
    const parts: string[] = [];
    parts.push(`Viewing ${viewMode === "diagnostics" ? "Diagnostics" : "Tasks"}`);
    if (selectedRunId) {
      parts.push(`run ${selectedRunId}`);
      if (inspectorOpen) {
        const tabLabel = {try: "Try product", proof: "Result", diff: "Code changes", logs: "Logs", artifacts: "Artifacts"}[inspectorMode];
        parts.push(`${tabLabel} tab`);
      }
    }
    return parts.join(", ");
  }, [viewMode, selectedRunId, inspectorOpen, inspectorMode]);
}

export function handleActionResult(
  result: ActionResult,
  fallback: string,
  showToast: (message: string, severity?: ToastState["severity"]) => void,
  setResultBanner: (banner: ResultBannerState | null) => void,
) {
  const severity = actionToastSeverity(result);
  const message = result.message || fallback;
  if (result.clear_banner) {
    setResultBanner(null);
  }
  if (result.ok && !result.modal_title && !result.modal_message) {
    setResultBanner(null);
  }
  if (result.modal_title || result.modal_message) {
    setResultBanner({
      title: result.modal_title || (severity === "error" ? "Action failed" : "Action result"),
      body: result.modal_message || message,
      severity,
    });
  }
  showToast(message, severity);
}

export function actionToastSeverity(result: ActionResult): ToastState["severity"] {
  const severity = String(result.severity || "").toLowerCase();
  if (severity === "error") return "error";
  if (severity === "warning") return "warning";
  return result.ok ? "information" : "warning";
}

export function isAttentionStatus(status: string | null | undefined): boolean {
  return ["failed", "cancelled", "interrupted", "stale"].includes(String(status || "").toLowerCase());
}

export function canMerge(landing?: LandingState): boolean {
  return Boolean(landing && landing.counts.ready > 0 && !landing.merge_blocked);
}

export function canResolveRelease(data?: StateResponse | null): boolean {
  const landing = data?.landing;
  if (!landing) return false;
  if (mergeRecoveryNeeded(landing)) return true;
  if (canMerge(landing)) return true;
  return supersededFailedTaskIds(landing).length > 0;
}

export function mergeRecoveryNeeded(landing?: LandingState): boolean {
  if (!landing?.merge_blocked) return false;
  const blockers = landing.merge_blockers.join(" ").toLowerCase();
  return blockers.includes("merge in progress") || blockers.includes("unmerged path");
}

export function mergeButtonTitle(landing?: LandingState): string {
  if (mergeRecoveryNeeded(landing)) return "Recover or abort the interrupted landing before merging.";
  return landing?.merge_blocked ? "Commit, stash, or revert local project changes before merging." : "";
}

export function mergeBlockedText(landing: LandingState): string {
  if (mergeRecoveryNeeded(landing)) return "Landing is blocked by an interrupted git merge. Use Recover landing.";
  const suffix = landing.dirty_files.length ? `: ${landing.dirty_files.slice(0, 3).join(", ")}` : "";
  return `Merge blocked by local changes${suffix}`;
}

export function landingBulkConfirmation(landing?: LandingState): string {
  const ready = (landing?.items || []).filter((item) => item.landing_state === "ready");
  const target = landing?.target || "main";
  const taskList = ready.slice(0, 5).map((item) => item.task_id).join(", ");
  const suffix = ready.length > 5 ? `, +${ready.length - 5} more` : "";
  const changed = ready.reduce((sum, item) => sum + Number(item.changed_file_count || 0), 0);
  const collisionCount = landing?.collisions.length || 0;
  const collisionNote = collisionCount
    ? ` ${collisionCount} ready-task collision${collisionCount === 1 ? "" : "s"} detected; Otto will fail safely if git cannot merge them.`
    : "";
  return `Land ${ready.length} ready task${ready.length === 1 ? "" : "s"} into ${target}: ${taskList}${suffix}. This uses transactional fast merge, so ${target} updates only if every branch merges cleanly. It will stage ${changed} changed file${changed === 1 ? "" : "s"} across the ready work.${collisionNote}`;
}

export function releaseResolutionConfirmation(data: StateResponse | null): string {
  const landing = data?.landing;
  if (!landing) return "Otto will inspect release state and run the first safe recovery action it can prove.";
  if (mergeRecoveryNeeded(landing)) {
    return "Otto will abort the interrupted git merge, then relaunch conflict-resolving landing for the remaining ready work. This may invoke the configured merge provider.";
  }
  if (canMerge(landing)) {
    return landingBulkConfirmation(landing);
  }
  const cleanup = supersededFailedTaskIds(landing);
  if (cleanup.length) {
    const preview = cleanup.slice(0, 4).join(", ");
    const suffix = cleanup.length > 4 ? `, +${cleanup.length - 4} more` : "";
    return `Otto will clean ${cleanup.length} failed card${cleanup.length === 1 ? "" : "s"} already superseded by landed work: ${preview}${suffix}. Branches and history stay preserved.`;
  }
  return "Otto will inspect release state and report if no safe automated action is available.";
}

export function supersededFailedTaskIds(landing?: LandingState): string[] {
  if (!landing) return [];
  const landed = new Set(
    landing.items
      .filter((item) => item.landing_state === "merged")
      .map((item) => summarySignature(item.summary))
      .filter(Boolean),
  );
  if (!landed.size) return [];
  return landing.items
    .filter((item) => ["failed", "interrupted", "cancelled", "stale"].includes(item.queue_status))
    .filter((item) => item.landing_state === "blocked")
    .filter((item) => landed.has(summarySignature(item.summary)))
    .map((item) => item.task_id);
}

export function summarySignature(value: string | null | undefined): string {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, " ").slice(0, 500);
}

export function isWaitingLandingItem(item: LandingItem): boolean {
  return item.landing_state === "blocked" && ["queued", "starting", "initializing", "running", "terminating"].includes(item.queue_status);
}

export function providerLine(detail: RunDetail): string {
  return providerConfigLine(detail.build_config) || [detail.provider, detail.model, detail.reasoning_effort].filter(Boolean).join(" / ") || "-";
}

export function providerConfigLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "";
  const buildAgent = primaryAgentConfig(config);
  return [
    buildAgent?.provider || config.provider,
    buildAgent?.model || config.model || "provider default model",
    buildAgent?.reasoning_effort || config.reasoning_effort || "provider default reasoning",
  ]
    .filter(Boolean)
    .join(" / ");
}

export function certificationLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  if (config.skip_product_qa) return "Skipped product certification";
  return capitalize(config.certification || `${config.certifier_mode || "fast"} certification`);
}

export function planningLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "";
  if (config.planning === "spec_review") return "Spec review gate";
  if (config.planning === "spec_auto") return "Spec auto-approved";
  if (config.planning === "spec_file") return config.spec_file_path ? `Spec file ${config.spec_file_path}` : "Spec file";
  return "";
}

export function timeoutLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  return [
    config.queue?.task_timeout_s !== null && config.queue?.task_timeout_s !== undefined
      ? `queue timeout ${formatDuration(config.queue.task_timeout_s)}`
      : "queue timeout disabled",
    config.run_budget_seconds ? `run budget ${formatDuration(config.run_budget_seconds)}` : "",
  ].filter(Boolean).join(" · ");
}

export function limitLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  return [
    config.max_certify_rounds ? `${config.max_certify_rounds} cert rounds` : "",
    config.max_turns_per_call ? `${config.max_turns_per_call} max turns/call` : "",
  ].filter(Boolean).join(" · ") || "-";
}

export function flagsLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  const flags = [
    config.split_mode ? "split mode" : "agentic mode",
    config.strict_mode ? "strict" : "",
    config.allow_dirty_repo ? "dirty repo allowed" : "",
  ].filter(Boolean);
  return flags.length ? flags.join(" · ") : "default safeguards";
}

export function agentsLine(config: RunBuildConfig | null | undefined): string {
  const agents = config?.agents;
  if (!agents) return "-";
  const rows = agentRowsForConfig(config).map(([name, label]) => {
    const agent = agents[name];
    const parts = [agent?.provider, agent?.model, agent?.reasoning_effort].filter(Boolean);
    return `${label}: ${parts.join("/") || "default"}`;
  });
  return rows.join(" · ");
}

export function primaryAgentConfig(config: RunBuildConfig): AgentBuildConfig | undefined {
  if (config.command_family === "certify") return config.agents?.certifier;
  if (config.command_family === "improve" && config.split_mode) return config.agents?.fix;
  return config.agents?.build;
}

export function agentRowsForConfig(config: RunBuildConfig): Array<["build" | "certifier" | "spec" | "fix", string]> {
  if (config.command_family === "certify") return [["certifier", "certifier"]];
  if (config.command_family === "improve") {
    return config.split_mode
      ? [["certifier", "evaluator"], ["fix", "improver"]]
      : [["build", "improver"]];
  }
  return config.split_mode
    ? [["build", "builder"], ["certifier", "certifier"], ["fix", "fixer"]]
    : [["build", "builder"]];
}

export function projectConfigLine(config: RunBuildConfig | null | undefined): string {
  if (!config) return "-";
  return [
    config.default_branch ? `target ${config.default_branch}` : "",
    config.test_command ? `tests: ${config.test_command}` : "",
    config.queue?.concurrent ? `${config.queue.concurrent} parallel` : "",
    config.queue?.worktree_dir ? `worktrees ${config.queue.worktree_dir}` : "",
    config.queue?.merge_certifier_mode ? `merge cert ${config.queue.merge_certifier_mode}` : "",
  ].filter(Boolean).join(" · ") || "-";
}

export function productKindHint(kind: string): string {
  switch (kind) {
    case "web":
      return "Start the web server, open the local URL, and exercise the primary browser workflow.";
    case "api":
      return "Start the API service, call the documented endpoints, and verify response bodies and status codes.";
    case "cli":
      return "Run the CLI help, then execute the main happy-path command and check stdout, stderr, and exit code.";
    case "desktop":
      return "Launch the desktop app and walk through the primary window interaction.";
    case "library":
      return "Import the public package from a fresh script and call the documented API.";
    case "worker":
    case "service":
    case "pipeline":
      return "Run the process with a small fixture and verify its output, side effects, and logs.";
    default:
      return "Use the README and artifacts to run the product's main user workflow.";
  }
}

export function shortPath(path: string | null | undefined): string {
  if (!path) return "-";
  const parts = path.split("/");
  if (parts.length <= 4) return path;
  return `.../${parts.slice(-3).join("/")}`;
}

export function detailStatusLabel(detail: RunDetail): string {
  const readiness = detail.review_packet.readiness.state;
  if (readiness === "blocked" || readiness === "merged") return readiness;
  return detail.display_status || "-";
}

export function actionName(key: string): string {
  return {c: "cancel", r: "resume", R: "retry", x: "cleanup", m: "merge", M: "merge-all", a: "approve-spec", g: "regenerate-spec"}[key] || key;
}

export function actionConfirmationBody(action: string, label?: string): string {
  const normalized = (label || action).toLowerCase();
  if (action === "cancel") return "Cancel this run?";
  if (action === "merge") return "Land this task into the target branch?";
  if (action === "approve-spec") return "Approve this spec and start the build?";
  if (action === "regenerate-spec") return "Request spec changes and regenerate before build starts?";
  if (normalized === "remove") return "Remove this queue task?";
  if (normalized === "cleanup") return "Clean up this run?";
  if (normalized === "requeue") return "Requeue this task?";
  if (normalized.startsWith("resume")) return "Resume this run from the saved checkpoint?";
  return `${capitalize(normalized)} this run?`;
}

// Build the merge confirm dialog body from the most recent diff fetch.
// We spell out branch + target with their captured SHAs so the operator
// can compare against the diff metadata header before clicking through —
// this is the human half of the diff-freshness contract.
export function mergeConfirmationBody(diff: DiffResponse): string {
  const target = diff.target || "target";
  const branch = diff.branch || "branch";
  const targetSha = diff.target_sha ? diff.target_sha.slice(0, 7) : null;
  const branchSha = diff.branch_sha ? diff.branch_sha.slice(0, 7) : null;
  const branchPart = branchSha ? `${branch} @ ${branchSha}` : branch;
  const targetPart = targetSha ? `${target} @ ${targetSha}` : target;
  const lead = `Land branch ${branchPart} into target ${targetPart}?`;
  return `${lead} This is the diff you reviewed.`;
}

// --------------------------------------------------------------------------
// Destructive-action confirm helpers (mc-audit Phase 4 cluster H).
// Findings closed:
//   #1 Land all ready: scrollable enumeration + checkbox gate
//   #2 Single merge: task id, branch->target, file count, files preview
//   #3 Cleanup: status-specific irreversible copy, mentions worktree
//   #4 Cancel: task id + SIGTERM 30s + work-loss warning
//   #5 Watcher stop: pid + running/queued/backlog counts
// --------------------------------------------------------------------------

export function proofLine(item: LandingItem): string {
  const passed = Number(item.stories_passed || 0);
  const tested = Number(item.stories_tested || 0);
  if (tested) return `${passed}/${tested} stories`;
  if (item.queue_status) return item.queue_status;
  return "-";
}

export function evidenceLine(packet: RunDetail["review_packet"]): string {
  if (packet.readiness.state === "in_progress") return "-";
  if (isRepositoryBlockedPacket(packet)) return "-";
  const reviewEvidence = packet.evidence.filter(isReviewEvidenceArtifact);
  const existing = reviewEvidence.filter(isReadableArtifact).length;
  if (!reviewEvidence.length) return "-";
  if (!existing) return "not attached";
  return `${existing}/${reviewEvidence.length}`;
}

export function preferredProofArtifact(artifacts: ArtifactRef[]): ArtifactRef | null {
  const existing = artifacts.filter(isReadableArtifact);
  if (!existing.length) return null;
  const preferredLabels = ["proof markdown", "proof json", "summary", "queue manifest", "manifest", "primary log", "intent"];
  for (const label of preferredLabels) {
    const match = existing.find((artifact) => artifact.label.toLowerCase() === label);
    if (match) return match;
  }
  return existing[0] || null;
}

/**
 * Overlay optimistic run states onto the live-run items returned by
 * /api/state. Used by mc-audit microinteractions I4 to flip a row's
 * displayed status to "cancelling" the moment the operator confirms a
 * cancel, instead of waiting for the next /api/state poll. The original
 * server item is otherwise untouched.
 */
export function applyOptimisticRunStates(
  items: LiveRunItem[],
  overlays: Record<string, "cancelling">,
): LiveRunItem[] {
  if (!items.length || !Object.keys(overlays).length) return items;
  return items.map((item) => {
    const overlay = overlays[item.run_id];
    if (!overlay) return item;
    return {
      ...item,
      display_status: overlay,
      active: true,
      last_event: "cancelling",
    };
  });
}

export function canShowDiff(detail: RunDetail | null): boolean {
  if (!detail) return false;
  const packet = detail.review_packet;
  if (!packet.changes.branch || packet.changes.diff_error) return false;
  return packet.readiness.state !== "in_progress";
}

/**
 * Human-readable reason that the Diff control is disabled. Used for `title=`
 * on every Diff button so operators are not left guessing why the control is
 * grey (mc-audit microinteractions C4).
 */
export function diffDisabledReason(detail: RunDetail | null): string {
  if (!detail) return "Select a run to view its diff.";
  const packet = detail.review_packet;
  if (packet.changes.diff_error) return `Diff failed: ${packet.changes.diff_error}`;
  if (!packet.changes.branch) return "Diff is unavailable until the task creates a branch.";
  if (packet.readiness.state === "in_progress") return "Diff is unavailable while the run is still in progress.";
  return "Diff is not available for this run.";
}

export function isReadableArtifact(artifact: ArtifactRef): boolean {
  return artifact.exists && artifact.kind !== "directory";
}

export function isReviewEvidenceArtifact(artifact: ArtifactRef): boolean {
  return artifact.kind !== "directory";
}

export function isLogArtifact(artifact: ArtifactRef | null): boolean {
  if (!artifact) return false;
  const kind = artifact.kind.toLowerCase();
  const label = artifact.label.toLowerCase();
  const path = artifact.path.toLowerCase();
  return kind === "log" || label.includes("log") || path.endsWith(".log");
}

export function artifactKindLabel(artifact: ArtifactRef): string {
  if (!artifact.exists) return `${artifact.kind} (missing)`;
  if (artifact.kind === "directory") return "directory - use Diff for code review";
  if (artifact.kind === "html") return "HTML report";
  if (artifact.kind === "json") return "JSON metadata";
  if (artifact.kind === "text") return "readable text";
  if (artifact.kind === "image") return "image evidence";
  if (artifact.kind === "video") return "video evidence";
  return artifact.kind;
}

export function formatArtifactContent(content: string): string {
  const trimmed = content.trim();
  if (!trimmed) return "";
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return content;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return content;
  }
}

export function renderDiffText(text: string) {
  return text.split(/(\n)/).map((part, index) => {
    if (part === "\n") return part;
    const className = diffLineClass(part);
    return <span className={className} key={`${index}-${part.slice(0, 12)}`}>{part}</span>;
  });
}

interface DiffFileSection {
  path: string;
  text: string;
}

export function splitDiffIntoFiles(text: string, files: string[]): DiffFileSection[] {
  const sections: DiffFileSection[] = [];
  let current: DiffFileSection | null = null;
  const lines = text ? text.split("\n") : [];
  for (const line of lines) {
    const match = line.match(/^diff --git a\/(.*) b\/(.*)$/);
    if (match) {
      current = {path: match[2] || match[1] || `file-${sections.length + 1}`, text: line};
      sections.push(current);
      continue;
    }
    if (current) {
      current.text += `\n${line}`;
    }
  }
  if (sections.length) return sections;
  if (text.trim()) return [{path: files[0] || "diff", text}];
  return files.map((path) => ({path, text: ""}));
}

export function diffLineClass(line: string): string {
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")) return "diff-meta";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-context";
}

export function renderLogText(text: string) {
  const lines = text.split("\n");
  return lines.map((line, index) => (
    <span className={`log-line ${logLineClass(line)}`} key={`log-${index}`}>
      {line ? renderAnsiText(line) : ""}
      {index < lines.length - 1 ? "\n" : ""}
    </span>
  ));
}

export function logLineClass(line: string): string {
  const clean = stripAnsi(line).toLowerCase();
  if (/(— .*starting —|— .*complete —|certify round|fix round|run summary|━━━)/.test(clean)) return "log-line-phase";
  if (/(\bfatal\b|\berror\b|traceback|exception|\bfailed\b|\bfail\b|exit code [1-9])/.test(clean)) return "log-line-error";
  if (/(\bwarn\b|warning|blocked|stale|retry|skipped|caution)/.test(clean)) return "log-line-warn";
  if (/(\bpass\b|passed|success|completed|ready|done)/.test(clean)) return "log-line-success";
  if (/(story_result|story result|stories_tested|stories_passed|verdict|diagnosis|coverage_observed|coverage_gaps|pytest|npm|uv run|\btest\b|collecting|running|\[build\]|\[certify\]|\[merge\]|\[queue\]|\binfo\b)/.test(clean)) return "log-line-info";
  return "log-line-muted";
}

export function stripAnsi(text: string): string {
  return text.replace(/\x1b\[[0-9;]*m/g, "");
}

export function renderAnsiText(text: string) {
  const segments: ReactNode[] = [];
  const pattern = /\x1b\[([0-9;]*)m/g;
  let lastIndex = 0;
  let style: {fg: string; bold: boolean} = {fg: "", bold: false};
  let key = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > lastIndex) {
      appendAnsiSegment(segments, text.slice(lastIndex, match.index), style, key++);
    }
    style = applyAnsiCodes(style, match[1] || "0");
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    appendAnsiSegment(segments, text.slice(lastIndex), style, key++);
  }
  return segments.length ? segments : text;
}

export function appendAnsiSegment(segments: ReactNode[], text: string, style: {fg: string; bold: boolean}, key: number) {
  if (!text) return;
  const className = [style.fg ? `ansi-${style.fg}` : "", style.bold ? "ansi-bold" : ""].filter(Boolean).join(" ");
  if (!className) {
    segments.push(text);
    return;
  }
  segments.push(<span className={className} key={`ansi-${key}`}>{text}</span>);
}

export function applyAnsiCodes(current: {fg: string; bold: boolean}, rawCodes: string): {fg: string; bold: boolean} {
  const codes = rawCodes.split(";").filter(Boolean).map((code) => Number(code));
  if (!codes.length) return {fg: "", bold: false};
  let next = {...current};
  for (const code of codes) {
    if (code === 0) next = {fg: "", bold: false};
    else if (code === 1) next.bold = true;
    else if (code === 22) next.bold = false;
    else if (code === 39) next.fg = "";
    else if (ANSI_COLOR_CLASS[code]) next.fg = ANSI_COLOR_CLASS[code];
  }
  return next;
}

const ANSI_COLOR_CLASS: Record<number, string> = {
  30: "black",
  31: "red",
  32: "green",
  33: "yellow",
  34: "blue",
  35: "magenta",
  36: "cyan",
  37: "white",
  90: "gray",
  91: "red",
  92: "green",
  93: "yellow",
  94: "blue",
  95: "magenta",
  96: "cyan",
  97: "white",
};

export function isRepositoryBlockedPacket(packet: RunDetail["review_packet"]): boolean {
  return packet.readiness.blockers.some((blocker) => blocker.startsWith("Repository has local changes"));
}

export function runEventText(item: LiveRunItem, landingByTask: Map<string, LandingItem>): string {
  const landingItem = item.queue_task_id ? landingByTask.get(item.queue_task_id) : undefined;
  if (landingItem?.landing_state === "ready") return "Ready for review";
  if (landingItem?.landing_state === "merged") return "Landed";
  if (landingItem && isWaitingLandingItem(landingItem)) return landingItem.queue_status === "queued" ? "Queued" : "In progress";
  if (String(item.last_event || "").toLowerCase() === "legacy queue mode") return "Queue task";
  return item.last_event || "-";
}

export function landingStateText(item: LandingItem): string {
  if (item.landing_state === "ready") return "Ready to land";
  if (item.landing_state === "merged") return "Landed";
  if (isWaitingLandingItem(item)) return item.queue_status === "queued" ? "Queued" : "In progress";
  return item.label || "Needs action";
}

export function diagnosticLandingAction(item: LandingItem): string {
  if (item.landing_state === "ready") return `${changeLine(item)} changed; review evidence before landing.`;
  if (item.landing_state === "merged") return item.merge_id ? `Landed by ${item.merge_id}.` : "Already landed.";
  if (item.queue_status === "queued") return "Start the watcher to run this task.";
  if (item.queue_status === "failed") return "Open review packet and requeue or remove.";
  if (item.queue_status === "stale") return "Open review packet and remove stale work.";
  if (item.diff_error) return formatTechnicalIssue(item.diff_error);
  return "Open review packet for next action.";
}

export function changeLine(item: LandingItem): string {
  if (item.diff_error) return "diff error";
  const count = Number(item.changed_file_count || 0);
  if (!count) return "-";
  return `${count} file${count === 1 ? "" : "s"}`;
}

// formatEventTime / storiesLine moved to utils/format.ts (Wave 8).

export function reviewActionLabel(label: string): string {
  const normalized = label.toLowerCase();
  if (normalized === "merge selected") return "Land selected";
  if (normalized === "cleanup") return "Clean run record";
  if (normalized === "remove") return "Remove task";
  return normalized.includes("merge") ? "Land task" : capitalize(label);
}

export function formatReviewText(message: string): string {
  return formatTechnicalIssue(message);
}

export function userVisibleDetailLine(line: string): string | null {
  const normalized = line.toLowerCase();
  if (normalized.startsWith("compat:")) return null;
  return line.replace("legacy queue mode", "queue compatibility mode");
}

// formatTechnicalIssue moved to utils/format.ts (Wave 8).

// mc-audit visual-coherence F10 — status badges must NOT rely on colour
// alone. Prefix every check/story badge with a glyph so deuteranopia /
// protanopia users (~5% of men) can still tell pass/warn/fail apart.
// The icon char is exposed via a `.status-icon` span so test harnesses
// and styling can target it independently of the text label.
export function checkStatusIcon(status: string): string {
  return {
    pass: "✓",
    warn: "⚠",
    fail: "✗",
    pending: "…",
    info: "i",
  }[status] || "i";
}

export function storyStatusIcon(status: string): string {
  return {
    pass: "✓",
    warn: "⚠",
    fail: "✗",
    skipped: "–",
    unknown: "i",
  }[status] || "i";
}

export function checkStatusLabel(status: string): string {
  return {
    pass: "Pass",
    warn: "Warn",
    fail: "Fail",
    pending: "Wait",
    info: "Info",
  }[status] || capitalize(status || "info");
}

export function storyStatusLabel(status: string): string {
  return {
    pass: "Pass",
    warn: "Warn",
    fail: "Fail",
    skipped: "Skip",
    unknown: "Info",
  }[status] || capitalize(status || "info");
}

export function storyStatusClass(status: string): string {
  const normalized = String(status || "unknown").toLowerCase();
  if (["pass", "warn", "fail", "skipped"].includes(normalized)) return normalized;
  return "unknown";
}

// shortText moved to utils/format.ts (Wave 8).

export function compactLongText(value: string, maxLength: number): {text: string; truncated: boolean} {
  if (value.length <= maxLength) return {text: value, truncated: false};
  const tail = value.slice(-maxLength);
  const firstLineBreak = tail.indexOf("\n");
  const lineAlignedTail = firstLineBreak >= 0 ? tail.slice(firstLineBreak + 1) : tail;
  const visibleLines = lineAlignedTail ? lineAlignedTail.split(/\n/).filter((line) => line.length > 0).length : 0;
  return {
    text: `[showing latest ${visibleLines.toLocaleString()} complete lines]\n\n${lineAlignedTail}`,
    truncated: true,
  };
}

// capitalize / refreshLabel moved to utils/format.ts (Wave 8).

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "Unknown error");
}

export function detailWasRemoved(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

// ---------------------------------------------------------------------------
// Heavy-user paper-cut #3 — notifications when long runs finish
// ---------------------------------------------------------------------------

let _notificationPermissionRequested = false;

/**
 * Request notification permission on the user's first interaction. Browsers
 * only allow `Notification.requestPermission()` to fire from a user gesture,
 * so we tie this to "open job dialog" / "start watcher" — natural moments
 * the user has signalled intent to track work.
 *
 * Idempotent: only fires once per page load. Gracefully degrades when the
 * Notification API is unavailable (older browsers, secure-context-required
 * pages) — caller flow is unaffected.
 */
export function requestNotificationPermissionOnce(): void {
  if (_notificationPermissionRequested) return;
  if (typeof window === "undefined") return;
  const notif = (window as unknown as {Notification?: typeof Notification}).Notification;
  if (!notif || typeof notif.requestPermission !== "function") return;
  if (notif.permission !== "default") {
    _notificationPermissionRequested = true;
    return;
  }
  _notificationPermissionRequested = true;
  try {
    const result = notif.requestPermission();
    // Some older browsers return undefined (callback-only). Both are safe.
    if (result && typeof (result as Promise<NotificationPermission>).then === "function") {
      void (result as Promise<NotificationPermission>).catch(() => {/* user denied; degrade silently */});
    }
  } catch {
    // Some embedded browsers throw on requestPermission — never rethrow.
  }
}

/**
 * Track the live run set across polls; when a previously-live run drops out
 * (i.e. transitioned to a terminal state) AND the tab is currently hidden,
 * fire a Notification. The set lives in a ref so we re-compute the diff in
 * place rather than re-creating subscriptions per poll. We also reset the
 * baseline when the project changes (different `data.project.path`) so a
 * project switch isn't mis-read as "all runs just finished."
 */
export function useNotificationsOnRunFinish(data: StateResponse | null): void {
  const previousLiveIdsRef = useRef<Set<string>>(new Set());
  const previousProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const projectPath = data?.project?.path || null;
    if (previousProjectRef.current !== projectPath) {
      // Project changed (or first hydration). Snapshot the current live set
      // as the new baseline; do NOT fire notifications for "runs that
      // disappeared" because they belonged to the previous project.
      previousProjectRef.current = projectPath;
      const initial = new Set<string>();
      for (const item of data?.live.items || []) initial.add(item.run_id);
      previousLiveIdsRef.current = initial;
      return;
    }
    const currentIds = new Set<string>();
    for (const item of data?.live.items || []) currentIds.add(item.run_id);
    const finished: string[] = [];
    for (const prevId of previousLiveIdsRef.current) {
      if (!currentIds.has(prevId)) finished.push(prevId);
    }
    previousLiveIdsRef.current = currentIds;
    if (!finished.length) return;
    const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
    if (!hidden) return;
    const notif = (window as unknown as {Notification?: typeof Notification}).Notification;
    if (!notif) return;
    if (notif.permission !== "granted") return;
    try {
      // One notification per finished batch — multiple completions in a
      // single poll fire one consolidated message rather than spamming.
      const body = finished.length === 1
        ? `Run ${finished[0]} finished.`
        : `${finished.length} runs finished.`;
      // eslint-disable-next-line no-new -- side-effect intentional
      new notif("Otto: build completed", {body});
    } catch {
      // Some browsers throw if the page lifecycle frame doesn't permit it.
    }
  }, [data]);
}

// ---------------------------------------------------------------------------
// Heavy-user paper-cut #4 — Cmd-K command palette
// ---------------------------------------------------------------------------

/**
 * Quick project switcher. Cmd-K opens, type to fuzzy-filter (substring on
 * name + path), Up/Down (or j/k) to navigate, Enter to select, Escape to
 * close. The current project is rendered with a "current" badge and is
 * non-selectable to avoid an accidental no-op switch.
 *
 * Modal isolation: we render inside `.modal-backdrop` and use
 * `useDialogFocus`, mirroring JobDialog/ConfirmDialog. The `inertSiblings`
 * pattern at the App shell respects this: when paletteOpen is true the
 * sidebar/main go inert, so click-through and tab traversal stay inside.
 */
/**
 * Substring-everywhere fuzzy filter for the palette. We compare against
 * lowercased name + path so a query like "kanb" matches "kanban-portal" or
 * a project under `~/projects/kanban`. Order is preserved (no scoring) —
 * keeps things predictable for muscle-memory users who recognise the order
 * of their last 5 projects.
 */
