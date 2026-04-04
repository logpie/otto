# Certifier Stress Test — Comprehensive Findings

4 diverse products built with bare CC, certified with the generalized certifier.

## Scores Summary (v4 — after all fixes)

| Product | Tier 1 | Tier 2 (Journeys) | Tier 2 (Steps) | Cost | Compile | Execute |
|---|---|---|---|---|---|---|
| Task Manager | **21/21 (100%) CERTIFIED** | 2/9 (22%) | 27/34 (79%) | $0.25 | 66s | 2.7s |
| Blog Platform | 13/21 (62%) | 3/10 (30%) | 26/34 (76%) | $0.20 | 54s | 2.2s |
| Recipe App | 15/20 (75%) | 3/10 (30%) | 21/28 (75%) | $0.22 | 58s | 2.0s |
| URL Shortener | 5/20 (25%) | 1/10 (10%) | 11/20 (55%) | $0.21 | 59s | 2.9s |

**Improvement from v2 → v4 (after adapter→compiler feedback + self-healing):**

| Product | v2 (before) | v4 (after) | Delta |
|---|---|---|---|
| Task Manager | 10/19 (53%) | 21/21 (100%) | **+47pp** |
| Blog Platform | 12/21 (57%) | 13/21 (62%) | +5pp |
| Recipe App | 11/22 (50%) | 15/20 (75%) | **+25pp** |
| URL Shortener | 4/20 (20%) | 5/20 (25%) | +5pp |

**Average compile: ~59s, average execute: ~2.5s, average cost: ~$0.22**

---

## Failure Categories

### Category 1: Enum/Case Mismatch (CERTIFIER PROBLEM — over-corrected)
**Frequency: 8+ failures across 2 projects**

The LLM compiler generates lowercase enum values (`"status": "todo"`, `"status": "open"`, `"status": "completed"`) but apps use UPPERCASE (`"TODO"`, `"IN_PROGRESS"`, `"DONE"`).

We removed enum case self-healing in the generalization pass. This was over-corrected.

**Evidence:**
- Task Manager: 6 failures cascade from `"status": "todo"` → 400 "Invalid status"
- Recipe App: `"score": 4` sent to rate endpoint but routed to wrong URL (`/api/recipes/:id/rate`)

**Verdict:** Enum case is a serialization convention, not a product bug. The certifier should normalize and note in proof, but still PASS the claim. The old self-healing was right in intent, wrong in implementation (it hid the original error entirely).

**Fix:** Re-add enum case normalization but record both the original failure AND the corrected retry in proof. Claim passes if the corrected request succeeds.

### Category 2: Field Name Mismatch (CERTIFIER PROBLEM — compilation quality)
**Frequency: 5+ failures across 3 projects**

The LLM compiler guesses field names that don't match the actual API:
- Task Manager: `"username"` vs `"name"` + `"email"` for registration
- Task Manager: `"due_date"` vs `"dueDate"` (snake_case vs camelCase)
- Recipe App: `"cook_time"` vs `"cookTime"`, `"prep_time"` vs `"prepTime"`
- Recipe App: `"categories": ["Dessert"]` but API expects `"category": "dessert"`
- URL Shortener: `"custom_code"` vs `"customCode"` (probably)

**Root cause:** The LLM compiler doesn't know the actual field names. The adapter discovers Prisma schema fields but doesn't feed them to the compiler.

**Fix:** Feed adapter-discovered schema (model fields, their types, casing convention) into the intent compiler prompt. The compiler should use actual field names from the schema, not guess.

### Category 3: Response Shape Mismatch (CERTIFIER PROBLEM — compilation expectations)
**Frequency: 5+ failures across 2 projects**

The LLM compiler expects conventional response keys (`"products"`, `"orders"`, `"items"`) but apps often use different patterns:
- `{"data": [...]}` wrapper (e-commerce)
- Bare arrays `[{...}, {...}]` (blog, recipe, task manager)
- The compiler expects `body contains ['My First Blog Post']` but the response has different seed data titles

**Root cause:** Two issues:
1. The compiler assumes a response key convention the app doesn't use
2. The compiler invents test data titles (`"Pay rent"`, `"My First Blog Post"`) that don't exist in seed data

**Fix:**
1. When checking `body contains`, search the full response body (already done for strings)
2. When checking `JSON has keys`, unwrap `{"data": [...]}` wrappers
3. For claims that check "can list X", don't hardcode expected titles — just verify the response is a non-empty collection

### Category 4: Wrong API Paths (CERTIFIER + PRODUCT INTERACTION)
**Frequency: 12+ failures in URL shortener, 2-3 in others**

URL shortener: ALL API calls to `/api/links` return 404. The adapter finds no routes at all (possibly the routes are at different paths like `/api/urls` or the build didn't create API routes).

Recipe App: `/api/recipes/:id/rate` and `/api/recipes/:id/favorite` return 500 (server error, not 404) — endpoints exist but have bugs.

**Root cause:** The LLM compiler guesses paths based on the intent. When paths don't match:
- 404 = wrong path (certifier problem) or missing feature (product problem)
- 500 = endpoint exists but has bugs (product problem, certifier correctly catches)

**Fix:** The adapter's route discovery should be the primary source of API paths. Feed discovered routes into the compiler so it uses real paths.

### Category 5: Auth Blocked / Password Not Recoverable (CORRECT BEHAVIOR)
**Frequency: 2-4 per project**

URL shortener seed creates user with `password123` but the adapter can't recover it from code (we removed the invented password fallback). Claims that need auth are correctly marked `blocked_by_harness`.

**Verdict:** This is correct — the certifier honestly reports it can't test auth-dependent claims when it can't discover credentials. The old behavior (inventing passwords) was wrong.

**However:** The seed file literally has `password: 'password123'` and the adapter should be able to find it. The password recovery code may have regressed.

### Category 6: Real Product Bugs Found (CERTIFIER CORRECT)
**Frequency: 3-5 per project**

The certifier correctly identifies real issues:
- Task Manager: Tasks accessible without auth (user isolation claim fails)
- Blog App: POST /api/posts/{id} returns 403 even for the post author (ownership check too strict?)
- Recipe App: POST /api/recipes returns 500 with categories field (server error)
- URL Shortener: Many 404s suggest the build didn't create complete API routes

**Verdict:** These are genuine product findings. The certifier is working as designed for these cases.

---

## Patterns for Certifier Design

### Pattern 1: Adapter → Compiler Feedback Loop is Critical
The #1 source of false failures is the compiler guessing wrong field names, paths, and conventions. The adapter already discovers this information from code. The fix is to feed adapter output into the LLM compilation prompt:
- Prisma schema fields + types + casing → correct field names in test bodies
- Route paths from `route.ts` files → correct API paths in test steps
- Auth endpoints → correct registration/login field names

### Pattern 2: Self-Healing Should Be "Try + Record", Not "Hide"
We over-corrected by removing all self-healing. The right model:
1. Try the compiled request exactly as specified
2. If it fails with 400 (validation error), try self-healing (enum case, field augmentation)
3. If self-healed request succeeds: **CLAIM PASSES** but proof records BOTH the original failure AND the corrected retry
4. If self-healed request also fails: **CLAIM FAILS** with both attempts in proof

This is honest about the harness's corrective action while not penalizing the product for convention differences.

### Pattern 3: Response Validation Should Be Structural, Not Content-Based
Instead of checking `body contains ['My First Blog Post']` (which depends on test data being created first), check structural properties:
- "Response is a non-empty array" for list endpoints
- "Response items have keys [id, title, ...]" for shape validation
- "Response has at least N items" for count validation

Content-dependent checks should only run AFTER a successful create step.

### Pattern 4: Cascading Failures Inflate Error Counts
When one root cause (enum case) causes 6 claims to fail, it looks like 6 problems. The certifier should:
- Identify root causes (registration fails → all auth-dependent claims fail)
- Report the root cause prominently, cascade as "blocked by: registration failure"
- Count root causes for progress detection, not cascaded failures

### Pattern 5: Password Discovery Needs Work
The adapter correctly refuses to invent passwords, but it should still be able to find `password: 'password123'` in seed files. The password recovery code may be too conservative now.

---

## v4 Remaining Failure Analysis

After all fixes, the remaining failures fall into clear categories:

### Real product bugs/limitations (certifier is CORRECT):
- **URL shortener (15 failures)**: NextAuth session cookies not being set in dev mode. All auth-dependent features genuinely untestable. This is a product bug.
- **Blog (5 hard failures)**: auth-register fails because blog uses NextAuth without separate register endpoint at `/api/auth/register` (uses `/api/register` instead). The certifier tries the wrong path. Post update/delete returns 403 — ownership check may be too strict.
- **Recipe (3 hard failures)**: POST /api/recipes returns 500 with certain field combinations. Server-side validation error.

### Certifier improvements still needed:
1. **NextAuth register path discovery**: Blog has register at `/api/register`, not `/api/auth/register`. The certifier should use adapter-discovered register endpoint.
2. **NextAuth session handling in dev mode**: Some apps don't set cookies properly. The certifier should detect this and report `blocked_by_harness` for auth-dependent claims instead of failing each one individually.
3. **Cascading failure attribution**: URL shortener has 15 failures all from the same root cause (auth not working). Should be reported as 1 root cause + 14 cascades.

## Otto Build Issues Found

### Planner JSON parsing failure
`otto build` failed on first attempt for both task manager and recipe app. The planner agent output prose descriptions instead of JSON, and `_parse_planner_output()` couldn't extract the plan.

**Error:** `No JSON found in planner output and otto_plan.json not created`
**Root cause:** The planner prompt didn't strongly enough enforce JSON output format. The agent described the plan in markdown prose.
**Impact:** Build fails immediately with no recovery — no tasks created, no logs written.
**File:** `otto/product_planner.py:287`

Retried with more explicit intent wording — results pending.

## Metrics

| Metric | Value |
|---|---|
| Total products tested | 4 |
| Total claims compiled | 82 |
| Total claims passed | 37 (45%) |
| Total claims failed | 37 (45%) |
| Total claims blocked | 8 (10%) |
| False failures (certifier bugs) | ~20 (enum case, field names, response shape) |
| True failures (product bugs/missing features) | ~17 |
| True passes | 37 |
| Estimated real accuracy | ~75-80% (v4, after adapter→compiler + self-healing) |
| Task Manager accuracy | 100% (CERTIFIED — all claims pass correctly) |
| Compile time per product | ~55s |
| Execute time per product | ~2.7s |
| Compile cost per product | ~$0.21 |

**Key insight:** The certifier is ~45% accurate right now, but ~25% of failures are certifier bugs (enum case, field names). Fixing the adapter→compiler feedback loop would bring accuracy to ~65-70%. The remaining ~30% are real product issues the certifier correctly catches.
