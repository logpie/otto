# Product Certifier — Independent Evidence-Based Product Evaluation

**Date**: 2026-03-30
**Status**: v1 implemented, v1.1 in progress
**Location**: `otto/certifier/` (to be decoupled from otto later)

---

## What It Is

A builder-blind evaluation tool. Takes any software product + the original intent and answers: **did this product deliver what was asked for?**

Does NOT know if otto, bare CC, Cursor, a human, or GPT built it. Pure third-party certification.

---

## Architecture

```
Intent ──→ Intent Compiler ──→ Requirement Matrix (testable claims)
                                        │
                                        ├──→ Tier 0: Adapter (static code analysis)
                                        │      "What features exist in the code?"
                                        │
                                        ├──→ Tier 1: Baseline (deterministic HTTP/CLI probes)
                                        │      "Do the features actually work?"
                                        │
                                        └──→ Tier 2: Agentic (browser agent, future)
                                               "Can a real user complete the journeys?"
                                        │
                                        ▼
                                   Judge ──→ Certification Report
```

### Components

| Component | Status | What it does |
|---|---|---|
| **Intent Compiler** | Done | Turns "e-commerce store with auth, cart, Stripe" into 20+ testable claims with structured test steps |
| **Product Classifier** | Done | Auto-detects: Next.js, Express, Flask, React, Python CLI, etc. |
| **Adapter** | Done | Reads code: actual routes, seeded credentials, auth mechanism, data models. Finds structural gaps. |
| **Baseline Runner** | v1 done | HTTP probes, CLI commands, source code checks. Uses adapter output for auth + endpoint discovery. |
| **Judge** | Not started | Aggregates evidence across tiers. Produces final report with tiered scores. |
| **Report Generator** | Partial | JSON output exists. Needs markdown + tiered display. |

---

## Tiers

### Tier 0: Structural Analysis (Adapter)
- **Cost**: $0 (no LLM, no runtime)
- **Speed**: <1 second
- **What it checks**: Does the code contain the features?
  - Registration endpoint exists or not
  - Cart model exists or not
  - API routes discovered
  - Auth mechanism identified
  - Seeded credentials found
- **Outcome per claim**: `present` / `not_implemented` / `unknown`
- **Value**: Catches scope cuts instantly. The 5 missing features in bare CC were found here.

### Tier 1: Deterministic Probes (Baseline)
- **Cost**: ~$0.20 (one LLM call to compile intent, rest is HTTP)
- **Speed**: 5-30 seconds (plus intent compilation ~30s first time, cached after)
- **What it checks**: Do the features actually work at runtime?
  - App starts
  - API endpoints return expected responses
  - Auth flow works (seeded credentials)
  - CRUD operations succeed
  - Error handling for invalid input
- **Outcome per claim**: `pass` / `fail` / `not_implemented` / `blocked_by_harness`
- **Value**: Catches runtime bugs, wrong response formats, broken endpoints

### Tier 2: Agentic Exploration (Future)
- **Cost**: ~$2-5 per evaluation
- **Speed**: 3-10 minutes
- **What it checks**: Can a real user complete the product journeys?
  - Browser-based navigation
  - Form filling, button clicking
  - Multi-step flows (register → login → add to cart → checkout)
  - Visual rendering checks (screenshots)
- **Outcome per claim**: `pass` / `fail` with screenshot evidence
- **Value**: Catches UX issues, JavaScript errors, flow-breaking bugs

---

## Requirement Matrix

The intent compiler produces a matrix of testable claims. Each claim has:

```yaml
id: auth-register
description: Users can register with email and password
priority: critical          # critical | important | nice
category: feature           # feature | ux | data | error-handling | security
test_approach: api          # api | browser | cli | code-review
hard_fail: true             # failing this fails certification
test_steps:                 # machine-executable steps
  - action: http
    method: POST
    path: /api/auth/register
    candidate_paths: [/api/auth/register, /api/register, /api/signup]
    body: {email: "test@eval.com", password: "test123", name: "Test"}
    expect_status: [200, 201]
    expect_json_keys: [id, email]
```

### Matrix Rules
- **Compiled from intent ONLY** — never sees the code. Evaluation criteria are independent of implementation.
- **Compiled ONCE, reused** — same matrix for comparing otto vs bare CC vs any other builder.
- **Cached by intent hash** — same intent string produces same cache key.
- **10-25 claims** for a typical product. Not over-decomposed.

---

## Claim Outcomes

| Outcome | Meaning | Counts as |
|---|---|---|
| `pass` | Claim verified with evidence | Pass |
| `fail` | Claim tested and failed | Fail |
| `not_implemented` | Feature doesn't exist in code (adapter) | Fail (structural) |
| `blocked_by_harness` | Can't test due to certifier limitation | Neither (excluded from score) |
| `not_applicable` | Claim doesn't apply to this product type | Neither (excluded from score) |

**Key**: `blocked_by_harness` is NOT a failure. It means the certifier can't verify this claim. The product might be fine — we just can't prove it deterministically. This prevents false negatives from auth limitations.

---

## Report Format

### Summary (what the user sees first)

```
╔══════════════════════════════════════════════════╗
║  CERTIFICATION REPORT                            ║
║  Intent: e-commerce store with auth, cart, admin  ║
╠══════════════════════════════════════════════════╣
║                                                  ║
║  Tier 0 (Structure):  18/23 features present     ║
║                        5 NOT IMPLEMENTED          ║
║                                                  ║
║  Tier 1 (Runtime):    15/20 probes passed        ║
║                        3 blocked (auth harness)   ║
║                        2 failed                   ║
║                                                  ║
║  Verdict: NOT CERTIFIED (5 missing features)      ║
║                                                  ║
╚══════════════════════════════════════════════════╝
```

Note: Tier 1 denominator excludes `blocked_by_harness` claims. You can't fail what you can't test.

### Per-claim detail

```
NOT IMPLEMENTED (5):
  ✗ auth-register     Users can register with email and password
    Evidence: No registration endpoint found in codebase (adapter)
  ✗ cart-add-item     Users can add a product to their cart
    Evidence: No CartItem model in Prisma schema (adapter)
  ...

FAILED (2):
  ✗ catalog-search    Users can search products by name
    Evidence: GET /api/products?q=test → 200 but returned all products
  ...

BLOCKED (3):
  ? admin-create      Admin can create new products
    Reason: Auth session not established — harness limitation
  ...

PASSED (13):
  ✓ catalog-list      Users can browse product catalog
    Evidence: GET /api/products → 200, 12 products returned
  ✓ cart-view         Users can view their cart
    Evidence: GET /api/cart → 200, cart items returned
  ...
```

### Comparison mode

```
╔══════════════════════════════════════════════════╗
║  COMPARISON: Otto vs Bare CC                     ║
╠══════════════════════════════════════════════════╣
║                    Otto        Bare CC            ║
║  Structure     23/23 (100%)  18/23 (78%)         ║
║  Runtime       15/20 (75%)    7/15 (47%)         ║
║  Not impl.          0             5              ║
║  Blocked            3             3              ║
╠══════════════════════════════════════════════════╣
║  DIFFERENCES:                                    ║
║  ! auth-register   pass    not_implemented       ║
║  ! cart-add-item   pass    not_implemented       ║
║  ! cart-view       pass    not_implemented       ║
║  ! cart-remove     pass    not_implemented       ║
║  ! cart-update     pass    not_implemented       ║
╚══════════════════════════════════════════════════╝
```

---

## Implementation Plan

### v1.1 (accuracy + reporting) — NOW

| Task | What | Effort |
|---|---|---|
| **Tiered reporting** | Separate Tier 0 and Tier 1 scores. Exclude blocked claims from denominator. | Small |
| **Auth → blocked** | When auth-dependent claims fail because auth probe can't establish session, mark as `blocked_by_harness` instead of `fail`. | Small |
| **Shared matrix param** | `certify(project, intent, matrix_path=...)` to enforce same matrix across comparisons. | Small |
| **Markdown report** | Human-readable report alongside JSON. | Medium |
| **Comparison function** | `compare(result_a, result_b)` produces diff table. | Small |

### v1.2 (auth fix)

| Task | What | Effort |
|---|---|---|
| **NextAuth CSRF fix** | The manual flow works. Align baseline's implementation with the verified flow. | Medium |
| **Session propagation** | Ensure authenticated session carries across all claims, not just cart. | Small |
| **Auth result caching** | Authenticate once per run, reuse for all claims. | Small |

### v2 (agentic + generality)

| Task | What | Effort |
|---|---|---|
| **Agentic Tier 2** | Browser agent for multi-step flows. Uses chrome-devtools MCP. | Large |
| **CLI product support** | Test CLI tools (python todo.py add, list, done). | Medium |
| **API-only support** | Test pure APIs without UI (Express, FastAPI). | Small |
| **Benchmark runner** | Batch certify N products, aggregate comparison. | Medium |

### v3 (independence)

| Task | What | Effort |
|---|---|---|
| **Decouple from otto** | Move to standalone package. No otto imports. | Medium |
| **pip installable** | `pip install product-certifier` | Small |
| **CI integration** | GitHub Action / pre-merge gate | Medium |

---

## Scoring Rules

### Certification verdict

A product is **CERTIFIED** when:
1. Zero `hard_fail` claims in `fail` or `not_implemented` state
2. At least 80% of testable claims pass (excluding blocked)
3. App starts successfully

A product is **NOT CERTIFIED** when:
1. Any `hard_fail` claim is `fail` or `not_implemented`, OR
2. Less than 80% of testable claims pass, OR
3. App fails to start

### Score calculation

```
Tier 0 score = present / (present + not_implemented)
Tier 1 score = pass / (pass + fail)           # excludes blocked + NI + NA
Overall = weighted(Tier0 * 0.3, Tier1 * 0.7)  # structure matters but runtime matters more
```

Blocked claims reduce confidence, not score. Report shows: "3 claims could not be verified."

---

## Evidence Standards

Every claim result MUST have evidence:

| Outcome | Required evidence |
|---|---|
| pass | Command run + response received |
| fail | Command run + expected vs actual |
| not_implemented | Adapter finding (what was searched, what was missing) |
| blocked | What was attempted + why it failed |

**No evidence = no claim.** The certifier never says "I read the code and it looks right." It runs commands and observes behavior.

---

## Known Limitations (v1)

1. **Auth-dependent claims often blocked** — NextAuth CSRF flow partially working. 8 claims affected on typical Next.js app.
2. **Response format sensitivity** — expects specific JSON keys. Partially mitigated by wrapper detection ({data:[...]}).
3. **Non-deterministic matrix** — different compile runs produce different claims. Must use shared matrix for fair comparison.
4. **Web-only** — CLI and API-only products not fully tested.
5. **No visual checks** — can't verify UI rendering, layout, styling.

---

## First Benchmark Results

Shared 23-claim matrix, e-commerce store intent:

| | Otto (i2p) | Bare CC |
|---|---|---|
| Tier 0 (structure) | 23/23 (100%) | 18/23 (78%) |
| Tier 1 (runtime) | 15/20 (75%) | 7/15 (47%) |
| Not implemented | 0 | 5 |
| Blocked (auth) | 3 | 3 |
| **Verdict** | Not certified (auth blocked) | Not certified (5 missing) |

If auth probes fully working: Otto ~21/23 (91%), Bare CC ~12/23 (52%).

**Key finding**: Bare CC's 5 missing features (registration, cart×4) were detected by Tier 0 alone — from code structure, no runtime needed.
