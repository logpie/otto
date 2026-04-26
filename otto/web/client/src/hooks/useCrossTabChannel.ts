// W10-CRITICAL-1/2: cross-tab state-mutation broadcast.
//
// Two operators on one project (each in their own browser tab) was silently
// broken: a job submitted in tab A would not appear in tab B's task board
// for at least the poll cadence (~1.5s under best case, longer if the
// browser throttled timers). Likewise a cancellation issued from tab B
// would not flip tab A's row until A's next poll fired.
//
// The fix is defence in depth:
//   1. Server adds `Cache-Control: no-store` to /api/* (in app.py) so a
//      caching layer can never serve a stale `/api/state` snapshot.
//   2. Both tabs subscribe to a `BroadcastChannel('mc-state-mutation')`.
//      Whenever a tab issues a mutation (queue submit, cancel, watcher
//      start/stop, merge-all), it posts a message; peer tabs receive it
//      and trigger an immediate `/api/state` refresh instead of waiting
//      for the next poll tick.
//   3. The polling loop continues to run as a safety net — if
//      BroadcastChannel is unavailable (older Safari) we fall back to a
//      `storage` event on `localStorage` for the same effect, and the
//      poll cadence keeps the UI eventually-consistent.
//
// A NEW BroadcastChannel instance is created per hook mount; they are
// "same-origin" by default so cross-tab same-app delivery works without
// any backend involvement.

import {useEffect, useRef} from "react";

export type MutationKind =
  | "queue.submit"
  | "queue.action"
  | "watcher.start"
  | "watcher.stop"
  | "merge-all"
  | "project.change";

export interface MutationMessage {
  kind: MutationKind;
  // Optional run id / task id the peer can use to scope refreshes. Not
  // required — listeners always do a full refresh today.
  runId?: string;
  taskId?: string;
  // Monotonic timestamp from the sender so receivers can de-dupe.
  ts: number;
  // Tag that uniquely identifies the sending tab session. The sender
  // ignores its own messages on receive so we never re-trigger refresh
  // in the originating tab.
  origin: string;
}

const CHANNEL_NAME = "mc-state-mutation";
const STORAGE_KEY = "mc-state-mutation:last";

function newOriginTag(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `o-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

interface ChannelHandle {
  /**
   * Broadcast a mutation event to peer tabs. The originating tab does NOT
   * receive its own broadcast — callers should still trigger their own
   * refresh path explicitly.
   */
  publish: (kind: MutationKind, opts?: {runId?: string; taskId?: string}) => void;
}

/**
 * Subscribe to peer-tab mutations. ``onPeerMutation`` is invoked whenever
 * a *different* tab broadcasts a mutation event. The handle returned lets
 * the caller publish its own mutations.
 *
 * The hook is safe to call when ``BroadcastChannel`` is missing (older
 * Safari) — it transparently falls back to a ``storage`` event.
 */
export function useCrossTabChannel(
  onPeerMutation: (msg: MutationMessage) => void,
): ChannelHandle {
  // Stable origin tag for this tab session.
  const originRef = useRef<string>("");
  if (originRef.current === "") {
    originRef.current = newOriginTag();
  }
  // Keep the latest handler in a ref so the channel subscription does not
  // need to tear down on every render.
  const handlerRef = useRef(onPeerMutation);
  useEffect(() => {
    handlerRef.current = onPeerMutation;
  }, [onPeerMutation]);

  // BroadcastChannel + storage fallback are wired once per hook lifecycle.
  const channelRef = useRef<BroadcastChannel | null>(null);
  // Track the last-seen ts per origin so a storage-event echo doesn't
  // replay an old message.
  const seenRef = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    if (typeof window === "undefined") return;

    let bc: BroadcastChannel | null = null;
    if (typeof BroadcastChannel !== "undefined") {
      try {
        bc = new BroadcastChannel(CHANNEL_NAME);
        bc.onmessage = (ev: MessageEvent<MutationMessage>) => {
          const msg = ev.data;
          if (!isMutationMessage(msg)) return;
          if (msg.origin === originRef.current) return;
          const last = seenRef.current.get(msg.origin) || 0;
          if (msg.ts <= last) return;
          seenRef.current.set(msg.origin, msg.ts);
          handlerRef.current(msg);
        };
      } catch {
        bc = null;
      }
    }
    channelRef.current = bc;

    // Storage-event fallback (Safari <= 15.3, server-side rendering, or
    // sandboxed BroadcastChannel failures). We write the message to
    // localStorage with a unique key/value pair on every publish; peers
    // receive a `storage` event with the new value.
    const onStorage = (ev: StorageEvent) => {
      if (ev.key !== STORAGE_KEY || ev.newValue === null) return;
      let msg: unknown;
      try {
        msg = JSON.parse(ev.newValue);
      } catch {
        return;
      }
      if (!isMutationMessage(msg)) return;
      if (msg.origin === originRef.current) return;
      const last = seenRef.current.get(msg.origin) || 0;
      if (msg.ts <= last) return;
      seenRef.current.set(msg.origin, msg.ts);
      handlerRef.current(msg);
    };
    window.addEventListener("storage", onStorage);

    return () => {
      window.removeEventListener("storage", onStorage);
      if (channelRef.current) {
        try {
          channelRef.current.close();
        } catch {
          // close() can throw if the channel was already closed; ignore.
        }
      }
      channelRef.current = null;
    };
  }, []);

  return {
    publish: (kind, opts) => {
      const msg: MutationMessage = {
        kind,
        ts: Date.now(),
        origin: originRef.current,
        ...(opts?.runId ? {runId: opts.runId} : {}),
        ...(opts?.taskId ? {taskId: opts.taskId} : {}),
      };
      if (channelRef.current) {
        try {
          channelRef.current.postMessage(msg);
        } catch {
          // Channel can throw if closed mid-publish; fall through to the
          // storage fallback so the mutation still propagates.
        }
      }
      // Always also write to localStorage so older browsers without
      // BroadcastChannel still get a `storage` event in the peer tab.
      if (typeof window !== "undefined" && window.localStorage) {
        try {
          window.localStorage.setItem(STORAGE_KEY, JSON.stringify(msg));
        } catch {
          // Quota / private-mode failures are best-effort only — the
          // BroadcastChannel path or the next poll tick will catch up.
        }
      }
    },
  };
}

function isMutationMessage(value: unknown): value is MutationMessage {
  if (!value || typeof value !== "object") return false;
  const obj = value as Record<string, unknown>;
  if (typeof obj.kind !== "string") return false;
  if (typeof obj.origin !== "string") return false;
  if (typeof obj.ts !== "number") return false;
  return true;
}
