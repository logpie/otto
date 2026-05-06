// useRunView — fetches /api/run-view/<sessionId> and returns {data, loading, error}.
//
// Per A4 contract: backend `otto/mission_control/run_view.py:build_run_view`
// returns a dict matching the RunView TS interface (otto/web/client/src/types/run.ts).
// This hook does the network plumbing; <RunDrawer view={data}/> consumes the result.
//
// Live polling: while the run is in-flight (RunStatus is one of the active
// lifecycle states — queued/compiling/awaiting_spec_review/building/auditing/
// rendering/landing), we refetch every POLL_INTERVAL_MS so the drawer reflects
// new feature/component status without user-driven refreshes. Polling stops
// once the run reaches a terminal status (passed/partial/blocked/landed/
// interrupted/aborted/failed) — no infinite loop on the terminal frame. Network blips
// during polling are logged but do NOT clobber the last successful snapshot.

import { useCallback, useEffect, useRef, useState } from "react";
import type { RunStatus, RunView } from "../../types/run";

export interface UseRunViewState {
  data: RunView | null;
  loading: boolean;
  error: string | null;
  // Initial-fetch HTTP status (when the failure was an HTTP error, not a
  // network error). Surfaces 404 specifically so the page can render a
  // friendly "Run not found" instead of the raw HTTP message (B19).
  errorStatus: number | null;
  reload: () => void;
}

// Live-poll cadence while a run is in flight. 3s feels human — fast enough
// to track stage transitions, slow enough to avoid hammering the backend.
const POLL_INTERVAL_MS = 3000;

const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "landed",
  "blocked",
  "partial",
  "passed",
  "interrupted",
  "aborted",
  "failed",
]);

function isTerminalStatus(status: RunStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

export function useRunView(sessionId: string | null): UseRunViewState {
  const [data, setData] = useState<RunView | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [reloadCounter, setReloadCounter] = useState<number>(0);

  // Mirror of latest status so the polling effect can decide whether to keep
  // ticking after each refetch without re-running on every data change.
  const statusRef = useRef<RunStatus | null>(null);

  const reload = useCallback(() => {
    setReloadCounter((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!sessionId) {
      setData(null);
      setError(null);
      setErrorStatus(null);
      setLoading(false);
      statusRef.current = null;
      return;
    }
    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;
    const controllers = new Set<AbortController>();

    const fetchOnce = (isInitial: boolean) => {
      if (isInitial) {
        setLoading(true);
        setError(null);
        setErrorStatus(null);
      }
      const controller = new AbortController();
      controllers.add(controller);
      fetch(`/api/run-view/${encodeURIComponent(sessionId)}`, { signal: controller.signal })
        .then((resp) => {
          if (!resp.ok) {
            const httpErr = new Error(`HTTP ${resp.status} ${resp.statusText}`);
            // Tag the error with its HTTP status so the consumer can
            // distinguish 404 (resource missing) from 5xx (transient).
            (httpErr as Error & { status?: number }).status = resp.status;
            throw httpErr;
          }
          return resp.json() as Promise<RunView>;
        })
        .then((view) => {
          if (cancelled) return;
          setData(view);
          if (isInitial) setLoading(false);
          statusRef.current = view.status;
          // If the run reached a terminal state mid-poll, stop the interval.
          // This guarantees exactly one refetch fires after the transition
          // (the one that observed the terminal status) and then polling halts.
          if (intervalId !== null && isTerminalStatus(view.status)) {
            clearInterval(intervalId);
            intervalId = null;
          }
        })
        .catch((err: Error & { status?: number }) => {
          if (err.name === "AbortError") return;
          if (cancelled) return;
          if (isInitial) {
            setError(err.message || String(err));
            setErrorStatus(typeof err.status === "number" ? err.status : null);
            setLoading(false);
          } else {
            // Mid-poll fetch failure: keep the last successful snapshot in
            // place rather than blanking the UI. Surface the blip in the
            // console so the user can see it if dev-tools are open; the
            // next tick will retry.
            // eslint-disable-next-line no-console
            console.warn(
              `[useRunView] poll refetch failed for ${sessionId}: ${err.message || String(err)}`,
            );
          }
        })
        .finally(() => {
          controllers.delete(controller);
        });
    };

    // Initial fetch, then start polling if the run is still in flight after
    // the first response. We start the interval immediately (without waiting
    // for the first response) so a slow initial fetch doesn't delay polling
    // forever; the interval handler tolerates concurrent fetches and the
    // terminal-status check inside the .then() will stop polling promptly.
    fetchOnce(true);
    intervalId = setInterval(() => {
      // Skip the tick if we already detected a terminal status — defense
      // in depth against the .then() not yet having cleared the interval.
      const status = statusRef.current;
      if (status !== null && isTerminalStatus(status)) {
        if (intervalId !== null) {
          clearInterval(intervalId);
          intervalId = null;
        }
        return;
      }
      fetchOnce(false);
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      for (const controller of controllers) {
        controller.abort();
      }
      controllers.clear();
      if (intervalId !== null) {
        clearInterval(intervalId);
        intervalId = null;
      }
    };
  }, [sessionId, reloadCounter]);

  return { data, loading, error, errorStatus, reload };
}

export default useRunView;
