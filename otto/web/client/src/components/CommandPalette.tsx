import {useCallback, useEffect, useMemo, useState} from "react";
import type {MouseEvent as ReactMouseEvent} from "react";
import {BrandMark} from "./BrandMark";
import {useDialogFocus} from "../hooks/useDialogFocus";
import type {ManagedProjectInfo} from "../types";

interface CommandPaletteProps {
  projects: ManagedProjectInfo[];
  currentPath: string | null;
  onSelect: (path: string | null) => void;
  onClose: () => void;
}

export function CommandPalette({projects, currentPath, onSelect, onClose}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const dialogRef = useDialogFocus<HTMLDivElement>(onClose, false);
  const filtered = useMemo(() => filterPalette(projects, query), [projects, query]);
  // Keep the highlight pinned in range when the filter narrows.
  useEffect(() => {
    setHighlight(0);
  }, [query]);
  const moveHighlight = useCallback((dir: 1 | -1) => {
    if (!filtered.length) return;
    setHighlight((prev) => (prev + dir + filtered.length) % filtered.length);
  }, [filtered.length]);
  const onBackdropClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    onClose();
  };
  return (
    <div className="modal-backdrop" role="presentation" onClick={onBackdropClick} data-testid="command-palette-backdrop">
      <div
        ref={dialogRef}
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-labelledby="commandPaletteHeading"
        data-testid="command-palette"
        tabIndex={-1}
      >
        <header>
          <h2 id="commandPaletteHeading" className="sr-only">Command palette</h2>
          <input
            value={query}
            type="search"
            placeholder="Switch project — type to filter"
            data-testid="command-palette-input"
            aria-label="Filter projects"
            autoFocus
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" || (event.key === "j" && (event.ctrlKey || event.metaKey))) {
                event.preventDefault();
                moveHighlight(1);
              } else if (event.key === "ArrowUp" || (event.key === "k" && (event.ctrlKey || event.metaKey))) {
                event.preventDefault();
                moveHighlight(-1);
              } else if (event.key === "Enter") {
                event.preventDefault();
                const target = filtered[highlight];
                if (!target) return;
                if (target.path === currentPath) return; // no-op for current
                onSelect(target.path);
              }
              // Escape is handled by useDialogFocus.
            }}
          />
        </header>
        <ul className="command-palette-list" data-testid="command-palette-list" role="listbox" aria-label="Recent projects">
          {filtered.length === 0 && (
            <li
              className="command-palette-empty"
              data-testid="command-palette-empty"
            >No projects match.</li>
          )}
          {filtered.map((project, idx) => {
            const isCurrent = project.path === currentPath;
            const isHighlighted = idx === highlight;
            return (
              <li
                key={project.path}
                role="option"
                aria-selected={isHighlighted}
                className={`command-palette-row ${isHighlighted ? "highlighted" : ""} ${isCurrent ? "current" : ""}`}
                data-testid={`command-palette-row-${project.path}`}
              >
                <button
                  type="button"
                  className="command-palette-row-button"
                  data-testid={`command-palette-select-${project.path}`}
                  disabled={isCurrent}
                  onMouseEnter={() => setHighlight(idx)}
                  onClick={() => {
                    if (isCurrent) return;
                    onSelect(project.path);
                  }}
                >
                  <strong>{project.name}</strong>
                  <code>{project.path}</code>
                  {isCurrent && <span className="command-palette-badge">current</span>}
                </button>
              </li>
            );
          })}
        </ul>
        <footer className="command-palette-footer">
          <span>↑/↓ to navigate · Enter to switch · Esc to close</span>
        </footer>
      </div>
    </div>
  );
}

export function filterPalette(projects: ManagedProjectInfo[], query: string): ManagedProjectInfo[] {
  const trimmed = query.trim().toLowerCase();
  if (!trimmed) return projects;
  return projects.filter((project) => {
    const haystack = `${project.name || ""} ${project.path || ""}`.toLowerCase();
    return haystack.includes(trimmed);
  });
}
