import {useCallback, useEffect, useRef, useState} from "react";

/**
 * `useInFlight` provides a synchronous lock + reactive `pending` flag for an
 * async action. It exists to prevent the duplicate-POST class of bug that the
 * mc-audit hunters flagged across multiple themes (microinteractions C2, state
 * management #10, first-time-user #14):
 *
 *   - React `useState` updates are *asynchronous*; the second `onClick` fired
 *     within the same React batch sees `pending === false` and dispatches a
 *     second POST.
 *   - The synchronous `useRef` lock here is checked *before* the state update,
 *     so a second call returns the in-flight promise instead of starting a new
 *     request. The button can rely on `pending` for visual disable; the lock
 *     is what actually prevents the duplicate fetch.
 *
 * The hook also tracks mount status so a late-resolving promise does not call
 * `setPending(false)` on an unmounted component (which would print a React
 * warning and is a real bug-source when rapid mount/unmount happens — e.g.
 * inspector close mid-action).
 */
export interface InFlight {
  pending: boolean;
  run: <T>(fn: () => Promise<T>) => Promise<T>;
}

export function useInFlight(): InFlight {
  const [pending, setPending] = useState(false);
  const lockRef = useRef<Promise<unknown> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const run = useCallback(<T,>(fn: () => Promise<T>): Promise<T> => {
    // Synchronous lock: a second click while a promise is in flight returns
    // the same promise. Callers MUST tolerate receiving the in-flight result
    // (cancel/merge/etc are idempotent enough for this — see mc-audit C2).
    if (lockRef.current) return lockRef.current as Promise<T>;
    setPending(true);
    const promise = (async () => {
      try {
        return await fn();
      } finally {
        lockRef.current = null;
        if (mountedRef.current) setPending(false);
      }
    })();
    lockRef.current = promise;
    return promise;
  }, []);

  return {pending, run};
}
