# Backlog

Forward-looking items captured during the Part 1–4 simplification
campaign + the v5→run rename. Not blockers; not in flight. Listed here
so the gaps stay visible.

Format: each entry has **what** / **why** / **rough size** so future
sessions can pick one up without context-loading the whole campaign.

Audit reports backing these items live in `archive/audits/{,round3,round4}/`.

---

## ~~Prompt content — 27 findings (Round 4)~~ — DONE (R4 follow-up)

All 27 findings applied to `otto/prompts/lead.md` (+134 LOC) and
`otto/prompts/lead-integration.md` (+41 LOC). Each section ends with a
`<!-- audit:F-NN applied -->` marker for traceability. Constraints
respected: webapp/React/FastAPI guidance kept as the canonical example;
non-webapp branches added; JSON-schema strict-check work left in code
(intentional — only the prompt was tightened). Linkboard e2e run
afterward verified the prompt rewrites don't regress passing flows.

---

## Prompt content (original audit) — pre-Round-4 historical

`otto/prompts/{lead.md,lead-integration.md,setup-claude.md}` were
deep-audited for brittleness, magic numbers, contradictions, and
project-kind assumptions. 2 HIGH-severity, 15 MEDIUM, 10 LOW. Full
catalog at `archive/audits/round4/audit-prompt-content.md`.

### HIGH severity

**F-05 — Framework/stack pinned without fallback** (lead.md:73-74). Prompt
hard-codes "Vite/TS-strict, React, zustand, FastAPI, SQLAlchemy single-Base,
ports/start.sh" as the assumed stack. A CLI / library / Vue project gets
misled. Fix: reframe as "use the stack from DECOMP_RUNTIME_CONTEXT or
the project's existing stack; do NOT assume React/FastAPI/SQLAlchemy."

**F-25 — Integration agent path whitelist excludes non-JS/Python**
(lead-integration.md:95-99). Whitelist includes `package.json`,
`pyproject.toml`, `uv.lock` but not `Cargo.toml`, `go.mod`,
`CMakeLists.txt`. Fix: replace fixed list with categories
("source dirs, manifests, lock files, config files").

### MEDIUM severity (15) — categorical

- **Magic numbers without justification**: "3-5 build leaves" (lead.md:45),
  `dec-...` decision_id format unspecified (lead.md:254), `check: literal vs semantic`
  with no examples (lead.md:104).
- **Project-kind brittleness**: 7 separate sections assume webapp+React+
  SQLAlchemy (lead.md:73-74, 87-92, 101-102, 117-130, 138-145, 149, 150-167).
  Each leaves CLI / library projects without guidance.
- **Contradictions**: "avoid recursive decomposition" vs "architect MUST
  NOT decompose" (lead.md:54 vs 68). "Mock at boundary" (lead.md:223) vs
  "no mocks allowed" (lead-integration.md:46).
- **Undebuggable instructions**: "do not run E2E on empty shell" but never
  defines "empty shell" (lead.md:212). "Verify with smallest build command"
  but doesn't say how to discover it (lead.md:211).
- **Format coupling without enforcement**: Verdict schema described in
  prompt (lead.md:237-256) but lead.py accepts any dict with "verdict"
  and silently fills defaults. Same for `decisions_appended` and
  `intent_coverage` shape.

### LOW severity (10)

Mixed voice ("you" vs "the architect"), undefined terms ("empty shell",
"smallest command"), unused input vars (`IS_ROOT`). All clarity issues.

**Recommendation:** prompt rewrites need real-LLM testing to validate that
the fixes don't regress current passing runs. Defer until there's a
session dedicated to prompt engineering with budget for live-run validation.

---

## CLI gaps from the v5→run promotion

### `otto verify [<session-id>]` — re-verify without rebuilding

**What:** A new top-level verb that runs only the journey verification +
clean-deploy oracle against the current main (or a named session). No
spec compile, no Lead, no merge. Just re-cert.

**Why:** With `otto certify` removed in Part 1, there's no clean way to
re-verify a product after a manual edit or after a passing run. Today
the only options are (a) re-run `otto run` (resume short-circuits if
nothing changed, but it still touches the pipeline) or (b) call
`otto clean-verify` (mechanical only, no journeys). Power users will
want a dedicated re-cert path.

**Size:** ~50–100 LOC of new CLI wiring; re-uses
`otto.v5_clean_verify.verify_from_clean_oracle` and
`otto.lead_verify.run_journey_verification`. New file
`otto/cli_verify.py`; register from `cli.py`.

**Risk:** Low — read-only verification, no graph mutation.

---

### `--target` / `--standard` / `--thorough` flags on `otto run`

**What:** Re-introduce the legacy verbosity/focus flags as syntactic
sugar on the unified `otto run`. e.g.
`otto run "p95 < 100ms" --target` would tier-bias the Lead toward
measurable-goal framing; `--thorough` would enable adversarial
edge-case probes in the certifier.

**Why:** Part 1 collapsed `otto improve bugs|feature|target` and the
`otto certify --standard|thorough` modes into one freeform `otto run`.
The Lead is supposed to read intent and pick a tier, but there's no
shorthand for measurable-goal or adversarial-test framing.

**Size:** ~30–80 LOC depending on how deep the flag pushes into the
pipeline. The `--target` flag is mostly intent-rewriting; `--thorough`
needs an adversarial-probe path in the certifier.

**Risk:** Low–Medium. Mostly additive; the danger is the flags
overlapping with `--tier` semantics in confusing ways.

---

## Carry-overs from the simplification audits (audit3 + earlier)

### Workflow: collapse repair-loop redundancy (audit3 finding)

**What:** The `_repair_stale_target_and_retry_merge` function in
`otto/v5/repair.py` is misnamed (audit3 confirmed). It runs a full Lead
repair agent (~200–300s) and is the only auto-fix path for upward-
merge-gate conflicts. Two candidate moves:

1. Rename to `_repair_child_upward_merge_after_conflict` to reflect
   what it actually does (3 call sites in `otto/v5/merge.py`).
2. Add `git fetch origin <target>` before the merge attempt so stale-
   ref failures are eliminated as a class — doesn't replace the
   Lead-agent repair, just removes one failure mode.

**Why:** The misleading name causes operators to underestimate the
cost when the function fires. The `git fetch` add-on closes a small
class of stale-ref merge failures without changing other behavior.

**Size:** Rename = 30 min, no behavior change. `git fetch` add = 30 min,
small behavior change (fetches before each merge attempt).

**Risk:** Low. Both items are scoped narrow.

---

### Workflow: fix conditional smoke preflight bug (audit3 finding)

**What:** Bug 1 in `audit3-repair-loops.md` — smoke preflight runs only
at one of three merge call sites (when `run_smoke_preflight=True`); the
other two skip it. Fix: either pass the flag at all sites or always
run smoke after merge.

**Why:** Some out-of-scope failures are silently missed at two of the
three merge call sites. Audit-flagged as MEDIUM.

**Size:** ~10 LOC, Codex-gated per concurrency-policy.

**Risk:** Medium. Always-on smoke means more time per merge and may
expose previously-silent failures as new noisy verdicts. Needs a real-
LLM run to validate.

---

### State: cost canonical in summary.json (P2-D deferred)

**What:** Make `summary.json` the canonical cost record. Drop the
duplicate cost fields in `checkpoint.json`, `proof-packet.json`, and
elsewhere. Today cost is written in 4 places, which has caused at
least one silent drift bug (BUG-CRITICAL fixed in P2-A).

**Why:** Single source of truth = no drift. Audit-2 estimated ~150 LOC
saved + invariant simplification.

**Size:** ~150 LOC + careful test of resume paths (summary.json must
exist at every checkpoint write point, which today isn't guaranteed).

**Risk:** Medium. Touches resume contract; needs coordinated update of
`mission_control/model.py` + `proof-packet` renderer.

---

### State: group `last_*` fields under `_observability` (P2-D deferred)

**What:** Move 7 `last_*` fields out of the top-level checkpoint schema
into an optional `_observability` dict. Today they're never read on
resume but clutter every checkpoint.

**Why:** Schema clarity. Resume-critical vs observability-only fields
are visually mixed.

**Size:** ~80 LOC + coordinated rename across
`otto/mission_control/{model.py,serializers.py}` (which consume
`last_round_failures` and `last_activity_at`).

**Risk:** Medium. Cross-surface schema rename.

---

### Stragglers from the v5→run rename

**What:** Three artifacts still reference deleted-since-Part-1 CLI
verbs but were out of scope for the rename pass:

1. `scripts/web_as_user.py` W12b scenario uses `otto queue build`.
2. `scripts/bench_build_merge_with_cert.py` uses `otto queue build`
   + `otto merge --all` (both gone since Part 1).
3. `tests/test_improve_branches_from_prior_run.py` docstring mentions
   `otto queue improve …`; the 11 tests pass because they bypass the
   CLI verb directly.

**Why:** These were broken before the rename; calling them out for a
focused dead-script cleanup pass.

**Size:** ~30 min — delete or update each.

**Risk:** Low; mostly delete-and-confirm-smoke.

---

## Architecture: deferred from Part 2 audit

### Removing the foundation-contract-amendment system

**What:** Audit's Candidate C: when a child's merge blocks on a
contract union conflict, instead of scheduling an amendment task,
re-run root decomposition with the conflict as context.

**Why:** ~1,800 LOC of subsystem complexity could go. But amendments
preserve the owner's intent while fixing compatibility; re-decomp might
overshoot and change the owner's design.

**Size:** Significant. ~1–2 weeks of careful work + real-LLM
validation. Needs user sign-off on the semantic change (the audit
explicitly flagged this as HIGH risk).

**Risk:** HIGH. The user has explicitly deferred this twice (Part 2
and Round 3). Not a quick win.

---

## Audit-only follow-ups

### Mission Control deep dive

**What:** The Part 2 audit said MC's 13.8k LOC is "justified", but the
audit was high-level. A deeper pass may find consolidation
opportunities in `mission_control/service.py` (5.2k LOC) or
`run_view.py` (1.8k LOC).

**Size:** 1 audit session (~1 hour).

---

### Web frontend (`otto/web/client/`)

**What:** The React/TypeScript client was never audited in Parts 1–3.
The committed bundle in `otto/web/static/` is rebuilt by
`scripts/check_bundle_committed.py`. Probably has its own cruft.

**Size:** 1 audit session.

---

## Round 4 carry-overs (brittleness audit, May 2026)

### Migrate specific call sites to use `otto/schemas.py` TypedDicts

**What:** Round 4 added `otto/schemas.py` with `TaskGraphEntry`,
`PipelineEvent`, `RepairPacket` as documentation TypedDicts (total=False;
zero-runtime cost). The producer signatures (e.g.,
`otto.queue.task_graph.record_task`, `v5_runner._emit`,
`v5_preflight_repair.RepairPacket`) can opt in to enforced typing one
at a time.

**Why:** Schemas as docs help AI editors grep for shapes. Enforcing
them at producer sites catches typos at the source. Today
schemas.py is read-only documentation; nothing references it.

**Size:** ~30 min per producer site. Start with `record_task` (smallest,
clearest contract).

**Risk:** Low — TypedDicts with total=False are forward-compatible.

---

### ~~13 more silent-return paths to log~~ — DONE (R4 follow-up)

10 done in Round 4 + R4 follow-up across v5_runner, cli_queue, config,
cli, web/run_view_routes, mission_control/{autopilot,model},
queue/runtime. Remaining ~8 are in files without an established logger
(otto/agent/codex.py, mission_control/adapters/queue.py,
v5_preflight_repair.py); each needs a lightweight logger setup before
the log line. Low priority.

---

### ~~Centralize timestamp helpers~~ — DONE (R4 follow-up)

71 raw `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` sites
across 25 modules migrated to `otto.observability.iso_timestamp()`.

### JSON-read helpers — STILL DEFERRED

6 helpers (`_read_json`, `_read_json_object`, `_read_json_artifact`,
etc.) with different size-limit and error-handling semantics. A unified
API needs to capture max_chars / default-on-error / dict-vs-Any as
parameters; not a pure rename. **Size:** ~1 hour design + ~30 sites of
migration. **Risk:** Medium (semantic differences could silently
change behavior).

---

### ~~Rename names-that-lie~~ — DONE (R4 follow-up, Codex-gated)

Renamed:
  `_repair_stale_target_and_retry_merge` → `_repair_child_upward_merge_after_failure`
  `_StaleTargetRetryResult` → `_UpwardMergeRetryResult`
  `_carry_prior_repair_packets` → `_carry_and_reset_prior_repair_packets`

3 other names-that-lie in `audit-ai-sloppiness.md` are smaller-scope
and remain (e.g. `_repair_*_once` siblings). Pick up individually if
they bite again.

---

### ~~Hardcoded `otto_logs/` path violations~~ — DONE (R4 follow-up)

6 sites migrated to use `paths.sessions_root()` / `paths.logs_dir()` /
`paths.cross_sessions_dir()` helpers:
  v5_runner.py (2 sites), v5_capability_inventory.py, cli_proof.py,
  v5_spec_cache.py, web/session_resolver.py, v5/dispatch.py (2 sites),
  v5/repair.py.

Remaining literal `"otto_logs/"` strings are legitimate (gitignore
file content; display strings in journal.py; the literal "allowed_paths"
arg sent to a repair agent in v5/repair.py:1352-1353).

---

### Prompt content fixes — 27 findings

See the **Prompt content** section at the top of this file. 2 HIGH +
15 MEDIUM + 10 LOW findings catalogued in
`archive/audits/round4/audit-prompt-content.md`. Defer until a
prompt-engineering session with real-LLM validation budget.

---

## How to use this file

Pick an item; verify it's still relevant (the codebase has been moving);
check its "Risk" + "Size" against current priorities. Each item is
small enough to land in one focused session — most under 200 LOC.

Items are intentionally NOT ordered by priority because priority
changes with context. Read all of them; pick the one that matches the
session's energy.
