# Certifier v2: Agentic Verification

## Why v1 Failed

v1 tried to pre-plan all HTTP requests from intent alone, then correct them at runtime.
This is fundamentally brittle because:
- The LLM guesses paths, fields, bodies without seeing the API
- Corrections introduce new bugs (foreign keys in bodies, auth leaking, negative tests fixed)
- Self-healing hides real failures
- Every product has different conventions → endless patching

The root cause: **the certifier tries to predict what it could observe.**

## Design Principle

**Don't predict. Interact.**

A QA engineer doesn't write all tests before seeing the product. They interact with it,
observe responses, reason about what they see, and adapt. The certifier should do the same.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ 1. COMPILE (LLM, one-shot, shared across products)         │
│    Intent → SemanticClaim[]                                 │
│    "Users can create tasks"                                 │
│    Product-independent. No HTTP details.                    │
│    Cached by intent hash.                                   │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│ 2. ANALYZE (deterministic, per-product)                     │
│    Code → ProductManifest                                   │
│    Routes, models, fields, enums, auth, seeded users.       │
│    Adapter already does this. Add manifest formatting.       │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│ 3. VERIFY (agentic, per-claim, per-product)                 │
│    Agent(claim + manifest + running app) → ClaimResult      │
│    Agent reasons, probes, adapts, collects evidence.        │
│    Budget-capped: max N tool calls per claim.               │
│    Structured output: pass/fail + evidence chain.           │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│ 4. REPORT (deterministic)                                   │
│    ClaimResult[] → proof-of-work.md + .json                 │
│    Every claim backed by request/response/timestamp.        │
└─────────────────────────────────────────────────────────────┘
```

## Phase 1: Semantic Claims

Same as v2 spec. The LLM produces WHAT to test, not HOW.

```python
@dataclass
class SemanticClaim:
    id: str                     # "task-create"
    description: str            # "Authenticated users can create tasks"
    category: str               # "crud" | "auth" | "access-control" | "validation" | "persistence" | "search"
    entity: str                 # "task" — the domain object
    action: str                 # "create" | "read" | "list" | "update" | "delete" | "search" | "filter"
    priority: str               # "critical" | "important" | "nice"
    hard_fail: bool
    preconditions: list[str]    # ["authenticated"] — semantic
    negative: bool              # True for validation/error claims
    expected_behavior: str      # "returns created entity with ID"
```

### Compiler prompt

```
Given this product intent: {intent}

List every testable feature claim. Each claim is something a QA engineer
would verify. Include:
- CRUD operations for every entity mentioned
- Authentication (register, login, invalid login, password security)
- Access control (auth required, user isolation, admin restrictions)
- Validation (required fields, invalid input, duplicates)
- Persistence (data survives across requests)
- Search/filter/sort if mentioned

For each claim, specify the domain entity, the action, preconditions
(authenticated, admin, unauthenticated), whether it's a negative test,
and the expected behavior in plain English.

Do NOT include HTTP paths, field names, request bodies, or status codes.
Those are implementation details the verifier will discover.
```

### Fairness

Claims are compiled from intent ONLY. Same intent → same claims → fair comparison.
Cached by intent hash. Shareable via `--claims` flag.

## Phase 2: Product Manifest

The adapter produces a structured manifest of what the product actually offers.
This is the existing `TestConfig` formatted for the verification agent.

```python
@dataclass
class ProductManifest:
    framework: str              # "nextjs", "express", "flask", etc.
    auth_type: str              # "nextauth", "jwt", "session", "none"
    routes: list[RouteInfo]     # path, methods, requires_auth, requires_admin
    models: list[ModelInfo]     # name, fields with types
    enum_values: dict           # field → valid values
    seeded_users: list          # email, password, role
    register_endpoint: str
    login_endpoint: str
```

Formatted as text for the agent:

```
Product Manifest:
  Auth: NextAuth with credentials provider
  Register: POST /api/auth/register
  Login: POST /api/auth/callback/credentials (CSRF flow)
  Seeded users: alice@example.com / password123 (user)

  Routes:
    GET  /api/tasks       [auth required] → list
    POST /api/tasks       [auth required] → create
    GET  /api/tasks/:id   [auth required] → read
    PUT  /api/tasks/:id   [auth required] → update
    DELETE /api/tasks/:id [auth required] → delete

  Models:
    Task: title(String), description(String?), status(String: TODO|IN_PROGRESS|DONE), dueDate(DateTime?), userId(String, FK)
    User: id, email(String), name(String), password(String, hashed)
```

### What's new

The manifest is mostly existing adapter data. New additions:
- Route-to-model association (which model does /api/tasks operate on)
- Response shape hints (if discoverable from code: wraps in {data:[]}, returns bare array, etc.)
- Explicit FK marking (userId is server-set, not client-provided)

## Phase 3: Agentic Verification

This is the core innovation. For each claim, a verification agent interacts
with the running app to determine if the claim is satisfied.

### Agent design

```python
async def verify_claim(
    claim: SemanticClaim,
    manifest: ProductManifest,
    base_url: str,
    session_factory: Callable,    # creates fresh or authenticated sessions
) -> ClaimResult:
```

The agent receives:
- The semantic claim (what to verify)
- The product manifest (what the product offers)
- Access to the running app (via HTTP tools)
- A budget (max tool calls)

The agent returns:
- pass/fail verdict
- Evidence chain (every HTTP request/response with timestamp)
- Reasoning (why it passed or failed)

### Agent prompt

```
You are a QA verification agent. Your job is to verify ONE claim about a product.

Claim: {claim.description}
Category: {claim.category}
Entity: {claim.entity}
Action: {claim.action}
Preconditions: {claim.preconditions}
Negative test: {claim.negative}
Expected behavior: {claim.expected_behavior}

Product manifest:
{manifest_text}

Base URL: {base_url}

INSTRUCTIONS:
1. Use the manifest to identify the relevant route, model, and fields.
2. If the claim requires authentication, authenticate using the seeded credentials
   and the auth flow described in the manifest.
3. Make HTTP requests to verify the claim. Observe responses carefully.
4. If a request fails unexpectedly, REASON about why:
   - Wrong path? Check the manifest for alternatives.
   - Wrong fields? Read the error message for hints.
   - Auth issue? Try the auth flow described in the manifest.
   - Server error? That's a real product bug — record it.
5. For negative tests (validation, auth rejection): verify that the product
   correctly rejects invalid input.
6. Record your verdict with evidence.

You have a budget of {budget} tool calls. Use them wisely.

Output your result as JSON:
{
  "verdict": "pass" | "fail" | "not_implemented" | "blocked",
  "evidence": [
    {
      "step": "description of what you did",
      "request": {"method": "POST", "url": "...", "body": {...}},
      "response": {"status": 201, "body": {...}},
      "timestamp": "..."
    }
  ],
  "reasoning": "why you reached this verdict"
}
```

### Agent tools

The agent has exactly 3 tools:

```python
def http_request(method: str, url: str, body: dict | None = None,
                 headers: dict | None = None) -> dict:
    """Make an HTTP request. Returns {status, headers, body}."""

def authenticate(strategy: str, credentials: dict) -> dict:
    """Authenticate using the specified strategy.
    strategy: "nextauth" | "credentials_post" | "bearer"
    Returns {success: bool, session_info: dict}."""

def fresh_session() -> None:
    """Start a new unauthenticated session (clear cookies)."""
```

That's it. No file reading, no code exploration, no bash. The agent can only
interact with the running app via HTTP.

### Budget and fairness

Each claim gets the same budget: **max 10 tool calls**.

This ensures:
- Equal effort per claim per product
- Agent can't spend 50 calls on one product and 3 on another
- Enough for: auth (2-3 calls) + setup (1-2 calls) + test (2-3 calls) + verify (1-2 calls)
- Prevents runaway costs

If the agent can't verify a claim in 10 calls, the claim is `blocked`
with evidence of what was tried.

### Why this is different from product_qa.py

| | product_qa.py | Agentic certifier v2 |
|---|---|---|
| Scope | Entire product | One claim at a time |
| Guidance | "test user journeys from spec" | "verify THIS claim using THIS manifest" |
| Evidence | Prose verdict | Structured request/response chain |
| Budget | Unlimited | 10 tool calls per claim |
| Knowledge | Explores codebase from scratch | Starts with product manifest |
| Determinism | Different every run | Same claims, same budget, same tools |
| Cost | $0.50+ per product | ~$0.02-0.05 per claim |

### Parallelization

Claims are independent. Verify them in parallel:
- 20 claims × 10 tool calls = 200 total calls
- At 5 concurrent agents = ~40 sequential calls
- At ~1 second per call = ~40 seconds total
- Plus LLM thinking time: ~60-90 seconds total
- Cost: 20 × $0.03 = ~$0.60

### Per-claim agent model selection

Not every claim needs a full agent:
- Simple claims (auth-register, task-not-found) → lightweight model or deterministic probe
- Complex claims (user-isolation, persistence) → full agent
- Negative claims (validation, error handling) → deterministic probe with 2-3 requests

```python
def select_verifier(claim: SemanticClaim) -> Verifier:
    if claim.category == "validation" and claim.negative:
        return DeterministicProbe(claim)      # no LLM needed
    if claim.category == "auth" and claim.action in ("register", "login"):
        return AuthProbe(claim)               # fixed protocol
    if claim.category == "access-control":
        return AccessControlProbe(claim)      # fixed protocol
    return AgentVerifier(claim, budget=10)     # full agent for complex claims
```

This hybrid approach uses agents where they add value (complex reasoning)
and deterministic probes where they don't (simple status code checks).

## Phase 4: User Journeys

Individual claims test features in isolation. Journeys test multi-step flows.

### Journey compilation

The LLM produces journeys from intent + claim list:

```python
@dataclass
class SemanticJourney:
    name: str                   # "New User First Task"
    description: str            # "Register, create a task, verify it appears in list"
    persona: str                # "new_user" | "returning_user" | "admin"
    steps: list[JourneyStep]
    critical: bool

@dataclass
class JourneyStep:
    claim_ref: str              # references a SemanticClaim.id
    action_detail: str          # "create a task with title 'My First Task'"
    verify: str | None          # "task appears in list" — what to check after
    uses_output_from: str | None  # step that produces data this step needs
```

### Journey verification

A journey agent gets the full journey + manifest + running app.
It executes steps in sequence, carrying state between steps.

The journey agent is a single agent session (not per-claim) because
steps depend on each other (create returns ID → update uses ID).

Budget: max 5 tool calls per step × number of steps.

```
Journey: "New User First Task"
  1. [auth-register] Register a new account → save user session
  2. [task-create] Create a task titled "My First Task" → save task ID
  3. [task-list] List tasks → verify "My First Task" appears
  4. [task-update] Update task status to DONE → verify response
  5. [task-list] List tasks again → verify status changed
```

The agent reasons through the journey, adapting to each response.
If step 2 fails, it stops and reports which step broke the flow.

## Fair Comparison

```bash
# Step 1: Compile shared claims (product-independent)
otto certify --compile "task manager with auth and CRUD" -o /tmp/claims.json

# Step 2: Verify each product (same claims, same budget, same agent)
otto certify ./otto-app --claims /tmp/claims.json --port 4001
otto certify ./barecc-app --claims /tmp/claims.json --port 4005

# Step 3: Compare at claim level
otto compare /tmp/otto-results.json /tmp/barecc-results.json
```

Fairness guarantees:
- Same claims (compiled once from intent)
- Same verification agent (same model, same prompt, same tools)
- Same budget per claim (10 tool calls)
- Same evidence format (structured request/response chains)
- Comparison at semantic level ("did task-create pass?"), not HTTP level

## Implementation Plan

### What to keep from v1
- `adapter.py` — route/model/field/auth discovery (add manifest formatting)
- `classifier.py` — framework detection
- `AppRunner` — start/stop apps
- `pow_report.py` — evidence reporting (update for new evidence format)
- CLI structure — `otto certify` command

### What to build new
- `semantic_compiler.py` — new compiler prompt, SemanticClaim dataclass
- `manifest.py` — ProductManifest formatting from TestConfig
- `verifier.py` — per-claim agent verification (the core new component)
- `journey_verifier.py` — multi-step journey verification
- `probes.py` — deterministic verification probes for simple claims

### What to remove
- `binder.py` — no longer needed (agent handles binding implicitly)
- `intent_compiler.py` test_steps generation — replaced by semantic claims
- Self-healing code in `baseline.py` — agent adapts instead of self-heals
- Route fallback logic — agent discovers routes from manifest
- Body correction logic — agent reads error messages and adapts

### Implementation order

1. `SemanticClaim` dataclass + new compiler prompt
2. `ProductManifest` formatting from existing adapter
3. `DeterministicProbe` for simple claims (auth, validation, access-control)
4. `AgentVerifier` for complex claims (CRUD, persistence, search)
5. Wire into `otto certify` CLI
6. `JourneyVerifier` for multi-step flows
7. `otto compare` command
8. Test on all 8 stress-test projects
9. Remove v1 code (binder, self-healing, route fallback)

## Cost Analysis

| Component | Cost | Time |
|---|---|---|
| Claim compilation | $0.15 | ~30s |
| Adapter analysis | $0 | <1s |
| Simple claim probes (10 claims) | $0 | ~2s |
| Agent verification (10 claims) | $0.30 | ~30s |
| Journey verification (3 journeys) | $0.15 | ~20s |
| **Total** | **~$0.60** | **~80s** |

For comparison mode (2 products): $0.15 (shared claims) + 2 × $0.45 (per product) = **$1.05**

## What This Does NOT Handle (explicit scope)

- Browser/visual testing — future Tier 3 with browser agent
- Non-HTTP protocols — WebSocket, gRPC, GraphQL mutations
- Performance/load testing
- Security beyond basic auth checks
- Mobile responsiveness
- Accessibility

These are explicitly out of scope. The certifier reports on what it CAN test
and abstains on what it can't. Honest about its limitations.

## Success Criteria

The certifier v2 is done when:
1. All 8 stress-test projects produce stable, sensible scores
2. Old path (v1) and new path (v2) produce comparable results on the same product
3. Fair comparison (shared claims) produces identical claim lists for both products
4. Agent verification matches or exceeds deterministic probe accuracy
5. No self-healing, no route fallback, no body correction in the pipeline
6. Proof-of-work reports show complete evidence chains
