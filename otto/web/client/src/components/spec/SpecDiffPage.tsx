// SpecDiffPage — wireframe 4d.
//
// Compares two spec snapshots for a session and renders a line-level
// diff. Each side may be either an archived version `spec-v<N>.{md,json}`
// or the live "current" working spec (`spec.json` + `spec.md`).
//
// Backend contract:
//   GET /api/specs/<session>/versions       → {versions: number[]}
//   GET /api/specs/<session>/diff?from=X&to=Y
//      where X/Y is an int version OR the literal string "current"
//
// Defaults (B28): when at least one archived version exists, the page
// loads with `From=last archived` and `To=current` — this gives the
// most useful diff out of the box (what changed since the last save?).
// When zero archived versions exist (B15), the dropdowns still render
// but only "current" is selectable; the empty-state explanation is
// shown above the diff pane.
//
// We deliberately avoid heavyweight diff libraries: `diff` is not in the
// dependency tree, and the spec markdown is small (hundreds of lines at
// most), so an inline LCS over lines is plenty.

import { useEffect, useMemo, useState } from "react";

type VersionId = number | "current";

interface DiffPayload {
  from_version: VersionId;
  to_version: VersionId;
  from_md: string;
  to_md: string;
  from_json: unknown;
  to_json: unknown;
}

interface VersionsPayload {
  session_id: string;
  versions: number[];
}

type DiffOp = "context" | "add" | "del";

interface DiffLine {
  op: DiffOp;
  text: string;
}

// R2-B26: a "fold" entry replaces a run of consecutive context lines when
// the user toggles "Show only changes". Rendered as a single
// `… N unchanged lines …` placeholder.
interface DiffFold {
  op: "fold";
  count: number;
}

type DiffEntry = DiffLine | DiffFold;

interface Props {
  sessionId: string;
}

// LCS-based line diff. Quadratic memory but bounded — spec markdown
// rarely exceeds a few hundred lines, so the table is comfortably small.
function diffLines(fromText: string, toText: string): DiffLine[] {
  const a = fromText.split("\n");
  const b = toText.split("\n");
  const n = a.length;
  const m = b.length;
  // dp[i][j] = LCS length of a[i..n-1] and b[j..m-1].
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    const dpI = dp[i] as number[];
    const dpI1 = dp[i + 1] as number[];
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        dpI[j] = (dpI1[j + 1] as number) + 1;
      } else {
        dpI[j] = Math.max(dpI1[j] as number, dpI[j + 1] as number);
      }
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const ai = a[i] as string;
    const bj = b[j] as string;
    if (ai === bj) {
      out.push({ op: "context", text: ai });
      i++;
      j++;
    } else if ((dp[i + 1] as number[])[j]! >= (dp[i] as number[])[j + 1]!) {
      out.push({ op: "del", text: ai });
      i++;
    } else {
      out.push({ op: "add", text: bj });
      j++;
    }
  }
  while (i < n) out.push({ op: "del", text: a[i++] as string });
  while (j < m) out.push({ op: "add", text: b[j++] as string });
  return out;
}

function prefixFor(op: DiffOp): string {
  if (op === "add") return "+";
  if (op === "del") return "-";
  return " ";
}

function classFor(op: DiffOp): string {
  if (op === "add") return "diff-add";
  if (op === "del") return "diff-del";
  return "";
}

function labelFor(v: VersionId): string {
  return v === "current" ? "current" : `v${v}`;
}

function parseVersionId(raw: string): VersionId {
  if (raw === "current") return "current";
  const n = Number(raw);
  return Number.isFinite(n) ? n : "current";
}

function encodeVersionParam(v: VersionId): string {
  return v === "current" ? "current" : String(v);
}

// R2-B26: collapse consecutive context lines into a single fold entry.
// We use the same threshold for "minimum run worth folding" as common
// patch tools (3 lines). Smaller runs are kept inline because the fold
// placeholder would be longer than the context itself.
function collapseContext(lines: DiffLine[], minRun = 3): DiffEntry[] {
  const out: DiffEntry[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as DiffLine;
    if (line.op !== "context") {
      out.push(line);
      i++;
      continue;
    }
    let j = i;
    while (j < lines.length && (lines[j] as DiffLine).op === "context") {
      j++;
    }
    const run = j - i;
    if (run >= minRun) {
      out.push({ op: "fold", count: run });
    } else {
      for (let k = i; k < j; k++) out.push(lines[k] as DiffLine);
    }
    i = j;
  }
  return out;
}

export function SpecDiffPage({ sessionId }: Props) {
  const [versions, setVersions] = useState<number[] | null>(null);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [from, setFrom] = useState<VersionId | null>(null);
  const [to, setTo] = useState<VersionId | null>(null);
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  // R2-B26: collapse-context toggle. Default false → preserves the
  // existing behavior (full diff with all context lines visible).
  const [onlyChanges, setOnlyChanges] = useState<boolean>(false);

  // Fetch the available versions on mount.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/specs/${encodeURIComponent(sessionId)}/versions`)
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        return resp.json() as Promise<VersionsPayload>;
      })
      .then((payload) => {
        if (cancelled) return;
        const list = [...(payload.versions || [])].sort((x, y) => x - y);
        setVersions(list);
        // Default selection (B28): most-recently-archived → current. This
        // shows "what changed since the last save" out of the box.
        if (list.length >= 1) {
          setFrom(list[list.length - 1] as number);
          setTo("current");
        } else {
          // No archived versions yet (B15). Both sides default to
          // "current" — the no-op message (B29) will surface
          // immediately, telling the user there's nothing to diff yet.
          setFrom("current");
          setTo("current");
        }
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setVersionsError(err.message || String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Re-fetch the diff payload whenever from/to change. Skip the fetch
  // entirely when the two sides are the same — there's no diff to
  // render and the no-op message (B29) handles the empty state.
  useEffect(() => {
    if (from === null || to === null) return;
    if (from === to) {
      setDiff(null);
      setDiffError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setDiffError(null);
    const url =
      `/api/specs/${encodeURIComponent(sessionId)}/diff` +
      `?from=${encodeVersionParam(from)}&to=${encodeVersionParam(to)}`;
    fetch(url)
      .then(async (resp) => {
        if (!resp.ok) {
          let detail = `HTTP ${resp.status}`;
          try {
            const body = (await resp.json()) as { detail?: string };
            if (body.detail) detail = body.detail;
          } catch {
            /* ignore */
          }
          throw new Error(detail);
        }
        return resp.json() as Promise<DiffPayload>;
      })
      .then((payload) => {
        if (cancelled) return;
        setDiff(payload);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setDiffError(err.message || String(err));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, from, to]);

  const lines = useMemo<DiffLine[]>(() => {
    if (!diff) return [];
    return diffLines(diff.from_md, diff.to_md);
  }, [diff]);

  // R2-B26: when "Show only changes" is active, collapse runs of
  // unchanged context lines into single fold placeholders.
  const entries = useMemo<DiffEntry[]>(() => {
    if (!onlyChanges) return lines;
    return collapseContext(lines);
  }, [lines, onlyChanges]);

  // R2-B27: swap the From and To selections — trivial UX shortcut so
  // users don't have to manipulate two dropdowns to reverse a comparison.
  const handleSwap = () => {
    if (from === null || to === null) return;
    setFrom(to);
    setTo(from);
  };

  const isNoop = from !== null && to !== null && from === to;
  // Options always include "current" so the user can compare any
  // archived version against the live working spec (B28). The list is
  // ordered "current" first, then v1..vN ascending.
  const dropdownOptions = useMemo<VersionId[]>(() => {
    const archived = (versions ?? []).slice().sort((a, b) => a - b);
    return ["current", ...archived];
  }, [versions]);

  if (versionsError) {
    return (
      <main className="spec-diff-page">
        <h1>Spec diff</h1>
        <p className="spec-diff-error" role="alert">
          Failed to load versions: {versionsError}
        </p>
      </main>
    );
  }

  if (versions === null) {
    return (
      <main className="spec-diff-page">
        <p>Loading spec versions…</p>
      </main>
    );
  }

  const hasArchived = versions.length > 0;

  return (
    // R2-B29 (option a): the page now uses the app's light-theme tokens
    // for cross-screen consistency. The `.diff-pane` keeps its own
    // background/foreground via CSS so the diff itself reads as an
    // intentional embedded code block, not a different app.
    <main className="spec-diff-page">
      <header className="spec-diff-header">
        <h1 className="spec-diff-title">
          Spec diff
          {from !== null && to !== null ? (
            <>
              {" "}
              {/* R2-B28: render the version labels with identical
                  styling on both sides (same color + weight, no italic).
                  Previously the inline span was a single muted block
                  which made `v1 → current` read asymmetrically. */}
              <span className="spec-diff-title-versions">
                ·{" "}
                <span className="spec-diff-version-label">
                  {labelFor(from)}
                </span>{" "}
                →{" "}
                <span className="spec-diff-version-label">
                  {labelFor(to)}
                </span>
              </span>
            </>
          ) : null}
        </h1>
        <div className="spec-diff-controls">
          <label className="spec-diff-label">
            <span>From</span>
            <select
              value={from === null ? "" : encodeVersionParam(from)}
              onChange={(e) => setFrom(parseVersionId(e.target.value))}
              aria-label="Compare from version"
              data-testid="spec-diff-from"
            >
              {dropdownOptions.map((v) => (
                <option key={String(v)} value={encodeVersionParam(v)}>
                  {labelFor(v)}
                </option>
              ))}
            </select>
          </label>
          {/* R2-B27: swap From and To. Disabled when either side is
              unset (shouldn't happen post-load, but defensive). */}
          <button
            type="button"
            className="spec-diff-swap"
            data-testid="spec-diff-swap"
            aria-label="Swap From and To versions"
            title="Swap versions"
            onClick={handleSwap}
            disabled={from === null || to === null}
          >
            ⇄
          </button>
          <label className="spec-diff-label">
            <span>To</span>
            <select
              value={to === null ? "" : encodeVersionParam(to)}
              onChange={(e) => setTo(parseVersionId(e.target.value))}
              aria-label="Compare to version"
              data-testid="spec-diff-to"
            >
              {dropdownOptions.map((v) => (
                <option key={String(v)} value={encodeVersionParam(v)}>
                  {labelFor(v)}
                </option>
              ))}
            </select>
          </label>
          {/* R2-B26: show-only-changes toggle. Default unchecked →
              preserves prior behavior (full diff with all context).
              R3-B45: re-styled as a clearly-toggle button — outlined
              when inactive, filled-blue when active. `aria-pressed`
              is already set so screen readers + assistive tech read
              the toggle state correctly.
              R3-B47: when disabled (no diff to filter), surface a
              title tooltip explaining why. */}
          <button
            type="button"
            className="spec-diff-fold-toggle"
            data-testid="spec-diff-fold-toggle"
            aria-pressed={onlyChanges}
            onClick={() => setOnlyChanges((v) => !v)}
            disabled={!diff || isNoop}
            title={!diff || isNoop ? "No diff to filter" : undefined}
          >
            {onlyChanges ? "Show full diff" : "Show only changes"}
          </button>
        </div>
      </header>

      {/* R3-B44: collapse the previously-stacked empty-state messages
          into a single block when there are no archived versions. The
          `!hasArchived` case used to render BOTH the spec-diff-empty
          paragraph AND the diff-noop-message (since from === to ===
          "current"), which read as two separate empties. We now show
          one unified message and an Edit-spec CTA (R3-B46). */}
      {!hasArchived ? (
        <div className="spec-diff-empty-block" data-testid="spec-diff-empty">
          <p className="spec-diff-empty">
            No archived spec versions for this session yet. Spec versions
            are created each time the spec is edited and saved through
            the spec-review flow.
          </p>
          <p className="spec-diff-empty-cta">
            <a
              className="spec-diff-empty-cta-link"
              data-testid="spec-diff-edit-spec-cta"
              href={`?view=spec-review&spec=${encodeURIComponent(sessionId)}`}
            >
              Edit spec to create a version
            </a>
          </p>
        </div>
      ) : isNoop ? (
        <p
          className="diff-noop-message"
          data-testid="diff-noop-message"
        >
          Pick two different versions to see a diff.
        </p>
      ) : null}

      {diffError && !isNoop ? (
        <p className="spec-diff-error" role="alert">
          Failed to load diff: {diffError}
        </p>
      ) : null}

      {loading && !diff && !isNoop ? <p>Loading diff…</p> : null}

      {diff && !isNoop ? (
        <pre
          className="diff-pane spec-diff-pane"
          aria-label={`Spec diff ${labelFor(diff.from_version)} to ${labelFor(diff.to_version)}`}
        >
          {entries.length === 0 ? (
            <span className="spec-diff-pane-empty">
              (no textual differences between {labelFor(diff.from_version)} and{" "}
              {labelFor(diff.to_version)})
            </span>
          ) : (
            entries.map((entry, idx) => {
              if (entry.op === "fold") {
                return (
                  <span
                    key={`fold-${idx}`}
                    className="diff-fold"
                    data-testid="diff-fold"
                  >
                    {`… ${entry.count} unchanged line${entry.count === 1 ? "" : "s"} …`}
                  </span>
                );
              }
              return (
                <span key={idx} className={`diff-line ${classFor(entry.op)}`}>
                  {prefixFor(entry.op)} {entry.text}
                </span>
              );
            })
          )}
        </pre>
      ) : null}
    </main>
  );
}

export default SpecDiffPage;
