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

export function SpecDiffPage({ sessionId }: Props) {
  const [versions, setVersions] = useState<number[] | null>(null);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [from, setFrom] = useState<VersionId | null>(null);
  const [to, setTo] = useState<VersionId | null>(null);
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

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
      <main className="spec-diff-page" style={{ padding: 24 }}>
        <h1>Spec diff</h1>
        <p role="alert" style={{ color: "#fca5a5" }}>
          Failed to load versions: {versionsError}
        </p>
      </main>
    );
  }

  if (versions === null) {
    return (
      <main className="spec-diff-page" style={{ padding: 24 }}>
        <p>Loading spec versions…</p>
      </main>
    );
  }

  const hasArchived = versions.length > 0;

  return (
    <main
      className="spec-diff-page"
      style={{ padding: 24, display: "flex", flexDirection: "column", gap: 16, height: "100%" }}
    >
      <header
        style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}
      >
        <h1 style={{ margin: 0 }}>
          Spec diff{" "}
          {from !== null && to !== null ? (
            <span style={{ color: "#94a3b8", fontWeight: 400 }}>
              · {labelFor(from)} → {labelFor(to)}
            </span>
          ) : null}
        </h1>
        <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "#94a3b8" }}>From</span>
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
        <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "#94a3b8" }}>To</span>
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
      </header>

      {!hasArchived ? (
        <p className="spec-diff-empty" style={{ color: "#94a3b8", margin: 0 }}>
          No archived spec versions for session <code>{sessionId}</code>.
          Versions are created each time the spec is edited through the
          spec-review flow.
        </p>
      ) : null}

      {isNoop ? (
        <p
          className="diff-noop-message"
          data-testid="diff-noop-message"
          style={{ margin: 0, color: "#94a3b8" }}
        >
          Pick two different versions to see a diff.
        </p>
      ) : null}

      {diffError && !isNoop ? (
        <p role="alert" style={{ color: "#fca5a5" }}>
          Failed to load diff: {diffError}
        </p>
      ) : null}

      {loading && !diff && !isNoop ? <p>Loading diff…</p> : null}

      {diff && !isNoop ? (
        <pre
          className="diff-pane"
          aria-label={`Spec diff ${labelFor(diff.from_version)} to ${labelFor(diff.to_version)}`}
          style={{ flex: 1, margin: 0 }}
        >
          {lines.length === 0 ? (
            <span style={{ color: "#94a3b8" }}>
              (no textual differences between {labelFor(diff.from_version)} and{" "}
              {labelFor(diff.to_version)})
            </span>
          ) : (
            lines.map((line, idx) => (
              <span
                key={idx}
                className={classFor(line.op)}
                style={{ display: "block" }}
              >
                {prefixFor(line.op)} {line.text}
              </span>
            ))
          )}
        </pre>
      ) : null}
    </main>
  );
}

export default SpecDiffPage;
