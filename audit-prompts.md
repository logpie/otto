# Otto prompt-chain audit — 2026-05-21

Scope: pull every prompt the runtime feeds an LLM agent, check for
cross-prompt consistency on the load-bearing invariants. Triggered by the
linkboard 2026-05-21 run where the root Lead violated the
"foundation-must-not-seed-feature-owned" rule despite the rule being
written down — suggesting the prompt chain has structural / wording
problems, not just bug-level fixes.

## Prompt inventory

| Where | Lines | Driver | Audience |
|---|---|---|---|
| `otto/prompts/lead.md` | 428 | `_render_lead_prompt(kind="plan_or_inline")` | **Every** non-integration Lead — root, foundation child, feature child |
| `otto/prompts/lead-integration.md` | 201 | `_render_lead_prompt(kind="integration")` | The integration Lead only |
| `otto/prompts/autopilot-pilot.md` | 69 | Autopilot recovery agent | Recovery pilot (incidents) |
| `otto/prompts/setup-claude.md` | 28 | `otto setup` CLI | Project bootstrap, not in-run |
| Inline in `v5_preflight_repair.py:1238 _repair_prompt` | ~40 | Repair sessions (foundation, child-verify, etc.) | Repair agent |
|  ↳ `_oracle_focus_guidance` | data-driven | repair | — |
|  ↳ `_HARNESS_BLACKBOX_GUIDANCE` | constant | repair | — |
|  ↳ `_LIVE_BROWSER_REPAIR_GUIDANCE` | constant | repair | — |
| Inline in `spec_compile_flat.py:786 _render_contract_repair_prompt` | ~40 | Spec-compile contract repair | — |
| `submit_subtask(intent=...)` | n/a | — | Children read this AS their intent; the SAME `lead.md` renders around it |

Critical observation: **one prompt drives every Lead role.** Root, foundation,
and feature children all see the same `lead.md`. The "if root then ... if feature
then ..." conditioning happens inside the prompt's prose, not via separate
templates.

## Findings

### F-1 — `lead.md` is too long and the load-bearing rules are buried

The `lead.md` template is 428 lines, with **"Hard Rules" at line 420** —
literally the last section. The stub-anti-pattern rule the linkboard run
violated lives at lines 282-308, **inside a 41-line bullet** about
`feature_owned_paths` authoring. Agents pattern-match on the most concrete /
most recent bullet they read; rules buried 280 lines in compete with everything
else.

**Action:** restructure with a `## Hard Rules` block at the **top** (right after
the role statement), pulling out the no-stub-seeding, no-`git add -A`,
honest-verdict invariants. Move detail/explanation/rationale below.
Keep the prompt under ~250 lines if possible (the agent's instruction-following
degrades materially past that length per the instruction-following note set).

### F-2 — Root vs feature-child Lead share the same prompt; the conditioning is implicit

`lead.md` contains the entire architect-task guidance (lines 89-327) inside one
`## Decide` section, with phrasing like "(this section describes what you, the
agent, must do if you build the scaffold inline, AND what your architect child
must do if you emit one)". Feature children read all of it. They are told to
emit `architect/foundation` children and partition `feature_owned_paths` —
rules that are dead for them but cost context and create confusion.

**Action:** split the architect/foundation guidance into a clearly-titled
`## If you are the Architect / Foundation Lead` block, with an explicit
"otherwise skip" pointer. Or render two templates: `lead-root.md` vs
`lead-leaf.md`, sharing common content via include.

### F-3 — The CHARTER ownership model has TWO parallel fields (`feature_owned_paths` + `leaf_extension_globs`)

`lead.md` line 280 says "Feature paths must live under
`registration_isolation.leaf_extension_globs`." But `feature_owned_paths` is the
field that the union guard reads directly. These are two parallel CHARTER
fields that the prompt requires must agree. There is no compile-time check that
they DO agree, only the runtime union-guard surfacing the disagreement after
build (the same lateness that bit the linkboard run).

**Action:** either unify into one field (drop `leaf_extension_globs` or compute
it from `feature_owned_paths`), or add a compile-time check that the two are
consistent at CHARTER-write time. Same logic as task #72's "foundation
seeded feature-owned path" check, just one layer earlier.

### F-4 — `check: literal` vs `check: semantic` is documented in prompts but only partially enforced in code

`lead.md` lines 158-165 carefully distinguish:
- `check: "literal"` — byte-exact match required (route registries)
- `check: "semantic"` — public behavior preserved (conftest, base classes)

But the union guard (`_integration_union_missing_contributions` in `merge.py`)
applies **literal line-preservation to EVERY contribution**, regardless of the
contract's declared `check` mode. The semantic escape only kicks in via
`_semantic_foundation_contract_satisfied` when a contract is BOTH `semantic`
AND declares `required_exports` or `behavior_probes`. The default for a
plain `semantic` contract with no probes is still literal-line-preservation.

**Action:** make `check: semantic` actually semantic by default. If a contract
is declared semantic but provides no `behavior_probes`, the union guard should
either (a) skip line-preservation entirely (relying on `required_exports`) or
(b) fail at CHARTER-write time demanding probes.

### F-5 — Verification semantics are heavily text-search; behavioral checks are only at integration

| Check | Method | Where it runs |
|---|---|---|
| `page_has_ia_route` | id match | leaf + integration verifier |
| `entity_has_empty_state` | doc string match | leaf + integration verifier |
| `action_has_test` | grep filenames+text | leaf + integration verifier |
| `mutating_action_has_feedback` | grep text | leaf + integration verifier |
| `no_stub_text` | grep for "TODO" etc | leaf + integration verifier |
| `_semantic_foundation_contract_satisfied` | regex on `export` + substring probes | union guard |
| Integration union | literal line preservation | union guard |
| **Behavior journey self-verify** | **agent driving chrome-devtools / curl** | **integration Lead only** |

Six text-search checks vs ONE behavioral check. The text-search checks are now
mostly `ADVISORY_KINDS` (post-P0a + commit `bd89feb96`) so they don't demote,
but they still consume verifier wall time and add log noise.

**Action:** with the behavioral journey self-verify proven (linkboard run), the
text-search checks can probably collapse to pure linting. Consider promoting
the integration Lead's journey self-verify to the **sole source of truth** for
"does the product behave correctly?", and demoting the text-grep checks to
optional linting that emits suggestions but doesn't appear in CLI output by
default.

### F-6 — Leaf verdict standard vs integration verdict standard differ; journey credibility threshold isn't documented in prompts

`lead.md` (leaf): a leaf can claim `journeys: [{id, passed, detail}]` with any
non-empty detail. There is no length floor in the prompt.

`lead-integration.md`: the integration Lead is told to give detail like
"navigated to /tags, clicked the 'Add' button, filled name='dev', confirmed
the new tag appeared in the list with color swatch — verified at <timestamp>"
— effectively 100+ chars per journey.

`journey_verdict_sink.agent_self_verified_executor_results`: enforces a
40-char detail floor + evidence-list presence to count a claim as credible.

The runtime constraint is hidden from the prompt. A leaf that writes a 12-char
detail will silently fail-closed without knowing why.

**Action:** lift the credibility constraint into the prompt. Either:
- Tell every Lead "your `detail` must be at least 40 chars describing what you
  observed AND your verdict must list at least one evidence file" — OR
- Reduce the runtime threshold and rely on prompt-level pressure for honesty.

### F-7 — Children inherit ownership rules via the intent text the root Lead writes; no structural validation

The root Lead writes child intents via `submit_subtask(intent=...)`. The child
sees that intent string PLUS the standard `lead.md`. Nothing structurally
guarantees the root Lead included ownership/partition invariants in the child
intent — it depends entirely on the root Lead's discretion.

For the linkboard run, child intents DID explicitly say "OVERWRITE the stub
`BookmarksPage.tsx`" — i.e. the root Lead's intent text told the children to
violate the partition rule. The children obeyed the intent.

**Action:** consider a rubric in `lead.md` for what child intents must include
(stack constraint, owned-paths, forbidden-paths, depends_on). And a
runtime check that submit_subtask intents don't ask children to edit paths
outside their declared `owned_paths`.

### F-8 — Repair prompts diverge subtly from main prompts on scope

`lead-integration.md` line 44: "Integration may edit across subsystems. Keep
fixes scoped to glue, arbitration, and repair needed for the merged product to
run."

Repair prompt (`_repair_prompt`): "Repair only the scoped paths in this repair
unit" (when `allowed_paths` present).

These are reconciled by `allowed_paths` being set differently for integration
repair vs child-verify repair. But the principle is the same: "stay in your
scope." The wording diverges enough that a reader (LLM or human) needs the
runtime context to know which scope applies. Probably fine, but worth a single
sentence cross-referencing.

### F-9 — `lead-integration.md` is well-aligned with the unified-verifier architecture (post-Phase 1)

Line 8-9: "You do not hand off to a separate verifier or repair agent. You ARE
the verifier and the repair loop. The Python orchestrator that used to run
journeys for you and spawn a separate repair agent is gone."

This is correct after Phase 1 + the centralization commits. The leaf-level
verifier is NOT yet at this maturity (still partly text-search-driven; no
chrome-devtools at leaf time). Phase 2 (task #63) would bring leaves into
parity.

**Strength, not finding** — this is doing what we want.

### F-10 — Repair prompt's "BLACK-BOX harness" rule is sharp and correct

`_HARNESS_BLACKBOX_GUIDANCE` explicitly forbids repair agents from
reverse-engineering Otto's source / oracle / journey executor to satisfy the
harness. This is correct and well-stated.

**Strength, not finding.**

## Sequencing — what to ship

Ranked by impact-per-effort:

1. **F-1 + F-2** restructure `lead.md` — hard rules to top, split architect
   guidance into its own block, target <300 lines. Pure prompt work, ~30 min.
   Highest impact: makes every subsequent rule actually readable.

2. **F-3** unify `feature_owned_paths` and `leaf_extension_globs` (or add a
   compile-time consistency check between them). Same pattern as task #72.

3. **F-7** add ownership/partition rubric to `lead.md` for what child intents
   must carry; consider runtime check on intent paths.

4. **F-5** with the integration Lead's behavioral verification proven, demote
   the leaf text-search checks to pure linting (no CLI surfacing). Verifier
   then has ONE source of truth for "does the product behave."

5. **F-6** sync the prompt-level journey-detail constraint with the runtime
   40-char + evidence requirement (one-liner per prompt).

6. **F-4** make `check: semantic` actually behave semantically without
   `required_exports`/`behavior_probes` — currently it falls back to literal.

7. **F-8** one-sentence cross-reference between integration scope and repair
   scope rules.

F-9 and F-10 are strengths to preserve.

## Recommendations the user explicitly asked about

**Generic vs project-specific:** my own prompt edit `235a8de62` was overfit
(linkboard session ID, specific paths); already reverted in `8295dea05` to a
stack-agnostic restatement. Going forward: prompt edits should mention an
invariant + a positive alternative + at most ONE generic-shape anti-example.
No run IDs, no project names, no specific paths.

**Behavioral vs literal verification:** the union guard's literal
line-preservation invariant (`_integration_union_missing_contributions`) is the
single biggest source of false-demote stress. Replacing it with a
semantic-registration check ("every declared endpoint / route / page is
reachable in the final union; every declared `required_exports` is present and
typed; behavior journeys pass") would eliminate an entire class of bugs at the
cost of a careful refactor. F-5 is the right way in: keep the integration
Lead as the authority on behavior, demote everything else to advisory linting.
