# Otto: Intent-to-Product Design

Date: 2026-05-02
Branch: `cc-i2p-2`
Status: Historical design context — baseline implemented May 2026

> Current runtime reference: see `docs/architecture.md`.
>
> This document records the original i2p architecture proposal. The shipped
> runtime follows the same compile -> build -> merge -> audit -> render shape,
> but several names and surfaces changed during implementation:
>
> - Runtime and UI use **Group** where this document says "slice".
> - `otto run` is the canonical direct intent-to-product CLI.
> - `otto proof ...` is the canonical proof/artifact namespace; `pow`,
>   `render`, and `history` are compatibility aliases.
> - The committed Mission Control frontend reflects the post-RUA redesign
>   documented under `docs/rua/`.

## What Otto is

Otto is an intent-to-product control system. Ambiguous user intent becomes a
visible, editable spec, then flows through build, audit, and proof — owned by
Otto end to end. Coding is one means; the product is the goal.

## The model

**Four stages. One artifact. Three roles.**

```
                          ┌──────────────┐
                          │    intent    │  user prompt
                          └──────┬───────┘
                                 │
                                 ▼
                       ┌───────────────────┐
                       │   COMPILE         │  LLM authors spec:
                       │   (compile agent) │  structure (file layout, routes,
                       └─────────┬─────────┘  components, key text), slices,
                                 │            tasks, checks, deps, done_means
                                 ▼
                          ┌────────────┐
                          │   spec     │  ◄────┐
                          │ (one JSON) │       │ user reviews,
                          └─────┬──────┘       │ edits, approves
                                │ ─────────────┘
                                ▼
        ╔═══════════════════════════════════════════════════════════╗
        ║                       BUILD                               ║
        ║                                                           ║
        ║   build lane (parallel, dep-aware)                        ║
        ║   ┌─────────┐    ┌─────────┐    ┌─────────┐               ║
        ║   │ slice S1│    │ slice S2│    │ slice S3│   ...         ║
        ║   │ agent A │    │ agent B │    │ agent C │               ║
        ║   │  tasks  │    │  tasks  │    │  tasks  │               ║
        ║   │ checks  │    │ checks  │    │ checks  │               ║
        ║   │ on own  │    │ on own  │    │ on own  │               ║
        ║   │ branch  │    │ branch  │    │ branch  │               ║
        ║   └────┬────┘    └────┬────┘    └────┬────┘               ║
        ║        │              │              │                    ║
        ║   check fail? ───► same agent, fresh prompt, retry        ║
        ║   check pass? ───► slice becomes merge candidate          ║
        ║        │              │              │                    ║
        ║        ▼              ▼              ▼                    ║
        ║   ┌──────────────────────────────────────────┐            ║
        ║   │   merge lane (serial per target branch)  │            ║
        ║   │                                          │            ║
        ║   │   eligibility:                           │            ║
        ║   │     deps merged                          │            ║
        ║   │     base not stale                       │            ║
        ║   │     not superseded                       │            ║
        ║   │   FIFO within eligible                   │            ║
        ║   │                                          │            ║
        ║   │   slice's agent (same process):          │            ║
        ║   │     refresh target                       │            ║
        ║   │     rebase                               │            ║
        ║   │     rerun slice + cross-slice checks     │            ║
        ║   │     conflict/fail? repair in worktree    │            ║
        ║   │       (edit scope: owned_paths only)     │            ║
        ║   │     land atomically                      │            ║
        ║   │     post-land: verify resolved target    │            ║
        ║   └──────────────────┬───────────────────────┘            ║
        ║                      │                                    ║
        ║                      ▼                                    ║
        ║              all slices landed?                           ║
        ║                no ─► loop                                 ║
        ║                yes ─► continue                            ║
        ╚════════════════════════╤══════════════════════════════════╝
                                 │
                                 ▼
                       ┌───────────────────┐
                       │   AUDIT           │  one LLM pass on integrated product:
                       │   (certifier)     │  end-to-end journeys, walkthrough video,
                       └─────────┬─────────┘  screenshots, narrative, verdict
                                 │
                       ┌─────────┴─────────┐
                       │ findings?         │
                       └────┬─────────┬────┘
                            │ yes     │ no
                            │         │
              ┌─────────────┘         │
              ▼                       ▼
       route to fix loop:      ┌───────────────────┐
       relevant slice's agent  │   RENDER          │  proof packet:
       re-engages, checks      └─────────┬─────────┘  spec + slice statuses
       rerun, merge again,                            + audit video/screenshots
       certifier re-audits                            + narrative + known limits
       (bounded retries)                              + merge/rollback state
                                         │
                                         ▼
                                ┌────────────────┐
                                │  proof packet  │
                                │  for human     │
                                └────────────────┘
```

LLM is in the hot path only at **compile** and **audit**. Inside build, only
deterministic checks and same-agent retries.

### Stages

1. **Compile** — intent → spec. LLM authors a concrete spec (structure,
   slices, tasks, checks). User reviews and edits before build starts.
2. **Build** — agents work the spec's slices on branches. Deterministic
   checks run continuously. Merges land slice-by-slice into a per-target
   serial queue.
3. **Audit** — one LLM pass against the integrated product. Walks user
   journeys, produces video/screenshots, judges cross-slice coherence. Routes
   findings back to build if anything is wrong; otherwise hands off to render.
4. **Render** — assembles the spec, latest evidence, and audit report into a
   proof packet a human can scan.

### Artifact: the spec

Single JSON document at `<session>/spec.json`. Replaces both branches'
`product_contract.json` and the separate `OraclePlan` artifact.

```python
@dataclass
class Spec:
    intent: str
    project_kind: ProjectKind          # "webapp" | "cli" | "library" | "api"
    structure: StructureDecisions      # see below — payload per kind
    slices: list[Slice]
    cross_slice_checks: list[Check]    # only meaningful on integrated product
    non_goals: list[str]
    done_means: str
    schema_version: int = 1

@dataclass
class StructureDecisions:
    """Project-kind-specific structural decisions. Concrete enough that two
    independent agents reading the spec produce structurally compatible code
    (same paths, same names, same shapes). See 'Concrete enough' below."""
    payload: dict                      # validated against per-kind schema

@dataclass
class Slice:
    id: str
    title: str
    depends_on: list[str]              # other slice ids
    owned_paths: list[str]             # globs; edit-scope enforces these
    tasks: list[Task]                  # guidance only — see "Task semantics"
    checks: list[Check]                # gate the slice

@dataclass
class Task:
    """Guidance for the build agent inside a slice. Tasks are NOT individually
    trackable; the slice's checks are the gate. Tasks decompose work for the
    agent's planning, nothing more."""
    description: str

@dataclass
class Check:
    id: str
    kind: CheckKind                    # discriminated union, payload per kind

class CheckKind: ...                   # base

@dataclass
class PytestCheck(CheckKind):
    selector: str                      # pytest -k or file::node id
    must_pass: bool = True

@dataclass
class RepoTestCheck(CheckKind):
    command: list[str]
    expected_exit: int = 0
    stdout_contains: list[str] = field(default_factory=list)

@dataclass
class ApiProbe(CheckKind):
    method: Literal["GET","POST","PUT","DELETE","PATCH"]
    path: str
    body: dict | None = None
    expected_status: int = 200
    expected_body_matches: dict | None = None    # JSONPath → expected

@dataclass
class BrowserJourney(CheckKind):
    steps: list[BrowserStep]           # navigate, click, assert text/element
    capture_screenshots: bool = True

@dataclass
class StateInvariant(CheckKind):
    predicate: str                     # e.g. "exactly one of app/main.py vs microfeed/"
```

There is no `Contract`, `ProductSlice`, `OraclePlan`, `AcceptanceCheck`,
`ProofRequirement`, or `slice.acceptance` field. The slice's `title` plus
its `checks` carry the meaning a separate `acceptance` string would.

### "Concrete enough" — operational definition

The spec is concrete enough when **two independent agents reading only the
spec produce structurally compatible code**: same file paths, same component
names, same public API shapes.

Testable: a `compile_validator` emits, per slice, a deterministic skeleton
of files/exports the slice should produce; run it twice from clean state
and diff the skeletons. Compatible iff diff is empty (modulo whitespace
and comments).

If the validator finds drift, the spec needs more `StructureDecisions`
content before build can start. This is the property the compiler must
hit; if benches still regress, the fix is more compiler work, not more
orchestration.

### Roles

- **Compile agent** — writes the spec from intent. One per run. May
  regenerate after user edits; otherwise single-shot.
- **Build agent** — does a slice. **Long-lived process** while its slice
  is in flight: handles tasks → checks → fix retries → merge → conflict
  repair → land in one OS process attached to one worktree. "Fresh
  context" on retry means a prompt-level reset (clear conversation,
  re-read spec + diff + failure narrative, start over) — **not** a
  process restart. Same model throughout. One per slice in flight.
- **Certifier** — one LLM pass at end against integrated product.
  Produces video, screenshots, narrative report, verdict. May re-run
  after fixes (bounded).

## Build, in detail

```
Two lanes:
  Build lane:  parallel, dep-aware.
  Merge lane:  serial per {project, target_branch}.

For each slice whose deps are met:
  spawn a build agent (long-lived process) in its own branch/worktree
  agent reads spec, plans against tasks, runs deterministic checks
  on check failure: same agent, prompt-level reset, retry
  on check pass: slice becomes a merge candidate

Merge queue (per target branch):
  eligible := merge candidates AND
              deps merged AND
              base not stale (target HEAD has not advanced past the slice's
                              merge-base since last rebase) AND
              not superseded by a later attempt for the same slice id
  pick oldest eligible (FIFO within eligibility)
  the slice's build agent (same process, still attached to worktree):
    refresh target
    rebase slice branch onto target
    rerun slice checks + cross-slice checks against the rebased tree
    on conflict or check failure:
      same agent repairs in worktree
      edit scope: paths in slice.owned_paths + conflict regions only
                  never check definitions, never other slices' files
    land atomically
  post-land: verify resolved target state
```

### Phase A implementation notes (A11, A12, B9, B10)

The shipping merge queue is single-worktree mode (default per-slice
worktree = `lambda _s: project_dir`). The full design above applies
to the eventual multi-worktree extension; what shipped:

- **No `git rebase`, no remote refresh.** Replaced with merge-first-then-
  verify-with-rollback against current HEAD. See `merge_queue.py:8-22`
  docstring.
- **Superseded eligibility is implemented.** Merge queue uses the
  latest `BuildResult`/`ComponentResult` per id; an older PASSING result
  cannot leak through after a later attempt for the same Group/Component.
- **Base freshness is implemented as merge-into-current-HEAD plus
  verification/rollback**, not as a separate pre-rebase gate. This
  matches the current merge-first executor: land the candidate into the
  current target, rerun Group + cross-Group checks, then rollback on
  failure. A future true multi-worktree/rebase executor can add an
  explicit stale-base predicate without changing the proof contract.
- **Repair scope is enforced after the repair agent returns.**
  `run_merge_queue` computes unstaged/staged/untracked paths, runs
  `detect_scope_violations`, emits `scope.warning`, discards the
  uncommitted repair, and blocks the Group when repair edits cross into
  peer-owned paths.
- **Repair-time counter is split**: cost is shared across build/audit/
  merge_queue; repair wall-time is build-only (audit/merge don't call
  `charge_repair`). The design implied a single shared time counter;
  reality is unified-cost + build-only-time.

These are explicit Phase A simplifications acknowledged in code
comments. Multi-worktree mode is the v2 priority where they all
become load-bearing.

### Bounds and budgets

Defaults (per-run overridable):

- **Per-slice retries**: 3 attempts; then `blocked` with narrative; build
  proceeds with remaining slices.
- **Per-slice wall budget**: 30 minutes; then `blocked` regardless of
  attempt count.
- **Per-run wall budget**: inherited from existing Otto budget.
- **Per-run cost ceiling**: inherited from existing cap; build stops
  early and hands what it has to audit + render if hit.
- **Audit retries**: 2 audit→fix→audit cycles; then run ends with partial
  proof packet and the audit's outstanding findings.

A blocked slice still appears in the proof packet with its narrative;
the user decides whether to ship partial or intervene.

### Cross-slice check timing — pre-land

Pre-land verification: every merge runs cross-slice checks before atomic
land, so failures keep target clean.

Cost: cross-slice checks (browser journeys especially) add minutes per
merge. Trade considered: post-land with rollback would be faster steady
state but introduces footguns (a later slice may have already started
rebasing onto the bad state) and complicates the merge model.

Decision: **pre-land**. If benches show this dominating runtime later,
revisit with concrete numbers.

### Why no central merge composer

The Microfeed regression came from a central resolver flattening rich UI
work to make checks pass — it had no slice context. Slice-owned merge
avoids this: the agent that built the work resolves conflicts in the
work's context, with `owned_paths` bounding edit scope.

### Why concrete spec is the prerequisite

Slice-owned merge only works if slices in flight share structure. If two
slices invent competing app shells, no merge model can win. Compile must
produce a spec concrete enough that subsequent slices read it and extend
rather than invent.

This is the **planned** load-bearing fix for the Microfeed regression —
not yet validated. Re-bench against this design will confirm or reject.
If rejected, the fix is more compiler work (richer
`StructureDecisions`, sharper validator), not new orchestration.

## Audit, in detail

Certifier runs **once** at end. Distinct from per-slice checks because:

- **Scope**: integrated product, not per-slice.
- **Method**: end-to-end user journeys, cross-slice navigation, real
  walkthrough.
- **Output**: walkthrough video, screenshot set per major surface,
  narrative report, per-slice verdict.
- **Role**: produces the human-trustable proof. Deterministic checks
  proved correctness; the certifier produces evidence a human can scan.

### Implementation reality vs design (post-Phase-A)

Three honest deviations from the doc-as-written:

**1. Walkthrough video and screenshots are bundled for default webapps.**
When a project declares a `BrowserJourney`, Otto still runs the
project's own Playwright/Cypress command and collects its configured
`evidence_globs`. When a webapp has no `BrowserJourney`,
`_synthesized_webapp_walkthrough` now captures the home page itself:
Flask/static HTML discovery first, then Playwright against `base_url`
or the generated body artifact. The audit dir records
`screenshot-home.png`, `dom-home.html`, `walkthrough.webm` when the
browser writes video, `browser-capture.log`, and a conservative
`walkthrough.jsonl`. If Playwright or the browser binary is missing,
the log says so and the HTML artifact remains as fallback evidence.

**2. The live runner has two retry layers.**
`run_audit` still has a compatibility Group-level fix loop for direct
callers that pass `fix_agent`. The orchestrated i2p runner does not
use that loop: it calls `run_audit(..., fix_agent=None)` for the judge
pass, then uses `audit_loop.repair_failing_features` as the sole repair
layer. Layer 2 re-audits narrowed Feature ids via `feature_scope_ids`.

**3. Build/fix retries keep provider session continuity.**
`BuildAgentOutput.session_id` is threaded back into the next
`BuildAgentInput.agent_session_id`, and `default_build_agent` maps it
to `AgentOptions.resume`. This gives Codex/SDK session-pinned
conversation continuity across build retries, merge repair, audit
compatibility repair, and Layer 2 repair when the provider supports
resume. PID reuse is not required for the product contract.

### Cross-slice fix loop

If certifier finds an issue deterministic checks missed (cross-slice
coherence bug), it routes to fix loop: relevant slice's build agent
re-engages, deterministic checks rerun on its branch, merge queue lands
the fix, certifier re-audits. Bounded by `audit_retries` (default 2);
after that, run ends with partial proof packet.

## Render, in detail

The proof packet is the user's actual deliverable; designed for at-a-glance
human review.

**Two formats, one source:**

- **`proof-packet.html`** — primary human format. Self-contained except
  for asset links. Layout:
  - Header: intent, project_kind, top-line verdict (passed / partial /
    blocked), wall time, cost.
  - Spec summary: structure, slices (collapsed), non-goals, done-means.
  - Per-slice section, ordered by dep topology:
    - status (passed / blocked), title, owned_paths
    - check results table (kind, expected, evidence link, pass/fail)
    - branch/commits
    - inline thumbnails for screenshot evidence; click for full-size
  - Audit section: walkthrough video (embedded `<video>` with first-frame
    thumbnail), screenshot grid per major surface, narrative report,
    per-slice verdict.
  - Known limitations: deferred items + blocked slices with narratives.
  - Merge state: ordered list of what landed, what was rejected and why.
- **`proof-packet.json`** — machine-readable companion. Same content,
  structured. For programmatic consumers (CI integrations, future Otto
  features, bench harnesses). Schema versioned.

**Evidence directory layout:**
```
<session>/
  spec.json
  proof-packet.html
  proof-packet.json
  evidence/
    slice-S1/check-C1/{screenshots,stdout,stderr}/
    slice-S2/check-C7/...
    audit/{video.webm,screenshots/,narrative.md}
```

Render reads `<session>/spec.json` plus the evidence directory; no
parallel state. Blocked slices render with narrative and partial
evidence, not omitted.

## Resume and crash recovery

Long-running builds need to survive Mission Control restarts, network
blips, and agent crashes. Source of truth: `<session>/spec.json`
(immutable after approval) plus `<session>/state.jsonl` (append-only
event log).

Events:
- `slice.started` — id, branch, worktree path, pid
- `slice.check.started` / `slice.check.finished` — id, check id, evidence path, verdict
- `slice.attempt.failed` — slice id, attempt #, narrative
- `slice.merge.eligible` / `slice.merge.started` / `slice.merge.landed`
- `slice.blocked` — id, reason
- `audit.started` / `audit.finished` — verdict, evidence path
- `run.finished` — verdict

On resume:
1. Replay `state.jsonl` to derive each slice's state.
2. For `landed`/`blocked`/`finished`: trust the log.
3. For `started` but not `finished`: check the worktree.
   - If build agent process is alive (PID file + healthcheck): attach.
   - If not: respawn against existing worktree with prompt-level reset;
     worktree git state is the truth.
4. Merge queue rebuilds from eligibility events.
5. Audit re-runs only if it never finished or its findings are unaddressed.

Inherits codex-feats's checkpoint logic (commit `1165cf0f2`'s spec-gate
resume rules) and codex-i2p's queue persistence. No new checkpoint
format — `state.jsonl` is just structured events.

## What survives from each branch

### Keep from `codex-feats`
- Spec compiler LLM prompt and review UX (`otto/spec.py`,
  `otto/prompts/spec-light.md`, MC spec review workspace).
- Schema validation and fail-closed normalization (rewritten against
  unified `Spec` dataclass, not REQ/AC dual-numbering).
- Visual proof matching, viewport/chrome fixes, MC spec-action banner.
- Spec-gate resume rules.

### Keep from `codex-i2p`
- Check runtime: browser journey, API probe, state invariant, repo test
  executors with screenshot/video capture (`otto/oracles.py` ported into
  `otto/checks.py` against typed `Check` payloads).
- Queue + work-graph readiness + per-target serialization (`otto/queue/`).
- Edit-scope hardening (`otto/merge/edit_scope.py`).
- Slice-owned conflict repair (in-worktree fix, post-fix in commit
  `209ed8591`).
- Bench harnesses (`scripts/bench_microfeed_*`) and the
  `.codex/skills/otto-test-protocol` skill.

### Delete (eventually)
- `otto/product_contract.py` (both versions).
- `tests/test_product_contract.py` (both versions).
- `OraclePlan`, `AcceptanceCheck`, `ProductSlice` types.
- REQ/AC dual-numbering scheme.
- Markdown spec.md alongside JSON contract — JSON is source of truth, MC
  renders an editable view.
- Multiple "planner modes."
- `Campaign` as a top-level concept.

## Migration / coexistence

Deletions are end state, not day one. Path:

1. **Phase A (parallel)** — introduce `otto/spec.py`, `otto/checks.py`,
   `otto/build.py`, `otto/merge.py`, `otto/state.py`, `otto/render.py`
   alongside existing code. New CLI command (`otto run` or repurposed
   `otto plan`) routes through the new stack; old commands still go
   through `campaign.py` / `oracles.py` / `product_contract.py`.
2. **Phase B (cutover)** — after Microfeed bench validates parity,
   default commands route through new stack. Old code remains importable
   but marked deprecated.
3. **Phase C (cleanup)** — after one full cycle of bench runs without
   regression, delete deprecated modules and their tests. Update CLI
   help, MC labels, docs.

Each phase is a separate PR with its own bench gate. No big-bang switch.

## Project-kind handling in compile

`StructureDecisions.payload` schema is project-kind-specific:

- **webapp**: file layout, route map, component names + key text, data
  model, shared styles.
- **cli**: entry point path, subcommand map, flag schema (per command),
  exit-code semantics, expected stdout shape.
- **library**: public API surface (modules + exported symbols with
  signatures), import contract, no-side-effects-on-import invariants.
- **api**: base URL, endpoint map (method/path/request/response shapes),
  auth model, error response shape.

Compile classifies project_kind from intent and emits the matching
schema. The `compile_validator` (concrete-spec test) runs against the
per-kind schema. Webapp gets the most attention first because it is the
Microfeed bench target; CLI/library/API are stubbed initially and
fleshed out as benches for them are added.

## Files after the work

```
otto/spec.py                # compile_spec(intent) → Spec; review/edit/approve/validator
otto/checks.py              # Check kinds + run_check(check) → Evidence
otto/build.py               # build loop: dispatch slices, run checks, fix retries
otto/merge.py               # merge queue, eligibility, slice-owned merge step
otto/certifier.py           # final audit pass
otto/render.py              # proof-packet.html + proof-packet.json renderer
otto/state.py               # state.jsonl events + resume

otto/web/.../SpecReview.tsx # editable spec UI (per-kind structure renderer)
otto/web/.../RunPanel.tsx   # one MC hierarchy: spec → slices → check results

tests/test_spec.py          # compile + lifecycle + review + validator
tests/test_checks.py        # each check kind, evidence capture
tests/test_build.py         # parallel slices, retries, blocked
tests/test_merge.py         # eligibility, FIFO, conflict repair, owned_paths
tests/test_certifier.py     # audit pass + retry bound
tests/test_render.py        # html + json render, blocked slice handling
tests/test_state.py         # event log + resume
tests/integration/test_intent_to_proof.py   # full pipeline on a fixture intent
```

## Microfeed worked example

**Compile** produces a Spec with `project_kind: "webapp"` and `structure`:
- App entry: `app/main.py`, single FastAPI app.
- Routes: `/`, `/posts`, `/posts/{id}`.
- Home page contains: `<Nav>` (Home, Posts, About), `<Hero>` (H1
  "Microfeed", CTA "Browse posts"), `<CardGrid>` rendering recent posts.
- Components live under `app/components/`.

Slices:
- S1: app shell + home page (no deps; `owned_paths: ["app/main.py", "app/components/**"]`)
- S2: posts model + API (deps: S1; `["app/models/**", "app/api/**"]`)
- S3: posts list page (deps: S2; `["app/pages/posts.py", "app/templates/posts/**"]`)
- S4: create post form (deps: S2; `["app/pages/create.py", "app/templates/create.html"]`)
- S5: post detail page (deps: S2; `["app/pages/post_detail.py", "app/templates/posts/detail.html"]`)

`compile_validator` confirms two trial passes produce identical file
skeletons. User reviews, approves.

**Build**: S1 runs first; subsequent slices read the spec, see the shell
exists at `app/main.py`, extend it. S2/S3/S4 (where deps allow) run
concurrently, each restricted to its `owned_paths`. Each slice's checks
include "page contains `<Hero>` with text 'Microfeed'" — concrete and
verifiable.

**Merge**: per-target serial queue, pre-land verification. Each agent
rebases, runs slice + cross-slice checks, lands. A merge that flattens
the hero fails the "page contains `<Hero>`" check before landing; the
slice's agent fixes in its worktree (it can't dodge by editing the check
— edit scope blocks it; can't touch other slices' files — `owned_paths`
blocks it).

**Audit**: certifier walks list posts → open post → create post journey,
captures video, screenshots each surface, judges coherence. Verdict +
proof packet.

**Render**: human opens `proof-packet.html`, sees spec, slice statuses
with thumbnails, walkthrough video, per-surface screenshots. Decides
whether to ship.

Planned mechanism — bench evidence is the validation, not this
walkthrough.

## Sequencing

Adversarial Codex review (Plan/Implementation gate) is **deferred this
session** — credits exhausted. Review checkpoints are human-in-the-loop
against this doc until credits return.

All experiments in this session use `--provider claude` and the Sonnet
defaults at `otto/config.py:105-107`. No Codex provider runs.

1. This doc reviewed and approved.
2. `otto/spec.py` + `Spec` dataclass + `compile_validator`. Spec compiler
   prompt tightened to emit concrete `structure` for `webapp` first.
   Tests for compile + validator + a few representative intents.
3. `otto/checks.py` ported from `codex-i2p`'s `oracles.py`, rewired
   against typed `Check` payloads. Tests for each kind.
4. `otto/state.py` — event log + resume. Tests: round-trip, resume after
   each event type.
5. `otto/build.py` — slice dispatch, parallel build, fix retries, bounds
   and budgets. Tests for dep-aware readiness, retry, blocked.
6. `otto/merge.py` — eligibility-gated FIFO queue, slice-owned merge,
   pre-land cross-slice checks, edit-scope rules. Tests for ordering,
   conflict repair, owned_paths enforcement.
7. `otto/certifier.py` — final audit, video capture, retry bound. Tests
   on a fixture spec.
8. `otto/render.py` — html + json proof packet, blocked-slice handling.
   Tests for layout + machine schema.
9. MC view (one hierarchy: spec → slices → checks). One PR per major
   page; no big-bang.
10. **Phase A coexistence**: new `otto run` alongside old commands.
11. Real Microfeed bench (`--mode new`, Sonnet). Compare against:
    - `bench-results/microfeed-realweb-20260503-033646` (codex-i2p alone)
    - `bench-results/microfeed-realweb-20260503-020705` (mono baseline)
    Validate or reject the concrete-spec hypothesis. If rejected,
    iterate on compile prompt + validator before continuing.
12. **Phase B cutover** once bench shows parity. **Phase C cleanup**
    after one full cycle without regression.

## Sandboxed builders

Out of scope this round. Worktrees remain the isolation primitive.
BoxLite slots in later without changing the model — a build agent runs
against an isolated tree regardless of whether that tree lives in a
worktree or a sandbox.

## Net

- 4 stages, 1 artifact, 3 roles.
- One name per concept; vocabulary collapsed.
- 6 of 7 north-star concerns in scope this round; sandboxed builders
  deferred (1 of 7).
- Slice-owned merge with eligibility-gated FIFO queue, pre-land
  cross-slice checks.
- LLM in hot path only at compile and audit; deterministic checks during
  build.
- "Concrete spec" defined operationally: two trial compiles produce
  identical structural skeletons.
- Build agents are long-lived processes per slice; "fresh context" is a
  prompt-level reset, not a process restart.
- Slices declare `owned_paths`; edit scope enforces them.
- Bounds and budgets stated with concrete defaults.
- Resume is journaled events + worktree-as-truth.
- Render produces both human HTML and machine JSON.
- Project kinds (webapp/cli/library/api) handled by per-kind structure
  schemas; webapp first.
- Migration is three phases (parallel → cutover → cleanup), gated on
  bench evidence.
- Microfeed regression fix is a hypothesis, validated by bench, not
  asserted as already solved.
- Codex review deferred this session; Sonnet/Claude provider throughout.
