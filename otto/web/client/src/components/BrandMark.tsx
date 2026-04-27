/**
 * Otto brand mark — designed to read as "iterate / loop" rather than a
 * generic letter. Two arcs offset around a center: the outer arc is the
 * agent loop (build → verify → fix), the inner dot is the artifact landing.
 *
 * mc-audit redesign Phase D. Replaces the flat "O" letter mark.
 */
export function BrandMark({size = 36, ariaLabel = "Otto"}: {size?: number; ariaLabel?: string}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      role="img"
      aria-label={ariaLabel}
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect x="0" y="0" width="40" height="40" rx="10" fill="var(--accent)" />
      {/* Outer arc — left/top open to suggest motion */}
      <path
        d="M 20 9 a 11 11 0 1 0 11 11"
        fill="none"
        stroke="white"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      {/* Inner dot — landed artifact / pivot */}
      <circle cx="20" cy="20" r="2.6" fill="white" />
    </svg>
  );
}
