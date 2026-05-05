// Pill — scoped status pill primitive for the RunDrawer surface.
//
// Tones: ok | warn | error | info | muted. Used for Run verdicts,
// Feature verdicts, Group statuses, and FeatureDrilldown headers
// (post-RUA round 1, B2). A separate `severity` variant covers
// finding severities (B3): critical=error, important=warn, polish=muted.
//
// Kept under components/run/ rather than alongside the global Pill
// in components/ because the tone vocabulary here ("ok|warn|error|
// info|muted") is RunDrawer-specific and the wireframe spec maps
// directly onto these tokens. The global Pill keeps its own
// "neutral|success|info|warning|danger" tone set unchanged.
import type { ReactNode } from "react";
import type { FindingSeverity } from "../../types/run";

export type PillTone = "ok" | "warn" | "error" | "info" | "muted";

interface PillProps {
  tone: PillTone;
  children: ReactNode;
  title?: string;
  className?: string;
  testId?: string;
}

export function Pill({ tone, children, title, className, testId }: PillProps) {
  const cls = ["run-pill", `run-pill-${tone}`, className].filter(Boolean).join(" ");
  return (
    <span className={cls} title={title} data-testid={testId}>
      {children}
    </span>
  );
}

interface BadgeProps {
  severity: FindingSeverity;
  children?: ReactNode;
  title?: string;
  testId?: string;
}

const SEVERITY_TONE: Record<FindingSeverity, PillTone> = {
  critical: "error",
  important: "warn",
  polish: "muted",
};

export function Badge({ severity, children, title, testId }: BadgeProps) {
  const tone = SEVERITY_TONE[severity];
  const cls = `run-pill run-pill-${tone} run-badge run-badge-${severity}`;
  return (
    <span className={cls} title={title} data-testid={testId}>
      {children ?? severity}
    </span>
  );
}

export default Pill;
