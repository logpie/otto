# Research — Are we over-constrained? Why linkboard never converges

Created 2026-05-18. Trigger: user pushback — "running experiments without
real progress... bigger system design issue... over-constraining ourselves?"
This is a Phase-4.5 architecture-question artifact (systematic-debugging):
3+ fixes failed in different places ⇒ question the architecture, do NOT
write patch #N+1. Discuss with human before any further fix.

## Evidence (real, not guessed)

1. **Product complexity (linkboard intent, `/tmp/fastrepro_linkboard_intent.txt`):**
   4 entities, 2 features, ~10 endpoints, 2 pages, 1 journey, SQLite,
   single start.sh. A one-day CRUD app. Bare CC / single agent: ~20–30 min,
   one shot, zero coordination failures.

2. **Patch-tail (git, last 200 commits):** 171 are v5/i2p. Class histogram:
   Gate 30, port 19, architect 14, contract 13, foundation 11, scaffold 8,
   clean-deploy 7, probe 5, clean-boot 4, cascade 1. Long visible
   brittle-predicate tail (`validate structure not enum`,
   `not-all-or-nothing`, `collect from any list value not a key allowlist`,
   `drop leaf_extension_globs over-constraint`, ...).

3. **Self-incriminating design note (`plan-fast-e2e-repro.md`):** linkboard
   seams were *"deliberately engineered in"* to "reproduce iTracker bug
   classes." The test was built to detonate the orchestrator's seams.

## Root cause

otto applies ONE maximal coordination regime uniformly regardless of
product size. Failure-handling is **global-on-local-failure** (child/contract
gate failure ⇒ fresh lead re-runs whole decomposition — the p0fix2/3/4
cascade) and **all-or-nothing** (foundation block starves everything —
`project_otto_structural_block_gap.md`). Each gate is a STATIC predicate
over open-ended generated code; no static predicate is both sound and
complete over generated code ⇒ every new product shape breaks a different
predicate ⇒ infinite patch tail (the ~170 commits).

Compounding independent failure surfaces (compile gate, contract gate,
foundation barrier, architect-reentry, clean-deploy, clean-boot, journey)
each <100% reliable ⇒ product of probabilities ⇒ reliable convergence
improbable for ANY product. Linkboard just exposes it cheaply.

## Methodology bug

linkboard is too simple to exhibit scope-cutting, so it cannot measure
otto's actual value proposition (scope accountability vs. bare-CC silent
scope-cut). It can only measure overhead. Even a green linkboard proves
nothing about the hypothesis. "Experiments without progress" because the
instrument cannot register the quantity of interest.

## Strategic options (need user decision — do not guess)

**A. Right-size the regime (recommended core).** Detect product scope at
compile; if it fits one agent's reliable reach, run a single-agent path
(no tree, no contracts, no foundation barrier). Decomposition only when
scope genuinely exceeds one agent. Validate reliability on a product that
*actually needs* decomposition, with the single-agent path as the floor.

**B. Fix the failure regime (global→local).** Keep decomposition but make
repair LOCAL (never re-run global decomposition on a child failure),
foundation degradation GRACEFUL (ship the substantially-complete partial),
gates BEHAVIORAL (does it build+boot+journey?) not STATIC predicates.
This is the "design a protocol, stop patching" move.

**C. Both A+B.** A makes simple products stop paying the tax; B makes the
tax survivable when decomposition is genuinely warranted. They reinforce.

**D. Status quo + patch #171.** Rejected by Phase-4.5 / patches-to-protocols
unless the user explicitly overrides.

## User decision (2026-05-18)

- Approach = **simplify back to the landing regime + bake in the genuinely
  general fixes from the campaign** (surgical, not blind revert).
- Foundation = **degrade to scaffold** (foundation fails ⇒ materialize a
  minimal working scaffold so feature children still merge + land on top).
- North-star invariant: **integration/merge ALWAYS succeeds best-effort;
  every built branch lands; gates ANNOTATE, never discard/cascade; bugs are
  acceptable output, thrown-away work is not.**

## The inflection point (bisect result — evidence)

Through **2026-05-14** = the regime that *landed iTracker*. Journeys soft
(`c8228aabd` 05-13 "journeys as examples, intent as contract"); the team
had already simplified once (`b14d0ce13` 05-14 "v6.5 simplification — trust
the agent, delete patches").

**`b0e4a6012` (2026-05-15) "journey-verification Unit 1 — fail-closed
sink"** + `07205c72d` (UI executor *gates* at root integration) +
`bb0d1d427` = the inversion: soft journey check → **fail-closed blocking
gate**. Everything 05-16→05-18 (S0/S1/S2/S4 ownership Implementation-Gate
R1–R4, brittle-predicate loosening tail, foundation clean-boot probe, P0
scaffold seed) is downstream cope-machinery for that one inversion.

Bisect anchor for "the landing regime": the tree state at **2026-05-14
(`29243bc3b` v6.6 consolidation / `b14d0ce13`)**, immediately before the
05-15 journey-verification fail-closed series.

## Commit triage (general fix to KEEP vs constraint-creep to NEUTER)

KEEP — genuine general correctness, not constraints:
- `b998dfe5f` git check-ignore as noise oracle
- `5a9299b9b` THE silent self-merge no-op (real nested-decomp merge bug)
- `13af1ef39` re-merge passed-but-unmerged branches (this IS the north
  star, partially built — strengthen into the invariant)
- `c9d77bd3a` generic additive-union merge for shared files (the core
  always-land primitive — KEEP + promote)
- clean-deploy port hermeticity bundle (`809033823` `f04e9010f`
  `a48de32b6` `2049cae00` `115599f0b` `bdc737565` `e8d41758e` `7fc7a6804`
  `c4a4e0812`) — real env bugs, generally useful
- `6f650ac91` oracle (not wall-clock) decides repair success
- `1a59f651a` foundation-contracts not all-or-nothing (already aligned)
- `1157df2a1` checkpoint/resume infra
- P0 scaffold seed (`be3c48a43` `7d4f44cf5` `06af75462` `db1fcb559`) —
  REPURPOSE as the degrade-to-scaffold target, do NOT delete

NEUTER (fail-closed → fail-open / advisory; keep the *insight*, drop the
*blocking*):
- journey-verification fail-closed sink (`b0e4a6012` `07205c72d`
  `bb0d1d427`) — restore "journeys as examples" 05-13 philosophy; annotate
  proof packet, never block merge
- S0/S1/S2/S4 ownership gates + brittle-predicate tail (`02ef92796`
  `ce5e2b138` `5252f5a44` `104522af8` `32f61e0ba` `343562e91` `692897a16`)
  — keep "foundation owns shared models" as guidance; demote enforcement
  to annotation
- foundation clean-boot probe blocking (`f2aa00b25` `e6cd82173`
  `8a044cacf`) — convert block → degrade-to-scaffold trigger
- architect-reentry-fresh-lead cascade (the p0fix2/3/4 budget-killer) —
  replace with LOCAL repair; never re-run global decomposition on a child
  failure

## Resulting target architecture (skeleton — plan next)

1. Journey/UI/ownership verification → **advisory**: write findings into
   the proof packet; do not block/discard/cascade.
2. Foundation fail → **degrade to P0 scaffold** (reuse the seed
   materializer) so features merge on top.
3. Terminal merge → **always-land union** (promote `c9d77bd3a` +
   `13af1ef39` to a hard invariant): every built branch merges; conflicts
   resolved additively; known issues annotated; nothing discarded; no
   global re-decomposition.
4. KEEP the general correctness fixes above unchanged.

## LOCKED DESIGN (2026-05-19) — supersedes earlier merge-mechanism notes

One hard gate, everything else advisory.

**HARD (non-negotiable): merge conflicts get resolved → a coherent,
bootable product.** Mechanism, simplest-first:
- git auto-merge for disjoint files (most files; free; keeps both).
- structured union drivers ONLY for additive manifests (package.json,
  lockfiles) where line-union breaks syntax. Bounded known set.
- a BOUNDED repair pass for genuine semantic conflicts whose mandate is
  scoped to *coherent + builds + boots* (NOT "journeys pass"). Best-effort,
  time-boxed, non-blocking: on timeout it lands what it has + annotates
  `boots: false — <reason>`.
- foundation fails → degrade to P0 scaffold (reuse the seed materializer)
  so feature children still merge on top.
- Success bar = **builds + boots**. Never refuse, never cascade, never
  re-decompose.

**ADVISORY (never a gate): journeys, feature completeness,
ownership/contract findings.** Pass or fail → annotated in the proof
packet for human review (`project_proof_of_work.md`: proof-of-work is
documentation, not a trust/gate mechanism). Always land.

**Corrections baked in (from this conversation):**
- `ours`-wins on a conflicting code seam = REJECTED (silently drops a
  feature's real work — violates the north star).
- `union`-keep-both on conflicting code = REJECTED (interleaves
  incompatible code → unbootable pile; a pile of files is not a product).
- Therefore a reconciliation step is IRREDUCIBLE; "delete the agent
  entirely" was wrong. What we delete is the fail-closed/refuse/cascade
  WIRING, not the bounded repair itself. The repair's mandate shrinks
  from "every behavior journey passes" → "the product boots."

**What this single split subsumes (the actual simplification):** the
journey-verification fail-closed sink (`b0e4a6012`/`07205c72d`/`bb0d1d427`),
S0/S1/S2/S4 ownership Implementation-Gates, foundation clean-boot
*blocking*, the `_conflict_packet_for_refusal` machinery, and the
architect-reentry-fresh-lead cascade — all collapse into "resolve
conflicts to a bootable product; report everything else honestly."

## Next step

Proceed: writing-plans skill → plan file with Verify: lines (Verify must
prove: zero refusal/cascade code paths remain; merged product boots;
journeys are advisory-only) → /codex-gate Plan Gate → implement. No code
before Plan Gate APPROVED.
