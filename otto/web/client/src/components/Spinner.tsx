/**
 * Pure-CSS rotating spinner. The single visual loading indicator used across
 * Mission Control — replaces the text-only "Working", "Queueing", "loading"
 * affordances flagged in mc-audit microinteractions C3.
 *
 * Conventions:
 *   - `aria-hidden="true"`: screen readers should announce the surrounding
 *     button label ("Starting…", "Refreshing…"), not "spinner spinner spinner".
 *   - `currentColor`: inherits text color so it works on light buttons,
 *     primary/danger CTAs, and dark sidebar buttons without re-theming.
 *   - Animation styles live in styles.css (`.mc-spinner` keyframes) so this
 *     stays a server-renderable, prop-driven leaf component.
 *
 * Sizes map to a fixed CSS pixel scale (sm 12, md 16, lg 22) so callers do
 * not invent new sizes per site — a recurring drift the audit flagged.
 */
export type SpinnerSize = "sm" | "md" | "lg";

export interface SpinnerProps {
  size?: SpinnerSize;
  className?: string;
}

export function Spinner({size = "sm", className}: SpinnerProps) {
  const cls = ["mc-spinner", `mc-spinner-${size}`, className].filter(Boolean).join(" ");
  return <span className={cls} aria-hidden="true" data-testid="mc-spinner" />;
}
