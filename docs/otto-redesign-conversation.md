# Otto redesign — conversation transcript

This file captures the design discussion that produced the new Otto
mental model (Feature / Group / Guardrail / Audit / Proof). Written so a
future session can re-enter the design with full context.

Date: 2026-05-04. Branch: `cc-i2p-2`. Driver: user (logpie).

---

## Trigger — RUA found a broken detail panel

User opened MC drawer for a passed i2p run on `p7-shortener`. Stories
column showed `-`, Changes showed `-`, Checks listed two WARN items
("No story pass count was recorded", "No changed files were detected"),
Work timeline showed `Build / Certify / Proof` as Skipped, Proof button
routed to a legacy endpoint returning "No media recorded."

Every line of the panel was wrong because the legacy panel was built
for the `otto build` queue model and i2p runs were welded onto it. My
first response was "make the panel domain-aware." User pushed back:

> "what do you mean 'domain-aware'. do we have other modes other than
> i2p??"

That question exposed the design fork. There are five `domain` values
today (build, certify, merge, improve, i2p). Per the design plan,
build/certify/improve are all going away in Phase B/C — they're legacy.
"Domain-aware branching" frames the work backwards: it bakes in tech
debt that gets removed in a few weeks. Right framing: build the
i2p-native panel; treat legacy as the temporary residue.

User followed up: "first, let's shoot for the ideal state directly in
terms of design, impl and test. second, what's the user flow for the
ideal state? how does otto and mc work? do we still have otto
build/certify/improve etc.??"

I sketched the ideal state — one verb (`otto run`), one panel (the i2p
detail drawer), legacy commands deleted in Phase C. User then asked the
load-bearing questions that drove the rest of the conversation.

---

## Q1 — Slice as atomic unit?

User:

> "so, a slice becomes an atomic unit instead of the task? If so, that
> seems like mainly for a greenfield project? What if users want to
> manually add a slice or manually add a task under a slice? What if
> users want to certify each slice rather than waiting until the entire
> project is built? Relatedly, do we have proof of work for each slice?"

My answer (compressed):

The model conflated three things:
- **Slice as a planning unit** (a vertical of capability)
- **Slice as a parallel-dispatch unit** (worktree, branch, agent)
- **Slice as a verification unit** (where verdicts attach)

The atomic unit of *value* is a capability — one acceptance criterion
with a verdict and evidence. Slice is a coordination unit. Task is the
agent's internal todo list.

Greenfield-only: yes, today's design optimizes for greenfield. Brownfield
needs (a) compile mode that reads the working tree, (b) tightly-scoped
owned_paths, (c) capability-first MC surface.

Manual additions: capabilities and slices yes (in spec review); tasks
no (agent's internal scratch).

Per-slice cert: two flavors. (i) Pre-merge per-slice audit gate as
opt-in flag. (ii) Post-hoc per-capability re-audit as the natural
primitive (`otto run --audit ... --capability <id>`).

Proof per slice: per-slice and per-capability data already exist in
`proof-packet.json`. What's missing: per-slice/per-capability rendered
HTML endpoints, per-slice cost/wall, audit captures tagged by capability.

---

## Q2 — User owns the plan, or Otto owns the plan?

User:

> "we definitely just need one system for supporting different plans.
> Imagine it's a hybrid: we support a fully automated mode where OTTO
> owns the plan, but users can also provide their own plan or edit
> existing ones by adding do's and don'ts on top of OTTO's suggestions.
> ...this requires a good UI/UX design."

User's example: "If I say I want to build a doc editor, OTTO can
suggest decomposing the intent to different product slices (or just
one). Then for each slice it decomposes into capabilities (previously
called task???), e.g. doc viewer needs commenting, MD rendering. Then,
users can add/remove/edit on top of those, e.g. add image upload, add
guardrails like 'don't support videos yet.' These could be checkboxes
that work at the intent/feature level."

User then said:

> "I'm a little lost now. We have so many concepts—slices, capabilities,
> and tasks—and I'm not exactly getting what these are anymore."

And asked five sharp questions:

1. **Primary surface:** if capability is what we surface, what is a slice?
2. **Observability:** if a slice is a product surface, why would we lose
   observability into it?
3. **Manual revisions:** when a user adds image upload, do we dispatch
   a slice or a capability? what about infra work?
4. **Execution:** what's the unit of concurrency?
5. **Tasks:** if not exposed, what's their definition? why need them?

Plus the audit-vs-certifier-vs-proof confusion: "How do we differentiate
them? Which should users care about? Which one affects our loop?
...should video walkthrough be at capability level, slice level, what
level?"

My answer (compressed):

**Restart from user's mental model.** What users see when reading a plan:
features they want, groups Otto plans to build them in, guardrails (don'ts).
That maps to:

| User-facing | Meaning |
|---|---|
| Feature | What the product does. Has a verdict. The user's concern. |
| Group | A bundle of features Otto plans together because they share files. |
| Guardrail | A "don't" pinned to the spec. |

Tasks disappear as a user concept. They're the agent's internal todo
list — same as Claude's scratchpad — never user-facing.

Answers to the five questions:
1. **Surface:** Both. Verdict header zooms out (8/9 features). Body
   zooms in to features. Group expander is one click below.
2. **Observability:** No loss. Click a Group → see agent narrative,
   diff, repair history. Same data, two zoom levels.
3. **Manual revisions:** User adds a Feature. Otto decides if it lives
   in an existing Group (file overlap) or spawns a new one. User
   doesn't see the decision; they see "added to Editor surface." Infra
   work like "add a smoke test" is also a Feature — its evidence is
   `RepoTestCheck` instead of browser walkthrough.
4. **Concurrency unit:** Group. Each gets its own worktree, branch,
   agent. Inside a Group the agent runs serially.
5. **Tasks:** Agent's private todo list. Word is overloaded; retire
   it from user surfaces. Internally, "agent steps" or "todo items."

**Audit vs Certifier vs Proof:**
- Certifier: legacy word, drop everywhere.
- Audit: the verb. The judging pass after build. LLM-judged.
- Proof: the noun. The artifact produced by Render at end of run.
- Users care about Proof (the artifact). They don't watch audit happen.
- Loop: there's no separate "build → certify → fix" cycle. Each stage
  has its own internal retry. Build retries on `check_evidence`
  failures. Audit retries on Feature failures.

Walkthrough level: per-Run by default with Feature anchors;
per-Feature on demand via re-audit.

---

## Q3 — Feature-level proof, retry depth, brownfield routing, vocab

User:

> "If feature is the unit users care about, then it's better to support
> proof of work for the feature, right?
>
> i am worried that waiting until everything is built to generate proof
> of work would be huge, and we might not be able to pin down what users
> actually care about. On the other hand, for a feature-level proof, we
> should probably try best effort to show it. If it fails, perhaps we
> shd not retry hard in an inner loop, because I'm worried that the
> system will become really slow if we have too many inner loops. But i
> am also debating, bc if we only do outer-loop retries at the final
> built-product level, then fixing or proofing certain features might
> be much harder. I'm not sure; we need a balance here.
>
> regarding the group concept, since it is a group of features, users
> can still see the grouping and the actual logs inside that group
> execution, right? Since group is an execution unit, for brownfield
> when a user adds a new feature, that is logically a group or do you
> route to an existing group? if routing, since prev features have been
> implemented, what's the exec unit now? is the group gonna be re-done
> or we have targeted feature impl? relatedly, We need to see the
> actual actions/logs of the agent running to implement that feature.
> In this case, it feels like one feature equals one group. but then
> this mental model seems to be a conflict with the group concept.
>
> Finally, I would strongly suggest you align those terminologies for
> both the user-facing side and the internal implementation. Although
> they can differ to some extent (some terminology can be shielded with
> users) but lets try not to have different namings for the same
> concept internally; ow it's harder to reason, discuss or debug."

My answer (compressed):

**Feature-level proof: yes, primarily.** Per-Feature blocks under
`proof/features/<feature-id>/`. Whole-product packet aggregates per-
Feature blocks plus integration-level checks. One audit pass produces
per-Feature evidence by tagging walkthrough actions.

**Two retry layers, no more:**
- Layer 1 (Check loop) — deterministic, fast, inside Build. Per-Group
  agent retries on failed `check_evidence` (RepoTestCheck, ApiProbe,
  StateInvariant, focused BrowserJourney). LLM-judgment-free.
- Layer 2 (Audit loop) — semantic, expensive, after Audit. One repair
  attempt per failing Feature, route to its Group's agent, re-audit
  only the affected Features.
- No Layer 3. Quality findings are informational, never blocking.
- All counts/timeouts/budgets exposed as config; nothing hardcoded.

**Group is logical first, dispatch second:**
- A Group is a product vertical (name, description, contained Features) —
  user-facing.
- It has a dispatch plan (one or more execution units, file-ownership
  rules) — internal.
- Logical layout often correlates with file-ownership but doesn't have
  to. Comments and Image upload may both touch `templates/edit.html`
  (file overlap) but are different verticals (logical split).
- Logical wins for layout (spec-review presents Groups as verticals).
  File-ownership wins for dispatch (groups can split internally for
  parallelism, or serialize via merge queue).

**Brownfield routing:**
- Modifying existing Feature → re-dispatch existing Group with focused
  intent. Same agent, same branch, surgical edits.
- New Feature, new files → new Group, fresh agent.
- New Feature, overlaps existing files → new Group with extended
  `owned_paths`, merge queue serializes the land.

**Unified vocabulary** (single set of words used everywhere — code, UI,
prompts, file paths, debug output):

| Term | Meaning |
|---|---|
| Intent | What the user wants. Free text. |
| Spec | Compiled plan: Features + Groups + Guardrails + structure. |
| Feature | One unit of value. Has a verdict. The user's concern. |
| Group | Logical product vertical with a dispatch plan. |
| Guardrail | A "don't" pinned to the spec. |
| Audit | The verification stage. Verb. |
| Proof | The final artifact. Noun. |
| Stage | Compile / Build / Audit / Render / Land. |
| Run | One end-to-end session: intent → proof. |

Retired everywhere: slice, capability, capability_verdict, task (user-
facing), certifier, story, acceptance check.

---

## Q4 — What is Render? Per-Feature proof confirmed?

User:

> "what is 'render'? also to confirm, do we have proof of work per-feature?"

My answer (compressed):

**Render = the publish stage.** Reads everything earlier stages produced
(spec, audit, group logs, state) and writes the Proof packet — HTML +
JSON, top-level + per-Feature, plus assets. Pure deterministic
transformation, no LLM. Re-runnable on old sessions (`otto render
<session-id>`) to fix presentation without re-auditing.

**Per-Feature proof: confirmed first-class.**
- Path: `proof/features/<feature-id>/proof.{html,json}`
- API: `/api/sessions/<id>/features/<feature-id>`
- Anchors in whole-product packet: `proof-packet.html#feature-<id>`
- Each per-Feature block contains: verdict + detail, walkthrough
  segment, screenshots/DOM, deterministic checks, Group/files info,
  repair history, audit narrative excerpt, spec context.

**Required to make it real:**
- Audit must tag every walkthrough action with the Feature(s) it
  evidences. Today's monolithic walkthrough log changes to emit
  `{feature_id, action, evidence}` records.
- Build records which Group built which Feature (already in spec).
- `check_evidence` gains a `feature_id` field per check.
- Audit screenshots tagged with the Feature.
- Render does the slicing.

---

## Q5 — Dump and roll forward

User:

> "Yes. Now, first, let's dump our multiple exchanges about the entirely
> new OTTO design to a file so that you can refer to them later. Then,
> let's roll out all our agreed discussions (concepts, work units,
> designs, ui/ux, vocab etc etc) into research.md, update whatever plan,
> and go ahead with Sketching"

This file is the dump. `research.md` (project root) is the rewritten
research doc. `plan.md` (project root) is the implementation plan.
`docs/otto-wireframes.md` is the screen sketches.

---

## Decisions captured (normative)

These are the design decisions agreed across the conversation. They
override anything in older docs.

### Vocabulary (unified, used everywhere)

- Intent — user input, free text
- Spec — compiled plan with Features, Groups, Guardrails, structure
- Feature — atomic unit of value with verdict and evidence
- Group — logical product vertical with internal dispatch plan
- Guardrail — pinned negative scope ("don't")
- Stage — Compile / Build / Audit / Render / Land
- Audit — verification pass (verb)
- Proof — final artifact (noun)
- Run — one end-to-end session

Retired: slice, capability, capability_verdict, task (user-facing),
certifier, story, acceptance check.

### Atomic units

- **Feature** = unit of value (audit verdict)
- **Group** = unit of dispatch (branch, worktree, agent)
- **Stage** = unit of pipeline progress
- **Run** = unit of session

Tasks (todo items inside an agent's loop) are below the user surface,
internal-only.

### Retry / loop layers (only two)

- **Check loop** — Layer 1, per-Group, deterministic, inside Build.
  Runs on `check_evidence` failures.
- **Audit loop** — Layer 2, per-Feature, LLM-judged, after Audit.
  One repair attempt per failing Feature, re-audit affected Features only.
- No Layer 3. Quality findings are informational.

### Configurable budgets

All retry counts, timeouts, cost caps, audit modes live in `otto.yaml`
project defaults. CLI flags override per-run. Spec-level overrides
allowed per-Group/Feature for advanced cases. Single `otto/defaults.py`
in code; nothing else embeds magic numbers.

### Stages of a Run

1. **Compile** — LLM produces Spec from Intent (or normalizes a
   user-supplied Spec).
2. **Spec review** (optional gate) — user edits Features, Groups,
   Guardrails. Approve or recompile.
3. **Build** — agents run in parallel, one per Group. Internal Check
   loop per Group.
4. **Audit** — single LLM pass walking the integrated product, tags
   walkthrough actions with Feature ids. Audit loop runs per failing
   Feature.
5. **Render** — deterministic publish. Writes Proof packet (whole-
   product + per-Feature) and assets.
6. **Land** — merge each Group's branch into target, in dep order via
   eligibility-gated FIFO.

### CLI surface (ideal state)

- `otto run [intent | --intent-file path]`
- `otto run --resume <session-id>`
- `otto run --rerun-audit <session-id>` — re-audit same code, full or
  per-Feature scope
- `otto run --recompile <session-id>` — regenerate Spec from updated
  intent, dispatch only changed Groups
- `otto render <session-id>` — re-render Proof from existing audit (no
  cost)
- `otto history`, `otto sessions <id>`, `otto setup`, `otto web`,
  `otto replay <session-id>`

Legacy commands (`otto build`, `otto certify`, `otto improve`) get
deprecated and deleted in Phase C.

### Proof granularity

- Whole-product Proof packet at session root
- Per-Feature mini-packet under `proof/features/<feature-id>/`
- Per-Group narrative + diff under `groups/<group-id>/`
- All API-accessible, all linkable, all bookmarkable

### Hybrid plan ownership

Otto can own the plan (default greenfield) or the user can own the
plan. Same pipeline, different inputs:

- User provides Intent only → Compile produces full Spec → spec review
  → Build.
- User provides Intent + concrete Features → Compile normalizes (no
  derivation) → spec review (default-skip if user marked it complete)
  → Build.
- User edits a generated Spec at the spec-review gate by adding/
  removing Features and Guardrails.

### What never appears in user surface

- Slice, capability, story, AC, certifier, task (the words)
- Agent todo lists / step counts
- Internal dispatch decisions (single agent vs split-within-Group)
- File-ownership conflict resolution mechanics
- LLM provider/model identity unless user opts to surface it

---

## Open items deferred

These came up but were not fully resolved; left as TODO for later turns:

- **Per-Feature audit cost vs whole-product audit cost.** Empirical:
  measure on a real run before committing to one being "expensive."
- **Multi-Feature evidence cross-linking** — the renderer treatment for
  walkthrough segments that evidence multiple Features (sketched as
  "list under each, mark 'shared with X'" but not implemented).
- **Per-Group provider/model surfacing.** Cheap to add to the Proof,
  open question whether the user wants it visible by default.
- **Brownfield "compile reads working tree" mode.** Acknowledged needed,
  not yet specified — what does Compile read, how does it diff against
  existing Spec, what's the editor for "this file is mine, leave it
  alone."

---

## Files produced from this conversation

- `docs/otto-redesign-conversation.md` — this transcript
- `research.md` — full design research with vocabulary, decisions,
  constraints, verification plan
- `plan.md` — implementation plan staged across vocabulary refactor →
  Feature-aware audit → Per-Feature proof renderer → MC redesign →
  legacy deletion
- `docs/otto-wireframes.md` — text-based screen sketches for spec
  review and run-detail drawer

---

## Q6 — User-facing vs runtime spec; spec editor wireframes

User:

> "do you have wireframe for spec? feature editing? btw, user-facing spec
> might be different from internal specs used for otto's actual running,
> right? user-facing needs to be human readable and edittable"

My answer (compressed):

**Two artifacts, one source of truth.** The Spec exists in two
synchronized files:

- `spec.md` — Markdown with HTML-comment metadata. User-facing,
  free-text editable, sharable (paste in PR, render in GitHub).
  Source of truth for prose: intent, Feature names + descriptions,
  Guardrails.
- `spec.json` — JSON, schema-validated. Runtime-facing. Source of
  truth for mechanics: Feature ids, group_ids, owned_paths,
  dependencies, evidence_kinds, dispatch_plan.

Three rules:
1. User owns prose; Otto owns mechanics. User edits feature names,
   descriptions, group placements, guardrails. Otto re-derives ids,
   owned_paths, dependencies. User cannot edit ids — they're stable
   across renames.
2. Round-trip is byte-stable. `parse_spec_md(render_spec_md(s), base=s) == s`.
   Otto's HTML-comment markers persist through user edits.
3. Runtime never reads markdown. All Compile/Build/Audit/Render/Merge
   stages read `spec.json`. If user breaks markdown, runtime is
   unaffected — but next save fails until fixed.

API in `otto/spec.py`: `parse_spec_md(md_text, base=None) -> Spec | ParseError`,
`render_spec_md(spec) -> str`, `compile_spec(intent, project_kind, base=None) -> Spec`.

Versioning: every user-save creates `spec-v<N>.{md,json}` side by side.
Latest symlinked as `spec.{md,json}`.

Final artifact: at end of Run, Render emits `proof/spec-final.md` —
what was actually built, with verdict annotations per Feature.

Wireframes added: 4a Markdown view of spec editor, 4b Form view with
[⋮] menus per Group/Feature, 4c Add Feature modal with Otto-suggestion
micro-compile, 4d Spec diff vN→v(N+1) with re-dispatch impact.

---

## Q7 — Five review agents + anti-drift loop scaffolding

User:

> "yea fold them in. then can you dispatch multiple agents (claude only)
> to 1) mentally think thru using real-world examples (e.g. build a
> browser, build a twitter, build a slack IM webapp, build some
> API/lib/CLI). and see if the current design, including
> concepts/UIUX/implementtion details all work out. 2) review and
> cross-check the actualy implementation plan. polish them for our
> actual implementation and verification.
>
> then, for impl and verification, this is a big project. to prevent
> drifting and slop, we prolly need to use the loop feature to
> repeatedly check if our impl is consistent with the plan and do
> verification promply. can you think about how we can do so? what
> prompt should we tell the loop feature? what persistent files you
> need to involve."

I dispatched five Sonnet agents (parallel, background):
1. Build a web browser (adversarial scale test)
2. Build a Twitter clone (sweet-spot product test)
3. Build a Slack IM webapp (mid-complexity SaaS test)
4. Build API/library/CLI (non-webapp generalization test)
5. Cross-check the implementation plan against the codebase

Reports saved to:
- `docs/review-walkthrough-browser.md`
- `docs/review-walkthrough-twitter.md`
- `docs/review-walkthrough-slack.md`
- `docs/review-walkthrough-non-webapp.md`
- `docs/review-plan.md`

I designed two-loop anti-drift scaffolding:
- **Loop 1 (drift sentinel)** — every commit OR every 60 min: vocabulary
  scan, magic-number scan, scope check vs current phase, fast unit
  tests, update progress.md drift counters; halt on critical drift.
- **Loop 2 (phase advance gate)** — explicit invocation when phase
  claims done: full test suite, integration tests, honest-failure
  tests, RUA checklist, Bench A; mark phase complete or halt.

Persistent files added:
- `progress.md` — live phase checklist + drift counters
- `drift-log.md` — append-only drift incidents
- `docs/anti-drift-loops.md` — loop design + ready-to-paste prompts
- `review.md` (already existed; new entries appended by Loop 2)

---

## Q8 — Synthesizing 5 agent reports

When the agents completed, the convergent findings were:

**Plan reviewer's critical finding** (most important):
> "The plan describes greenfield work on modules that already exist."
> `otto/build.py` (1000+ lines), `otto/audit.py` (1400+ lines),
> `otto/render.py` (500+), `otto/checks.py`, `otto/spec_compile.py`
> (the real spec module — not the planned `otto/spec.py`),
> `otto/merge_queue.py`, `otto/cli_run.py` all already exist. Phase A1
> is a refactor, not net-new code.

Phase A0 timing: 1-2 days → **5-7 days**. ~2,353 retired-vocab hits
across ~110 files. JSON-key compatibility shims required for legacy
session dirs.

**16 design gaps identified across the 4 walkthrough reports:**

| # | Gap | Source | Fix |
|---|---|---|---|
| 1 | `owned_paths` overlap rule too aggressive | browser, twitter, slack | `shared_paths[]` |
| 2 | Shared infra has no spec home | twitter, slack | `Component` first-class |
| 3 | Multi-actor audit limit | twitter, slack | `evidence_completeness` flag |
| 4 | `acceptance_detail` freeform prose | twitter | structured `acceptance_steps[]` |
| 5 | No adversarial test generation | twitter | deferred to v2 |
| 6 | Quality findings always informational | twitter | severity ladder |
| 7 | Systems-level products produce false positives | browser | out-of-scope clause |
| 8 | No audit fixture state | twitter, slack | `audit_fixtures[]` + Seed stage |
| 9 | Spec review overflows at 30+ Features | slack | filter/collapse (deferred UX) |
| 10 | Cost cap default $5 too low | browser, slack | per-project default + pre-start estimate |
| 11 | `coverage_confidence` field missing | browser | added to Feature verdict |
| 12 | `structure` schema only for webapp | non-webapp | per-kind schemas |
| 13 | Evidence kinds browser-only | non-webapp | add `CLIProbe`, `ImportCheck`, `TypeCheck` |
| 14 | Walkthrough schema browser-native | non-webapp | `action_kind` discriminator |
| 15 | Proof template screenshot-led | non-webapp | per-`action_kind` template branches |
| 16 | UI evidence checkboxes not per-kind | non-webapp | filter by `project_kind` |

**10 plan edits** from the plan reviewer (all applied to plan.md):
1. "Existing state" callout per Phase
2. A0 timing 1-2d → 5-7d
3. Split A1 → A1a/A1b/A1c
4. A2 coverage threshold (≥90%) + backup plan
5. A4 type contract pre-step
6. Phase B parity criteria copied inline + "2 of 3" requirement
7. A1 round-trip property test
8. `otto/checks.py` "modified" not "new"
9. `otto/spec_compile.py` added to A0 rename scope
10. A3 determinism guard — semantic not byte equality

All applied to research.md / plan.md / progress.md.

---

## Q9 — Autonomous-loop prompt for full implementation + e2e

User:

> "dump the above exchanges to the files too so later you can refer to.
> then revise the plan based on subagents findings (if you haven't done
> so). no need to worry or constrain the implementation for costs.
>
> i mean i want to use the /loop feature to AUTONOMOUSLY finish the
> implementation to 100% (based on the persistent designs), plus
> realworld e2e test to ensure our new design works as intended. what
> should i tell it in one prompt? it needs to pick up the
> implementation phases too (since you sometimes stop in the middle),
> not just for grepping slop. what should i tell it so we can
> consistently pushing for **autonomous** work that dont drift from the
> plan and wireframes. no manual stuff. and during the loop it should
> do e2e test with real projects to verify everything runs smoothly. no
> slop, no hack, no bandaids, no overfitting to a specific project for
> spot fixes; needs max generalization. after /loop finishes, we
> basically want a working new-generation otto that works out of box
> for a REAL USER."

The autonomous loop is a single, long, opinionated prompt that fires
each tick and decides what work to do based on `progress.md`. The full
prompt and rationale are in `docs/autonomous-loop.md` (added in this
turn).

Key design rules I committed to:

- **Loop reads `progress.md` first** — it's the source of truth for
  what's done, in-flight, and next.
- **Loop picks up implementation phases**, not just runs lints. Each
  tick: identify next-`[ ]` step in current phase, implement it,
  verify, update progress.md.
- **Verification is part of every tick.** No phase progresses without
  the Loop 2 gate (full tests + honest-failure tests + bench-A on
  bench-gated phases + RUA on MC phases).
- **E2E with real projects every M ticks.** Run a fresh greenfield
  Run end-to-end against one of three diverse fixture intents
  (webapp, cli, library). Different intent each cycle.
- **Anti-spot-fix discipline.** When fixing a bug, the loop is required
  to: (a) write a failing test that reproduces the bug, (b) fix the
  root cause, (c) confirm test passes, (d) grep for similar patterns
  elsewhere in the codebase. No "fix this one occurrence" patches.
- **Anti-overfitting discipline.** The loop never special-cases the
  specific E2E project under test. If a generalization-breaking patch
  is the cheapest fix, the loop refuses it and writes a `drift-log.md`
  entry instead.
- **Stop conditions are explicit.** Loop stops when (a) Phase C
  complete + sign-off criteria from research §4.8 met, OR (b) any
  critical drift can't be auto-resolved within the tick, OR (c) cost
  cap or wall cap hit.
- **Failure visibility.** Every tick writes a one-line summary to
  `progress.md`'s "Tick log" section, and a full report to
  `loop-report.md`. No silent failures.

