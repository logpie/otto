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

## v2 design principles (the synthesis)

1. **Permissive everywhere**. Schema parses what it gets, coerces
   obvious mistakes, warns instead of rejecting. Hard fail only on
   "literally unusable input."

2. **Spec is a living document**. Mutable during the run. Build agents
   can amend deps, owned_paths, slice decomposition, even checks.
   Every amendment is hash-chained for audit.

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
2. **Mutable spec during build**. Implement amendment requests from
   build agents. Test the feedback loop.
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
