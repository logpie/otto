// useSpecMd — fetches /api/specs/<specId>/markdown and returns
// {data, loading, error, reload}. Mirrors useRunView's network plumbing.
//
// Backend endpoint lands tick 35: GET /api/specs/<spec_id>/markdown
// returns SpecMdView JSON (otto/web/client/src/types/spec.ts).

import { useCallback, useEffect, useState } from "react";
import type { SpecMdView } from "../../types/spec";

export interface UseSpecMdState {
  data: SpecMdView | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useSpecMd(specId: string | null): UseSpecMdState {
  const [data, setData] = useState<SpecMdView | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadCounter, setReloadCounter] = useState<number>(0);

  const reload = useCallback(() => {
    setReloadCounter((n) => n + 1);
  }, []);

  useEffect(() => {
    if (!specId) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(`/api/specs/${encodeURIComponent(specId)}/markdown`)
      .then((resp) => {
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        }
        return resp.json() as Promise<SpecMdView>;
      })
      .then((view) => {
        if (cancelled) return;
        setData(view);
        setLoading(false);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setError(err.message || String(err));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [specId, reloadCounter]);

  return { data, loading, error, reload };
}

export default useSpecMd;
