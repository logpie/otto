// RunListLanding — default landing page for the post-Phase-C frontend.
//
// Phase C.4 deleted the legacy <App/> Mission Control inspector and its
// /api/runs/<id>/... backend surface. The default landing now lists
// sessions from `/api/run-view` and links each to `?view=run-view&session=<id>`,
// which mounts <RunViewPage/> via main.tsx routing.
//
// Empty state: "no sessions yet" — first run will populate. The new
// design's Mission Control surface is intentionally minimal here; richer
// per-project dashboards come in Phase D / post-deletion.

import { useEffect, useState } from "react";

interface Props {
  // Optional override for tests. Defaults to /api/run-view.
  endpoint?: string;
}

interface ListResponse {
  runs: string[];
}

export function RunListLanding({ endpoint = "/api/run-view" }: Props) {
  const [runs, setRuns] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(endpoint)
      .then((resp) => {
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        }
        return resp.json() as Promise<ListResponse>;
      })
      .then((body) => {
        if (cancelled) return;
        setRuns(body.runs ?? []);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setError(err.message || String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [endpoint]);

  if (error) {
    return (
      <div className="run-list-landing run-list-landing--error" data-testid="run-list-error">
        <h1>Otto Mission Control</h1>
        <p>Failed to load sessions: {error}</p>
      </div>
    );
  }

  if (runs === null) {
    return (
      <div className="run-list-landing run-list-landing--loading" data-testid="run-list-loading">
        <h1>Otto Mission Control</h1>
        <p>Loading sessions…</p>
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="run-list-landing run-list-landing--empty" data-testid="run-list-empty">
        <h1>Otto Mission Control</h1>
        <p>No sessions yet. Run <code>otto build</code> to create one.</p>
      </div>
    );
  }

  return (
    <div className="run-list-landing" data-testid="run-list">
      <h1>Otto Mission Control</h1>
      <p>{runs.length} session{runs.length === 1 ? "" : "s"}</p>
      <ul>
        {runs.map((sessionId) => (
          <li key={sessionId}>
            <a href={`?view=run-view&session=${encodeURIComponent(sessionId)}`}>
              {sessionId}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default RunListLanding;
