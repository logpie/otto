import type {ReactNode} from "react";

/**
 * Tiny prop-only leaf components extracted from App.tsx for reuse and to
 * keep App.tsx focused on composition. mc-audit redesign Wave 8 follow-up.
 *
 * These components have no internal state and no dependencies on App-level
 * state — they're pure rendering helpers.
 */

export function MetaItem({label, value, tooltip}: {label: string; value: string; tooltip?: string}) {
  return (
    <div>
      <dt title={tooltip}>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function FocusMetric({label, value}: {label: string; value: string}) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function OverviewMetric({label, value, tone}: {
  label: string;
  value: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}) {
  return (
    <div className={`overview-metric tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function ProjectStatCard({label, value, detail, tone}: {
  label: string;
  value: string;
  detail: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}) {
  return (
    <div className={`project-stat-card tone-${tone}`}>
      <span>{label}</span>
      <strong title={value}>{value}</strong>
      <p title={detail}>{detail}</p>
    </div>
  );
}

export function HealthCard({title, status, detail, next, tone}: {
  title: string;
  status: string;
  detail: string;
  next: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}) {
  return (
    <article className={`health-card tone-${tone}`}>
      <span>{title}</span>
      <strong>{status}</strong>
      <p>{detail}</p>
      <em>{next}</em>
    </article>
  );
}

export function ReviewMetric({label, value, onClick, disabled = false, title, testId}: {
  label: string;
  value: string;
  onClick?: (() => void) | undefined;
  disabled?: boolean;
  title?: string;
  testId?: string;
}) {
  const content = (
    <>
      <span>{label}</span>
      <strong>{value}</strong>
    </>
  );
  if (!onClick) return <div title={title} data-testid={testId}>{content}</div>;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      data-testid={testId}
    >
      {content}
    </button>
  );
}

export function ReviewDrawer({title, meta, defaultOpen = false, children}: {
  title: string;
  meta: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <details className="review-drawer" open={defaultOpen}>
      <summary>
        <span>{title}</span>
        <strong>{meta}</strong>
      </summary>
      <div className="review-drawer-body">{children}</div>
    </details>
  );
}

export function CommandList({commands}: {commands: Array<{label: string; command: string}>}) {
  return (
    <div className="handoff-command-list">
      {commands.map((command, index) => (
        <div className="handoff-command" key={`${command.command}-${index}`}>
          <span>{command.label}</span>
          <code>{command.command}</code>
        </div>
      ))}
    </div>
  );
}
