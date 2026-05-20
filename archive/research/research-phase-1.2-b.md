# Research + Plan — Phase 1.2-B (cross-run repair-packet carrying)

Created 2026-05-19. Per user: **Codex off, full impl (option b: carry the
entire repair packet across runs, not just session_id)**. Extra-careful
TDD since no adversarial-review safety net.

## Why this is needed

Phase 1.2-A (88d974a3b) resumes the *orchestrator* — skips compile +
decompose + child rebuild, re-enters at integration. But each
`otto v5 run` creates a new timestamped `session_dir`, so the new run's
`repair_packet.json` path differs from the prior run's path.
`_run_preflight_payload_repair_session`'s "load-if-exists" check
(v5_preflight_repair.py:1296-1300) fires only intra-run, never
cross-run → every resume's repair turn starts a *fresh* Claude SDK
session, re-reading + re-diagnosing from scratch.

## Schema (`RepairPacket`, v5_preflight_repair.py:85-148)

10 fields; one is the schema-bug source:

| field | meaning | cross-run handling |
|---|---|---|
| `repair_unit: dict` | unit meta (id, worktree, …) | **stable** — `worktree` is the project_dir which persists |
| `acceptance_oracle: dict` | success criteria | stable |
| `latest_oracle_result: dict` | last oracle output | stable (informational) |
| `product_contract: dict` | invariants | stable |
| `integration_context: dict` | context | stable |
| `attempt_history: list[dict]` | prior repair attempts | **carry verbatim** (preserves "what's been tried") |
| `current_state: dict` | branch / head / pre_repair_head / scope_baseline / scope_baseline_captured_at | **carry verbatim** — HEAD refs remain valid since we use commit_worktree (no destructive ops) |
| `budget: RepairBudget` | wall_clock_s, attempt-counts | **fresh budget per run** — the existing function does `loaded.budget = packet.budget` (overrides with incoming). New run gets a fresh budget per turn. |
| `packet_dir: Path` | where packet lives | **THE BUG** — serialized as a full path including the OLD session_dir. Subsequent `persist()` would write to the OLD path. **Must rewrite on copy.** |
| `agent_session_id: str` | Claude SDK session id | **the prize** — pass to options.resume next turn |

Sibling artifact: `repair_packet.events.jsonl` in the same `packet_dir`.
Append-only event stream. Carry too for continuity.

## Path construction

For the integration smoke-pre repair: `<session_dir>/integration/repair/
<unit_name>/repair_packet.json`. Real-world `<unit_name>` values seen:
`root-checkout_clean-root_integration_start`, `root-integration_smoke-pre_agent`.

Mirror path under the new session: same relative shape under the new
`<root_session_dir>` that `_resume_root_from_checkpoint` already manages.

## Load semantics (the key seam)

`_run_preflight_payload_repair_session` (v5_preflight_repair.py:1292+):
```python
packet = repair_packet                          # incoming (caller-constructed; new packet_dir)
if packet.packet_path.exists():                 # ← we want this to be True post-1.2-B
    loaded = RepairPacket.load(packet.packet_path)  # load file at NEW path
    loaded.budget = packet.budget               # override budget
    packet = loaded                             # use loaded
packet.persist()
```

`RepairPacket.from_jsonable` (line 126) uses
`packet_dir = Path(str(payload.get("packet_dir") or packet_path.parent))`
— **the serialized `packet_dir` field wins over the load-path's parent**.
So if we copy the prior JSON verbatim, `loaded.packet_dir` is the OLD
path → `persist()` writes to the OLD path. **We must rewrite the
serialized `packet_dir` field to the new mirror path during the copy.**

## Implementation plan

### Step 1 — TDD: pure helper `_carry_prior_repair_packets`

```python
def _carry_prior_repair_packets(
    project_dir: Path, new_root_session_dir: Path
) -> int:
    """Phase 1.2-B: copy the most recent prior session's
    integration/repair/<unit>/repair_packet.json (+ events.jsonl) into
    the new session's mirror path, rewriting the serialized packet_dir
    field to the new location. Most-recent wins per unit.
    Returns the number of packets carried."""
```

Tests in `tests/test_phase_1_2_b_carry_repair_packet.py`:

- `test_carries_session_id_and_rewrites_packet_dir`: fixture creates a
  prior session with one packet (session_id=XYZ, packet_dir=PRIOR_PATH);
  after `_carry_prior_repair_packets`, the new session has the packet at
  the mirror path, its serialized `packet_dir` equals NEW_PATH, and its
  `agent_session_id` is still XYZ.
- `test_copies_events_jsonl_sibling`: events.jsonl is copied too.
- `test_most_recent_per_unit_wins`: two prior sessions both have a
  packet for the same unit; the more-recent one wins.
- `test_no_prior_packets_is_noop`: returns 0; new_session_dir unchanged.

### Step 2 — Wire into `_resume_root_from_checkpoint`

Right after the spec.json copy and before the event emit:
```python
_carried = _carry_prior_repair_packets(project_dir, root_session_dir)
_emit(on_event, {
    "event": "v5_resume_carries_repair_packets",
    "task_id": ROOT_TASK_ID,
    "carried": _carried,
})
```

### Step 3 — CLI banner extension (Polish 1.2-A surface)

When `v5_resume_carries_repair_packets` fires, the resume banner already
shows. Add a line if any packets carried: `"♻ resumed with N prior
repair packet(s) — agent will continue prior conversation(s)"`.

### Step 4 — Live e2e validation

Re-run on the same project_dir (`fastrepro-linkboard-validate-155831`)
that just landed `partial`. With 1.2-B in place, the resume's repair
agent should pick up its prior SDK session — verifiable by:
- Banner line shows `"carried N prior repair packet(s)"`.
- Repair narrative's first few lines should reference prior context
  ("continuing my prior analysis of...") rather than reading the spec
  from scratch.
- Wall-time should be shorter than the prior 1277s resume (less
  re-orientation overhead).

## What deliberately stays out of scope (this turn)

- Cross-run forgetting strategy (when to NOT carry — e.g., if intent
  drifted, the prior agent's context is wrong). Today's
  `_resume_root_from_checkpoint` already guards against intent drift
  (returns None if drifted), so carrying is gated correctly via that.
- Garbage-collecting old packets (`otto_logs/` size). Out of scope.
- Multi-attempt resume bounding. Out of scope.
- Per-turn budget being budget-aware (Phase 1.2-A polish item, not 1.2-B).

## Risk register (Codex off — log them honestly)

| Risk | Mitigation |
|---|---|
| Rewriting `packet_dir` is the only path change; if any OTHER field embeds a session-scoped path, we'd miss it | Schema audit (above) — only `packet_dir` is session-scoped. `repair_unit.worktree` is project_dir-scoped (stable). |
| `agent_session_id` may not be SDK-resumable forever (TTL on the Claude server side?) | Out of our hands; if expired, the SDK falls back to fresh — same as today. Worst case = no improvement, not a regression. |
| `current_state` keys could be stale post-resume (e.g., HEAD advanced via Part B commits) | The agent can run `git log/diff` to reorient; HEAD is informational. Re-baselining is the function's own concern (already handled via `if "scope_baseline" not in current_state: capture_scope_baseline()`). |
| `repair_packet.events.jsonl` append from a new run mixes with prior events | Continuity is the point. Append-only stream with `_written_at` timestamps stays coherent. |
| Most-recent-per-unit collision when multiple prior sessions exist | mtime sort + dedupe by unit name; explicit + testable. |
