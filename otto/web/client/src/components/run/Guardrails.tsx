// Guardrails — pinned negative scope (research §2 vocabulary: "don't" rules).
// Renders as tone-coded pills:
//   verified=true   → ✓ success tone (audit confirmed the guardrail held)
//   verified=false  → ✗ danger tone (audit caught a violation)
//   verified=null   → ⊘ neutral tone (pre-audit, not yet checked)
// Glyph + tone together give a visual distinction so reviewers don't
// mistake an unverified guardrail for a satisfied one (RUA tick 62 W6-C).

import type { GuardrailView } from "../../types/run";

interface Props {
  guardrails: GuardrailView[];
}

function guardrailVariant(verified: boolean | null | undefined): {
  cls: string;
  glyph: string;
  label: string;
} {
  if (verified === true) {
    return { cls: "verified", glyph: "✓", label: "verified" }; // ✓
  }
  if (verified === false) {
    return { cls: "violated", glyph: "✗", label: "violated" }; // ✗
  }
  return { cls: "unverified", glyph: "⊘", label: "not yet verified" }; // ⊘
}

export function Guardrails({ guardrails }: Props) {
  if (guardrails.length === 0) return null;
  return (
    <section className="guardrails" data-testid="guardrails">
      <h3>Guardrails</h3>
      <ul>
        {guardrails.map((g) => {
          const v = guardrailVariant(g.verified);
          return (
            <li
              key={g.id}
              className={`guardrail guardrail-${v.cls}`}
              title={`applies to: ${g.applies_to} (${v.label})`}
              data-testid={`guardrail-${g.id}`}
              data-verified={
                g.verified === true ? "true" : g.verified === false ? "false" : "unknown"
              }
            >
              <span className="guardrail-glyph" aria-label={v.label}>
                {v.glyph}
              </span>
              <span className="guardrail-text">{g.text}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export default Guardrails;
