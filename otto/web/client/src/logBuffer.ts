export const LOG_BUFFER_MAX_BYTES = 1_048_576;
export const LOG_POLL_BASE_MS = 1200;
export const LOG_POLL_BACKOFF_MS = [2000, 5000, 15000, 30000];

export type LogStatus = "idle" | "loading" | "ok" | "missing" | "error";

export interface LogState {
  text: string;
  totalLines: number;
  totalBytes: number;
  droppedBytes: number;
  path: string | null;
  status: LogStatus;
  error: string | null;
  lastUpdatedAt: number | null;
  pollIntervalMs: number;
  consecutiveErrors: number;
}

export const initialLogState: LogState = {
  text: "",
  totalLines: 0,
  totalBytes: 0,
  droppedBytes: 0,
  path: null,
  status: "idle",
  error: null,
  lastUpdatedAt: null,
  pollIntervalMs: LOG_POLL_BASE_MS,
  consecutiveErrors: 0,
};

export function bytesToString(value: string): number {
  if (typeof TextEncoder === "undefined") return value.length;
  return new TextEncoder().encode(value).length;
}

// Count newline characters (\n). When appending an incremental chunk this
// gives the number of additional lines closed by the chunk, which lets us
// maintain a running totalLines counter without ever re-splitting the full log.
export function countLines(text: string): number {
  if (!text) return 0;
  let count = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) count += 1;
  }
  return count;
}

export function appendToLogBuffer(
  prev: string,
  chunk: string,
  maxBytes: number,
): {text: string; droppedBytes: number} {
  if (!chunk) return {text: prev, droppedBytes: 0};
  const combined = prev + chunk;
  const combinedBytes = bytesToString(combined);
  if (combinedBytes <= maxBytes) return {text: combined, droppedBytes: 0};
  // Drop characters from the front until we are under the cap, then snap to
  // the next newline so partial lines don't sit at the head of the buffer.
  // We approximate bytes with characters for the slice search; exact byte
  // alignment is not meaningful when chunks can already split mid-grapheme.
  const overshootChars = Math.max(0, combined.length - maxBytes);
  let cut = overshootChars;
  const newlineAfterCut = combined.indexOf("\n", cut);
  if (newlineAfterCut >= 0 && newlineAfterCut - cut < 4096) cut = newlineAfterCut + 1;
  const truncated = combined.slice(cut);
  const droppedBytes = bytesToString(combined.slice(0, cut));
  return {text: truncated, droppedBytes};
}
