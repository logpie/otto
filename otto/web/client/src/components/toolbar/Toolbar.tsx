import {useEffect, useRef, useState} from "react";

import {Spinner} from "../Spinner";
import {useDebouncedValue} from "../../hooks/useDebouncedValue";
import {refreshLabel} from "../../utils/format";
import type {Filters, ViewMode} from "../../uiTypes";
import type {OutcomeFilter, RunTypeFilter} from "../../types";

const defaultFilters: Filters = {
  type: "all",
  outcome: "all",
  query: "",
  activeOnly: false,
};

export function Toolbar({filters, refreshStatus, refreshPending, viewMode, onChange, onRefresh, onViewChange}: {
  filters: Filters;
  refreshStatus: string;
  refreshPending: boolean;
  viewMode: ViewMode;
  onChange: (filters: Filters) => void;
  onRefresh: () => void;
  onViewChange: (viewMode: ViewMode) => void;
}) {
  const [localQuery, setLocalQuery] = useState(filters.query);
  const debouncedQuery = useDebouncedValue(localQuery, 200);
  const lastCommittedRef = useRef(filters.query);
  useEffect(() => {
    if (debouncedQuery === lastCommittedRef.current) return;
    if (debouncedQuery === filters.query) return;
    lastCommittedRef.current = debouncedQuery;
    onChange({...filters, query: debouncedQuery});
  }, [debouncedQuery]);
  useEffect(() => {
    if (filters.query !== lastCommittedRef.current) {
      lastCommittedRef.current = filters.query;
      setLocalQuery(filters.query);
    }
  }, [filters.query]);
  return (
    <header className="toolbar">
      <div className="view-tabs" role="group" aria-label="Mission Control views">
        <button
          className={viewMode === "tasks" ? "active" : ""}
          type="button"
          aria-pressed={viewMode === "tasks"}
          data-testid="tasks-tab"
          onClick={() => onViewChange("tasks")}
        >
          Tasks
        </button>
        <button
          className={viewMode === "diagnostics" ? "active" : ""}
          type="button"
          aria-pressed={viewMode === "diagnostics"}
          data-testid="diagnostics-tab"
          onClick={() => onViewChange("diagnostics")}
        >
          Health
        </button>
      </div>
      <div className="filters" role="group" aria-label="Run filters">
        <label>Type
          <select data-testid="filter-type-select" value={filters.type} onChange={(event) => onChange({...filters, type: event.target.value as RunTypeFilter})}>
            <option value="all">All</option>
            <option value="build">Build</option>
            <option value="improve">Improve</option>
            <option value="certify">Certify</option>
            <option value="merge">Merge</option>
            <option value="queue">Queue</option>
          </select>
        </label>
        <label>Outcome
          <select data-testid="filter-outcome-select" value={filters.outcome} onChange={(event) => onChange({...filters, outcome: event.target.value as OutcomeFilter})}>
            <option value="all">All</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
            <option value="interrupted">Interrupted</option>
            <option value="cancelled">Cancelled</option>
            <option value="removed">Removed</option>
            <option value="other">Other</option>
          </select>
        </label>
        <label className="search-label">Search
          <input
            value={localQuery}
            type="search"
            placeholder="run, task, branch"
            data-testid="filter-search-input"
            onChange={(event) => setLocalQuery(event.target.value)}
          />
        </label>
        <label className="check-label">
          <input
            checked={filters.activeOnly}
            type="checkbox"
            onChange={(event) => onChange({...filters, activeOnly: event.target.checked})}
          />
          Active
        </label>
        <button type="button" onClick={() => onChange(defaultFilters)}>Clear filters</button>
      </div>
      <div className="toolbar-actions">
        {refreshLabel(refreshStatus) && <span className="muted">{refreshLabel(refreshStatus)}</span>}
        <button type="button" data-testid="toolbar-refresh-button" disabled={refreshPending} aria-busy={refreshPending} onClick={onRefresh}>{refreshPending ? <><Spinner /> Refreshing...</> : "Refresh"}</button>
      </div>
    </header>
  );
}
