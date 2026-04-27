import {useEffect, useRef} from "react";
import type {StateResponse} from "../types";
import {refreshIntervalMs} from "../utils/missionControl";

const STATE_POLL_HIDDEN_MS = 30_000;
const STATE_POLL_MIN_GAP_MS = 1000;

type RefreshMissionState = (showStatus?: boolean, signal?: AbortSignal) => Promise<void>;

export function useMissionStatePolling(refresh: RefreshMissionState, data: StateResponse | null) {
  const statePollTimerRef = useRef<number | null>(null);
  const statePollVisibleRef = useRef(true);
  const statePollLastAtRef = useRef<number>(0);
  const statePollAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;

    const cadenceMs = (): number =>
      statePollVisibleRef.current ? refreshIntervalMs(data) : STATE_POLL_HIDDEN_MS;

    const cancelPending = () => {
      if (statePollTimerRef.current !== null) {
        window.clearTimeout(statePollTimerRef.current);
        statePollTimerRef.current = null;
      }
    };

    const scheduleNext = (delayMs: number) => {
      if (cancelled) return;
      cancelPending();
      statePollTimerRef.current = window.setTimeout(() => {
        statePollTimerRef.current = null;
        void fireOnce();
      }, delayMs);
    };

    const fireOnce = async () => {
      if (cancelled) return;
      const now = Date.now();
      const sinceLast = now - statePollLastAtRef.current;
      if (sinceLast < STATE_POLL_MIN_GAP_MS) {
        scheduleNext(STATE_POLL_MIN_GAP_MS - sinceLast);
        return;
      }
      statePollLastAtRef.current = now;
      if (statePollAbortRef.current) {
        statePollAbortRef.current.abort();
      }
      const controller = new AbortController();
      statePollAbortRef.current = controller;
      try {
        await refresh(false, controller.signal);
      } finally {
        if (statePollAbortRef.current === controller) {
          statePollAbortRef.current = null;
        }
      }
      if (cancelled) return;
      scheduleNext(cadenceMs());
    };

    const onVisibilityChange = () => {
      if (typeof document === "undefined") return;
      const visible = document.visibilityState !== "hidden";
      const wasVisible = statePollVisibleRef.current;
      statePollVisibleRef.current = visible;
      if (visible === wasVisible) return;
      if (statePollAbortRef.current) {
        statePollAbortRef.current.abort();
        statePollAbortRef.current = null;
      }
      cancelPending();
      if (visible) {
        void fireOnce();
      } else {
        scheduleNext(cadenceMs());
      }
    };

    if (typeof document !== "undefined") {
      statePollVisibleRef.current = document.visibilityState !== "hidden";
    }

    void fireOnce();
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return () => {
      cancelled = true;
      cancelPending();
      if (statePollAbortRef.current) {
        statePollAbortRef.current.abort();
        statePollAbortRef.current = null;
      }
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    };
  }, [refresh, data?.live.refresh_interval_s]);
}
