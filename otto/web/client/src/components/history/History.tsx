import {useEffect, useMemo, useState} from "react";
import type {HistoryItem, StateResponse} from "../../types";
import {tokenTotal, usageLine} from "../../utils/format";
import {HISTORY_PAGE_SIZE_OPTIONS} from "../../uiTypes";
import type {HistorySortColumn, HistorySortDir} from "../../uiTypes";

export function History({
  items,
  totalRows,
  page,
  totalPages,
  pageSize,
  requestedPage,
  loaded,
  selectedRunId,
  sortColumn,
  sortDir,
  onSelect,
  onChangePage,
  onChangePageSize,
  onCycleSort,
}: {
  items: HistoryItem[];
  totalRows: number;
  page: number;
  totalPages: number;
  pageSize: number;
  // The page the *user* asked for, in 1-based terms. May exceed totalPages
  // if a stale deep-link was pasted; in that case the server clamps and
  // returns the last valid page in `page`, and we render a recovery hint.
  requestedPage: number;
  // Whether we have a server response yet. Drives the "loading" copy when
  // navigating between pages so the table doesn't flash to "No matching
  // history" while the next response is in flight.
  loaded: boolean;
  selectedRunId: string | null;
  // Heavy-user paper-cut #2: which column the user clicked, and the direction.
  // Both null → no sort applied (server natural order wins).
  sortColumn: HistorySortColumn | null;
  sortDir: HistorySortDir | null;
  onSelect: (runId: string) => void;
  onChangePage: (nextPage: number) => void;
  onChangePageSize: (nextSize: number) => void;
  onCycleSort: (column: HistorySortColumn) => void;
}) {
  // Local mirror for the jump-to-page input. Plain text input so the user
  // can clear it without us snapping back to the canonical page; we commit
  // on Enter or blur.
  const [jumpDraft, setJumpDraft] = useState<string>(String(page));
  useEffect(() => {
    setJumpDraft(String(page));
  }, [page]);

  const requestedOutOfRange = loaded && requestedPage > totalPages;
  const showRecovery = requestedOutOfRange && totalRows > 0;

  const commitJump = () => {
    const parsed = Number.parseInt(jumpDraft, 10);
    if (!Number.isFinite(parsed) || parsed < 1) {
      setJumpDraft(String(page));
      return;
    }
    onChangePage(parsed);
  };

  // Heavy-user paper-cut #2: apply local sort when a column is selected.
  // Sort is page-local (we sort the rows the server gave us); a true
  // server-side sort across all 200+ rows is in the followups.
  const sortedItems = useMemo(
    () => sortHistoryItems(items, sortColumn, sortDir),
    [items, sortColumn, sortDir],
  );

  const sortIndicator = (col: HistorySortColumn): string => {
    if (sortColumn !== col || !sortDir) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  };
  const ariaSort = (col: HistorySortColumn): "ascending" | "descending" | "none" => {
    if (sortColumn !== col || !sortDir) return "none";
    return sortDir === "asc" ? "ascending" : "descending";
  };
  const renderSortableTh = (col: HistorySortColumn, label: string) => (
    <th
      aria-sort={ariaSort(col)}
      data-testid={`history-th-${col}`}
      className={`history-th-sortable ${sortColumn === col && sortDir ? "active" : ""}`}
    >
      <button
        type="button"
        className="history-sort-button"
        data-testid={`history-sort-${col}`}
        aria-label={`Sort by ${label} (${sortColumn === col && sortDir ? sortDir : "asc"} on click)`}
        onClick={() => onCycleSort(col)}
      >
        {label}{sortIndicator(col)}
      </button>
    </th>
  );

  return (
    <section className="panel history-panel" aria-labelledby="historyHeading">
      <div className="panel-heading">
        <h2 id="historyHeading">Run History</h2>
        <span className="pill">{totalRows}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {renderSortableTh("outcome", "Outcome")}
              {renderSortableTh("run", "Run")}
              {renderSortableTh("summary", "Summary")}
              {renderSortableTh("duration", "Duration")}
              {renderSortableTh("usage", "Usage")}
            </tr>
          </thead>
          <tbody>
            {showRecovery ? (
              <tr>
                <td colSpan={5} className="empty-cell" data-testid="history-out-of-range">
                  Page {requestedPage} doesn&rsquo;t exist; only {totalPages} {totalPages === 1 ? "page" : "pages"} available.
                  {" "}
                  <button type="button" data-testid="history-recover-button" onClick={() => onChangePage(1)}>
                    Jump to page 1
                  </button>
                </td>
              </tr>
            ) : sortedItems.length ? sortedItems.map((item) => (
              <tr
                key={item.run_id}
                className={item.run_id === selectedRunId ? "selected" : ""}
                aria-selected={item.run_id === selectedRunId}
              >
                <td className={`status-${(item.terminal_outcome || item.status || "").toLowerCase()}`}>{item.outcome_display || "-"}</td>
                <td>
                  <button
                    type="button"
                    className="row-link"
                    data-testid={`history-row-activator-${item.run_id}`}
                    aria-label={`Open history run ${item.queue_task_id || item.run_id}`}
                    title={item.run_id}
                    onClick={() => onSelect(item.run_id)}
                  >{item.queue_task_id || item.run_id}</button>
                </td>
                <td>
                  <span className="cell-overflow" aria-label={item.summary || ""}>{item.summary || "-"}</span>
                </td>
                <td>{item.duration_display || "-"}</td>
                <td>{usageLine(item)}</td>
              </tr>
            )) : (
              <tr><td colSpan={5} className="empty-cell">{loaded ? "No matching history." : "Loading…"}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {(totalRows > 0 || totalPages > 1) && (
        <nav
          className="history-pagination"
          data-testid="history-pagination"
          aria-label="History pagination"
        >
          <span className="history-pagination-status" data-testid="history-pagination-status">
            Page {page} of {totalPages} &middot; {totalRows} {totalRows === 1 ? "run" : "runs"}
          </span>
          <div className="history-pagination-controls">
            <button
              type="button"
              data-testid="history-prev-button"
              disabled={page <= 1}
              aria-disabled={page <= 1}
              onClick={() => onChangePage(page - 1)}
            >
              &larr; Previous
            </button>
            <label className="history-pagination-jump">
              Go to
              <input
                type="number"
                min={1}
                max={totalPages}
                value={jumpDraft}
                data-testid="history-jump-input"
                aria-label="Jump to page"
                onChange={(event) => setJumpDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    commitJump();
                  }
                }}
                onBlur={commitJump}
              />
            </label>
            <button
              type="button"
              data-testid="history-next-button"
              disabled={page >= totalPages}
              aria-disabled={page >= totalPages}
              onClick={() => onChangePage(page + 1)}
            >
              Next &rarr;
            </button>
            <label className="history-pagination-size">
              Per page
              <select
                value={pageSize}
                data-testid="history-page-size-select"
                onChange={(event) => onChangePageSize(Number.parseInt(event.target.value, 10))}
              >
                {HISTORY_PAGE_SIZE_OPTIONS.map((option) => (
                  <option value={option} key={option}>{option}</option>
                ))}
              </select>
            </label>
          </div>
        </nav>
      )}
    </section>
  );
}

export function sortHistoryItems(
  items: HistoryItem[],
  column: HistorySortColumn | null,
  dir: HistorySortDir | null,
): HistoryItem[] {
  if (!column || !dir || items.length < 2) return items;
  const factor = dir === "asc" ? 1 : -1;
  const comparators: Record<HistorySortColumn, (a: HistoryItem, b: HistoryItem) => number> = {
    outcome: (a, b) => safeCompareString(a.outcome_display || a.terminal_outcome || a.status, b.outcome_display || b.terminal_outcome || b.status),
    run: (a, b) => safeCompareString(a.queue_task_id || a.run_id, b.queue_task_id || b.run_id),
    summary: (a, b) => safeCompareString(a.summary, b.summary),
    duration: (a, b) => safeCompareNumber(a.duration_s, b.duration_s),
    usage: (a, b) => safeCompareNumber(usageSortValue(a), usageSortValue(b)),
  };
  const cmp = comparators[column];
  // Slice so we never mutate the caller's array — React identity matters
  // for memoization and for the test harness that snapshots `items`.
  return [...items].sort((a, b) => cmp(a, b) * factor);
}

export function usageSortValue(item: {token_usage?: StateResponse["project_stats"]["token_usage"]; cost_usd?: number | null}): number | null {
  const tokens = tokenTotal(item.token_usage);
  if (tokens > 0) return tokens;
  return item.cost_usd ?? null;
}

export function safeCompareString(a: string | null | undefined, b: string | null | undefined): number {
  const av = (a || "").toLowerCase();
  const bv = (b || "").toLowerCase();
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
}

export function safeCompareNumber(a: number | null | undefined, b: number | null | undefined): number {
  const av = typeof a === "number" && Number.isFinite(a) ? a : Number.NEGATIVE_INFINITY;
  const bv = typeof b === "number" && Number.isFinite(b) ? b : Number.NEGATIVE_INFINITY;
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
}
