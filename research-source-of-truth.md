# research: scaffold + operating notes as a single source of truth

Date: 2026-05-13

## Why this doc

Recent v5 runs surfaced a recurring class of bug:

> The architect writes a policy claim in CHARTER's *Agent operating
> notes* (e.g., "leaves use Vitest; Playwright is integration-only")
> but doesn't scaffold the infrastructure that claim requires (no
> `vitest` in `devDependencies`, no `vitest.config.ts`, no test
> script). Feature children read the claim, find no infrastructure
> to follow it, and use the only tool available (Playwright). The
> result: 70+ minute test-debugging spirals.

This is the deeper symptom of a structural problem: **architect's
"stated policy" (operating notes prose) and "scaffold" (actual files)
are two independently-authored artifacts that need to agree, with
nothing enforcing agreement.** Today's incident is one instance;
other instances will follow the same pattern (state "use Zustand"
without store init; state "use react-query" without QueryClient
config; state "use click" without installing click).

We've discussed and rejected several patch-flavored fixes today:

- Add a prompt rule "if you state X, scaffold X" — patch trajectory
- Add `owned_paths` schema — over-constraining (user pushback)
- Add per-tool regex checks — narrow, registry grows forever
- Add structured CHARTER fields — over-engineering

A second-opinion consultation with Codex reframed this: it is a
**source-of-truth bug**, not a "Vitest bug" or an "operating notes
bug". The structural fix is to give the system one source of truth
for operational capability (the scaffold) and derive or validate the
prose against it.

## Diagnosis

Today's architect prompt (`lead.md`) asks the architect to do two
things that interact:

1. Write `CHARTER.md` including an *Agent operating notes* section
   covering: pre-installed state, test commands, paths to shared
   files, cross-cutting library choices.
2. Scaffold the project: `package.json`/`pyproject.toml`, configs,
   shell files.

These are independently composed. Nothing checks that operational
claims in (1) are realized in (2). The misalignment surface is:

```
operating notes  ←     (no enforcement)      →  scaffold
  "use Vitest"                                   only Playwright in deps
  "tests run from project root"                  pyproject without test entry
  "Zustand store interface in store/index.ts"    no store/index.ts file
  "API at /api/v1/..."                           router mounted at /v1/api/
```

In each case: the prose makes an operational claim. The scaffold
either doesn't fulfill it, contradicts it, or fulfills it differently.
Children read the prose and act on it; the prose may not be true.

There's also an existing internal contradiction in the current
prompts independent of today's edits: `lead.md`'s architect section
says "test framework choice is internal to leaf" while the leaf
section (post `1e244e090`) hardcodes "FE leaves use Vitest + RTL".
So the system can declare "leaves use Vitest" via the leaf prompt
while passing scaffolds with no Vitest installed.

## Constraints / what to avoid

- **No new schema** in CHARTER. User explicitly pushed back on
  `owned_paths`-style schemas.
- **No general-purpose prose understanding.** Don't try to parse
  every possible architect claim. Don't build a knowledge base of
  "this English sentence implies this scaffold requirement."
- **Cross-product portable.** Whatever we build must work for
  webapp, CLI, library, API-only, batch pipeline. Webapp-specific
  hardcoding is the kind of overfit we're escaping from.
- **Bounded mechanism cost.** ~200 LOC ceiling. If the fix needs
  more, the framing is wrong.

## Proposed approach (two parts)

### Part A: Scaffold-derived infrastructure inventory

After the architect completes, the runner walks the scaffold and
produces a deterministic **infrastructure inventory** — a small
machine-extracted summary of what the scaffold actually provides:

- For each `package.json` (any subdir): scripts (names + commands)
  and declared dev/runtime deps
- For each `pyproject.toml`: declared deps, optional-deps, scripts
  (`[project.scripts]`), pytest/ruff/etc. config keys present
- For each known config file present: name + brief role
  (`vitest.config.ts` → "Vitest configured", `playwright.config.ts`
  → "Playwright configured", `tailwind.config.js` → "Tailwind
  configured")
- Top-level files implying entry points (`start.sh`, `main.py`,
  `index.ts`, `Makefile`, etc.)

This inventory is built by Otto, not the architect. It's truthful
because it's read from the scaffold's actual state.

The inventory is then **injected** into each feature child's prompt
(rendered alongside CHARTER) as a section named *"Detected
infrastructure (auto-generated from scaffold; trust this for
operational facts)"*.

The architect's free-form *Agent operating notes* section narrows
to **non-derivable cross-child decisions only**: shared file paths
(where types live), conventions (ID format, time format), policy
deltas the architect wants to surface. The line is roughly:

| Architect-written (prose) | Otto-derived (inventory) |
|---|---|
| "Shared types in `lib/types.ts`" | Files present in scaffold |
| "Time format: ISO8601 UTC" | n/a |
| "Use the existing `api.ts` fetch wrapper" | `api.ts` exists |
| ~~"Leaves use Vitest"~~ | Vitest config present + `test` script |
| ~~"Tests run from project root"~~ | Test commands derived from scripts |
| ~~"Pre-installed: node_modules"~~ | Symlinked state visible |

This eliminates the misalignment surface for things derivable from
the scaffold. The architect can't claim "Vitest is the test runner"
incorrectly because Otto produces that line from `package.json`
content.

### Part B: Pre-fanout coherence gate

Even with derivation, the architect's prose can still reference
things that don't exist (a file path that's not there; a service
that wasn't scaffolded). To catch those, a generic preflight check
runs after architect-pass and before feature children dispatch:

- Parse the architect's *Agent operating notes* (free text, but
  bullet-list shape)
- Extract referenced filesystem paths (`frontend/src/lib/api.ts`)
  and shell commands (`cd api && uv run pytest`)
- For each referenced path: verify the file/dir exists
- For each shell command: verify the entry binary/script is
  reachable (script in `package.json`, or `node_modules/.bin/X`,
  or `pyproject.toml` script)
- Fail preflight if any referenced thing is missing

This is the **claim-vs-reality** check. It catches:

- "Tests run with `npm run test:unit`" but no `test:unit` script
  exists
- "Shared types in `frontend/src/lib/api.ts`" but file is missing
- "API at port 8000" but no service is configured to listen on 8000

It does NOT try to interpret abstract policy ("leaves don't use
Playwright"). It only verifies that concrete references resolve.

The check is generic — works against package.json, pyproject.toml,
Cargo.toml, or any scaffold shape — because it's about path
existence and script reachability, not specific tools.

### Cost estimate

- Part A (inventory): ~100 LOC
- Part A (injection into prompts): ~30 LOC (render layer)
- Part B (coherence gate): ~80 LOC
- Tests: ~80 LOC
- Prompt rewording (lead.md architect section): ~30 lines of prose

Total: ~250 LOC + ~30 lines prompt prose. Above my "~200 LOC
ceiling" caveat above but within reason.

## Alternatives considered + rejected

| Alternative | Why rejected |
|---|---|
| Prompt rule "scaffold what you state" | Patch trajectory; user pushback |
| Per-tool regex registry | Webapp-overfit; maintenance burden |
| Structured CHARTER schema (test_framework_leaf, etc.) | Schema burden; over-constraining |
| Revert operating notes entirely | Loses install-discipline value; doesn't address underlying inconsistency |
| Accept LLM variance | Honest fallback if structural fix is too much; chosen if Codex rejects this proposal |

## Open questions

1. **What if architect's operating notes are structurally minimal
   (just bullets)?** Probably most parsing is regex-tractable. Worth
   prototyping the parser on the past CHARTERs we have.

2. **What about subsystem-relative paths?** "tests run from
   project root" vs "from frontend/" — coherence gate needs to
   resolve. Probably: assume project root unless prefix matches
   subsystem dir.

3. **Does this interact with the architect retry path?**
   `bc43d7ae1` already retries the architect on scaffold preflight
   failure. The coherence gate's failure should likewise trigger a
   retry with the misalignment as the retry_reason.

4. **Does this fix the existing inconsistency** between architect
   section ("test framework is leaf-internal") and leaf section
   ("FE leaves use Vitest")? Part A would surface the inconsistency
   (no Vitest in inventory → "Detected: no FE unit test framework"
   → leaf prompt's "use Vitest" is unrealizable). Worth fixing the
   contradiction in prompt text too, separately.

## What this does NOT solve

- "Policy by omission" — architect says "don't use Playwright"
  without naming alternative. Coherence gate can't catch this
  because there's no concrete reference to validate.
- Genuine LLM judgment failures — architect picks the wrong tool
  for the job entirely. Different class.
- Cross-product semantic correctness — coherence gate checks
  structural existence, not semantic correctness ("this endpoint
  returns the right shape").

These remain in the LLM-variance bucket. The structural fix is for
the misalignment subclass that IS structural.

## Acceptance criteria

After implementation, on a fresh otto v5 run:

1. Architect's operating notes are shorter (mostly non-derivable
   cross-child decisions). Most of today's content is replaced by
   Otto's auto-derived "Detected infrastructure" section.

2. If the architect scaffolds Playwright-only but the leaf prompt
   expects Vitest, the coherence gate fires *before* feature
   children dispatch. Architect retries with the gap noted.

3. Feature children's first-minute behavior shifts: they trust the
   "Detected infrastructure" section instead of grepping/`ls`-ing
   to discover what's installed.

4. No new bugs that take 30+ min to manifest in subsequent runs.

## Codex review — key corrections (received 2026-05-13)

Codex (acting as critical reviewer) agreed on framing but flagged
substantive issues:

1. **Naming: "capability inventory" not "policy inventory."** The
   derived section should say "these scripts/deps/configs exist," not
   "therefore use Vitest." The latter recreates policy-drift in
   derived form.

2. **Delivery mechanism unclear.** Children read CHARTER from disk;
   they don't see runtime prompt appendices the runner injects. Pick
   one: (a) a generated file on disk that children are told to read,
   (b) a managed CHARTER block the runner writes/maintains, or (c)
   a prompt-appendix path. Each has trade-offs.

3. **Part B should parse code spans only, not prose.** Add a
   markdown convention: every file path, dir, script name, or shell
   command in *Agent operating notes* MUST be backticked. Parser
   looks at code-span content only — far less brittle than prose
   parsing.

4. **"API at port 8000" example removed.** Port references aren't
   simple path-existence checks; covered separately by CHARTER's
   port extraction.

5. **Cost estimate revised: 350-500 LOC** with tests + regression
   fixtures, not 250.

6. **The current leaf prompt has the misalignment baked in.**
   `lead.md` post-`1e244e090` hardcodes "FE leaves use Vitest +
   RTL" while the architect section says "test framework choice is
   leaf-internal." Part B alone won't fix this — leaf prompt must
   change to consume detected infrastructure rather than hardcoding
   a tool.

7. **Recommendation: ship Part A as advisory/non-blocking first.**
   Don't make Part B a blocking gate until fixtures prove it
   correctly catches the Vitest/Playwright class without false
   positives on unusual-but-valid projects.

## Additional open questions (from Codex)

- **Where does the inventory live? Who owns it?** Generated file
  vs CHARTER-managed block vs prompt appendix — each has trade-offs.
- **When is it regenerated?** After architect only? After
  architect retry? Before every child? After leaf edits that change
  manifests?
- **What wins when leaf prompt, CHARTER prose, and detected
  infrastructure conflict?** Precedence rule needed.
- **Are missing references always blocking, or are unknown command
  forms warnings?**
- **How do you prevent "no test runner detected" from becoming
  permission to skip tests?** This is the loophole concern.
- **What are deterministic acceptance tests?** "No 30+ min bugs"
  isn't testable. Need fixtures that demonstrate before/after.

## Revised acceptance criteria

After implementation:

1. The capability inventory exists as a deterministic, readable
   artifact (generated file or CHARTER block) the architect cannot
   contradict in operating notes.
2. The leaf prompt no longer hardcodes specific tools (Vitest, etc.);
   it instructs leaves to read the capability inventory.
3. Operating notes' code-span references all resolve (path exists,
   script reachable). Coherence gate fires when they don't.
4. A regression fixture: a hand-crafted scaffold where operating
   notes claim Vitest but scaffold has only Playwright; gate fires
   and architect re-dispatched.
5. Negative regression fixture: a CLI-only project with no FE; gate
   does not false-positive.

## Implementation timing — DEFER to clean session

Codex's risk-profile recommendation: **don't implement tonight.**
We've shipped 11 prompt/runner commits today. Adding a 350-500 LOC
structural change at this point is asking for new bug surface.

Suggested order for the next session:

1. **Fix leaf prompt precedence.** Remove the "FE leaves use Vitest"
   hardcoding; replace with "use what the capability inventory
   surfaces." Small standalone commit.
2. **Part A — capability inventory, advisory only.** Generated, but
   children don't yet trust it as authoritative. Prove the inventory
   is accurate against several real projects.
3. **Part B — coherence gate, non-blocking first.** Log warnings only.
   Observe false-positive/false-negative rates on a few runs.
4. **Promote Part B to blocking.** Once fixtures + observation prove
   it's correct.

## Next steps (this session)

1. Kill the in-flight run (done).
2. Ship the corrected research doc (this doc).
3. Stop here for tonight.
4. Resume implementation in a clean session.
