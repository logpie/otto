/**
 * Pure formatter utilities — no React, no DOM.
 *
 * Extracted from App.tsx during the mc-audit redesign §7 component
 * refactor. These were inline helpers for ages; pulling them out lets us
 * (a) shrink App.tsx, (b) test them in isolation, (c) reuse from new
 * components without retracing the import graph.
 *
 * The signatures are unchanged — App.tsx imports them by name and the
 * runtime behavior is identical.
 */

import type {DiffResponse, LandingItem, RunDetail, StateResponse, TokenUsage} from "../types";

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

export function formatCompactNumber(value: number): string {
  const amount = Math.max(Number(value || 0), 0);
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(Math.round(amount));
}

export function humanBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function tokenTotal(tokenUsage?: TokenUsage): number {
  if (!tokenUsage) return 0;
  const explicit = Number(tokenUsage.total_tokens || 0);
  if (explicit > 0) return explicit;
  const cacheCreation = Number(tokenUsage.cache_creation_input_tokens || 0);
  const cacheRead = Number(tokenUsage.cache_read_input_tokens || 0);
  const derived = Number(tokenUsage.input_tokens || 0)
    + cacheCreation
    + cacheRead
    + Number(tokenUsage.output_tokens || 0)
    + Number(tokenUsage.reasoning_tokens || 0);
  return Math.max(explicit, derived);
}

export function tokenBreakdownLine(tokenUsage?: TokenUsage): string {
  if (!tokenUsage) return "No token usage recorded";
  const input = Number(tokenUsage.input_tokens || 0);
  const cacheRead = Number(tokenUsage.cache_read_input_tokens || 0);
  const cacheWrite = Number(tokenUsage.cache_creation_input_tokens || 0);
  const cachedSubset = cacheRead || cacheWrite ? 0 : Number(tokenUsage.cached_input_tokens || 0);
  const output = Number(tokenUsage.output_tokens || 0);
  const reasoning = Number(tokenUsage.reasoning_tokens || 0);
  const parts = [
    input ? `${formatCompactNumber(input)} input` : "",
    cacheRead ? `${formatCompactNumber(cacheRead)} cache read` : "",
    cacheWrite ? `${formatCompactNumber(cacheWrite)} cache write` : "",
    cachedSubset ? `${formatCompactNumber(cachedSubset)} cached` : "",
    output ? `${formatCompactNumber(output)} output` : "",
    reasoning ? `${formatCompactNumber(reasoning)} reasoning` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "No token usage recorded";
}

export function usageLine(item: {token_usage?: TokenUsage; cost_usd?: number | null; cost_display?: string | null}): string {
  const tokens = tokenTotal(item.token_usage);
  const cost = item.cost_usd && item.cost_usd > 0 ? `$${item.cost_usd.toFixed(2)}` : "";
  const tokenText = tokens ? `${formatCompactNumber(tokens)} tokens` : item.cost_display || "";
  return [tokenText, cost && cost !== tokenText ? cost : ""].filter(Boolean).join(" · ") || "-";
}

export function storyTotalsFromLanding(items: LandingItem[]): {passed: number; tested: number} {
  return items.reduce(
    (totals, item) => {
      totals.passed += Number(item.stories_passed || 0);
      totals.tested += Number(item.stories_tested || 0);
      return totals;
    },
    {passed: 0, tested: 0},
  );
}

// Render an ISO timestamp as "Xs ago", "Xm Ys ago", etc. Returns "just now"
// for sub-second deltas and "in the future" if the server clock is ahead.
export function formatRelativeFreshness(iso: string): string {
  const fetched = Date.parse(iso);
  if (!Number.isFinite(fetched)) return "unknown";
  const deltaSec = Math.round((Date.now() - fetched) / 1000);
  if (deltaSec < 0) return "just now";
  if (deltaSec < 5) return "just now";
  if (deltaSec < 60) return `${deltaSec}s ago`;
  const minutes = Math.floor(deltaSec / 60);
  const seconds = deltaSec % 60;
  if (minutes < 60) {
    return seconds > 0 ? `${minutes}m ${seconds}s ago` : `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes > 0 ? `${hours}h ${restMinutes}m ago` : `${hours}h ago`;
}

export function formatEventTime(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

export function formatTechnicalIssue(message: string): string {
  const value = message.trim();
  if (/unknown revision|ambiguous argument|bad revision|invalid object name/i.test(value)) {
    return "Changed files could not be inspected because the source branch is missing or not reachable. Refresh after the task creates its branch, or remove and requeue the task.";
  }
  if (/working tree has|unstaged changes|uncommitted changes/i.test(value)) {
    return "Repository has local changes. Commit, stash, or revert them before landing.";
  }
  return value;
}

export function shortText(value: string, maxLength: number): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3))}...`;
}

export function capitalize(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : value;
}

export function titleCase(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

export function configSourceLabel(configFileExists: boolean): string {
  return configFileExists ? "otto.yaml" : "built-in default";
}

export function refreshLabel(status: string): string {
  if (status === "refreshing") return "refreshing";
  if (status === "error") return "refresh failed";
  return "";
}

export function storiesLine(packet: RunDetail["review_packet"]): string {
  const tested = Number(packet.certification.stories_tested || 0);
  const passed = Number(packet.certification.stories_passed || 0);
  return tested ? `${passed}/${tested}` : "-";
}

export function formatDiffTruncationBanner(diff: DiffResponse): string {
  const shownChars = diff.text ? diff.text.length : 0;
  const fullChars = diff.full_size_chars || 0;
  const shownLabel = humanBytes(shownChars);
  const totalLabel = humanBytes(fullChars);
  const hunksPart = diff.total_hunks > 0
    ? `Showing ${diff.shown_hunks.toLocaleString()} hunk${diff.shown_hunks === 1 ? "" : "s"} of ${diff.total_hunks.toLocaleString()}`
    : "Diff was truncated";
  const sizePart = `shown ${shownLabel} of ${totalLabel}`;
  return `${hunksPart} · ${sizePart}`;
}
