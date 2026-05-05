// SpecDiffPage — wireframe 4d.
//
// Compares two archived spec versions (`spec-v<N>.md`) for a session and
// renders a line-level diff. Versions list is fetched from
// `GET /api/specs/<session>/versions`; the diff payload from
// `GET /api/specs/<session>/diff?from=<N>&to=<M>` (lands tick spec-diff).
//
// We deliberately avoid heavyweight diff libraries: `diff` is not in the
// dependency tree, and the spec markdown is small (hundreds of lines at
// most), so an inline LCS over lines is plenty.

import { useEffect, useMemo, useState } from "react";

interface DiffPayload {
  from_version: number;
  to_version: number;
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

export function SpecDiffPage({ sessionId }: Props) {
  const [versions, setVersions] = useState<number[] | null>(null);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
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
        // Default: compare second-to-last vs last (the most recent edit).
        if (list.length >= 2) {
          setFrom(list[list.length - 2] as number);
          setTo(list[list.length - 1] as number);
        } else if (list.length === 1) {
          setFrom(list[0] as number);
          setTo(list[0] as number);
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

  // Re-fetch the diff payload whenever from/to change.
  useEffect(() => {
    if (from === null || to === null) return;
    let cancelled = false;
    setLoading(true);
    setDiffError(null);
    const url =
      `/api/specs/${encodeURIComponent(sessionId)}/diff` +
      `?from=${from}&to=${to}`;
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

  if (versions.length === 0) {
    return (
      <main className="spec-diff-page" style={{ padding: 24 }}>
        <h1>Spec diff</h1>
        <p>
          No archived spec versions for session{" "}
          <code>{sessionId}</code>. A version is created each time the
          spec is edited through the spec-review flow.
        </p>
      </main>
    );
  }

  return (
    <main className="spec-diff-page" style={{ padding: 24, display: "flex", flexDirection: "column", gap: 16, height: "100%" }}>
      <header style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <h1 style={{ margin: 0 }}>
          Spec diff{" "}
          {from !== null && to !== null ? (
            <span style={{ color: "#94a3b8", fontWeight: 400 }}>
              · v{from} → v{to}
            </span>
          ) : null}
        </h1>
        <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "#94a3b8" }}>From</span>
          <select
            value={from ?? ""}
            onChange={(e) => setFrom(Number(e.target.value))}
            aria-label="Compare from version"
          >
            {versions.map((v) => (
              <option key={v} value={v}>
                v{v}
              </option>
            ))}
          </select>
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "#94a3b8" }}>To</span>
          <select
            value={to ?? ""}
            onChange={(e) => setTo(Number(e.target.value))}
            aria-label="Compare to version"
          >
            {versions.map((v) => (
              <option key={v} value={v}>
                v{v}
              </option>
            ))}
          </select>
        </label>
      </header>

      {diffError ? (
        <p role="alert" style={{ color: "#fca5a5" }}>
          Failed to load diff: {diffError}
        </p>
      ) : null}

      {loading && !diff ? <p>Loading diff…</p> : null}

      {diff ? (
        <pre
          className="diff-pane"
          aria-label={`Spec diff v${diff.from_version} to v${diff.to_version}`}
          style={{ flex: 1, margin: 0 }}
        >
          {lines.length === 0 ? (
            <span style={{ color: "#94a3b8" }}>
              (no textual differences between v{diff.from_version} and v
              {diff.to_version})
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
