# Certifier E2E Results — 2026-03-31

## What was done

### 1. Certifier integrated into outer loop + CLI
- `otto certify --tier 2` now runs full pipeline: Tier 1 + Tier 2 + PoW report
- Outer loop uses certifier instead of product_qa.py (deleted agent-based verification)
- Fix tasks from outer loop now include step-level HTTP evidence
- Codex reviewed: 4 issues found and fixed (Tier 1 failures surfaced, empty Tier 2 check, JSON output, intent vs spec comment)

### 2. Certifier generalized (Codex adversarial audit)
15 issues found and fixed across 4 batches:
- **Batch A**: False-certification cleanup (registration, claim scoring, self-healing verdicts, auth laxity)
- **Batch B**: Tier 2 e-commerce removal (built-in journeys gated, path rewrites removed, generic ID extraction)
- **Batch C**: Generators made product-agnostic (intent/journey compiler prompts rewritten, legacy normalizer deleted)
- **Batch D**: Adapter/baseline generalized (analysis order fixed, has_cart_model → resource_models, generic entity discovery)

### 3. E2E certification results

**Ecommerce store (fresh compilation, tightened certifier):**
- Tier 1: 14/24 (58%) — previously 23/23 (100%)
- Tier 2: 3/10 (30%) — previously 7/9 (78%)
- NOT CERTIFIED (6 hard failures)

**Why scores dropped:**
The old scores were inflated by slop. The new certifier honestly reports:
- Response wrapper mismatches: API returns `{"data": [...]}` but claims expect `{"products": [...]}`. Not a product bug — the LLM compiler assumed conventional key names.
- Field name mismatches: `product_id` vs `productId`, flat string vs structured address object.
- Blocked claims now count in the denominator (previously excluded).
- Self-healed retries no longer flip verdicts.

**This is correct behavior.** The certifier is now honest about what passes and what doesn't. The gaps are in the LLM compilation quality (wrong field names, wrong response shapes), not in the product.

**Bookmark manager:** Could not test properly — port 3003 is running the e-commerce store, not the bookmark manager. Test environment issue.

## Open issues

### Certifier accuracy (LLM compilation quality)
The tightened certifier surfaces real gaps between what the LLM compiler expects and actual API conventions:
1. **Response wrappers**: APIs often return `{"data": [...]}` instead of `{"products": [...]}`. The compiler needs to handle this.
2. **Field naming**: The compiler guesses `product_id` but the app uses `productId`. Could be improved by feeding adapter-discovered schemas to the compiler.
3. **Request bodies**: The compiler guesses checkout body fields but the app expects different ones. Same solution — adapter info.

### Next steps for the user
1. **E2E test `otto build` with certifier in outer loop** — full PER cycle with fix tasks
2. **Test more product types** — task manager, blog, SaaS dashboard to validate generalization
3. **Compare otto vs bare CC** — re-run the benchmark with the honest certifier
4. **Audit the full system** — planner, workflow, observability, cost

## Test results
- 602 tests pass (521 core + 26 certifier + 55 CLI)
- 0 failures
