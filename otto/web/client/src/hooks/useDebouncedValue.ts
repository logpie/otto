import {useEffect, useState} from "react";

/**
 * Returns a debounced copy of `value`. The returned value updates `delay` ms
 * after `value` last changed. Used for search-input debouncing so that every
 * keystroke does not trigger a state-refresh cycle (mc-audit microinteractions
 * I3 / async-action discipline cluster).
 *
 * The initial render returns the initial `value` immediately (no delay) so
 * SSR/first-paint behaves predictably; only subsequent changes are debounced.
 */
export function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState<T>(value);
  useEffect(() => {
    if (delay <= 0) {
      setDebounced(value);
      return;
    }
    const handle = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(handle);
  }, [value, delay]);
  return debounced;
}
