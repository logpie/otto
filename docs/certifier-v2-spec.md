# Certifier v2: Semantic Claims Architecture

## Problem

v1 has the LLM compile specific HTTP requests (paths, fields, bodies) from intent alone.
Every product has different conventions → the LLM guesses wrong → we patch endlessly.
The binder corrects guesses → introduces new bugs → whack-a-mole.

## Core Insight

Split WHAT to test from HOW to test:
- **WHAT**: Semantic claims from intent. Product-independent. LLM's job.
- **HOW**: Executable test steps from code analysis. Product-specific. Adapter's job.

## Architecture

```
┌─────────────────────────────────────────────┐
│ 1. COMPILE (shared, cacheable)              │
│    Intent → SemanticClaim[]                 │
│    LLM produces WHAT to test, not HOW       │
│    No paths, no fields, no bodies           │
│    Same output for any product with         │
│    the same intent                          │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│ 2. ANALYZE (per-product, deterministic)     │
│    Code → AdapterOutput                     │
│    Routes, models, fields, enums, auth,     │
│    seeded users. Pure code analysis.         │
│    Already exists (adapter.py)              │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│ 3. BIND (per-product, deterministic)        │
│    SemanticClaim + AdapterOutput →          │
│    BoundTestStep[]                          │
│    GENERATES executable HTTP steps from     │
│    adapter data. No LLM. No guessing.       │
│    Audit trail shows every decision.        │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│ 4. EXECUTE (per-product, deterministic)     │
│    BoundTestStep[] + running app → Results  │
│    Fire HTTP probes, collect evidence.       │
│    No self-healing, no route discovery,     │
│    no runtime corrections.                  │
└─────────────────────────────────────────────┘
```

## Phase 1: Semantic Claims

### What the LLM produces

```python
@dataclass
class SemanticClaim:
    id: str                    # "task-create", "auth-register", "user-isolation"
    description: str           # human-readable: "Users can create tasks"
    category: str              # "crud", "auth", "access-control", "validation", "persistence", "search"
    entity: str                # "task", "recipe", "post", "link" — the domain object
    action: str                # "create", "read", "list", "update", "delete", "search", "filter"
    priority: str              # "critical", "important", "nice"
    hard_fail: bool            # must pass for certification
    preconditions: list[str]   # ["authenticated", "has_created_entity"] — semantic, not HTTP
    expected_behavior: str     # "returns created entity with ID" — semantic, not HTTP
    negative: bool             # True for validation/error claims
```

### What the LLM does NOT produce

- No HTTP methods (POST, GET, PUT, DELETE)
- No paths (/api/tasks, /api/todos)
- No field names (title, dueDate, status)
- No request bodies
- No expected status codes
- No response shapes

### Example compilation

Intent: "task manager with user auth, CRUD tasks with status and due date, user isolation"

```yaml
claims:
  - id: auth-register
    description: "New users can create an account"
    category: auth
    entity: user
    action: create
    priority: critical
    hard_fail: true
    preconditions: []
    expected_behavior: "account created, credentials stored"

  - id: task-create
    description: "Authenticated users can create tasks"
    category: crud
    entity: task
    action: create
    priority: critical
    hard_fail: true
    preconditions: [authenticated]
    expected_behavior: "task created with all specified fields, ID assigned"

  - id: task-list
    description: "Authenticated users can list their tasks"
    category: crud
    entity: task
    action: list
    priority: critical
    hard_fail: true
    preconditions: [authenticated, has_created_entity]
    expected_behavior: "returns array of tasks belonging to current user"

  - id: task-filter-status
    description: "Tasks can be filtered by status"
    category: search
    entity: task
    action: filter
    priority: important
    preconditions: [authenticated, has_created_entity]
    expected_behavior: "returns only tasks matching the status filter"

  - id: task-validation
    description: "Creating a task without required fields is rejected"
    category: validation
    entity: task
    action: create
    priority: important
    negative: true
    preconditions: [authenticated]
    expected_behavior: "returns validation error"

  - id: auth-protected
    description: "Task endpoints reject unauthenticated requests"
    category: access-control
    entity: task
    action: list
    priority: critical
    hard_fail: true
    preconditions: []  # deliberately unauthenticated
    expected_behavior: "returns 401 or 403"

  - id: user-isolation
    description: "Users cannot see other users' tasks"
    category: access-control
    entity: task
    action: read
    priority: critical
    hard_fail: true
    preconditions: [authenticated_as_other_user]
    expected_behavior: "returns 403 or 404 or empty list"
```

### Compiler prompt design

The prompt asks for CLAIMS, not test steps:

```
Given this product intent: {intent}

Generate a list of testable claims about what this product should do.
Each claim describes a FEATURE or BEHAVIOR, not how to test it.

Categories:
- auth: registration, login, invalid login, password security
- crud: create, read, list, update, delete for each entity
- access-control: auth protection, user isolation, admin restrictions
- validation: required fields, invalid input, duplicates
- persistence: data survives across requests
- search: filter, sort, search by keyword

For each claim, specify:
- The domain entity (task, recipe, post, user, etc.)
- The action (create, read, list, update, delete, filter, etc.)
- Whether it's a negative test (testing error handling)
- Preconditions (authenticated, has_created_entity, admin, unauthenticated)
- Expected behavior in plain English

Do NOT include HTTP details (paths, methods, status codes, field names).
```

This prompt is:
- Product-agnostic (works for any domain)
- Convention-agnostic (no assumptions about REST patterns)
- Stable across runs (semantic claims are less variable than HTTP details)

## Phase 2: Adapter (unchanged)

Already exists. Produces `TestConfig` with:
- `routes: list[RouteInfo]` — path, methods, requires_auth, requires_admin
- `models: list[str]` — model names
- `model_fields: dict[str, dict[str, str]]` — field names and types per model
- `creatable_fields: dict[str, list[str]]` — writable fields per model
- `enum_values: dict[str, list[str]]` — valid enum values
- `seeded_users: list[SeededUser]` — test credentials
- `auth_type: str` — nextauth, jwt, session
- `register_endpoint: str`
- `resource_models: list[str]`

## Phase 3: Bind (new — replaces both old binder and compiler test_steps)

### Core logic: semantic claim → executable steps

The binder is a **deterministic function** (no LLM) that maps semantic claims to HTTP requests using adapter data.

```python
def bind_claim(claim: SemanticClaim, adapter: TestConfig, profile: ProductProfile) -> BoundClaim:
```

### Binding rules by category

**auth claims:**
- `action=create` (register): POST to `adapter.register_endpoint` with model User's creatable fields
- `action=login`: NextAuth CSRF flow or POST to login endpoint with seeded user credentials
- `action=login` + `negative=True`: Same flow but with wrong password

**crud claims:**
- Find the model matching `claim.entity` in `adapter.models`
- Find the route matching the entity name in `adapter.routes`
- `action=create`: POST to route, body = all creatable fields with sensible defaults from types
- `action=read`: GET to route/{id} (ID from a prior create step)
- `action=list`: GET to route
- `action=update`: PUT to route/{id} with partial body
- `action=delete`: DELETE to route/{id}

**access-control claims:**
- `preconditions=[]` (unauthenticated): GET the entity route without auth → expect 401/403
- `preconditions=[authenticated_as_other_user]`: Auth as user B, try to access user A's entity → expect 403/404

**validation claims:**
- `negative=True`: Send request with missing required fields → expect 400/422

**persistence claims:**
- Create entity, then GET and verify it's returned

**search/filter claims:**
- `action=filter`: GET route with query param matching a model field (e.g., `?status=TODO`)
- `action=search`: GET route with `?q=keyword`
- `action=sort`: GET route with `?sort=fieldName`

### How the binder finds the right route

```python
def _find_entity_route(entity: str, adapter: TestConfig) -> RouteInfo | None:
    """Find the API route for a domain entity."""
    entity_lower = entity.lower()
    for route in adapter.routes:
        # Match by entity name in path: /api/tasks, /api/recipes, /api/posts
        path_parts = route.path.lower().strip("/").split("/")
        if entity_lower in path_parts or f"{entity_lower}s" in path_parts:
            return route
    return None
```

### How the binder generates request bodies

```python
def _build_create_body(entity: str, adapter: TestConfig) -> dict:
    """Build a request body from the model's creatable fields."""
    fields = adapter.creatable_fields.get(entity, {})
    model_fields = adapter.model_fields.get(entity, {})
    body = {}
    for field_name in fields:
        if field_name.endswith("Id") and field_name != "id":
            continue  # skip foreign keys
        field_type = model_fields.get(field_name, "String")
        body[field_name] = _default_for_type(field_name, field_type, adapter)
    return body
```

No guessing. Every field name comes from the Prisma schema. Every enum value comes from the adapter's discovered enums.

### Binding audit trail

Each BoundClaim records:
- Which route was matched and why
- Which fields were included and their sources
- Which auth mechanism was chosen
- Any claims that couldn't be bound (no matching route → `not_testable`)

## Phase 4: Execute (simplified)

With correct bindings, execution is trivial:
1. Pre-authenticate once (seeded user)
2. For each bound claim:
   - If structural (no route found): record as not_implemented
   - If negative test: use fresh session (no auth)
   - If auth test: use appropriate auth flow
   - Otherwise: fire HTTP request with bound route/body, check response
3. Collect evidence (request, response, timestamp)

No self-healing. No route fallback. No body correction. If the binding is correct, the request should work. If it fails, that's a real product issue.

## Journeys (Tier 2)

Semantic claims handle individual features. Journeys handle multi-step flows.

### Journey generation

The LLM generates journeys using the SAME semantic vocabulary:

```yaml
journeys:
  - name: "New User First Task"
    steps:
      - claim: auth-register      # references a semantic claim
      - claim: auth-login
      - claim: task-create
      - claim: task-list
        verify: "created task appears in list"
      - claim: task-update
        field: status
        value: "done"             # semantic — binder maps to actual enum
      - claim: task-list
        verify: "task shows updated status"
```

Each journey step references a semantic claim. The binder resolves each step's HTTP details from adapter data. The journey compiler doesn't need to know paths or fields.

### Journey binding

```python
def bind_journey(journey: SemanticJourney, claims: list[BoundClaim], adapter: TestConfig) -> BoundJourney:
    """Bind journey steps by looking up their corresponding bound claims."""
    steps = []
    for step in journey.steps:
        bound_claim = find_claim(claims, step.claim_id)
        bound_step = bound_claim.steps[0]  # reuse the claim's bound step
        steps.append(bound_step)
    return BoundJourney(steps=steps)
```

## Fair Comparison

```bash
# Step 1: Compile shared claims (product-independent)
otto certify --compile-only "task manager with auth and CRUD" --output /tmp/claims.json

# Step 2: Certify each product (claims shared, binding per-product)
otto certify ./otto-app --claims /tmp/claims.json --port 4001
otto certify ./barecc-app --claims /tmp/claims.json --port 4005
```

Both products tested against SAME semantic claims. Each product's bindings use ITS OWN routes and fields. Comparison is at the claim level: "did the product implement this feature?"

## What changes from v1

| Component | v1 | v2 |
|---|---|---|
| Compiler output | HTTP test steps (paths, fields, bodies) | Semantic claims (entity, action, behavior) |
| Binder | Corrects LLM guesses | Generates steps from adapter data |
| Self-healing | 4 types of runtime correction | None needed |
| Schema hint | Text summary fed to LLM | Not needed — binder reads adapter directly |
| Comparison fairness | Requires --matrix flag | Built-in (claims are product-independent) |
| Brittleness | High (guesses + corrections) | Low (code analysis + deterministic generation) |

## What stays the same

- Adapter (code analysis) — unchanged
- Classifier (framework detection) — unchanged
- AppRunner (start/stop apps) — unchanged
- PoW report (evidence format) — unchanged
- CLI structure — unchanged

## Implementation plan

1. New `SemanticClaim` dataclass + new compiler prompt (semantic only)
2. New `bind_claim()` that generates HTTP steps from adapter data
3. Update executor to use bound steps (already mostly done from v1 binder work)
4. New journey format that references semantic claims
5. Update CLI: `--claims` flag replaces `--matrix`
6. Remove: old compiler test_steps, old binder corrections, self-healing
7. Test on all 8 projects

## Risks

1. **Adapter coverage**: If the adapter can't discover a route or model, the binder can't generate steps. Mitigation: report these as "not_testable" — honest about limitations.
2. **Non-CRUD patterns**: WebSocket, file upload, streaming — the binder only handles REST CRUD. Mitigation: mark as "not_testable", extend binder later.
3. **Complex business logic**: "checkout creates an order and sends email" — can't be tested from adapter data alone. Mitigation: LLM can add domain-specific verifications in journey steps.
4. **Adapter inaccuracy**: If the adapter misreads the code, bindings will be wrong. Mitigation: adapter is deterministic and debuggable, unlike LLM guesses.
