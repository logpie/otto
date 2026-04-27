import type {ReactNode} from "react";

/**
 * Generic pill / chip primitive. mc-audit redesign §3 W3.2.
 *
 * Encapsulates the 5-state palette so chips, status tags, and counters
 * have one canonical look. Existing inline `className="pill"` usage stays
 * working — this is purely additive.
 */
export type PillTone = "neutral" | "success" | "info" | "warning" | "danger";

export function Pill({tone = "neutral", title, children, className}: {
  tone?: PillTone;
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  const cls = ["pill", `pill-tone-${tone}`, className].filter(Boolean).join(" ");
  return <span className={cls} title={title}>{children}</span>;
}
