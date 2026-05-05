# Spec edit-and-recompile mid-build invalidation (codex-followups A6)

## Problem

The plan promises: "edit spec mid-build → invalidates dependent in-flight
groups wholesale". Reality before this change:

* `otto/web/spec_review_routes.py` blocks any post-approval edit (HTTP
  409). The only exit hatch is the agent-side amendment path
  (`otto/spec_amend.py`) which is scoped per-Group and never cascades to
  peers / dependents.
* If the user changes their mind mid-build (e.g. drops a Feature, renames
  a Group, adds a `dependencies:` link), there is no surface to land
  that change without aborting the run and re-running from scratch.

A6 closes that gap with the smallest sensible footprint:

* allow post-approval edits while a build is in flight, gated by a new
  lifecycle state `editing_in_flight`;
* compute the set of Groups whose Spec contributions changed;
* emit a `group.invalidated_by_spec_edit` event for each such Group so
  the build orchestrator can abort the worktree and re-dispatch on the
  fresh spec.

## Scope clarifications (design ambiguities resolved)

* **Tier 1 fields stay locked.** The edit endpoint already enforces
  `intent_hash` round-trip; intent / intent_hash cannot drift via this
  path. Tier-1 violations stay 409.
* **Mid-build edit applies to ANY Spec field that round-trips through
  `parse_spec_md`.** The runtime trusts the parser's structural
  invariants (no dup ids, no cycle, etc.). We do NOT separately gate
  guardrails-only / non-goals-only edits; the simpler answer is "any
  edit that survives `parse_spec_md(...)` is welcome". If the user
  edits only guardrails, no Group surface changes, the invalidation
  set is empty, and nothing aborts.
* **Direct invalidation criteria** — a Group `g_new` is invalidated vs
  `g_old` if ANY of the following changed:
    * `name`
    * `feature_ids` (set inequality)
    * `dependencies` (set inequality)
    * `owned_paths` (set inequality)
    * `checks` (full equality on the dataclass)
    * Group disappeared entirely (id not in new spec).
* **Cascading invalidation** — any Group whose `dependencies` includes
  a directly-invalidated Group is also invalidated (transitively). This
  matches the plan promise that dependents re-build on the fresh spec.
* **Untouched Groups** — Groups whose post-edit shape equals their
  pre-edit shape AND whose deps closure does not contain an
  invalidated Group are left alone. Their in-flight attempt continues.
* **In-flight worktree handling on invalidation** — abort in place. The
  build loop checks the journal between attempts (cheap) and breaks
  out with `GroupResult(status=BLOCKED, failure_narrative="invalidated
  by spec edit", …)`. The runner then re-dispatches the same Group on
  a fresh attempt using the post-edit spec. The invalidated branch is
  left unmerged on disk for forensic value; future runs garbage-collect.
* **New lifecycle state `editing_in_flight`** — set by the runner when
  it begins the build phase (and reverted to `approved` when the build
  completes). The edit endpoint accepts a POST `/edit` only when
  lifecycle ∈ {`draft`, `editing_in_flight`}. Approved-but-not-building
  remains 409 (preserves the documented "amendment-flow only" promise).

## Files

* `otto/web/spec_review_routes.py`
  * Add `editing_in_flight` to `VALID_LIFECYCLES`.
  * Allow `/edit` when `lifecycle == "editing_in_flight"`. Compute the
    invalidation plan, persist + journal a `group.invalidated_by_spec_edit`
    event per invalidated Group.
  * Continue to 409 on `lifecycle == "approved"` (no live build).
* `otto/spec_amend.py`
  * Add `compute_invalidation(old_spec, new_spec) -> InvalidationPlan`.
  * `InvalidationPlan` exposes `entries: list[InvalidationEntry]` where
    each entry carries `group_id`, `reason`, `direct: bool`.
* `otto/spec_state.py`
  * Add `group.invalidated_by_spec_edit` to `EVENT_KINDS` /
    `EventKind`. Phase mapping: this event downgrades the Group's
    journal phase to `INVALIDATED` so subsequent replays can decide
    re-dispatch.
* `otto/build.py`
  * `_run_slice` checks for an `group.invalidated_by_spec_edit` event
    targeting the current Group between attempts. If present, return
    `BLOCKED` with `failure_narrative="invalidated by spec edit:
    <reason>"`. The build orchestrator (`run_build`) does NOT
    re-dispatch directly — it surfaces the result and the runner
    decides.
* `otto/runner.py`
  * After `run_build` returns, scan the journal for
    `group.invalidated_by_spec_edit` events and re-dispatch any
    invalidated Group whose post-edit shape exists in the new spec.
    Re-dispatch reuses `run_build` against the subset (one new
    `run_build` call with a Spec subset honouring the original
    deps DAG). Detect at most ONCE per pipeline run; further edits
    require a fresh `otto build --resume`.

## Out-of-scope deferrals (honest)

* **Concurrent multi-edit during a single build** — the implementation
  re-dispatches once. A second edit landing during the re-dispatch is
  observable in the journal but won't trigger a third pass; users get
  a clear log line ("spec edit landed during re-dispatch; ignored —
  resume the run").
* **Worktree cleanup of aborted branches** — left for the GC follow-up;
  branches are unmerged on disk, harmless.
* **Component invalidation** — Components are out of scope; they have
  no `feature_ids` and the merge-queue assumptions about Components
  diverge enough that mixing them in this hook risks correctness. A
  follow-up can mirror the Group logic if the need arises.
* **`actions.py` Mission Control surface** — not touched. The HTTP
  edit endpoint is the single ingress; MC clients call it the same
  way the spec-review UI does.

## Verification

* Unit test `compute_invalidation` for: name change, feature_ids
  change, dependency change, no-op edit, cascading dep invalidation,
  Group removed, Group added.
* Unit test for the new `EVENT_KINDS` membership and phase mapping.
* Integration-ish test in `tests/test_runner.py` that simulates a
  build start, drops a `group.invalidated_by_spec_edit` event, and
  asserts the runner re-dispatches the affected Group.
* End-to-end `/api/specs/{id}/edit` test asserting:
    * 409 on `approved` lifecycle (current behaviour preserved);
    * 200 on `editing_in_flight` lifecycle, with journal events emitted.
