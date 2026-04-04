# Certifier Hidden Inputs Audit

## The Bug Class

**Pattern:** Anything that varies between products but is invisible to the user → silent unfairness in comparison.

## All 9 Hidden Input Surfaces Found

### Compilation Phase (test plan differs)
| # | Input | Where | Fair? | Fix |
|---|---|---|---|---|
| 1 | Schema hint in matrix cache key | baseline.py:2680 | NO | ✅ `--matrix` flag |
| 2 | Journey compiled per-project | baseline.py:2705 | NO | ✅ `--journeys` flag |
| 3 | LLM compilation non-determinism | intent_compiler.py | OK | Caching + shared artifacts |

### Binding Phase (how test plan maps to product)
| # | Input | Where | Fair? | Fix |
|---|---|---|---|---|
| 4 | Adapter route injection | baseline.py:2149, tier2.py:370 | UNFAIR | Needs compile→bind→execute redesign |
| 5 | Self-healing (enum case, field casing, body augmentation) | baseline.py:1524 | UNFAIR | Same — bind phase, not execute phase |
| 6 | Auth principal selection (which seeded user) | adapter.py:310, baseline.py:2101 | Mostly OK | Document |

### Execution Phase (runtime state affects results)
| # | Input | Where | Fair? | Fix |
|---|---|---|---|---|
| 7 | Entity discovery (first record from live DB) | baseline.py:1257 | UNFAIR | Should use created data, not discovered |
| 8 | Claim execution order (shared session state) | baseline.py:293 | UNFAIR | Reset session between claims |
| 9 | Tier 1 contaminates Tier 2 (no DB reset) | __init__.py:90 | UNFAIR | Document / reset between tiers |

## Codex's Recommended Architecture

Replace ad-hoc flags with a 3-phase pipeline:

```
1. COMPILE (shared, product-independent)
   Intent → {claims, journeys, compiler_version, prompt_hash}
   Output: plan.json — one file, shareable

2. BIND (product-specific, auditable)
   plan.json + adapter output → bound_plan.json
   Only declared equivalence mappings:
     - Route binding (claim path → actual route)
     - Auth endpoint binding
     - Cookie name aliases
     - Response wrapper normalization
   NO opportunistic fallback. If binding fails → "unbound"

3. EXECUTE (deterministic)
   bound_plan.json → results
   No runtime mutations, no self-healing, no adapter calls
   If a step is unbound → blocked_by_harness
```

**Key invariant:** Same plan.json + same binding policy → same execution path.

## Current State vs Target

**Done:**
- `--matrix` flag (shared Tier 1 compilation) ✅
- `--journeys` flag (shared Tier 2 compilation) ✅
- Auth cookie name normalization ✅

**Not done (needs redesign):**
- Compile → bind → execute separation
- Eliminating runtime self-healing from execution
- Session/state reset between claims
- Binding audit trail
- Single `--plan` artifact replacing `--matrix` + `--journeys`

## When This Matters

- **Standalone certification (one product):** Hidden inputs are OK — adapter adaptation improves accuracy
- **Cross-product comparison:** Hidden inputs cause unfair results — must use shared plan
- **Regression testing (same product over time):** Hidden inputs cause flaky results — should pin the plan

The current design is fine for standalone certification. For comparison and regression, use `--matrix` + `--journeys`.
