# Robustness pass — Otto reliable under noisy agent output

**Trigger:** Live Phase 3 validation (2026-05-22) found a scheduler print-loop
(16951 emissions, 21 min wasted) caused by the architect agent writing
`"owner"` instead of `"owner_task_id"` in `foundation_contracts` entries.
Investigation confirmed this is NOT a Phase 3 regression — same intent +
same model + same code, but one LLM roll used the "wrong" field name and
the entire dispatch pipeline got stuck. **Prior runs happened to roll
correctly.** This is exactly the brittleness pattern that's been costing us.

**Principle:** Agents produce noisy output. The harness must be the immune
system. If the product can still build and integration journeys pass live,
the run should succeed — even if CHARTER is malformed, contracts miss
fields, partition has overlaps. Strict validation that BLOCKS is the
anti-pattern; structured-feedback + graceful-degrade is the pattern.

## Brittleness patterns observed this session

| Pattern | Concrete example | Cost when triggered |
|---|---|---|
| **A. Loop on stale state** | `_foundation_scheduler_feedback` keeps firing same finding because nothing tells it "feedback already given" | 16951 chokepoint prints / 21 min |
| **B. All-or-nothing parser** | `parse_foundation_contracts` returns 0 contracts when entries miss a field, instead of accepting valid ones + warning on bad | One bad entry blocks entire run |
| **C. Strict field naming** | `owner` vs `owner_task_id` — one rolls breaks pipeline | Random failure rate per intent/model |
| **D. Implicit retry loops** | Several "while loop + check" patterns without explicit budgets | Cost spirals before honest failure |
| **E. Missing structural backstops behind prompt rules** | Prompt says "use owner_task_id"; no parser fallback for synonyms | Prompt fidelity = correctness |
| **F. Unstable feedback signatures** (Codex) | Signatures include `_written_at` / ready-list order → equal-finding deduplication fails | Suppression doesn't suppress |
| **G. Unmapped terminal origins** (Codex) | `_cause_from_origin` defaults to PRODUCT/LAND for unmapped origins (merge.py:1023) — that's how this loop's warning got emitted 16951× | Silent default fires on every iter |
| **H. Parser/persistence semantic mismatch** (Codex) | `parse_foundation_contracts` returns findings AND parsed entries; persistence only commits if findings is empty | Partial-accept won't help unless persistence logic also splits |
| **I. Tests encoding old retry behavior** (Codex) | `test_no_architect_cascade` etc. encode current loop semantics; budget changes break them | Plan must update tests deliberately, not let them fail mysteriously |

## Revisions from Codex round 1 (NEEDS REVISION → folded in below)

1. **K differs per loop class.** Scheduler-style emissions get K=1 (no
   state changes between attempts → repeating is pointless). State-mutating
   retry loops (Phase 3 plan-amendment) keep K=2. Natural wait states
   (in-flight foundation hasn't returned yet) get no budget — they clear
   by state transition.
2. **Layer 3 needs an explicit allowlist of degradable kinds**, not
   blanket annotate-and-proceed. `feature_owned_paths_overlap` is on the
   allowlist (Phase 3 already wired up the integration prompt for it).
   Generic `partition_findings` are NOT — integration journeys are finite
   and may not catch ownership bugs the partition was supposed to prevent.
3. **Layer 2 needs an advisory-vs-blocking split.** Today any
   `CoherenceFinding` blocks at dispatch.py:393. Aliases must emit
   advisory-severity findings (canonicalize before persistence) so they
   don't trigger the block. True invalids stay blocking.
4. **"Derive ownership from git diff" is not parsing** — moved out of P0
   Layer 2 into Layer 3 (advisory only, behind allowlist), heavily constrained.
5. **Priority reshuffle** (see sequencing table at bottom): P0 = #89 +
   alias canonicalization + advisory-vs-blocking split + K=1 scheduler +
   JSON prompt example + NARROW audit of touched scheduler emissions.
   Broad Layer 5 audit + Layer 3 behavioral fallback move to next
   session.
6. **Missed patterns Codex flagged** added to "Brittleness patterns
   observed" table below: unstable feedback signatures, unmapped terminal
   origins, parser/persistence semantic mismatch, tests encoding old
   "dispatch foundation 3 times then block" behavior.

## The fix — 5 layers of defense

Listed by impact-per-risk. Layers are MOSTLY independent, with two
explicit dependencies (Layer 1 requires Layer 2b's severity split;
Layer 3 expansion requires per-kind integration prompt updates).
Each MUST land with structural tests + at least one adversarial
Codex review pass.

### Layer 1 (P0): Bail-out budgets on scheduler emissions — K=1, not K=2

**The immediate bug fix** + structural backstop. Two budget classes
(Codex round 1):

| Loop class | K | Rationale |
|---|---|---|
| Scheduler-style: `_foundation_scheduler_feedback`-like (no state mutation between attempts) | **K=1** | If nothing changed between firings, repeating accomplishes nothing. One emission → annotate + suppress. |
| State-mutating retry: plan-amendment, contract-amendment, scaffold-repair | K=2 (=`MAX_CONTRACT_AMENDMENT_ATTEMPTS`) | Each attempt commits changes; a second attempt with new feedback can succeed where the first didn't. |
| Bounded external-effect/probe loops: oracle, preflight, repair-agent, port-cleanup, lock contention | **Explicit attempt count or deadline** (Codex round 2) | These probe-external-world or contend-for-resource; need per-callsite budget tuned to operation cost. Not K=1 (probes can transiently fail), not natural wait (must give up eventually). Default 3 attempts with exponential backoff; 30s deadline for lock contention; per-callsite overrides documented. |
| Natural wait state: in-flight foundation hasn't returned yet | **no budget** | Clears by state transition; budgeting it would prematurely give up. |

**Feedback-signature stability (Codex finding F):** the
`(parent_id, finding_kind, signature)` key must NOT include `_written_at`
or ready-list ordering. Signature = stable hash of the *content* fields
(`overlapping_paths`, `affected_feature_task_ids` sorted, etc.). Add a
`_signature` helper that excludes volatile fields.

**Concrete callsites:**
- `_foundation_scheduler_feedback` → `foundation_contracts_missing_after_pass` (bug #89) — K=1
- `_foundation_scheduler_feedback` → `shared_foundation_not_ready` — K=1 IF foundation is non-in-flight terminal; no budget if foundation still in-flight
- `_foundation_scheduler_feedback` → `terminal_blocked_foundations` — K=1
- `_foundation_isolation_feedback` → already has Phase 3 K=2 for `feature_owned_paths_overlap` (state-mutating retry path) — KEEP
- Other isolation findings (`foundation_contract_owner_missing`,
  `foundation_contract_not_owned_by_owner`, `feature_overlaps_foundation_contract`,
  `feature_nested_under_foundation_tree`, `foundation_seeded_feature_path`)
  → K=1 each (no state change between scheduler iterations)

**Effort:** ~120 LOC + 60 LOC tests. Mirror Phase 3 pattern.

**Verify:** unit tests for the signature stability + counter logic.
Integration test: stubbed scheduler returning same finding forever →
loop exits after K=1 with parent annotated. Update tests Codex flagged
(`test_no_architect_cascade`) to reflect new semantics, not blanket-fix
red.

### Layer 2 (P0): Forgiving parsers — alias canonicalization + advisory split

Field-name drift is unfixable at the prompt level. Two coupled changes
(Codex round 1 fold):

**2a. Canonicalize aliases SILENTLY w.r.t. findings, but observably via telemetry**:
- `owner_task_id` ← also accept `owner`, `owner_id`, `task_id`
- `check` ← also accept `check_kind`, `kind`
- Canonicalize during parse, BEFORE any finding decision. The parser
  emits the entry with canonical field names; no `CoherenceFinding` for
  the alias.

**Alias-conflict rule (Codex round 2 — recognized-but-conflicting):**
If BOTH the canonical name AND an alias appear in the same entry with
DIFFERENT values (e.g. `"owner_task_id": "v5-aaa", "owner": "v5-bbb"`),
emit a `blocking` finding. Recognized alias + bad value must still
block. Spec: canonical wins iff non-empty AND alias either absent or
equal-after-strip; otherwise structural ambiguity → block.

**Telemetry concreteness (Codex round 2):**
"Silent" means "no `CoherenceFinding`," NOT "unobservable." Concrete:
`parse_foundation_contracts` currently returns `(parsed, findings)`.
Extend return to `(parsed, findings, telemetry_events)` where
`telemetry_events` is a list of dicts like
`{"kind": "field_alias_canonicalized", "from": "owner", "to": "owner_task_id", "entry_index": 2}`.
Callers persist these into `narrative.log` and proof packets via a new
`on_event` hookup (mirror `_v5r._emit` pattern). Findings stay
findings; aliases stay telemetry.

Why split: Codex finding H — today's persistence at
`v5_capability_inventory.py:898` only commits when findings is empty.
If we emit an advisory finding for "you used `owner` not `owner_task_id`",
persistence still blocks. Aliasing must be invisible to the
findings-driven gate. Telemetry is the observability channel.

**2b. Advisory-vs-blocking severity split** in `CoherenceFinding`:
- New field `severity: "blocking" | "advisory"` (default blocking for
  backwards-compat).
- `parse_foundation_contracts` returns valid entries + findings split by
  severity.
- **All findings-driven gates** (Codex round 3 — must update all 3):
  - `persist_foundation_contracts_from_charter` at `v5_capability_inventory.py:898`
  - `persist_feature_owned_paths_from_charter` at `v5_capability_inventory.py:1075`
  - `partition_findings` block at `dispatch.py:393`
- Each gate changes from `findings ≠ []` to `any(f.severity ==
  "blocking" for f in findings)`. Persistence runs even when only
  advisory findings exist; task metadata gets written.
- Update existing finding emitters to mark non-fatal ones as `advisory`
  (e.g. unknown extra field, deprecated shape).

**Regression test (Codex round 3 must-fix):** integration test where
`parse_foundation_contracts` returns 1 advisory finding (e.g. a
`deprecated_field_shape` advisory — NOT an alias canonicalization,
since per Layer 2a aliases produce telemetry not findings) AND valid
`feature_owned_paths`; task graph must still get the feature_owned_paths
persisted. Today it would silently swallow them because persistence
blocks on any finding.

**Concretely for the immediate bug:** the architect's `"owner"` would be
canonicalized → contract parses → no finding → run proceeds. NO loop.

**Effort:** ~80 LOC parser changes + 40 LOC severity field plumbing + 80 LOC tests.

**Verify:** unit tests per alias. Integration test: run with CHARTER
that uses `"owner"` → 4 contracts parsed, 0 findings, run proceeds.
Negative test: truly invalid entry (e.g. missing `path` entirely) emits
`blocking` finding and still blocks at dispatch.py:393.

**Out of scope here, deferred to Layer 3:** "0 contracts parse → derive
from git diff." That's not parsing, it's inference; treat separately.

**Risk:** alias canonicalization could mask the rare case where a
foreign field name was a real bug indicator. Mitigate by emitting a
non-finding telemetry event (`field_alias_canonicalized`) we can grep in
narrative logs without blocking.

### Layer 3 (P1, next session): Behavioral fallback — DEGRADABLE-KIND ALLOWLIST

Codex round 1: blanket "annotate + proceed" is unsafe. Integration
journeys are finite; they may not catch ownership bugs the partition
gate was supposed to prevent. Restricted to a documented allowlist.

**Allowlist (the ONLY kind currently safe to annotate-and-proceed):**

| Kind | Why safe to degrade | Annotation integration consumes |
|---|---|---|
| `feature_owned_paths_overlap` | Phase 3 already wired this — integration prompt has explicit union instructions for these paths | `decomposition_overlap_unresolved` |

**Candidate (NOT allowlisted) — needs deeper justification before adding:**

| Kind | Why candidate | Why not safe yet (Codex round 2) |
|---|---|---|
| `foundation_seeded_feature_path` (foundation committed file that's in a feature's declared path) | Integration's `git merge` brings both contributions in | **Merge success does not prove ownership semantics.** The feature may rely on the foundation NOT touching its path; conflict-free merge doesn't mean the runtime behavior is right. Needs per-kind plan + integration prompt enhancement before degrading. |
| Field-alias drift (Layer 2a will canonicalize) | Telemetry channel, not a finding | n/a — this is event-channel work, not allowlist work |

**NOT degradable (must block as today):**
- `foundation_contracts_contract_invalid` (genuine bad shape after
  canonicalization) — without contracts, features can't know what to
  import; advisory-only ships a broken product.
- `foundation_contract_owner_missing` (no agent owns the contract path).
- `foundation_contract_not_owned_by_owner` (contract says X owns Y but
  X's owned_paths don't include Y) — architectural inconsistency.
- `feature_overlaps_foundation_contract` / `feature_nested_under_foundation_tree`
  (feature claims a contract path) — would let features write into
  foundation's invariants, breaks the contract.
- "0 contracts parsed" → DON'T fall back to `git diff` inference. That's
  a strong signal the CHARTER is broken; block + plan-amendment.

**Integration packet/prompt changes required (Codex round 1 finding):**
The integration packet currently only carries `decomposition_overlap_unresolved`
in the `parent` block (dispatch.py:1827). To add a new degradable kind,
both:
1. Add the annotation field to `_write_integration_packet`'s `parent` block.
2. Add a Step 2-equivalent instruction in `lead-integration.md` so the
   integration agent knows how to consume it.

**Sequencing:** Layer 3 expansion (beyond `feature_owned_paths_overlap`
which is already done) is **NEXT SESSION** work. Requires:
- A separate plan doc per new degradable kind.
- Codex-gate before each addition.
- Live validation showing integration handles the case correctly.

**Out of this session.**

### Layer 4 (P1): Concrete JSON example in lead-architect.md

Prompt drift is reducible. Add a full literal JSON example to the
architect prompt showing every required field with the exact name. Not
"you'll write something like" — a literal block the architect can copy
verbatim. Phase 3 already added 4 resolution patterns; this extends to
JSON shape.

**Effort:** ~50 lines of prompt. Validate with 3 diverse re-runs.

**Risk:** small. Even if it doesn't reduce drift to 0, it raises the
fidelity rate.

### Layer 5 (P2): Audit-style sweep for "while loop with check"

Codify Layer 1's discipline as a static-ish check. Every dispatch-style
loop has either:
- A bounded iteration count, OR
- An explicit state transition that progresses each loop, OR
- A `# rationale: this loop is naturally bounded by X` comment

Document the pattern in CLAUDE.md. Catch new violations in code review.

**Effort:** audit ~30 min + comments + CLAUDE.md edit.

## Plan sequencing (REVISED after Codex round 1)

| Order | Layer | When | Reason |
|---|---|---|---|
| 1 | **Layer 2a: alias canonicalization** | This session | Smallest fix, no semantic change, unblocks #89 immediately |
| 2 | **Layer 2b: advisory/blocking finding split** | This session | Required for 2a to be effective at dispatch.py:393 + persistence |
| 3 | **Layer 1: K=1 scheduler bail-out + stable signatures** | This session | Subsumes bug #89's worst-case (loop even if 2a fails on a future case) |
| 4 | **Layer 4: JSON prompt example in lead-architect.md** | This session | Reduces drift rate; small change |
| 5 | **Narrow audit:** scan all scheduler-style emissions for K=1 budget compliance | This session | Catches new violations introduced by Layers 1-3 |
| 6 | Live re-validate Phase 3 (`--tier modular` multi-domain) | This session | Ship-test the robustness layer alongside Phase 3 |
| — | **Layer 3 (behavioral fallback — allowlist expansion)** | NEXT session | Higher-risk per Codex; needs per-kind plan + Codex-gate |
| — | **Layer 5 broad audit + CLAUDE.md culture doc** | NEXT session | Not code-blocking |
| — | **Address brittleness G** (unmapped terminal origins default to PRODUCT/LAND) | NEXT session | Codex-flagged; merge.py:1023 cleanup |
| — | **Tests Codex flagged (`test_no_architect_cascade` semantics)** | This session, alongside Layer 1 | Required to land Layer 1 without breaking known-good behavior |

**Layer 4 priority correction:** Codex caught the inconsistency. Layer 4
is P0 in sequencing because it pairs naturally with Layer 2 (prompt
fidelity is the upstream of parser tolerance).

## What does NOT belong in this plan

- Removing more orchestrator pre-flight — we already did that (Phase 2c,
  T1-1). Further orchestrator-pre-flight removal isn't this kind of
  brittleness.
- Bigger refactors to dispatch's state model (your "Direction 3" idea) —
  worthy but separate; logged in v6 punch-list as "explicit state machine
  for dispatch loop."

## Codex gate trail

- **Round 1**: NEEDS REVISION — K=2 wrong universally, Layer 3 blanket-degrade
  unsafe, Layer 2 missed advisory/blocking conflation, "derive from git diff"
  not parsing, 4 missed brittleness patterns.
- **Round 2**: NEEDS REVISION — Layer 3 allowlist still overstated
  (`foundation_seeded_feature_path` not safe yet), alias-conflict rule
  missing, telemetry needs concrete channel, K-split missing 4th
  external-effect/probe class, Layer 1 must ship after Layer 2.
- **Round 3**: NEEDS REVISION — `persist_feature_owned_paths_from_charter` had the same all-findings gate; Layer 2b must update all 3 persistence/gate sites. Regression test required. Plan's "independently shippable" claim contradicted the Layer 1→2b dependency.
- **Round 4**: **APPROVED.** All 3 persistence/gate sites enumerated; regression test required; dependency language softened. Plan implementable as-is.

## Plan sequencing dependency

Confirmed Codex round 2: **Layer 1 ships AFTER Layer 2** in this session.
- Layer 2 establishes the severity split. Without it, Layer 1's "annotate
  + suppress" still triggers blocking-by-default `partition_findings`
  check at dispatch.py:393.
- Layer 2a (alias canonicalization) is the smallest fix and unblocks
  #89 directly: the architect's `"owner"` would canonicalize → contracts
  parse → scheduler doesn't trip.
- Layer 1's K=1 budget is the FAILSAFE for cases Layer 2 doesn't cover.
