# Certifier v2: Product-Level QA

## What the certifier IS

The certifier is the **outer loop's QA agent**. It represents a USER.
It answers: "can a real user accomplish what this product promises?"

## What the certifier is NOT

- NOT per-endpoint verification (inner loop does that)
- NOT unit test runner (inner loop does that)
- NOT code review (inner loop does that)
- NOT adversarial testing (inner loop BREAK phase does that)

## Dedup with Inner Loop

```
INNER LOOP (per-task, already exists):
  ✓ Does POST /api/tasks return 201?
  ✓ Does the test suite pass?
  ✓ Does the code handle edge cases?
  ✓ Is the implementation sound?
  → Verifies EACH TASK's code works in isolation

OUTER LOOP / CERTIFIER (product-level, this spec):
  ✓ Can a new user register, create a task, and see it in their list?
  ✓ Does the task persist after logout and login?
  ✓ Can user A NOT see user B's tasks in practice?
  ✓ Does filtering actually show the right subset?
  ✓ Does the admin panel show all users' data?
  → Verifies THE PRODUCT works as an integrated whole
```

The certifier tests what individual task QA CANNOT:
- **Cross-task integration**: auth + tasks + filtering working together
- **Data flow**: created entity appears in lists, updates reflect everywhere
- **State persistence**: data survives across sessions
- **User isolation**: multi-user scenarios
- **Product coherence**: the whole experience makes sense

## Architecture

```
┌───────────────────────────────────────────────────────┐
│ 1. COMPILE: Intent → UserStories                      │
│    "As a new user, I register, create my first task,  │
│     see it in my list, mark it done, filter by done"  │
│    Product-independent. Shared for comparison.        │
└─────────────────────┬─────────────────────────────────┘
                      │
┌─────────────────────▼─────────────────────────────────┐
│ 2. ANALYZE: Code + App → ProductManifest              │
│    Routes, models, fields, auth, seeds.               │
│    Static analysis + runtime confirmation probes.     │
└─────────────────────┬─────────────────────────────────┘
                      │
┌─────────────────────▼─────────────────────────────────┐
│ 3. PRE-FLIGHT: Quick structural check (~3 seconds)    │
│    Are the basic endpoints alive? Is auth working?    │
│    If product is fundamentally broken, skip journeys  │
│    and report "product not ready" with specifics.     │
└─────────────────────┬─────────────────────────────────┘
                      │
┌─────────────────────▼─────────────────────────────────┐
│ 4. VERIFY: Journey agents simulate real users         │
│    THE MAIN EVENT                                     │
│    Each journey = one agent session                   │
│    Agent acts as a user, interacts with the app,      │
│    verifies each step, reports where flows break.     │
│    No turn limit.                                     │
│    Produces actionable fix tasks for outer loop.      │
└─────────────────────┬─────────────────────────────────┘
                      │
┌─────────────────────▼─────────────────────────────────┐
│ 5. REPORT: Proof-of-work + fix tasks                  │
│    Per-journey: narrative + evidence + diagnosis       │
│    For outer loop: fix tasks with root cause           │
└───────────────────────────────────────────────────────┘
```

## Phase 1: User Stories (not claims)

The LLM produces user stories, not endpoint claims. Each story is a
realistic scenario a user would go through.

```python
@dataclass
class UserStory:
    id: str                     # "new-user-first-task"
    persona: str                # "new_user" | "returning_user" | "admin" | "visitor"
    title: str                  # "New User Creates Their First Task"
    narrative: str              # "A new user registers, creates a task, verifies it
                                #  appears in their list, marks it done, and sees the
                                #  updated status."
    steps: list[StoryStep]      # ordered, each with what to do and what to verify
    critical: bool              # must pass for certification
    tests_integration: list[str]  # which features this journey integrates
                                  # ["auth", "task-crud", "task-status", "task-list"]
    break_strategies: list[str]  # BREAK patterns to try after happy path
                                 # ["double_submit", "direct_url_access", "long_input",
                                 #  "missing_fields_from_ui", "id_guessing"]

@dataclass
class StoryStep:
    action: str                 # "register a new account with email and password"
    verify: str                 # "account created, can proceed to use the app"
    verify_in_browser: str | None  # "registration form submits, redirected to dashboard"
    uses_output_from: int | None  # step index that produces data this step needs
    entity: str                 # "user" | "task" — for manifest matching
    operation: str              # "create" | "read" | "list" | "update" | "delete" | "auth"
    mode: str                   # "api" | "browser" | "both"
                                # "api": verify via HTTP request
                                # "browser": verify via UI interaction
                                # "both": verify backend via API AND frontend via browser
```

### Example stories for "task manager with auth, CRUD, status, user isolation"

```yaml
stories:
  - id: new-user-first-task
    persona: new_user
    title: "New User Creates Their First Task"
    critical: true
    tests_integration: [auth, task-crud, task-list]
    narrative: >
      A brand new user registers an account, creates their first task
      with a title and description, then verifies it appears in their
      task list with the correct details.
    steps:
      - action: "register a new account"
        verify: "registration succeeds, user can proceed"
        entity: user
        operation: auth
      - action: "log in with the new credentials"
        verify: "authenticated session established"
        entity: user
        operation: auth
      - action: "create a task with title 'Buy groceries' and description 'Milk, eggs, bread'"
        verify: "task created with an ID, title matches"
        entity: task
        operation: create
        uses_output_from: null
      - action: "list all tasks"
        verify: "the created task appears in the list with correct title"
        entity: task
        operation: list
        uses_output_from: 2  # needs the task we created

  - id: task-lifecycle
    persona: returning_user
    title: "Task Status Lifecycle"
    critical: true
    tests_integration: [auth, task-crud, task-status, task-filter]
    narrative: >
      A user creates a task, updates its status from TODO to IN_PROGRESS
      to DONE, and verifies the status changes are reflected. Then filters
      by status to see only DONE tasks.
    steps:
      - action: "log in with seeded user credentials"
        verify: "authenticated"
        entity: user
        operation: auth
      - action: "create a task with status TODO"
        verify: "task created with status TODO"
        entity: task
        operation: create
      - action: "update the task status to IN_PROGRESS"
        verify: "task status is now IN_PROGRESS"
        entity: task
        operation: update
        uses_output_from: 1
      - action: "update the task status to DONE"
        verify: "task status is now DONE"
        entity: task
        operation: update
        uses_output_from: 1
      - action: "filter tasks by status DONE"
        verify: "the completed task appears in the filtered list"
        entity: task
        operation: list

  - id: user-isolation
    persona: returning_user
    title: "User Data Isolation"
    critical: true
    tests_integration: [auth, task-crud, user-isolation]
    narrative: >
      Two different users each create tasks. Each user should only see
      their own tasks, not the other user's.
    steps:
      - action: "log in as user A (seeded user)"
        verify: "authenticated as user A"
        entity: user
        operation: auth
      - action: "create a task titled 'User A task'"
        verify: "task created"
        entity: task
        operation: create
      - action: "log in as user B (register new or use second seeded user)"
        verify: "authenticated as user B"
        entity: user
        operation: auth
      - action: "create a task titled 'User B task'"
        verify: "task created"
        entity: task
        operation: create
      - action: "list user B's tasks"
        verify: "'User B task' is present, 'User A task' is NOT present"
        entity: task
        operation: list

  - id: persistence-across-sessions
    persona: returning_user
    title: "Data Persists Across Sessions"
    critical: true
    tests_integration: [auth, task-crud, persistence]
    narrative: >
      A user creates a task, logs out, logs back in, and verifies
      the task is still there.
    steps:
      - action: "log in"
        verify: "authenticated"
        entity: user
        operation: auth
      - action: "create a task titled 'Persistent task'"
        verify: "task created"
        entity: task
        operation: create
      - action: "start a fresh session (log out)"
        verify: "session cleared"
        entity: user
        operation: auth
      - action: "log in again with same credentials"
        verify: "authenticated"
        entity: user
        operation: auth
      - action: "list tasks"
        verify: "'Persistent task' still appears"
        entity: task
        operation: list

  - id: visitor-browsing
    persona: visitor
    title: "Unauthenticated Access Blocked"
    critical: false
    tests_integration: [auth, access-control]
    narrative: >
      An unauthenticated visitor tries to access task endpoints
      and is rejected.
    steps:
      - action: "without logging in, try to list tasks"
        verify: "request rejected with 401 or 403"
        entity: task
        operation: list
      - action: "without logging in, try to create a task"
        verify: "request rejected with 401 or 403"
        entity: task
        operation: create
```

### Compiler prompt

```
Given this product intent: {intent}

Design user stories that test the product END-TO-END as real users would use it.

Each story is a realistic scenario from a specific persona's perspective:
- new_user: someone using the product for the first time (register → first action)
- returning_user: someone who already has an account (login → use features)
- admin: an administrator managing the product
- visitor: an unauthenticated person (testing access control)

Requirements:
1. EVERY feature in the intent must be covered by at least one story
2. Stories test INTEGRATION — features working together, not in isolation
3. Each step says WHAT to do and WHAT to verify (in plain English)
4. Steps have data dependencies (step 3 uses the task created in step 2)
5. Include at minimum:
   - A "first experience" story (register → use core feature → verify)
   - A "feature lifecycle" story (create → update → verify changes)
   - A "data isolation" story (multi-user, each sees only their own data)
   - A "persistence" story (data survives across sessions)
   - A "visitor access" story (unauthenticated access is blocked)

Do NOT include HTTP paths, field names, or status codes.
The verification agent will figure out HOW from the product manifest.
```

## Phase 2: Product Manifest (same as previous spec)

Static analysis + runtime probes. Provides the agent with:
- Routes, methods, auth requirements
- Models, fields, types, enums
- Seeded users with credentials
- Auth mechanism details

## Phase 3: Pre-flight Check

Quick structural verification before expensive journey testing.
~3 seconds, no LLM, deterministic.

```python
def preflight(manifest: ProductManifest, base_url: str) -> PreflightResult:
    """Quick check: is the product ready for journey testing?"""
    checks = []

    # Can the app respond at all?
    r = requests.get(base_url, timeout=5)
    checks.append(("app_alive", r.status_code < 500))

    # Can we authenticate?
    if manifest.seeded_users:
        session = authenticate(manifest, base_url)
        checks.append(("auth_works", session is not None))

    # Do the main entity routes respond?
    for route in manifest.routes:
        r = requests.options(f"{base_url}{route.path}", timeout=3)
        checks.append((f"route_{route.path}", r.status_code != 404))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)

    if passed / total < 0.5:
        return PreflightResult(
            ready=False,
            reason=f"Only {passed}/{total} structural checks pass. Product not ready.",
            checks=checks,
        )
    return PreflightResult(ready=True, checks=checks)
```

If pre-flight fails badly (< 50% routes alive), skip journeys and report:
"Product is not structurally ready. {N} of {M} routes return 404."
This becomes a fix task for the outer loop.

## Phase 4: Journey Verification (THE MAIN EVENT)

Each user story is verified by a journey agent — a Claude session with
HTTP tools that SIMULATES A REAL USER.

### Agent prompt

```
You are a QA tester simulating a real user of this product.
You will execute a user story step by step, interacting with the running
application via HTTP requests.

USER STORY:
  Title: {story.title}
  Persona: {story.persona}
  Narrative: {story.narrative}

PRODUCT MANIFEST:
{manifest_text}

Base URL: {base_url}

STEPS TO EXECUTE:
{formatted_steps}

INSTRUCTIONS:
1. Execute each step in order using the manifest to find the right routes and fields.
2. After each step, VERIFY the expected outcome — don't just check status codes,
   check that the data makes sense (correct title, correct status, correct user).
3. Carry state between steps — if step 2 creates a task, use that task's ID in step 4.
4. If a step fails, DIAGNOSE why:
   - Is the endpoint missing? (structural issue)
   - Is the response wrong? (logic bug)
   - Is auth not working? (auth issue)
   - Is the data inconsistent? (integration bug)
5. Continue to the next step even if one fails — test as much as possible.
6. For each step, record EXACTLY what you sent and received.

OUTPUT FORMAT:
For each step:
  - action_taken: what HTTP request(s) you made
  - outcome: "pass" | "fail" | "blocked"
  - evidence: request/response details
  - verification: what you checked and whether it matched
  - diagnosis: (if failed) root cause analysis
  - fix_suggestion: (if failed) what the developer should change

At the end:
  - journey_passed: true if all critical steps passed
  - summary: one paragraph describing the experience
  - blocked_at: which step broke the flow (if applicable)
```

### Agent tools: Two modes

The journey agent has BOTH API tools and browser tools. It chooses
the right tool for each step — API for backend verification, browser
for UI/UX verification.

**API tools (backend verification):**

```python
def http(method: str, path: str, body: dict = None,
         authenticated: bool = True) -> Response:
    """Make an HTTP request. Path is relative to base_url.
    If authenticated=True, uses the current session.
    Returns full response with status, headers, body."""

def login(email: str, password: str) -> AuthResult:
    """Authenticate. Uses the auth mechanism from the manifest."""

def register(email: str, password: str, name: str = "") -> AuthResult:
    """Register a new user."""

def logout() -> None:
    """Clear the current session."""

def login_as_new_user() -> AuthResult:
    """Register a fresh user and log in. Generates unique credentials."""
```

**Browser tools (UI/UX verification, via chrome-devtools MCP):**

```python
def navigate(url: str) -> PageInfo:
    """Navigate to a URL in the browser."""

def click(selector: str) -> None:
    """Click a button, link, or element."""

def fill(selector: str, value: str) -> None:
    """Fill a form field."""

def fill_form(fields: dict) -> None:
    """Fill multiple form fields at once."""

def screenshot() -> Image:
    """Take a screenshot of the current page."""

def get_text(selector: str) -> str:
    """Get text content of an element."""

def wait_for(selector: str, timeout: int = 5000) -> bool:
    """Wait for an element to appear."""
```

**When to use which:**
- API: backend logic (data flow, persistence, isolation, auth)
- Browser: user experience (can I find the button? does the form work?
  does the page show the right data? is the error message helpful?)
- Both: complete verification (create via API → verify appears in browser,
  or submit form in browser → verify via API that data was saved)

### No turn limit

The agent works until it completes all steps or determines a step
is unrecoverable. There's no artificial budget.

In practice, a 5-step journey takes ~10-20 HTTP requests, ~5-10
browser interactions, and ~30-50 LLM turns. Cost: ~$0.05-0.20 per journey.

### Two-phase verification: HAPPY PATH then BREAK

Each journey has two phases:

**Phase A: Happy Path (required, affects certification score)**

Execute the user story step by step. Verify each step succeeds.
This is the primary certification signal.

**Phase B: BREAK (optional, quality signal only)**

After the happy path, try to break what you just verified:

```
BREAK instructions:
After completing the happy path, spend a few additional turns trying
to break the product. Think like a creative user who doesn't read docs:

- Submit the same form twice rapidly — duplicates?
- Use very long strings (1000+ chars) — crash or graceful truncation?
- Navigate directly to a protected URL without auth — proper rejection?
- Delete something then try to use it — graceful error?
- Submit with missing/empty required fields from the UI — helpful error message?
- Go back in browser after submitting — double submit?
- Try to access another user's data by guessing IDs — blocked?

Report what you find as quality observations. These do NOT affect
the pass/fail certification — they are improvement signals.
```

**BREAK findings format:**

```python
@dataclass
class BreakFinding:
    technique: str          # "double submit", "long string", "direct URL access"
    description: str        # what you tried
    result: str             # what happened
    severity: str           # "critical" | "moderate" | "minor" | "cosmetic"
    fix_suggestion: str     # what the developer should change
```

### Scoring with BREAK

```
Certification: based on happy path only
  Critical journeys passed: 3/4 (75%)
  All journeys passed: 4/5 (80%)

Quality signals (from BREAK phase):
  2 critical: direct URL access bypasses auth, duplicate form submission
  3 moderate: 1000-char title causes layout break, missing field shows generic error
  1 minor: browser back after submit shows stale data

Overall quality grade: B
  (certified, but has critical quality issues to address)
```

The BREAK findings become LOW-PRIORITY fix tasks in the outer loop —
they don't block certification but improve the product on subsequent iterations.

### Evidence capture

The RUNTIME captures every tool call automatically:

```python
evidence_chain = []
for tool_call in agent_session:
    evidence_chain.append({
        "tool": tool_call.name,
        "input": tool_call.args,
        "output": tool_call.result,
        "timestamp": datetime.now().isoformat(),
    })
```

The agent's output (verdict, diagnosis) is separate from the evidence.
Evidence is ground truth. Agent output is interpretation.

## Phase 5: Output for Outer Loop

### Per-journey result

```python
@dataclass
class JourneyResult:
    story_id: str
    story_title: str
    persona: str
    passed: bool
    steps: list[StepResult]
    summary: str                    # agent's narrative summary
    evidence_chain: list[dict]      # runtime-captured tool calls
    blocked_at: str | None          # which step broke the flow

@dataclass
class StepResult:
    action: str
    outcome: str                    # "pass" | "fail" | "blocked"
    verification: str               # what was checked
    evidence: list[dict]            # requests/responses for this step
    diagnosis: str | None           # root cause (if failed)
    fix_suggestion: str | None      # what to fix (if failed)
```

### Fix task generation for outer loop

```python
def create_fix_tasks(journey_results: list[JourneyResult]) -> list[str]:
    tasks = []
    for jr in journey_results:
        if jr.passed:
            continue
        for step in jr.steps:
            if step.outcome == "fail":
                tasks.append(f"""
Fix product issue found in journey "{jr.story_title}":

Step: {step.action}
Expected: {step.verification}

Diagnosis: {step.diagnosis}

Suggested fix: {step.fix_suggestion}

Evidence:
{format_evidence(step.evidence)}

Context: This was step {step_index} of the "{jr.story_title}" journey
({jr.persona} persona). The journey tests: {jr.tests_integration}.
""")
    return tasks
```

The fix tasks are RICH:
- What the user was trying to do (story context)
- What went wrong (diagnosis with HTTP evidence)
- What to fix (specific suggestion)
- Where in the flow it broke (for prioritization)

### Scoring

```
Certification score = journeys_passed / journeys_total

Detailed breakdown:
  Critical journeys: 3/4 passed (75%)
  Non-critical journeys: 2/2 passed (100%)
  Steps completed: 18/22 (82%)

  Blocked at:
  - "User Data Isolation" step 5: listing tasks returns all users' tasks
    → user isolation not enforced
```

## Fair Comparison

```bash
# Compile shared stories (product-independent, cached)
otto certify --compile "task manager with auth CRUD status isolation" -o stories.json

# Certify each product (same stories, same agent, same tools)
otto certify ./otto-app --stories stories.json --port 4001
otto certify ./barecc-app --stories stories.json --port 4005

# Compare at the story level
otto compare otto-report.json barecc-report.json
```

### What's shared (ensures fairness)

- **Same user stories** compiled once from intent
- **Same BREAK strategies** (generic adversarial patterns, not product-specific)
- **Same agent** (same model, same prompt template, same tools)
- **Same evidence format** (runtime-captured, structured)

### What adapts per product (ensures accuracy)

- **Manifest** tells the agent how THIS product's API works
- **Agent reasoning** adapts to each product's conventions
- **Browser interaction** adapts to each product's UI layout

### Comparison output

```
                        Otto        Bare CC
Happy path journeys:    4/5 (80%)   3/5 (60%)
Steps completed:        18/22       14/22
BREAK findings:         2 critical  5 critical

Story-level diff:
  ✓/✓  New User First Task         (both pass)
  ✓/✗  Task Status Lifecycle       (bare CC: update returns 500)
  ✓/✗  User Data Isolation         (bare CC: no isolation enforced)
  ✓/✓  Persistence Across Sessions (both pass)
  ✗/✗  Admin Dashboard             (both: no admin feature built)
```

The comparison is at the USER STORY level: "which product lets users
accomplish more of what was promised?" Not at the HTTP level.

For high-stakes comparison: run 3 times each, report median + variance.
Happy path results are mostly deterministic (same flows). BREAK results
may vary (agent explores differently each time) — report as ranges.

## Implementation Plan

### New files
```
otto/certifier/
    stories.py              # UserStory compilation from intent
    manifest.py             # ProductManifest (enhanced adapter output)
    preflight.py            # Quick structural pre-check
    journey_agent.py        # THE MAIN THING — journey verification agent
    report.py               # Proof-of-work + fix task generation
```

### Implementation order
1. `UserStory` dataclass + compiler prompt
2. `ProductManifest` from adapter (enhance existing)
3. `preflight.py` — quick structural check
4. `journey_agent.py` — journey verification agent
5. Wire into `otto certify` + outer loop
6. Fix task generation for outer loop
7. `otto compare` command
8. Test on all 8 projects

### What to keep from v1
- `adapter.py` — route/model/field discovery (enhance, don't rewrite)
- `classifier.py` — framework detection
- `AppRunner` — start/stop apps
- CLI structure

### What to remove from v1
- `intent_compiler.py` — replaced by stories.py
- `binder.py` — agent handles binding implicitly
- `baseline.py` self-healing — agent adapts instead
- `baseline.py` per-claim execution — replaced by journey agent
- `tier2.py` old journey runner — replaced by journey agent

## Cost Estimate

| Component | Cost | Time |
|---|---|---|
| Story compilation | ~$0.15 | ~30s |
| Manifest (static + probes) | $0 | ~2s |
| Pre-flight check | $0 | ~3s |
| Journey agents (5 stories) | ~$0.25-0.75 | ~60-120s |
| **Total** | **~$0.40-0.90** | **~90-150s** |
| **Comparison (2 products)** | **~$0.80-1.80** | **~3-5 min** |

## What This Does Not Handle

- Performance/load testing
- Security testing beyond auth + access control
- Accessibility auditing
- Non-HTTP protocols (WebSocket, gRPC)
- Mobile-specific testing

## Success Criteria

1. Journey agents complete all stories on all 8 test projects
2. Failures produce actionable fix tasks that coding agents can act on
3. Stories compiled from intent cover all requested features
4. Fair comparison: same stories, consistent results across runs
5. Outer loop: fix → rebuild → re-certify shows improvement
6. The certifier catches integration bugs that per-task QA misses
