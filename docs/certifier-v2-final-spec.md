# Certifier v2: Final Spec

Incorporates feedback from Codex and Claude Plan agent reviews.

## Design Principles

1. **Don't predict. Interact.** The certifier interacts with the running app.
2. **Deterministic first, agentic when needed.** Fast probes handle the easy cases. Agents handle the hard ones.
3. **Reward signals, not just scores.** Every failure produces actionable fix guidance for the outer loop.
4. **No artificial limits.** The agent works until it has enough evidence. No budget caps.
5. **Fair by design.** Same claims, same standards. Not same effort — harder products need more verification.

## The Certifier's Job

The certifier sits in the outer loop:

```
Intent → Plan → Build → CERTIFY →
  if pass: done
  if fail:
    for each failed claim:
      create fix task with:
        - what the claim expects
        - what actually happened (HTTP evidence)
        - root cause diagnosis
        - suggested fix
    → rebuild → re-certify → ...
```

The certifier must produce outputs that are:
- **Accurate**: no false positives (inflate scores) or false negatives (miss working features)
- **Actionable**: each failure tells the coding agent WHAT to fix and HOW
- **Comprehensive**: covers all features in the intent
- **Fair**: enables honest comparison between builders
- **Rich**: provides reward signals that improve the product through iteration

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ 1. COMPILE (LLM, one-shot, shared across products)          │
│    Intent → SemanticClaim[]                                  │
│    "Users can create tasks"                                  │
│    Product-independent. No HTTP details.                     │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│ 2. ANALYZE (deterministic, per-product)                      │
│    Code + Running App → ProductManifest                      │
│    Static: routes, models, fields, auth, seeds               │
│    Runtime: probe key routes to confirm & enrich             │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│ 3. VERIFY (tiered, per-claim, per-product)                   │
│                                                              │
│    Tier 0: Structural check (does route/model exist?)        │
│            Deterministic. Free. Instant.                     │
│                                                              │
│    Tier 1: Deterministic probes (fire HTTP, check response)  │
│            No LLM. ~$0. ~0.1s per claim.                     │
│                                                              │
│    Tier 2: Agentic verification (for failures & complexity)  │
│            Agent reasons about responses, adapts, diagnoses. │
│            No turn limit. ~$0.02-0.10 per claim.             │
│            Produces actionable fix guidance.                 │
│                                                              │
│    Promotion: Tier 0 pass → Tier 1 → if ambiguous → Tier 2  │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│ 4. REPORT                                                    │
│    Per-claim: verdict + evidence chain + diagnosis            │
│    Per-product: scores + proof-of-work                       │
│    For outer loop: fix tasks with root cause + suggested fix  │
└──────────────────────────────────────────────────────────────┘
```

## Phase 1: Semantic Claims

Same as previous specs. LLM produces WHAT to test.

```python
@dataclass
class SemanticClaim:
    id: str                     # "task-create"
    description: str            # "Authenticated users can create tasks"
    category: str               # "crud" | "auth" | "access-control" | "validation" | "persistence" | "search"
    entity: str                 # "task"
    action: str                 # "create" | "read" | "list" | "update" | "delete" | ...
    priority: str               # "critical" | "important" | "nice"
    hard_fail: bool
    preconditions: list[str]    # ["authenticated"]
    negative: bool              # True for error/validation claims
    expected_behavior: str      # "returns created entity with ID"
```

No HTTP details. Shared across products via `--claims` flag.

## Phase 2: Product Manifest

### Static analysis (adapter — already exists)

```python
@dataclass
class ProductManifest:
    framework: str
    auth_type: str
    routes: list[RouteInfo]         # path, methods, requires_auth, associated_model
    models: dict[str, ModelInfo]    # name → fields with types, required/optional, FK markers
    enum_values: dict[str, list]    # field → valid values
    seeded_users: list[SeededUser]
    register_endpoint: str
    login_endpoint: str

    # NEW: richer model info
    creatable_fields: dict[str, list[str]]   # model → client-writable fields (no FKs, no auto-generated)
    query_params: dict[str, list[str]]       # route → supported query params (from code analysis)
    response_hints: dict[str, str]           # route → "array" | "object" | "wrapped:{key}"
```

### Runtime enrichment (new — probe key routes to confirm & fill gaps)

After static analysis, probe the running app to confirm and enrich:

```python
def enrich_manifest_from_runtime(manifest: ProductManifest, base_url: str) -> ProductManifest:
    """Probe the running app to confirm adapter findings and fill gaps."""
    for route in manifest.routes:
        # Confirm route responds
        r = requests.get(f"{base_url}{route.path}", timeout=5)
        route.confirmed = (r.status_code != 404)

        # Discover response shape
        if r.status_code == 200:
            route.response_shape = _classify_response(r.json())

        # For POST routes: discover required fields from 400 error
        if "POST" in route.methods:
            r = requests.post(f"{base_url}{route.path}", json={}, timeout=5)
            if r.status_code == 400:
                route.required_fields_hint = _parse_required_fields(r.text)

    return manifest
```

This is 1-2 probes per route, ~20 total, <1 second. Not a full discovery protocol —
just confirmation + enrichment of static analysis.

## Phase 3: Tiered Verification

### Tier 0: Structural Check (deterministic, free)

For each claim, check if the manifest has the expected capability:

```python
def tier0_check(claim: SemanticClaim, manifest: ProductManifest) -> Tier0Result:
    # Does the entity's model exist?
    model = find_model(manifest, claim.entity)
    if not model:
        return Tier0Result(status="not_implemented", reason=f"No model matching '{claim.entity}'")

    # Does the expected route exist?
    route = find_route(manifest, claim.entity, claim.action)
    if not route:
        return Tier0Result(status="not_implemented", reason=f"No {claim.action} route for '{claim.entity}'")

    # Does the route have the right auth?
    if "authenticated" in claim.preconditions and not route.requires_auth:
        return Tier0Result(status="warning", reason="Route exists but doesn't require auth")

    return Tier0Result(status="present", route=route, model=model)
```

### Tier 1: Deterministic Probes (no LLM, ~$0)

For claims that pass Tier 0, fire actual HTTP requests using manifest data:

```python
def tier1_probe(claim: SemanticClaim, manifest: ProductManifest,
                base_url: str, session: requests.Session) -> Tier1Result:
    route = tier0.route
    model = tier0.model

    if claim.action == "create":
        body = build_body_from_manifest(model, manifest)  # uses creatable_fields, enum_values
        r = session.post(f"{base_url}{route.path}", json=body)
        if r.status_code in (200, 201):
            return Tier1Result(status="pass", evidence=evidence(r))
        else:
            return Tier1Result(status="probe_failed", evidence=evidence(r),
                             error_detail=r.text[:500])

    if claim.action == "list":
        r = session.get(f"{base_url}{route.path}")
        if r.status_code == 200 and isinstance(r.json(), (list, dict)):
            return Tier1Result(status="pass", evidence=evidence(r))
        ...

    if claim.negative:  # validation claims
        body = build_invalid_body(model, claim)  # missing required fields
        r = session.post(f"{base_url}{route.path}", json=body)
        if r.status_code in (400, 422):
            return Tier1Result(status="pass", evidence=evidence(r))
        ...
```

Key: `build_body_from_manifest` uses REAL field names, types, and enum values
from the manifest. No guessing. If the manifest says the field is `dueDate: DateTime`,
the probe sends `{"dueDate": "2026-01-01T00:00:00Z"}`.

### Tier 2: Agentic Verification (for failures & complexity)

**When to promote to Tier 2:**
- Tier 1 probe got an unexpected response (not clear pass or clear fail)
- Claim requires multi-step reasoning (persistence, isolation, search)
- Claim needs cross-user verification (user isolation, admin access)
- Tier 1 failed but might be a probe construction issue, not a product issue

**No turn limit.** The agent works until it reaches a verdict.
The practical limit is the agent's own judgment — it stops when it has evidence.

**Agent prompt:**

```
You are a QA verification agent. Verify this claim about a running product.

Claim: {claim.description}
Expected behavior: {claim.expected_behavior}
Category: {claim.category}

Product manifest:
{manifest_text}

Base URL: {base_url}

Previous probe attempt (Tier 1 failed):
  Request: {tier1_request}
  Response: {tier1_response}

INSTRUCTIONS:
- Use the manifest to understand the product's actual API surface
- Make HTTP requests to verify the claim
- If something fails, REASON about why and try alternatives
- When you reach a verdict, explain your reasoning
- If this is a FAILURE, diagnose the root cause and suggest a fix

Your output MUST include:
1. verdict: "pass" | "fail" | "not_implemented"
2. evidence: every HTTP request/response you made
3. diagnosis: for failures, what's wrong and why
4. fix_suggestion: for failures, what the developer should change
```

**Agent tools:**

```python
def http_request(method: str, path: str, body: dict = None,
                 headers: dict = None, authenticated: bool = True) -> HttpResponse:
    """Make an HTTP request to the running app.
    If authenticated=True, uses the pre-established session.
    If authenticated=False, uses a clean session (for testing auth rejection)."""

def authenticate_as(email: str, password: str) -> AuthResult:
    """Authenticate as a specific user. Uses the auth flow from the manifest."""

def get_manifest() -> str:
    """Read the product manifest (routes, models, fields, auth)."""
```

**Evidence capture:** The RUNTIME captures every tool call, not the agent.
The agent's output is the verdict + diagnosis. The evidence chain is
captured independently by the tool execution framework. No hallucinated evidence.

### Tier 2 Output for Outer Loop

This is the key differentiator. The agent produces actionable fix guidance:

```python
@dataclass
class ClaimResult:
    claim_id: str
    verdict: str                    # "pass" | "fail" | "not_implemented"
    evidence: list[EvidenceEntry]   # runtime-captured, not agent-authored

    # Tier 2 agent additions (for failed claims):
    diagnosis: str                  # "POST /api/tasks returns 400 because status='todo'
                                    #  is rejected. The route validates against
                                    #  ['TODO','IN_PROGRESS','DONE']. The client sent
                                    #  lowercase."
    fix_suggestion: str             # "Update the route handler to accept case-insensitive
                                    #  status values, or document the valid values in the
                                    #  API response."
    failure_category: str           # "field_validation" | "missing_endpoint" | "auth_error" |
                                    # "server_error" | "wrong_response_shape" | ...
```

The outer loop uses `diagnosis` and `fix_suggestion` to create fix tasks:

```python
def create_fix_task_from_claim_result(result: ClaimResult) -> str:
    return f"""Fix: {result.claim_id} — {result.diagnosis}

Suggested approach: {result.fix_suggestion}

Evidence:
{format_evidence(result.evidence)}
"""
```

## Phase 4: User Journeys

Journeys test multi-step flows. They run as a SINGLE agent session
because steps depend on each other (create returns ID → update uses it).

```python
@dataclass
class SemanticJourney:
    name: str                   # "New User Complete Workflow"
    description: str
    steps: list[JourneyStep]    # ordered, dependent
    critical: bool

@dataclass
class JourneyStep:
    claim_ref: str              # references a SemanticClaim.id
    action_detail: str          # "create a task titled 'My First Task'"
    verify: str | None          # "task appears in list with correct title"
    uses_output_from: str | None  # previous step that produces data
```

The journey agent gets the full journey + manifest + running app.
No turn limit — it executes all steps, adapting as needed.
If a step fails, it records WHERE the flow broke and WHY.

## Fair Comparison

```bash
# Step 1: Compile shared claims
otto certify --compile "task manager with auth and CRUD" -o claims.json

# Step 2: Certify each product
otto certify ./otto-app --claims claims.json --port 4001
otto certify ./barecc-app --claims claims.json --port 4005

# Step 3: Compare
otto compare otto-results.json barecc-results.json
```

### Fairness guarantees

- **Same claims**: compiled once from intent, shared
- **Same standards**: same Tier 0/1/2 promotion logic
- **Same tools**: same agent, same model, same manifest format
- **Same evidence format**: runtime-captured, structured
- **No artificial caps**: both products get whatever verification effort they need

### Addressing non-determinism (from reviews)

For high-stakes comparison:
- Run each product **3 times**
- Report **median score** and **per-claim consistency** (3/3 pass, 2/3 pass, etc.)
- Claims with inconsistent results flagged for manual review

Most claims (Tier 0 + Tier 1) are deterministic. Only Tier 2 claims have variance.
If Tier 1 covers 70%+ of claims, the variance from Tier 2 is bounded.

## State Management

### Problem: claims share a running app and database

### Solution: sequential execution with unique test data

- Claims execute sequentially (not parallel) to avoid DB interference
- Each claim uses unique test data: `f"claim-{claim_id}-{run_id}@test.local"`
- Create claims generate fresh data; they don't depend on seed data for CRUD
- Auth claims use seeded users (they exist in DB)
- Isolation claims create TWO users, each with their own data

### DB state between certifier runs

- The certifier does NOT reset the database between runs
- It uses unique test data that doesn't conflict with previous runs
- Registration handles 409 (user already exists) as a valid response
- List endpoints may return more data than expected (from previous runs) —
  the verifier checks "does my created item appear?" not "is the list exactly N items?"

## Implementation Plan

### New files

```
otto/certifier/
    semantic_compiler.py    # SemanticClaim compilation from intent
    manifest.py             # ProductManifest generation + runtime enrichment
    verifier.py             # Tiered verification engine (Tier 0/1/2)
    probes.py               # Deterministic Tier 1 probes
    agent_verifier.py       # Tier 2 agentic verification
    journey_verifier.py     # Multi-step journey verification
```

### Files to modify

```
    adapter.py              # Add: route-to-model association, creatable field FK filtering
    __init__.py             # Update pipeline: compile → analyze → verify → report
    cli.py                  # Update: --claims flag, --compile flag, otto compare
    pow_report.py           # Update: new evidence format with diagnosis
```

### Files to eventually remove (after v2 is validated)

```
    intent_compiler.py      # Replaced by semantic_compiler.py
    binder.py               # Replaced by tiered verification
    baseline.py self-healing# Remove self-healing, keep AppRunner + HTTP execution utils
```

### Implementation order

1. `SemanticClaim` dataclass + compiler prompt (simplest, independent)
2. `ProductManifest` from adapter + runtime enrichment (builds on existing adapter)
3. Tier 0 structural checks (deterministic, simple)
4. Tier 1 deterministic probes (deterministic, uses manifest)
5. Tier 2 agent verifier (agentic, uses manifest + probe results)
6. Wire into `otto certify` CLI
7. Journey verifier
8. `otto compare` command
9. Test on all 8 stress-test projects
10. Fix tasks integration with outer loop

## Success Criteria

1. Tier 1 probes pass/fail matches manual testing on all 8 projects
2. Tier 2 agent correctly diagnoses failures with actionable fix suggestions
3. Fair comparison: same claims, deterministic Tier 0+1, bounded Tier 2 variance
4. Outer loop: fix tasks from certifier results are actionable by coding agent
5. No self-healing, no route fallback, no body correction in the pipeline
6. Works on Next.js, Express (and ideally Flask/Django with adapter extensions)

## What This Does NOT Handle (explicit scope)

- Browser/visual testing
- Non-HTTP protocols (WebSocket, gRPC, GraphQL)
- Performance/load testing
- Security beyond basic auth
- Mobile responsiveness
- Accessibility
- Complex async workflows

These are explicitly out of scope. Reported as "not_testable" with explanation.

## Cost Estimate

| Component | Cost | Time |
|---|---|---|
| Claim compilation | ~$0.15 | ~30s |
| Manifest (static + runtime) | $0 | ~2s |
| Tier 0 checks (20 claims) | $0 | <1s |
| Tier 1 probes (20 claims) | $0 | ~3s |
| Tier 2 agent (5-10 claims promoted) | ~$0.15-0.50 | ~30-60s |
| Journey verification (3 journeys) | ~$0.10-0.30 | ~20-40s |
| **Total** | **~$0.40-0.95** | **~55-100s** |
| **Comparison (2 products)** | **~$0.80-1.90** | **~2-3 min** |
