# Otto v2 — design notes for the next-version redesign

Date: 2026-05-03
Branch: `cc-i2p-2`
Status: Forward-looking notes; NOT scoped for v1 PR

This document captures architectural and prompt/schema brittleness
identified during the v1 (Phase A) build-out and Microfeed bench
iteration cycles (R3–R26+). v1 is a working frame that reaches 5/5
parity on the bench. v2 is the redesign that addresses the brittle
assumptions surfaced in v1.

The header observation that anchors everything:

> **Every time we converted a "rule" into a "flexible mechanism",
> reliability went up.** Strict scope rule → soft warnings: passes
> went up. Strict schema validation → permissive parsing (proposed):
> would eliminate cheap-fail rounds. The pattern is consistent.

v1 still has a lot of strict rules. v2 should systematically replace
them with generalizable mechanisms.

## Two layers of brittleness

### Layer 1 — prompt/schema strictness (in v1, partially addressable)

Surface-level. The agent emits something the validator rejects, or the
prompt has accumulated 250+ lines of "don't do X" warnings. Symptom:
cheap fails at compile/parse stage; LLM-output-shape brittleness.

We mitigated some of these in v1 (free-form `request_shape`/
`response_shape`, empty `owned_paths` allowed, soft scope warnings),
but the pattern persists across many fields.

### Layer 2 — system design strictness (deferred to v2)

Structural. The pipeline's shape itself encodes assumptions that don't
generalize: spec frozen at compile, mandatory slice decomposition,
sequential build, all-LLM execution, hardcoded retry counts. These
aren't fixable by prompt tweaks; they need architectural redesign.

---

## Layer 1 — prompt/schema findings (v1 mitigations + v2 generalizations)

### Findings ranked by impact

#### F1. Webapp schema requires `routes` + `components` always
Caused R3, R5, R8 cheap fails. v1 mitigation: tightened prompt + relaxed
some sub-fields. v2 generic fix: schema becomes advisory; validator
returns warnings, not errors, for missing recommended fields. Hard
reject only if spec is unusable (no slices, no intent, no project_kind).

#### F2. `state_invariant.expression` must be parseable Python
Caused R17. v1 mitigation: 15-line prompt clarification with examples
+ counter-examples. v2 generic fix: `expression` is free-form. Runtime
tries Python `eval` first; if SyntaxError, falls back to LLM-judge of
"is this invariant satisfied" or records as informational. Agent
doesn't need to write Python.

#### F3. `amendments` field rejecting non-objects
Caused R22 cheap fail. v1 mitigation: 6-line prompt note. v2 generic
fix: parser coerces. Non-dict entries become
`{"reason": str(entry), "actor": "compile-agent", "ts": ...}`. Spec
parses; warning surfaces.

#### F4. Slice ID regex `^[a-z][a-z0-9_-]*$`
The strictest cosmetic rule with no real rationale beyond "worktree-
friendly slugs". Rejects `Auth Slice`, `posts_v2`, `Slice 1`, etc.
v2 generic fix: accept any non-empty unique string; slugify internally
for paths. Spec author shouldn't think about regex.

#### F5. "Each slice must declare at least one check"
Same shape as the (now relaxed) "must declare at least one
owned_paths" rule. Forces an output the agent might not have natural
reason to emit. Some slices are purely structural. v2 generic fix:
`checks: []` allowed. Slice with no checks vacuously passes. Audit's
contract gate still verifies the integrated product.

#### F6. The compile prompt has grown to ~250 lines of "don't do X"
Cumulatively: a strict spec of acceptable LLM output. Each line was a
real failure shape. Now: brittle to novel mistakes. v2 generic fix:
short prompt + permissive parser + behavior tests. Prompt describes
the artifact's purpose, not failure modes.

#### F7. The build agent prompt lists peer paths as "Owned by other slices"
v1 mitigation: softened from FORBIDDEN to "warning territory". Section
is still 30 lines per slice — ceremony per build invocation. v2 generic
fix: drop the section entirely. Build agent doesn't need to know about
peers; the runtime detects warnings post-hoc. Prompt becomes: "build
slice X. Tasks: A, B, C. Checks: D, E. Existing project files:
F, G. Done."

#### F8. ApiProbe is "DEFERRED"
Right now we instruct the agent to NOT emit `api_probe` because the
runtime can't satisfy it (no server-boot). v2 fix: implement
server-boot lifecycle in the check runtime. ApiProbe becomes a
first-class check kind. This is engineering, not prompt-engineering.

#### F9. `project_kind` enumeration
Restricted to `webapp | cli | library | api`. Real projects are often
hybrid. v2 fix: open enum + per-slice kind override. Default to
"webapp" for unrecognized; let the agent supply free-form description.

#### F10. `shared_scaffold` as a list of glob strings only
Strict typed format. Agent might want `{"path": "models.py", "reason":
"extension by all slices"}` — more informative but rejected by parser.
v2 fix: accept either form (string or `{path, reason}`); coerce.

### v2 principle for layer 1

**Replace strict validation with permissive parsing**:
- Required fields with sensible defaults (intent → "" if missing;
  project_kind → "webapp" if missing; slices → [] if missing).
- Coerce obvious mistakes (non-array slices field → wrap in array;
  non-dict amendments → coerce; missing checks → empty list).
- Log warnings for departures from recommended shape, NOT errors.
- Reject hard only when the spec is unusable: no slices at all
  (literally nothing to build), or schema_version mismatch.

The compile prompt simplifies to: short description of the goal, one
example of a valid spec, structural fields' purposes. No "Critical
Rules" sections, no counter-examples, no enumeration of forbidden
patterns. Trust the parser.

### R26 confirmation (added after the v1 trajectory closed)

R26 (the round immediately following the soft-warning refactor land)
went 0/7 slices landed despite the soft-warning model working
correctly. Cause: the compile agent emitted a `state_invariant.expression`
as English prose ("App shell has create_app factory and database
setup") — exactly the F2 failure mode. R17's prompt clarification
didn't take this round.

R26 confirms F2 is a real, recurring brittleness. Soft-warning fixed
the scope-rule layer; F2 is the next strict-rule that needs the same
permissive treatment. Specifically: when `state_invariant.expression`
isn't parseable Python, the runtime should NOT fail the slice. Either:

- Treat as informational (log the description, mark check passed) so
  the slice's other checks decide its fate, OR
- Fall back to an LLM-judge that reads the description and inspects
  the project state (more powerful but adds an LLM call).

Either is consistent with the v2 principle: **trust the test-based
safety net, don't layer rules on top that block work the tests would
have validated**.

Empirical pass rate post-soft-warning, pre-F2-fix:
- R25: PASS 5/5
- R26: FAIL (state_invariant prose) 0/7 with all evaluators PASS

The 50% rate is exactly the "compile agent output variance" failure
mode, made concrete. F2's permissive parsing would close it.

---

## Layer 2 — system design findings (deferred to v2)

These are structural commitments that don't generalize. Each one
generates a class of failures rather than a specific failure shape.

### S1. Spec is frozen at compile time

**The biggest one.** Compile runs once, produces a spec, and that
spec is treated as gospel for the rest of the run. Every downstream
stage (build, merge, audit) reads it, never modifies it.

When compile is wrong (slice decomposition, dep graph, owned_paths),
the whole run is wrong. The only recovery is: throw the spec away,
recompile from scratch.

The amendment infrastructure (`Spec.amendments` with hash-chained
edits) already exists for this exact case. v1 only uses it as
documentation. We don't have a feedback path from "this slice keeps
tripping the same scope warning" to "maybe the deps need updating."

**v2 generic fix**: spec is mutable during the run. Build agents can
request "split this slice", "add this dep", "move this file to
`shared_scaffold`". The amendment chain records why. Compile becomes
initial guess, not contract.

### S2. Mandatory slice decomposition

Every product, no matter how small, goes through "compile produces
slices." A 50-line CLI script gets decomposed into a slice. A trivial
bug fix gets the full pipeline.

The slice abstraction is load-bearing for big multi-feature products
but pure ceremony for small or non-decomposable work. We pay compile +
per-slice build + merge + audit for things that should just be "agent
does the work, system verifies, ship."

**v2 generic fix**: slice decomposition is optional. If
`spec.slices == []` or `len(slices) == 1`, the build phase is a
single agent invocation; merge is a no-op; audit verifies. The
pipeline auto-collapses for small work.

### S3. Sequential build execution

We have a dep DAG. We use it like a totally-ordered list.

Wall time scales linearly with slice count. A 7-slice spec = 7 LLM
calls in sequence, even when 5 of them have no shared deps and could
run concurrently.

**v2 generic fix**: parallel where deps allow. The dep graph already
declares concurrency-safety; the build orchestrator should use it.
Conflict resolution at merge time when parallel slices touch shared
scaffolds.

### S4. All build is LLM

Every slice = an LLM agent invocation. Code generation, file
scaffolding, dependency install, running migrations — all LLM.

Some build steps are deterministic and don't need an LLM:
- `npm install`, `pip install -r requirements.txt`
- Copying a template directory
- Running migrations against a schema
- Formatting code (`black`, `prettier`)
- Regenerating bindings from a proto file
- `git add . && git commit -m "..."`

Wrapping these in LLM calls adds variance and cost.

**v2 generic fix**: a slice's "build" is ANY callable that produces a
diff. LLM is one option; bash scripts, codemods, code generators,
package-installers are others. The system is agnostic to the
implementation.

```python
# v2 sketch
@dataclass
class BuildAgent:
    kind: Literal["llm", "bash", "template", "codemod", ...]
    payload: dict  # kind-specific config
```

### S5. Build-agent-owns-merge conflation

Each slice's build agent also handles its merge (rebase, conflict
resolution, post-land verification). Two skills wedged into one agent.

The build agent has the slice's domain context. The merge agent needs
cross-slice context (what other slices touched these files, what
conflicts mean semantically). These are different jobs.

**v2 generic fix**: dedicated merge resolver. Build agent produces a
clean branch; a separate (smaller) merge step handles integration. This
is conventional in real CI/CD; we should match it.

### S6. One-shot compile

The compile agent has ONE shot. It reads the intent, emits a spec.
Done. If the spec is wrong, build fails.

Compile is making decisions (slice decomposition, deps, structure)
without seeing the actual implementation challenges. It could iterate:
"I tried this decomposition; it's hard; let me try this other one."
v1 doesn't allow that.

**v2 generic fix**: compile is invoked when needed, not just at start.
Build agents can request a recompile of subset of the spec ("the
decomposition is wrong; please redo just the slices below `accounts`").
The amendment chain captures the iteration as a series of
spec-evolution events.

### S7. Audit produces one verdict for everything

Audit returns `PASSED` / `PARTIAL` / `BLOCKED`. One value for the
whole product.

A product with 8 features working and 1 not — what's the verdict?
"Partial" — but the user wants the 8 features shipped, plus a roadmap
to fix the 9th. We collapse multi-dimensional reality into a single
label.

**v2 generic fix**: per-capability verdicts. The audit emits per-story
or per-slice judgment. The system can ship the working subset and
surface the rest as known limitations. The proof packet renders both
the overall summary and the per-feature drill-down.

### S8. No session continuity

Otto sessions are independent. Each `otto run` is single-shot. There's
no concept of "Otto has been maintaining this product across 5
sessions."

Real product development is iterative. Spec evolves over time. Otto's
single-shot model fits "spec → product" but not
"spec → product → user feedback → spec' → product'".

**v2 generic fix**: Otto sessions chain. A new session reads the
latest landed spec + audit verdict + proof packet, treats them as
input. Builds incrementally. The amendment chain spans across sessions.

### S9. Hardcoded retry counts

`per_slice_retries=3`, `audit_retries=2`. Arbitrary numbers. Why 3,
not 5? Why 2, not "until cost budget exhausted"?

**v2 generic fix**: bound by progress, not count.
- Stop when no measurable improvement between attempts (e.g., the
  same error reappears verbatim).
- Or by cost ceiling (`per_slice_cost_usd`, `total_run_cost_usd`).
- Or by an LLM judging "more iteration would help" / "stuck in loop."

### S10. Single `project_kind` per spec

`Spec.project_kind` is one of 4 values. Real projects are often
hybrid: a webapp WITH a CLI tool WITH library exports.

**v2 generic fix**: `project_kind` is per-slice. The spec-level
`project_kind` becomes a hint for the overall vibe, not a constraint.
Different slices may carry different kinds (CLI slice, webapp slice,
library slice).

---

## Safe mutability — preventing agents from hacking the spec

The mutable-spec idea (S1) opens a real attack surface: agents will
discover that "amend the spec to make my work valid" is easier than
"actually do the work correctly." If we just let any agent amend any
field for any reason, agents will:

- Expand `owned_paths` to retroactively legalize over-reach
- Remove or weaken slice checks that were going to fail
- Add themselves as transitive deps of every other slice
- Rewrite `done_means` to remove items they couldn't deliver
- Rewrite the intent itself to match what they built

**The principle**: not all parts of the spec are equal. **Intent is
immutable. Common guardrails are immutable. Only slice-local fields
are agent-mutable, and even those are bounded.**

### Three tiers of immutability

| Tier | Fields | Mutation rule |
|---|---|---|
| **1 — Bedrock** | `intent`, `intent_hash`, the user's verbatim request | IMMUTABLE. Hashed at session start. Verified at every persist_spec. Any attempt to change → hard reject + run blocked. |
| **2 — Locked guardrails** | `project_kind`, `done_means`, `non_goals`, `cross_slice_checks`, `test_command` (in otto.yaml), `slice.id`, references to private evaluators | LOCKED. Can only be amended by USER via spec-review gate, never by agents. Compile sets them once; they stay until human edits. |
| **3 — Slice-local** | A slice's `deps`, `owned_paths`, `tasks`, `shared_scaffold` (collective), the slice's checks (only TIGHTENING allowed) | MUTABLE by agents via `append_amendment()`, with the constraints below. |

The asymmetry: the LOOSER the constraint (tier 3), the more bounded
the agent's scope. They can refine their own slice's understanding;
they can't escape the universal contract.

### Constraints on tier-3 amendments

Even slice-local amendments are bounded:

1. **Scope-limited.** An agent can amend ONLY its own slice's fields.
   Modifying ANOTHER slice's deps/checks/owned_paths requires the
   other slice's permission (which agents don't have a way to grant).

2. **Checks can only be tightened, not weakened.** Adding a new check
   is allowed; removing an existing one is not; changing a check's
   behavior to be more permissive (e.g., turning `expected_status: 200`
   into `expected_status: 500`) is not. Strictly: amendments to checks
   are append-only. Removals are blocked at amendment time.

3. **Reasons must be linked to trigger events.** Every amendment must
   carry a `trigger_event_id` referencing a real journal event (a
   check failure, a scope warning, a build error). An amendment with
   no trigger event is suspicious and the audit will flag it.
   (Storage: amendment.trigger_event_id; journal events have
   stable IDs.)

4. **Hash-chained tampering detection.** Already implemented in v1 —
   `diff_sha256_before` and `diff_sha256_after` form a chain. Breaks
   in the chain indicate tampering. v2 adds: the audit verifies the
   full chain at end-of-run; broken chains BLOCK the verdict.

5. **Amendments are visible in the proof packet.** The human reviewer
   sees the full amendment chain with reasons + trigger events. If
   the chain looks adversarial (10 amendments removing checks, agent
   names suspicious), the human can reject the run.

### Audit-time defenses (independent of agent claims)

Even if an agent escapes the per-amendment rules, three independent
checks catch the cheating:

#### D1. The contract test is the truth

`test_command` lives in `otto.yaml` (LOCKED, tier 2). The audit ALWAYS
runs it against the integrated worktree at end-of-run. If the contract
test fails, the verdict cannot be PASSED, regardless of what the spec
says or what the LLM walkthrough thinks. Agents can amend the spec all
they want; if the integrated product doesn't satisfy the project's
own declared test, the run is BLOCKED.

This is the single most important defense. It's already implemented in
v1 (audit's contract gate fix, root-cause #2). Carries over to v2
unchanged.

#### D2. Private evaluators are outside the agent's view

The bench's hidden + browser evaluators are kept under
`bench-results/<run_id>/private-evaluator/` — outside the project repo,
outside any agent's worktree, never seeded into anything an agent
reads. Agents cannot game them because agents don't see them.

This is a project-author responsibility: keep the truth-evaluators
external. Otto's bench harness already does this; production users
should follow the same pattern (e.g., CI runs an external eval against
the merged result).

#### D3. The audit reviews the amendment chain

At end-of-run, the audit inspects every amendment:

- Does each amendment have a trigger event reference?
- Does the trigger event actually exist in the journal?
- Did the amendment weaken a check (removed, made more permissive)?
- Is the cumulative effect of amendments suspicious (e.g., 80% of
  checks were amended, mostly to lower expectations)?

If yes to any of these red flags, the audit verdict is at most
PARTIAL (never PASSED), and the proof packet surfaces the suspicious
amendments prominently.

This is a new v2 component (no v1 equivalent). It's an LLM
amendment-reviewer, but a SCOPED one — it judges only the amendment
chain, not the spec or the product. Limited surface, limited
brittleness.

### Concrete amendment flow under v2

When a slice's build agent finds a problem (e.g., scope warning fired
on `routes/social.py`):

1. Agent decides: "I need `social` in my deps to legitimately extend
   that file." Calls `request_amendment(slice="posts", deps_add=["social"],
   reason="follow data needed for timeline rendering",
   trigger_event_id="2026-05-03T16:35:12-scope-warning-456")`.

2. The runtime validates:
   - Slice "posts" amending its OWN deps → tier 3, allowed.
   - Reason is non-empty. ✓
   - Trigger event exists in journal. ✓
   - Hash chain extends correctly. ✓
   - `social` exists as a slice. ✓

3. Amendment persisted; spec updated; `posts.deps` now `[..., "social"]`.

4. The slice's next check run sees the updated deps; the modification
   to `routes/social.py` is no longer a scope warning.

5. End of run: the audit sees the amendment in the chain, verifies
   trigger linkage, judges plausibility. If clean, PASSED is allowed.

### What this design is NOT

It's NOT trying to make the spec immune to all bad-faith agents.
That's impossible. Sufficiently determined agents could:

- Skip the amendment flow and modify spec.json directly on disk
  (broken hash chain — caught at next persist_spec).
- Construct a fake journal event with a fake trigger ID (audit can
  cross-check journal integrity, but not 100% defense).
- Argue compelling-sounding reasons that aren't true (audit reviews;
  human can override).

The design is a defense-in-depth against the COMMON failure mode:
LLM agents taking the path of least resistance, which is amending the
spec to declare success rather than doing the work. The contract test
+ private evaluators are the real moats; the amendment rules are the
inner walls that keep agents pointed at honest work.

### Mapping to current v1 fields

| v1 field | v2 tier | v2 mutation rule |
|---|---|---|
| `Spec.intent` | 1 | Immutable. Hashed at session start. |
| `Spec.project_kind` | 2 | Locked at compile; user-only edit via review gate. |
| `Spec.shared_scaffold` | 3 | Slice agents can propose adding paths, with reasons. |
| `Spec.cross_slice_checks` | 2 | Locked. Compile sets; user can edit. |
| `Spec.non_goals` / `done_means` | 2 | Locked. |
| `Slice.id` | 2 | Locked once set (rename = drop + re-add, audit-visible). |
| `Slice.title` | 3 | Slice agent can refine. |
| `Slice.deps` | 3 | Slice agent can ADD; remove requires user. |
| `Slice.owned_paths` | 3 | Slice agent can refine; cannot expand into another slice's territory unilaterally. |
| `Slice.tasks` | 3 | Free-form, agent-editable. |
| `Slice.checks` | 3 (append-only) | New checks allowed; removal/weakening blocked at amendment time. |

### Implementation cost

This is meaningful work but not enormous. Approximate:
- Add `intent_hash` field + verification in persist_spec (~30 LOC).
- Add `mutable_by` enum on Spec fields (~20 LOC).
- Add `request_amendment()` API that validates tier rules
  (~80 LOC + tests).
- Add `trigger_event_id` to Amendment + journal event ID system
  (~50 LOC).
- Add audit-time amendment review (~150 LOC including the LLM call).
- Update existing tests for new flow (~50 LOC).

Total: ~400 LOC + ~100 LOC tests. Self-contained module
(`otto/spec_amend.py`). Most code is enum/validation; the LLM bit is
small.

---

## v2 design principles (the synthesis)

1. **Permissive everywhere**. Schema parses what it gets, coerces
   obvious mistakes, warns instead of rejecting. Hard fail only on
   "literally unusable input."

2. **Spec is a living document, but tiered**. Intent is immutable;
   global guardrails are locked (user-only edit); slice-local fields
   are agent-mutable with constraints. Every amendment is hash-chained,
   linked to a trigger event, and audit-reviewed at end-of-run. The
   contract test + private evaluators remain the truth, independent
   of any spec amendment. (See "Safe mutability" section above.)

3. **Pipeline scales by complexity**. Single-slice mode collapses to
   "one agent, one verification." Multi-slice mode uses the full
   compile→build→merge→audit pipeline.

4. **Build is pluggable**. LLM is one of several `BuildAgent` kinds.
   Deterministic operations stay deterministic; only judgment-required
   work calls the LLM.

5. **Verdicts are multi-dimensional**. Audit produces per-capability
   judgments. The proof packet renders both summary and detail.

6. **Sessions chain**. Otto can maintain a project across multiple
   runs. Spec evolves; product evolves; audit reflects progress.

7. **Bounds by progress, not by count**. Retries stop when iteration
   stops paying off, not at an arbitrary number.

8. **Trust the test-based safety net**. Real damage is caught by
   slice checks + cross-slice checks + audit's contract gate. Don't
   layer rules on top that block work the tests would have validated.

9. **No new strict rules**. Every "don't do X" prompt warning is
   technical debt. Prefer permissive parsing + post-hoc warnings.

10. **Observability everywhere**. Continuous proof packet (live JSON);
    per-stage event journal; agent narratives surfaced unfiltered.

---

## What v1 (this PR) ships

- 4-stage pipeline (compile → build → merge → audit → render).
- Single-shot, single project_kind, sequential build, frozen spec.
- 17 root-cause fixes from real Microfeed bench failures, all with
  regression coverage.
- Soft-warning scope rule (the one rule simplification we landed in
  v1, in response to the user's pushback that strict rules over-
  constrain agents).
- `~38 commits` worth of working frame; reaches 5/5 parity on Microfeed.

The architectural brittleness above is **not** addressed in v1. v1 is
a working frame for the v2 redesign — useful as a testbed, not as a
final shape.

## What v2 should NOT do

Don't try to fix everything at once. The brittleness is structural;
piecemeal fixes will fight each other. v2 should be a deliberate
redesign with the 10 principles above as foundation.

What v2 explicitly should NOT include:
- More strict rules. We've shown they don't generalize.
- More LLM critique stages. Adding another LLM is more brittleness.
- More retry-based recovery. Bound by progress, not count.
- More schema validators. Permissive parsing instead.

## Suggested v2 implementation sequence

1. **Permissive parser** for spec.json. Replaces the strict validator;
   most cheap fails go away. Run the bench; reliability goes up.
   Includes F2 fix specifically: state_invariant.expression non-Python
   becomes informational, not slice-blocking. (Prevents the R26 cascade.)
2. **Tiered mutable spec during build**. Implement
   `request_amendment()` API with the three-tier mutation rules.
   `intent_hash` immutability check. Trigger-event linkage on every
   amendment. Audit-time chain review. (See "Safe mutability" section
   for detailed design.) Test the feedback loop on a real run where
   compile gets the dep graph wrong and the agent self-corrects.
3. **Auto-collapse pipeline for tiny products**. Single-slice mode.
4. **Pluggable build agents**. `BuildAgent.kind` → dispatch to
   appropriate implementation.
5. **Parallel build execution**. Use the dep DAG.
6. **Per-capability audit verdicts**.
7. **Session continuity**.

Each step is independent enough to be a separate PR. The first
(permissive parser) is the highest-leverage and lowest-risk; do that
first as a soft handoff from v1 to v2.

---

## Acknowledgements

Most of these findings came from one of two sources:

1. **The Microfeed bench iteration cycle** (R3–R26+). Each failure
   shape revealed something about the system design that was wrong or
   over-constrained.
2. **User pushback during the iteration**. The shift from strict scope
   rule to soft warnings was driven by the user pointing out that
   strict was the most brittle option. The wider audit (this document)
   was prompted by the user asking what other parts of the system have
   the same shape.

The pattern across all of them: **whenever we tried to constrain the
agent's output, the system got more brittle. Whenever we relaxed and
trusted post-hoc verification, reliability went up.** v2 is the
generalization of that lesson.
