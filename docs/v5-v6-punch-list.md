# Otto v6 Punch List

Things deliberately not in v5, with rationale for deferral and pointers
to where they connect.

## NEXT-2 — Foundation test doubles must be endpoint stubs, not real-router-loaders

**Status:** queued.

### Pattern

A `lead-architect.md` directive told the foundation Lead to "provide
test doubles for sibling cross-feature dependencies." Intent: features
with cross-contract test needs (one feature's tests need a sibling
feature's endpoints to satisfy preconditions) use foundation-provided
mocks in their leaf tests instead of declaring `partial` because
their own worktree doesn't contain sibling code.

Live validation surfaced an interpretation gap. The foundation agent
built test infrastructure that uses the project's normal test-client
factory — which loads the real router files from the filesystem (the
stack's standard pattern). For cross-feature tests to actually
exercise sibling endpoints, sibling router files have to physically
exist in the testing feature's worktree. Since the testing feature
doesn't own those files, the agent's next move was to copy them,
violating the partition.

### Why the prompt was insufficient

"Test doubles" was interpreted as "test infrastructure that uses real
code" — the stack-conventional meaning of `TestClient` /
`MockMVC` / equivalent in most frameworks. A literal reading of
"doubles" would be "stubs that STAND IN for sibling code, not require
it." The agent picked the engineer-intuitive default. The
cross-feature isolation constraint Otto operates under didn't
override the framework convention.

### What the doubles should actually be

Endpoint stubs (or stack-equivalent: CLI subcommand stubs, library
function stubs, RPC handler stubs) that:

1. Register contract-shaped endpoints **without importing sibling
   implementation files**. They satisfy the contract; they don't
   route through real code.
2. Are **shaped by foundation_contracts**. The architect declares the
   contract (the public surface, the response/return shape); the
   stub implements that contract minimally. Same authority that
   decides the contract decides the stub.
3. Are **opt-in per test**. The testing feature's test-fixture
   imports the stub for sibling-owned contracts, then registers its
   OWN real implementation for the feature-under-test. Sibling
   features stubbed; feature-under-test real.

### The chain of responsibility

For this to work end-to-end every link must know what "doubles" means:

| Agent | Responsibility |
|---|---|
| Foundation Lead | Emit one stub artifact per shared contract. Declare each in `foundation_contracts` with a `testing_only` flag |
| Root Lead writing child intents | When a feature's tests need a sibling-owned contract, name the stub paths in the child's intent under "shared modules you import" |
| Feature Lead | Use the stubs in tests. Do not copy sibling code |
| Integration Lead | Real implementations are present at integration; verify the contract shape matches what stubs promised |

### Connects to

- The lead-architect.md "Provide test doubles" section is where the
  foundation prompt lives — needs the sharpening above.
- The lead.md child-intent rubric (F-7) needs an addition: child
  intents must name the stub paths in the "shared modules" list
  when the feature has cross-contract tests.
- The chokepoint LAND-with-annotation pattern is a PATCH for the
  consequence (a feature-partial cascading because it can't test
  in isolation); this is the PROTOCOL fix (the feature can pass
  cleanly because the stubs exist).

### Suggested first commits (smallest first)

1. Sharpen the foundation-prompt's "Provide test doubles" section:
   replace generic prose with the concrete shape — endpoint stubs,
   no sibling imports, declared in foundation_contracts. Include a
   stack-shape example (HTTP stub, CLI stub, library stub — one
   per major project kind).
2. Update the child-intent rubric: child intents MUST include a
   "shared stubs to import" list when the feature's tests need
   sibling-owned contracts.
3. Add a foundation-gate check: every declared `foundation_contracts`
   entry with `testing_only:true` must have a stub file at the
   declared path. Same shape as the existing partition checks.
4. Validate via a fresh project run. Expected outcome: testing
   feature's fixtures import the stubs, no copying of sibling
   code, all tests pass at leaf time, no branch-ancestry
   violation, aggregate verdict = pass.

### Why this matters

The "honest cross-feature isolation partial" problem doesn't go away
with one prompt edit. The shape of doubles is the load-bearing
detail; the chain from "foundation provides X" → "children use X"
needs every link to know what X is. Today only the foundation knows;
sibling features pattern-match to the framework's default
test-client.

### Risk

Medium. Stubs that drift from the real router's response shape break
integration. Mitigation: foundation_contracts declares the response
shape; both the stub and the real implementation must match it
(today's contract gate already enforces this for the real
implementation; extend to the stub).

---

## NEXT — Spec + journeys as a verified pair (auto-derived, human-editable)

**Status:** queued. Start after the current architectural pass is live-validated.

**Framing (user, 2026-05-21):**

> Spec and journey are pairs by VERIFICATION RELATIONSHIP, not by
> equivalence. The spec is the contract — "what the product does."
> Journeys are the e2e behavioral tests of the spec — "how I'd verify
> the contract holds." Same shape as code ↔ tests: code is the source
> of truth, tests check the code matches what-should-be. Each spec
> claim should have some journey coverage; editing a journey doesn't
> change what's claimed; editing the spec might require regenerating
> journeys to maintain coverage. Both should be auto-derived from
> intent and both should be human-overridable.

### What's wrong today

Journeys are generated by the spec-compile agent as "representative
samples" embedded inside `spec.json` (under `behavior_journeys`).
Five structural problems flow from that:

1. **Test embedded in the contract.** Tests of the contract should
   not live inside the contract artifact. Today `spec.json` contains
   both "what the product does" AND "how to verify a subset of it" —
   that's structurally muddled.
2. **Samples ≠ coverage.** A `pass` verdict on N journeys does not
   imply the spec holds — only that the N covered claims hold.
   Compile-time lint warnings ("you declared X but no journey
   exercises it") make the gap visible (linkboard 2026-05-21: 8
   uncovered actions/claims) but don't close it. Verifier should
   distinguish "all claims covered by journeys, all journeys passed"
   from "some-journeys-passed, claim coverage unknown."
3. **Pre-committed specifics are brittle.** A journey that says
   `name="Add tag"` mismatches a feature rendering `"Create tag"`.
   `lead-integration.md` partly mitigates with "treat as user-visible
   behaviors, not literal selectors" — a prompt patch on a structural
   issue.
4. **Generative non-determinism.** Different spec-compile runs
   produce different journey samples → unstable test suite for the
   same contract.
5. **Constrains agent creativity.** Children pattern-match to the
   official journey list and may under-deliver on actions the
   journeys don't exercise. See `feedback_leaf_agents_own_design` in
   memory.

### Direction (auto-derive both; human edits both)

Treat spec and journeys as two **separate artifacts** with a
verification relationship:

- **Spec** (`spec.md` / `spec.json`) is the contract. What the
  product does. Auto-derived from intent; user-overridable. Editing
  it changes what's claimed.
- **Journeys** (`journeys.md` / `journeys.json`) are the e2e tests
  of the spec. How to verify the contract holds. Auto-derived from
  the spec (NOT from intent directly — derive *from* the thing
  they're testing); user-overridable. Editing them changes what
  gets tested, not what's claimed.
- **Coverage relationship**: every spec claim (primary_action,
  intent_claim, quality_constraint, ...) should have at least one
  journey that exercises it. Lint surfaces uncovered claims as
  visible TODOs — they ARE the test-gap. Verifier's verdict
  should distinguish "spec fully covered + all journeys passed"
  (true pass) from "spec partly covered" (test-gap-partial).
- **Re-derivation safety**: user edits are preserved across
  re-runs via locking markers (same pattern as `intent.md`
  user-owned vs `intent.txt` runtime snapshot).

### Why this also fixes existing problems

- **Spec becomes human-readable by construction.** Today's
  `spec.json` is machine-only; an `spec.md` is the canonical
  artifact a reviewer reads, JSON is the lookup index.
- **Journeys earn their own surface.** Today they hide inside the
  spec; surfacing them gives users a place to add edge cases,
  rewrite ambiguous steps, mark `verification_level`. They
  remain TESTS — not contract.
- **Coverage lint becomes load-bearing.** The 8 warnings stop being
  diagnostic noise and become "your test suite is incomplete; fill
  these in or accept partial verdict."
- **Verifier interpretation simplifies.** User-locked journeys =
  agreed test cases, executed with permitted flexibility. Auto
  journeys = best-effort samples, behavioral interpretation. The
  per-journey lock state tells the verifier which mode to use.
- **Decomposition gets a stable input.** Root Lead reads the spec
  for what to decompose, reads journeys for what each child must
  end-to-end support.

### Open design questions

- **Storage**: `spec.md` + `journeys.md` checked into the product
  repo (alongside `intent.md`)? Compile-time JSON shapes derived
  from these or vice versa? Probably: markdown is canonical, JSON
  is the compiled index.
- **Edit conflict resolution**: when re-derivation would change a
  section the user edited, do we (a) skip re-deriving that section
  via a `<!-- user_locked -->` marker, or (b) merge with conflict
  markers? Likely (a) for predictability.
- **Coverage lint surfacing**: today the warnings live in
  `compile_metrics.json`. Should they appear as TODO items in
  `spec.md` itself — visible to the agent reading the spec?
- **Journey verification mode**: per-journey field
  `interpretation: "literal" | "behavioral"`. User-locked journeys
  default literal; auto journeys default behavioral.
- **Spec-change → journey-coverage drift**: when user edits spec
  to add a claim, how does Otto suggest a journey for it? Auto-add
  a draft journey at next compile? Lint warning?

### Connects to

- Memory `project_journey_intent_vs_contract` ("capability manifest
  Path A" was the prior framing of this direction).
- `audit-prompts.md` F-5 ("behavioral verification > text-search" —
  the verifier-side complement of this work).
- The lead.md / lead-architect.md split (2026-05-21): precedent for
  "auto-derived but human-editable artifacts at known paths in the
  repo."

### Suggested first commits (incremental, smallest steps first)

1. Render `spec.md` from `spec.json` at compile time — one-way,
   no edit loop yet. Land it in the session dir. Tests check
   it parses and round-trips. Establishes the human-readable
   artifact shape.
2. Render `journeys.md` from `spec.behavior_journeys` — separate
   file. Establishes the test artifact as a first-class surface
   distinct from the contract.
3. Surface coverage lint in `spec.md` as visible TODOs (claims
   without journeys), so the reviewer sees the gap.
4. Accept user-edited `journeys.md` at the product repo root
   (alongside `intent.md`). If present, replaces the auto-derived
   `spec.behavior_journeys` for that run.
5. Bidirectional compilation: parse user `spec.md` into
   `spec.json`; preserve user-locked sections across re-runs.
6. Verifier verdict distinguishes "spec fully covered + all
   journeys passed" from "spec partly covered" — make the
   coverage gap demote-worthy or advisory based on the user's
   policy.

Defer steps 5-6 until 1-4 ship and we see how users actually
edit these files.

---

## v6.6 — Safety-check Should Be Repair Entry, NOT Hard Exception

v6e crashed `catastrophic` despite successfully building the product.
Root cause: TWO of today's fixes contradict each other.

**The contradiction:**

- Morning fix (`_checkout_v5_branch_clean` in `otto/v5_runner.py:227`)
  raises `RuntimeError` if worktree is dirty before root integration
  start. Was added during worktree-hygiene bundle to prevent dirty
  state from accumulating across phases.
- Afternoon fix (v6.5 simplification of repair classifier) defaults
  fixable failures to coding-agent repair rather than escalation.

**The conflict:**
The hard exception fires at a pre-flight layer BEFORE the repair
classifier sees it. So when v6e's FE work was complete-but-uncommitted
(15+ modified files, no actual product bug), the safety check raised
RuntimeError → catastrophic crash. The agent never got a chance to
trivially fix it (`git commit -m "fe work" && retry`).

**v6.6 work:**

1. **Demote `_checkout_v5_branch_clean` from RuntimeError to
   classified failure.** It should signal `worktree_dirty_at_phase`
   to the repair loop, NOT throw.

2. **Repair classifier handles `worktree_dirty_at_phase`** as
   agent-fixable: spawn coding agent with the dirty file list +
   diff stat + phase context. Agent decides: commit / stash /
   revert (in tracked context).

3. **Cap by attempts as usual.** Repeated dirty-state after 2 fixes
   → escalate to merge_blocked.

**Why this matters:** v6e PROVED that Otto can build the product
(3000+ LOC real FE pages, real backend, all in 65 min). The only
failure was the pipeline refusing to finalize what was already done.
That's a runner-side ergonomics bug, not an agent capability bug.

This is one of the clearest examples of "patches contradict each
other" — exactly the pattern the Design Principle (Trust the agent,
minimize classification, reject patches when an LLM can decide) was
written to prevent. Even with the principle in place, we still
shipped two fixes that fought each other.

Lesson: when adding ANY new pre-flight check that raises, ask "if
this fires, can an agent fix it?" If yes, classify and dispatch the
agent rather than raising.

## v6.6 — Decomp Reasoning via Operational Inputs (NOT Rules)

Today's v6e showed the gap: agent decomp is decision-poor.
Lead doesn't know `max_parallel`, cost model, queue state, project
profile. Result: emits 3 children with a serial chain (FE→BE),
wasting the parallelism we configured.

The principle-aligned answer is NOT to add rules ("max 5 children").
It's to give the lead **operational facts** and ask it to reason
about critical path. Codex's design (audited self-critique against
the principle):

### New data passed into lead prompt (`decomp_runtime_context`)

```python
{
    "max_parallel": int,            # from CLI / config
    "run_budget_seconds": int,      # NOTE: time, NOT USD
    "run_elapsed_seconds": int,
    "cost_model_s": {
        "worktree_setup_s": 60,
        "prompt_render_s": 10,
        "min_leaf_runtime_s": 300,
    },
    "queue_state": {
        "active": int, "queued": int, "ready": int,
        "waiting_on_deps": int, "free_slots": int,
    },
    "spec_profile": {
        "project_kind": str, "intent_claims": int,
        "core_entities": int, "primary_actions": int,
        "behavior_journeys": int, "entry_routes": [str],
    },
    "runtime_policy": {
        "tier": str, "review_first_decomp": bool,
        "context_slicing": bool, "provider": str,
    },
    "recent_stats": {  # optional, from cross-sessions history
        "leaf_duration_p50_s": int,
        "leaf_duration_p80_s": int,
        "integration_duration_p50_s": int,
    },
}
```

### Lead prompt addendum (~30 lines)

> "Reason about wall-clock critical path, not child count. A child
> only creates parallelism if it can start and verify without
> waiting for another child's code. FE-then-BE is FAKE parallelism.
> Prefer either (a) a small scaffold task that makes later leaves
> independent, or (b) coherent VERTICAL leaves owning end-to-end
> user capability.
>
> For each plausible shape, estimate: ready-at-start children vs
> waiting, longest dependency chain, fixed overhead paid per child,
> tree budget consumed, integration risk.
>
> Pick the fastest correct end-to-end plan under current runtime
> facts. Valid to emit fewer, equal, or more children than
> max_parallel when critical path justifies it. After emitting,
> leave a concise rationale in the final message."

### Why this is inputs, not rules

- No "max N children" threshold
- No "max depth 2" guard
- No schema validator rejecting trees
- No new classifier
- Lead retains full judgment authority; just has the economics

### What's NOT in this design (deliberately deferred)

- Dynamic `max_parallel` auto-bumping (operator policy, not planning hint)
- Decomp self-review by separate agent (single-Lead reasoning only)
- Persistent stats collection if `recent_stats` unavailable (use defaults)

### Estimated LOC

~20 lines code (collect context, pass into prompt) + ~30 lines prompt.
**Net positive only because we're adding new context**, not because
we're adding new rules. Trade-off is acceptable.

### Validation strategy

After implementation, dogfood the SAME iTracker intent. Compare:
- Tree shape (number of children + parallelism)
- Wall clock
- Whether lead's emitted rationale reflects critical-path reasoning

If trees still serialize unnecessarily, iterate on the prompt's
reasoning prompts (not on new rules).

## Design Principle (governs all entries below)

**Trust the agent. Minimize classification. Reject patches when an LLM
can decide.**

Throughout v5/v6 we accumulated 20+ patches for things a coding agent
could figure out from context: failure classifiers, prompt instruction
heuristics, schema rigidity, etc. Each patch adds surface area for
future divergence and bugs.

The protocol going forward:
- **Auto-fix (no agent):** only truly algorithmic ops with no judgment
  (kill port, sanitize filename, chmod). 5-10 lines of code max.
- **Agent-fix (default for everything else):** spawn coding agent
  with error + git status + relevant paths + bounded scope. Let it
  decide what to commit/stash/fix/retry. Cap by attempts × cost.
- **Escalate (genuine hard stops only):** disk full, network down,
  repair packet budget exhausted, or a typed oracle/no-progress gate
  says the same repair unit is not improving. NOT "I don't have a
  hardcoded handler for this failure kind."

When tempted to add a classifier, prompt instruction, or rigid schema
check, ask: "could a coding agent figure this out from context?"
If yes, don't write the patch.

This principle should ripple through:
- Repair classifier (rip out the 4-handler taxonomy)
- DAG/decomposition heuristics (the agent can read its own context)
- CHARTER/schema validators (relax to "warn agent" not "hard reject")
- Coverage-matrix prompt enforcement (let the agent decide what to test)
- Decisions broadcast detection (just tell the agent to write decisions)

Most existing P1 entries below should be re-framed through this lens
before being implemented.

## Architectural

### Recursive in-session subagent dispatch (depth ≥ 2)
**State:** verified broken in SDK 0.1.50 via
`/tmp/sdk-smoke/test_depth_thorough.py`. Subagents cannot dispatch other
subagents via Task. v5 routes recursion through the queue
(`mcp__otto__submit_subtask` → fresh top-level `query()`); verified at
depth 3 in `test_queue_recursion.py`.

**v6 question:** if SDK adds depth-2 in-session, do we use it for
tightly-coupled work, or stick with queue recursion?

### Watcher integration
**State:** v5 has its own async runner (`otto/v5_runner.py`). The
existing watcher (`otto/queue/runtime.py`) does not yet schedule v5 tasks.
This means `otto queue submit "<intent>"` doesn't currently reach v5.

**v6 work:** extend the existing watcher's main loop to also poll
`v5_pending.jsonl` and spawn v5 child tasks via `otto run --queue-task=<id>`.
Estimated ~250 LOC. Tested by submitting tasks via `otto queue submit` and
verifying they appear in `task_graph.json`.

### Multi-user concurrent submitters
**State:** untested. v5_runner is in-process; two MC users running
`otto run` against the same project would each spin up their own
runner. Cross-task merge has fcntl on git operations, but the queue's
v5_pending.jsonl write-coordination is unverified at high concurrency.

**v6 work:** stress test 4+ concurrent submitters; harden any race seen.

### Lead orchestrator-only enforcement leaks via Bash
**State:** Lead's `disallowed_tools` blocks Write/Edit/MultiEdit/NotebookEdit
(verified live), but the Lead can still write files via Bash heredoc
(`cat > tests/foo.py << 'EOF' ... EOF`). The URL-shortener API child Lead
took this path to write tests instead of dispatching the test subagent.

**Impact:** moderate. Build/test code separation isn't enforced — same
LLM may write app and tests, defeating the cross-check intent. Verify
gate still catches false pass claims.

**v6 work:** either (a) PreToolUse hook that rejects Bash commands
matching `>\s*(tests/|.*\.test\.|.*\.spec\.)`, or (b) richer prompt
language with concrete examples that nudge to Task.

### Per-task isolation under conflict resolution
**State:** when a child's branch fails to merge into the parent's
integration branch, v5 marks `merge_blocked` and continues. Plan-v5 §3
described the option to "resume task's Lead via SDK session_id" with
the conflict markers in scope; not implemented. Today's behavior: child
worktree preserved, user can resolve manually.

**v6 work:** add a "conflict resolution Lead" that's spawned with the
conflict context and the task's prior session_id resumed; cap at 2
attempts.

### Enforce children's use of existing decisions.md broadcast
**State:** The append-only `decisions.md` broadcast channel already
exists. The lead prompt instructs children to read it (line 20: "Read
CHARTER.md and decisions.md at the repo root") and append to it
(Step 4 at line 479: "Record boundary decisions to decisions.md").
Sibling appends merge cleanly via union-merge (line 489). Children
that run later read decisions.md and see earlier siblings' entries.
The infrastructure is there.

What's missing is **agent culture of actually using it**. Sample audit
from sc6 (today's run): 7 entries, all from `architect` at 04:52,
zero from feature children — despite multiple feature children making
non-trivial cross-cutting choices (e.g., `created_at` formatting, draft
storage key shape, image upload endpoint path). The prompt instructs;
children don't comply.

**Why:** the append instruction is at Step 4 of the prompt — buried
after Build and Verify. Children focus on their scope, treat
decisions.md as "the architect's file," and skip it. No enforcement
mechanism downgrades verdict if a child made a broadcastable decision
without recording it.

**v6 work (small):** three escalating fixes, cheapest first:
1. Promote decisions.md append from Step 4 to a top-level
   "responsibility" with explicit triggers ("any choice that affects
   shape, naming, file ownership, env vars, or wire shapes → append").
2. Add `decisions_appended: [{decision_id, summary}]` to verdict.json
   schema. The agent must list its appends or explicitly state
   "no boundary decisions made in this scope".
3. Runner-side check: for every cross-subsystem touchpoint the agent
   modified (detected by file path heuristic — shared schemas, types,
   wire formats), require a matching decisions.md entry, else
   downgrade verdict to partial with diagnostic.

**Why this matters:** if children broadcast decisions, integration
gets a richer reconciliation trail and (more importantly) parallel
siblings landing AFTER a decision can read it. The "Slack equivalent"
debate dissolves: we don't need a comm channel because the broadcast
log already exists. We just need agents to actually post to it.

### Shell script + scaffold portability validation (shift-left)
**State:** sc6 run wasted 77 minutes because `start.sh` used bash-4
syntax (`${service^^}`) that fails on macOS's bash 3.2. The bug
existed at minute 9 (architect emitted start.sh); preflight discovered
it at minute 86. `verify_from_clean(scope="scaffold")` at
`otto/v5_preflight.py:193` runs build/typecheck but deliberately
skips executing `start.sh`. `bash -n` would NOT have caught this
(bash-3 parses `${var^^}` as syntactically valid; fails at runtime).

**v6 work:** add a `script_valid` check to `otto/v5_clean_verify.py`,
called post-architect:
- For every root-owned `*.sh`: shebang present, executable bit, `bash
  -n` syntax check, bash-4-feature detection when host bash <4
  (regex for `${var^^}` / `${var,,}` / arrays).
- Dynamic exec: bind a temporary local port, run `API_PORT=<busy>
  bash start.sh`, expect a clean PORT_CONFLICT message. Catches the
  exact untested branch without booting the full app.
- Improve `_parse_declared_ports()` to recognize CHARTER table rows
  like `| API_PORT | 8000 |` (today only catches inline forms).

### Inject preflight result into integration agent (autonomous fix for trivial blocks)
**State:** `_run_integration()` at `v5_runner.py:1178` runs
`smoke_clean_deploy()` but only logs and continues. The result is
NOT injected into the integration agent's task input. The
integration agent has prompt authority for "small fixes" up to 50
LOC (`lead-integration.md:211`) but bypassed `start.sh` entirely and
never saw the bug. So we lost autonomous repair for a one-line shell
fix; final verdict was `partial` on a product whose frontend code
never landed in `main`.

The legacy draft proposed a separate per-symptom preflight repair agent.
That is now obsolete. Preflight failures use the same durable
`RepairPacket` session as other repair units: typed clean-oracle result,
baseline scope, attempt journal, budget, commit hook, and composite gate.

**v6 work:** in `_run_integration()`:
1. Resolve integration worktree (NOT `project_dir` — `project_dir`
   may be on `main` while merged children live on
   `i2p/integ/<task>`)
2. Run the typed clean oracle on the integration worktree
3. Spawn repair/integration agent with a packet that includes:
   - children's verdicts (today)
   - typed clean-oracle issues and full artifact paths
   - scope baseline and allowed paths/contracts
4. Agent reconciles + applies repairs within the packet scope
5. Runner re-runs the clean oracle and composite gate after each agent
   turn; result stays `merge_blocked` if still red

**Deterministic fast-path criteria** (provably local and idempotent):

ELIGIBLE — bug class + scope of change:
- Clear an Otto-owned stale port before the packet repair session.
- Normalize filenames or permissions when the oracle identifies one
  exact path and the change is deterministic.
- Re-run the packet oracle immediately; if still red, hand off to the
  durable repair session.

NOT ELIGIBLE — anything that adds product surface:
- New API endpoints, routes, types, entities, dependencies
- New tests (test files belong to children's scope)
- Changes to auth logic, encryption, persistence layer
- Adding or removing user-visible features
- Hosts/infra issues that aren't agent-fixable (port busy on host,
  browser unavailable, install failures across many files)
- Repeated same failure (already tried once)

The hard gate is the repair packet: budget, typed oracle result, full
artifacts, and baseline-relative composite scope. Excerpts and legacy
display names are advisory only.

**Why this matters:** Otto v3 had repair loops via the certifier
("round 1 fail → round 2 fix"). v5 hierarchical traded that for
simpler one-shot verification. For a class of trivially-fixable
preflight failures (shell scripts, env vars, file modes), a tiny
autonomous repair loop restores the v3-era resilience without the
full certifier loop's complexity. Cost per repair attempt: ~$0.05 and
30 seconds. Cost of NOT having it (today): hours of wasted compute
+ user-visible "broken" final state.

### CRITICAL: Legacy repair classifier too narrow (deleted)
**State:** v6d hit `merge_worktree_dirty` — backend's merge blocked
because `decisions.md` had uncommitted appends from an earlier
phase. The legacy symptom controller classified this as "unknown" and
escalated even though a coding agent could handle it.

The current implementation removed that controller. Repair scope is now
derived from the typed `RepairPacket` and clean-oracle result, not from
legacy display issue names.

This is the same anti-pattern we keep hitting: over-prescriptive
classification when the agent can decide.

**v6 work (P1):**

1. **Default to agent-fix for any non-hard-stop classification.**
   Deterministic auto-fixes (no agent): port-busy (kill PIDs),
   filename-too-long (safe_slug), permissions/chmod patterns.
   Everything else that *might* be fixable: spawn the repair
   agent with the full error context and let it decide.

2. **Hard-stop list (genuine escalates only):** disk full, network
   unreachable, missing external service, repeated-same-failure-
   after-2-attempts. Don't bucket "uncommitted decisions.md" with
   "disk full."

3. **Agent gets enough context to fix anything:** failure message,
   offending file path(s), `git status` output, recent
   integration log excerpt. Coding agents are good at this kind
   of thing.

4. **Cap by attempts + cost, not by classification.** Today's
   caps (2 per kind, 3 total, <10% cost) are right. Don't add
   "and only if classifier knew the kind."

### CRITICAL: DAG breadth explosion (16 tasks is too many)
**State:** v6d produced 16 task graph nodes from a single iTracker
intent (v6c: 9, sc4: 4). Each layer adds breadth: root emits 6,
multiple emit grandchildren, some grandchildren emit
great-grandchildren. The DAG rule prevents chains (critical path
> 2) but doesn't prevent breadth — every child independently
decides "I'm too big, split."

Symptoms:
- More setup overhead (each task gets a worktree + prompt render)
- More integration sessions to run (each pending_children parent
  needs one)
- More cross-merge surface = more chances for `merge_worktree_dirty`
- Diminishing returns on parallelism past max-parallel=3

**v6 work (P1):**

1. **Total-nodes guard at root.** Root planner sees a count
   estimate (current tree + planned children + estimated
   sub-decomp). If projected total > N (e.g., 10), refuse to
   emit more leaves; tell child to inline instead.

2. **Sub-decomp consent budget.** When a child decides to
   sub-decompose, it doesn't get a free pass — it spends budget
   from a global counter. If budget exhausted, child must inline.

3. **Heuristic: budget proportional to scope.** A "12-entity
   Linear-clone" gets a higher budget than "a CLI tool."
   Architect could declare this.

4. **Make the cost of split visible to lead.** Today the lead
   prompt doesn't know "you're already at 12 tasks; splitting
   makes 14." Inject a hint with current node count.

### Integration packet + risk handoff (fresh-context plumbing)
**State:** Integration agent runs with fresh SDK session — no
inherited conversation from planning phase. Principled (forces
durable artifacts, external verifier role). But today the runner
renders only thin child info into the integration prompt:
`task_id`, `verdict`, truncated intent. Full child verdict
payloads (partial/skipped items, evidence paths, decisions
appended, runner check failures) don't reach the integration
prompt. Result: integration has to rediscover known issues from
code or misses them.

This is NOT a fresh-context problem per se. It's a missing-
artifacts problem. Two complementary fixes:

**v6 work:**

1. **`integration_packet.json` (runner-built):** before each
   integration session, runner writes a structured packet
   containing:
   - Full child verdict JSONs (not truncated)
   - Child session dirs + build branches
   - Changed-file summaries per child
   - `intent_coverage.partial/.skipped` from each child
   - `decisions_appended` aggregated
   - Runner check matrix failures per child
   - Preflight results
   - Applicable journey IDs

   Integration prompt instructs: "first read
   `<session_dir>/integration_packet.json` — it's your context."

2. **`risk_handoff.md` / `integration_risks.json` (agent-emitted):**
   distinct from decisions.md. Decisions = settled choices.
   Risk handoff = unsettled concerns. Each child verdict
   schema gains:
   - `known_gaps[]` — what the agent knows is missing
   - `contract_deltas[]` — assumptions made about
     siblings' interfaces that should be verified
   - `integration_checks_to_run[]` — concrete probes
     integration should run

   Integration agent reads these and runs the suggested
   checks before declaring pass.

**Why this matters:** today's design assumed `decisions.md` would
capture cross-cutting context. It doesn't capture *unsettled* state
— uncertainty + open questions + expected seams. Integration is
flying blind on exactly the things that most need verification.

Codex's framing: "Decisions capture settled choices. Integration
also needs unsettled risks."

Sound architecture, weak plumbing. P1 to make fresh-context
integration trustworthy.

### CRITICAL: verdict.json schema parsing brittleness
**State:** v6c live run surfaced this. Frontend child
`v5-cff316946049` ran 25:50, cost $4.58, wrote a valid JSON
verdict.json — but in a NON-CANONICAL schema:

```json
{"status": "success", "tests": {"total": 34, ...}, "deliverables": [...]}
```

Otto expects:

```json
{"verdict": "pass"|"partial"|"unverified", "journeys": [], "intent_coverage": {...}, ...}
```

Runner marked the session `unverified` ("Agent did not write
verdict.json"), discarded the work. 25 min + $4.58 thrown out.
The error message is also misleading — the file DID exist, just
not in the expected shape.

**Why this is critical:** the entire runner-verdict pipeline
(downgrades, status reflection, merge propagation) hinges on
canonical verdict shape. One schema slip = lost work. Provider
divergence likely makes this more frequent (Claude vs Codex may
default to different shapes).

**v6 work (P1 — must fix before v6 considered shipped):**
1. Forgiving verdict parser: detect known alternative shapes
   (e.g., agent's `status: "success"`) and map to canonical fields
   when possible. Keep a strict mode for new agents.
2. Better error message: distinguish "verdict.json missing"
   from "verdict.json found but unparseable" (with quoted
   excerpt + expected schema).
3. Prompt audit: make verdict schema example more prominent +
   include a NEGATIVE example showing what's wrong (e.g.,
   `{"status": "success"}` → REJECTED).
4. Recovery: when verdict is unparseable and tests appear to
   have passed (based on agent's reported deliverables), consider
   issuing a single retry round asking the agent to rewrite
   verdict in canonical shape. Cap at 1 retry to avoid loops.

This is now the #1 P1 item for any further v6 hardening. Verdict
shape is the load-bearing contract between agents and the runner;
brittle parsing means silently-discarded work.

### Journey contract-vs-reality gap (intent over literal selectors)

**State:** discovered concretely on iTracker (2026-05-20) after 3
iterations stabilized at `partial` verdict with 3 residual journey
fails. Diagnosis showed the 3 residuals are NOT real product bugs in
the way the verdict framed them:

| Journey | Looked for | Actual product state |
|---|---|---|
| kanban / setup.cycle | `label='Cycle name'` | Cycle creation UI genuinely missing (real gap) |
| comment_mention / setup.invite_bob | `label='Email'` | `<input type="email" placeholder="teammate@example.com">` exists, but no `<label>`/`aria-label` so `getByLabelText("Email")` misses |
| search / setup.issue_a | `label='New issue title'` | `aria-label="New issue title"` exists in source — likely a route/visibility timing miss, NOT a label miss |

The structural problem: `spec.behavior_journeys[].pass_model` mixes two
distinct layers in one declarative blob:
1. **Intent** — what behavior must work ("user invites teammate via
   email; teammate appears in members list")
2. **UI contract** — exact role/name/label selectors the planner
   *guesses* the children will use (written BEFORE the product exists)

Children build independently with their own naming choices ("Member
email" vs "Email", placeholder vs `<label>`, inline form vs modal).
Each is a correct realization of the intent. The literal contract
treats every divergence as a failure. The planner has no way to be
right consistently because there's no canonical "right" name.

Semantic fallback (`_semantic_locator` in `otto/journey_ui_executor.py`)
already softens this — it snapshots interactive controls, walks
preceding siblings to recover unassociated `<label>`, and fuzzy-matches
the step's intent. But it still requires `name`/`label`/`text` overlap
to find a candidate; an input with ONLY a placeholder and no label
falls through.

**Why this is architectural, not just three brittle locators:** every
coordination point that pre-commits before children make their
decisions creates a chokepoint. The planner is guessing what children
will name things. This is structurally identical to rigid microservice
contracts; the industry pattern there is consumer-driven contracts +
capability discovery, not stricter selectors.

**v6 reconciliation paths (in order of structural depth):**

- **Path A — Capability manifest (cheapest structural fix):** each
  child emits a manifest of what they built (route, form, fields with
  accessible attributes actually present, behavioral postcondition).
  Integration phase synthesizes concrete probe sequences by matching
  planner-intent → child-manifest. Planner stops writing selectors;
  children declare what they made; oracle reconciles. ~500 LOC,
  mostly in `build.py` (manifest emit) + `lead_verify.py` (match+
  generate). Matching itself is one LLM call per integration, not per
  step.
- **Path B — Intent + behavioral postcondition oracle:** restructure
  the journey schema so the oracle's success condition is "behavioral
  outcome occurred" (API call hit, DB row present, expected text
  visible *somewhere*) rather than "literal selector path executed".
  Existing `success_observables` already do this partially but they're
  gated behind the literal locator chain. ~300 LOC reshape of
  `_check_observable` + verdict structure.
- **Path C — LLM-agent oracle:** hand journey description + product URL
  to a Claude agent that discovers the UI like a real user. Robust to
  any UI shape; ~$5–20 per journey, non-deterministic. Reserve for
  the hardest cases (complex flows, a11y regressions); not the
  default.

**Concrete next step for v6 scoping:** write
`research-journey-intent-vs-contract.md` cataloging the journey types
we have today, the contract-vs-reality failures observed across
products (linkboard 8k/16k, iTracker, plus older cases), and a typed
capability-manifest schema proposal. Need to decide whether Path A
graduates to v6 or remains a v7 architecture beat.

**Source incidents:**
- iTracker Opus 2026-05-20 (3 iterations, ended at `partial` —
  `otto_logs/sessions/2026-05-20-211157-47cc57/`)
- Pre-existing semantic fallback was added for resume16k journey-1
  "Name" label drift (commit referenced inline at
  `otto/journey_ui_executor.py:1186`) — that fix is local; the
  architectural gap it patches is still open.

**Memory ref:** add `[[journey-intent-vs-contract]]` when written.

## Verifier / audit

### Audit's LLM-judge integration
**State:** v5's verifier (`otto/lead_verify.py`) runs the project's native
test suite (npm/pytest/cargo/go) plus a browser journey runner if one
exists. It does NOT invoke the existing LLM judge in `otto/audit.py`. For
trivial-to-medium products, native tests are sufficient; for large
products (browser-engine class), the LLM judge's holistic check matters.

**v6 work:** wire `lead_verify.run_verify_for_lead` to call
`otto/audit.run_audit` when `--strict-audit` is set or when the project
has no native tests. ~80 LOC.

### Cumulative project intent
**State:** each v5 task is independent. There's no "project's
INTENT.md" that accumulates user requirements across tasks. Plan-v5 §6
explicitly punted.

**v6 work:** maintain `INTENT.md` at project root; each task either
reads from it (if no inline intent) or appends its intent to it.
Audit at root then verifies against the cumulative intent.

### Mid-run intent amendment
**State:** snapshotted at session start, immutable for the run. Plan-v5 §6.

**v6 work:** detect changes to `intent.md` mid-run; in supervised mode,
prompt user to confirm; in autopilot, defer to a follow-up task.

## UI

### MC tree view
**State:** the run drilldown still shows the v4 stage list. Plan-v5 §7.2
called for a tree-view component showing the task graph (parent → children).
Not implemented.

**v6 work:** React component reading `task_graph.json` via existing API,
rendering as expandable tree with verdict pills, cost, drill-down to
proof packet.

### First-level review modal
**State:** `--review-first-decomp` flag works; `otto review` CLI works.
Plan-v5 §7.3 called for an MC modal that pops when root has emitted
children in supervised mode, allowing accept / edit / replace.

**v6 work:** modal component + WebSocket from watcher to MC for
notifications.

### Build form additions
**State:** today's build form in MC submits via `RunPayload` to the v4
pipeline. Plan-v5 §7.1 called for tier dropdown + manual tasks input.
Not implemented.

**v6 work:** extend build form; add `tier` and `tasks` fields to
`RunPayload`; route to `otto run` instead of `otto run` when tier is
set.

### Spec visualization — PM PRD layer
**State:** structured spec at `<session>/spec/spec.json` carries the
PM-PRD-layer fields (`product_overview.one_liner`, `primary_users`,
`top_level_pages`, `primary_navigation`, `out_of_scope`, `phases`) and
the engineering layer (`core_entities`, `primary_actions`,
`intent_claims`, `behavior_journeys`). Both are JSON; reviewers
currently inspect via `cat spec.json | jq` or by reading the file
manually.

**v6 work:** MC drilldown for a session adds a "Spec" tab rendering
the PM PRD as a clean product brief: one-liner header, user-type
cards, top-level page list with purposes, sidebar + command-palette
preview, out-of-scope list, must-have/should-have/nice-to-have phases
as collapsible groups. This is the artifact a stakeholder would
review before approving the build.

### Spec visualization — engineering PRD layer
**State:** same as above. The engineering layer is the contract
machines check, but humans currently read it as raw JSON.

**v6 work:** MC "Spec" tab also renders the engineering PRD with:
- entity cards (fields + states + primary_actions, expandable)
- a global table of primary_actions with `success_observable` and
  `error_observable` per action
- intent_claims list with `source_line` links back to intent.md
- journeys rendered as numbered steps with `covers_primary_actions`
  pills + `start_state`/`entry_route` chips
- traceability: hover an action → highlight the intent_claims that
  reference it; hover a claim → highlight downstream entities/actions

### Spec diagrams (auto-rendered from engineering PRD)
**State:** the engineering PRD has all the structured data needed for
several diagram types but produces none. Reviewers must reconstruct
the architecture mentally from JSON.

**v6 work:** auto-render diagrams from spec.json + CHARTER.IA:
- **Entity-relationship diagram (ERD):** nodes = core_entities, edges
  = field references (e.g., `issue.assignee → user.id`). Surface
  state machines per entity (the `states[]` list).
- **Action-surface map:** for each action, show which UI surfaces
  expose it (e.g., `issue.create` → [sidebar | command_palette |
  keyboard.C | empty_state.cta]). Highlights "single-surface" actions
  as discoverability risks.
- **Route graph:** routes as nodes, navigation edges between them.
  Marks `entry_route: "/"` as the cold-start root. Marks routes
  unreachable from any nav surface.
- **Journey flow:** for each behavior_journey, render
  `start_state → covers_primary_actions[] → terminal_state` as a
  swimlane with cross-references to action IDs.
- **Phase coverage:** stacked bar showing which entities/actions land
  in must_have vs should_have vs nice_to_have.

Use mermaid or d3 for rendering; both are stable and the data is
small enough that client-side render is fine.

## Cleanup

### Move v4 group/contract synthesis to legacy/
**State:** `otto/spec_compile.py` group synthesis (~800 LOC),
`otto/build.py` group orchestration (~600 LOC),
`otto/build.py::detect_critical_shared_contract_violations` (~600 LOC),
`otto/repair_gates.py` (~300 LOC) all still active. Today's `otto run`
goes through them.

**v6 work:** move to `otto/legacy/`; update imports; gate behind a
`--legacy` CLI flag; document migration path. Coupled to bench results.

### Today's pipeline removal
**State:** `otto run` (v4 pipeline) and `otto run` (v5 pipeline)
coexist. Plan-v5 §10 definition-of-done required `otto run` to route to
v5 by default after bench validates v5.

**v6 work:** flip the default; preserve `--legacy` for one release; then
delete.

## Wall-clock performance & scheduling

Findings from sc6 (today's live run with structured-contract +
PM-PRD-layer code): wall-clock regressed from 49 min → 63+ min vs
yesterday's pre-redesign baseline. Total agent-seconds: 72.5 min with
only ~13% parallelism gain. Codex's independent audit identified
several mitigations. Listed in priority order.

### Cache spec compile by intent hash
**State:** every run re-compiles the spec from intent.md, even when
intent and prompt are unchanged. On sc6, spec compile took 9:57 (vs
yesterday's 1:03) under Claude with the new structured-contract
schema. Retries pay this cost again.

**v6 work:** key spec.json on `(intent.md hash, compile-prompt hash,
provider, model, otto version)`. Reuse if all match. No cross-version
reuse (safety). Lowest-risk fix; ~10 min savings per retry.

### Critical-path-aware sub-decomposition
**State:** sc6's frontend lead sub-decomposed into a serialized chain
(auth → app_shell → views_pair → tests_only_child), serializing ~50
min of agent work that yesterday's inline frontend did in ~20 min.
The lead made the local decision to split correctly — but didn't
optimize the global DAG shape.

**v6 work:** add critical-path discipline to the lead prompt: *"If
your proposed child DAG critical path is > 2 build stages, either
restructure the shared contracts/scaffolds so leaves can fan out, or
inline the dependent chain."* Also: forbid `tests-only final child`
patterns — feature leaves must own their own tests, not pass them to
a final test-only sibling that serializes everything.

**Why deferred:** needs careful prompt language; risk of recreating
the "giant inline blob" anti-pattern if heuristic is too blunt.

### Run full check matrix only at integration nodes
**State:** every leaf session today runs the full 160-row check
matrix against the WHOLE CHARTER.IA, regardless of the leaf's actual
scope. Result: 100+ false-fail checks per leaf (route_resolves /
endpoint_resolves for surfaces the leaf doesn't own), which
correctly downgrade to partial but mislead the reader and burn
compute.

**v6 work:** leaves run only cheap local-scope checks (their own
files, their declared tests). Integration nodes run the full matrix
against the merged code. Saves ~5+ min/leaf and produces cleaner
per-leaf verdicts.

### Investigate Claude compile output explosion
**State:** sc6 spec compile took 9:57 (Claude) vs ~3:30 (codex same
prompt). Claude emitted ~200K total tokens and ~45K output tokens
for the spec; codex emitted similar structure in 1/3 the time. Not
plain "Claude is slow" — this is schema/output explosion. Schema
encourages verbose IDs and per-claim duplication.

**v6 work:** add provider timing metrics (time-to-first-token,
output tokens, validation retries, prompt/output byte sizes). Then
apply caps:
- max intent_claims (≤30)
- terse IDs instead of verbose repeated prose
- journeys cover representative critical flows only, not every surface
- "nice-to-have" contract detail degrades to notes, not matrix rows

If codex consistently produces valid specs in 3:30 with adequate
quality, route compile to codex by default (with claude as fallback).

### Toolchain pre-flight in shared worktree
**State:** every child agent fights `node_modules` install /
Vitest config / `playwright install` overhead during its build. Pure
runner overhead, paid N times.

**v6 work:** runner pre-flights the worktree once (npm install, uv
sync, playwright install) at architect time. Children inherit a
known-good install dir via the existing symlink propagation.

### Per-child context slicing
**State:** every child agent receives the full spec.json (~60KB) +
CHARTER.md (~36KB). Most of that content is irrelevant to the
child's scope. Big context = slower turns + higher cost.

**v6 work:** runner slices spec + CHARTER per child by scope
(filter `core_entities` and `action_surfaces` by what the child
owns). Send only the slice plus cross-reference index. Architect
still sees full content.

### Loosen spec-compile cross-coverage enforcement
**State:** Spec compile takes ~6:31 (v6c), of which ~3:10 (56%) is
agent reasoning before any structured output. Profiling v6c
spec.json shows the time isn't output bloat (core_entities is 42%
of bytes, journeys are 15%, etc. — fine) but the cross-coverage
enforcement in the prompt:

> "every intent_claim must be covered by at least one
> core_entity/primary_action/quality_constraint;
> every primary_action must be referenced by at least one
> behavior_journey"

(per `otto/spec_compile_flat.py:392` validator + the matching
prompt text). The agent mentally cross-checks ~53 claims against
47 fields × 16 actions × 5 journeys. That's the reasoning burden.

**v6 work (efficiency backlog — pair with other perf investigations):**
- Reduce mandatory cross-coverage to a smaller set (e.g.,
  intent_claims that map to a core flow only; not every claim
  must be entity-linked)
- Or: shorten `success_observable` / `error_observable` to
  ≤80 chars (currently agents write paragraphs)
- Or: skip per-field claim mapping; track only entity-level claim
  coverage
- Or: drop the requirement that EVERY primary_action be journey-
  covered; spot-check the critical 3-5 actions
- Estimated target: cut Claude compile from ~6:31 → ~2:00 without
  losing the structural contract

**Why not drop journeys (the cheaper path):** Codex audited and
journeys are 15% of output, not the bottleneck. Removing them
saves modestly while losing useful narrative context.

**Why deferred:** premature optimization until correctness is
fully settled. After v6d/v6e validate that nested decomp + repair
loop reliably land code in main, revisit compile performance as
part of a broader efficiency pass.

### Per-agent-role model tuning (cost/quality optimization)
**State:** Every agent role in Otto today uses the same model
(`sonnet` for Claude per `otto/config.py:142`). All phases — spec
compile, root planner, architect, leaf builds, fix loops,
integration sessions — run on the same Sonnet model. Otto v5
supports per-role CLI overrides (`--build-model`, `--fix-model`,
`--certifier-model`) but defaults treat all roles uniformly.

**v6 work (not critical, file for later iteration):**
- Audit which roles would benefit from a stronger model (e.g.,
  integration agent + final verifier might benefit from Opus —
  they're load-bearing for correctness).
- Audit which roles would tolerate a weaker model (e.g.,
  scaffolding-only architect, syntax-fix-only leaf retries —
  could use Haiku to cut cost).
- Build a per-role model matrix as a config preset (e.g.,
  "balanced" / "quality" / "cheap" profile in otto.yaml).
- Validate empirically: same intent across profiles, measure
  cost/wall/quality.

**Why deferred:** Sonnet uniformly works. Not a correctness gap.
Pure cost/quality optimization. Revisit after the structural
fixes settle and we have stable baselines to measure variance
against.

### Architect CHARTER output size cap
**State:** sc4 CHARTER was 1138 lines; sc6 was 902 lines. Both
include exhaustive prose alongside the structured IA JSON. The prose
duplicates much of what the JSON contract already declares.

**v6 work:** architect prompt instructs: "the IA JSON is the
contract; prose sections explain WHY, not WHAT. Prefer
machine-readable contracts; eliminate prose that just restates JSON
fields." Target ~500 lines max.

## Operations

### Crash recovery
**State:** Otto's existing checkpoint/resume works for v4. v5's runner
writes `summary.json` per task per philosophy invariant. Resume of a v5
session was not exercised under crash conditions.

**v6 work:** kill v5_runner mid-run; verify `otto run --resume <session>`
picks up the task graph and continues. Smoke test.

### Concurrency stress test
**State:** unit tests verify task_graph.json writes are atomic under 4
threads. Live LLM concurrency (2+ `otto run` against same project) is
not tested.

**v6 work:** stress test 4 concurrent live runs; profile contention.

### Provider fallback under live conditions
**State:** unit-tested via mocks. Live codex 402 → claude swap is
plausible but not exercised end-to-end with real provider failures.

**v6 work:** schedule a planned codex-credit-exhaustion test.

## Bench

### 5 reference projects
**State:** plan-v5 §6.6 required bench against finance-dashboard,
microblog, ops-dashboard, acme-expense, brownfield SAML. v5 has been
live-tested on:
- ✅ CSV→JSON CLI (trivial; passed in 99s, $0.17)
- ✅ TODO web app (medium; passed in 174s, $0.50)
- ⚠️ Finance-dashboard live test: in progress / pending verification

The other 3 reference projects are pending.

**v6 work:** finish the bench matrix; publish cost / time / pass-rate.

## Documentation

### Operator runbook
**State:** docs/v5-tier-guide.md and docs/v5-verdict-reference.md exist.
docs/v5-v6-punch-list.md (this file) exists. No operator runbook for
common failures (provider auth, disk full, watcher crashed, etc.).

**v6 work:** ~200 lines of "if X happens, do Y" troubleshooting.

### Architecture deep-dive
**State:** plan-v5 + plan-v5-tabletop in `.runlogs/architecture-debate/`
serve as the design doc. No condensed "how does v5 work" reference for
new contributors.

**v6 work:** `docs/v5-architecture.md` summarizing the recursive Lead +
queue + audit-at-each-merge model in ~5 pages.
