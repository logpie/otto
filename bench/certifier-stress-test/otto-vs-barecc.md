# Otto vs Bare CC — Head-to-Head Certification

Same intents, different builders, same certifier.

## Task Manager

| Builder | Tier 1 Score | Certified? | Cost | Time |
|---|---|---|---|---|
| **Bare CC** | **21/21 (100%)** | **YES** | ~$1 | ~5 min |
| Otto | 16/18 (89%) | NO | $6.42 | 15.4 min |

**Otto failures**: `/api/tasks` returns 401 (auth required) — the certifier can authenticate for bare CC's app but not otto's. Both use NextAuth + credentials. The difference is in compilation variance — the certifier compiled different test plans for each (different schema hints from slightly different Prisma schemas).

**Key insight**: Otto's product is actually functional (coding + tests passed), but the certifier's auth flow doesn't work for it. This is a **certifier auth robustness issue**, not an otto build quality issue.

## Recipe App

| Builder | Tier 1 Score | Certified? | Cost | Time |
|---|---|---|---|---|
| **Bare CC** | **15/20 (75%)** | NO | ~$1 | ~5 min |
| Otto | 5/21 (24%) | NO | ~$8 | ~20 min |

**Both fail certification**, but bare CC scores higher. Otto's recipe app has more 404s — routes may be at different paths or the app has more issues.

**Key insight**: Neither builder produces a fully certified recipe app. The recipe app has more complex features (ratings, favorites, search, categories) and both builders leave gaps. Bare CC scores higher because it builds a simpler, more working subset.

## Analysis

### The certifier is fair
- Same compilation per builder (schema-adapted)
- No builder-specific logic
- Failures are explained by real API differences

### Otto's value proposition
On the task manager, otto spent more ($6.42 vs ~$1) and more time (15 min vs 5 min) for a comparable product. The bare CC version actually scored higher on certification.

On the recipe app, otto spent much more ($8 vs ~$1) for a lower-scoring product.

### Caveats
1. **Certification ≠ completeness**: The certifier tests claimed features. A product could have MORE features than claimed (not penalized) or fewer (penalized).
2. **Auth flow variance**: The certifier's NextAuth auth flow works inconsistently across products. This inflates/deflates scores unpredictably.
3. **Single run**: One run per builder. LLM variance means results could differ on re-run.
4. **No outer loop**: Otto ran with `--no-qa` (no verification loop). With the certifier in the outer loop, otto could fix failures and improve.

### What this tells us about otto
Otto's current value is NOT "builds better products for more money." On simple products, bare CC is faster and cheaper with comparable quality. Otto's value is:
1. **Scope accountability** — otto decomposes and tracks all features
2. **Verification loop** — with certifier in outer loop, otto can fix what it breaks
3. **Complex products** — otto wins on multi-surface products where bare CC cuts scope

This stress test was on simple single-surface apps where bare CC excels. The real comparison needs multi-feature products where scope management matters.
