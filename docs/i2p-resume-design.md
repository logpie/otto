# `--resume` semantics for the i2p stack — design

**Status:** Implemented (cc-i2p-2 branch, tick — 2026-05-04). Open
questions resolved by user (cost-carry on by default with
`--reset-budget`; `otto certify --resume` supported; spec-edit policy
v1 = refuse-on-mismatch with `--force`). Surgical Feature-level
invalidation deferred to v2.

**Scope:** the new-stack pipeline driven by
`otto/runner.py:run_pipeline` and entered through
`otto/cli_run.py:orchestrate_run` / `orchestrate_certify` /
`orchestrate_improve`. The legacy `otto build --resume` machinery in
`otto/checkpoint.py` is kept verbatim for the old stack; this design
proposes a parallel resume path for i2p that **reuses the same
`paused` pointer + per-session checkpoint contract** but derives
in-flight state from `spec-state.jsonl` rather than from a hand-rolled
`current_round` field.

---

## 1. What "resume" means in i2p

The i2p chain has seven phases (`runner.py` lines 121–374):

```
compile → seed → build → merge → audit → repair (Layer 2) → render
```

Each phase has a different cost / safety story. Resume must be
**phase-aware** — restarting compile is cheap, restarting build wastes
$5–$30, restarting render is free.

| Phase    | Resume granularity            | Cost of restart | What we replay     |
|----------|-------------------------------|-----------------|--------------------|
| compile  | atomic (one agent call)       | low ($0.10–$0.30) | always re-run    |
| seed     | atomic (deterministic, fast)  | ~free           | always re-run     |
| build    | per-Component                 | high            | LANDED/REDUNDANT skipped, FAILED/PENDING re-dispatched, MERGING aborted+restarted |
| merge    | per-Component                 | medium          | re-derive eligibility from spec-state |
| audit    | per-attempt (retry-aware)     | medium          | re-run if `audit_finished=False` |
| repair   | per-failing-Feature           | medium          | re-run if audit non-PASS and fix_agent wired |
| render   | atomic                        | ~free           | always re-run     |

**Default model:** resume re-runs compile + seed + render unconditionally
(they are cheap or free), and uses `spec-state.jsonl` replay to skip
already-LANDED Components in build/merge. Audit and repair re-run
unless terminal events (`audit.finished`, `run.finished`) are already
on the journal.

---

## 2. Persistence model

### Two existing journals (canonical, **no new files**)

1. **`spec-state.jsonl`** (per-session, append-only, owned by
   `otto/spec_state.py`).
   - Already contains `slice.started`, `slice.merge.landed`,
     `slice.blocked`, `audit.attempt.finished`, `audit.finished`,
     `run.finished`, `seed.started`, `seed.finished`, etc.
   - Already has a `replay()` function returning a `RunState` with
     per-Component `phase` (`PENDING` / `BUILDING` / `CHECKING` /
     `FAILED` / `ELIGIBLE` / `MERGING` / `LANDED` / `REDUNDANT` /
     `BLOCKED`), `audit_started`, `audit_finished`, `audit_verdict`,
     `audit_attempts`, `run_finished`, `unreconciled_landed_ids`.
   - **This is the load-bearing observation:** the resume replay
     function already exists. The work is wiring it into the runner.

2. **`checkpoint.json`** (per-session, owned by
   `otto/checkpoint.py`).
   - Owns session-level metadata: `intent`, `command`, `phase`,
     `total_cost`, `started_at`, `git_sha`, `prompt_hash`.
   - Already has a `paused` pointer convention via
     `paths.PAUSED_POINTER` (`otto/paths.py:61`).
   - The legacy fields (`current_round`, `rounds[]`,
     `agent_session_id`) are **not used by the i2p stack** — they are
     legacy. i2p will write a smaller subset.

### Derived view: `ResumePlan`

Resume reads `spec-state.jsonl` + `checkpoint.json` and produces a
**read-only** `ResumePlan` dataclass (new, but a pure derivation —
not a third persisted file):

```python
@dataclass
class ResumePlan:
    session_id: str
    spec: Spec                       # loaded from session_dir/spec/spec.json
    spec_hash: str                   # sha256 of spec.json — invalidates resume on edit
    seed_done: bool
    components_landed: set[str]      # skip in build + merge
    components_in_flight: set[str]   # need git-state recovery + restart
    components_pending: set[str]     # never started
    audit_finished: bool
    audit_verdict: str               # "" if not finished
    halted_reason: str               # surfaced from prior run.finished, if any
    cost_so_far: float               # carry forward into shared BuildBudget
```

Construction (`otto/resume.py:plan_resume`, new):

1. Read `checkpoint.json` to find session_id, intent, spec path,
   spec_hash, started_at, cost_so_far.
2. Validate spec hash: re-hash `<session>/spec/spec.json`. If it
   differs → return `ResumePlan(spec_hash=<new>, …)` flagged as
   `spec_changed=True`; the CLI rejects with "spec was edited; cannot
   surgically resume — re-run fresh or pass `--force-resume`".
3. Load `Spec` from disk (already supported via
   `otto.spec_compile.load_spec`).
4. Call `replay(session_dir, slice_ids=spec.component_ids,
   project_dir=project_dir)` → `RunState`.
5. Project `RunState.slices` onto `landed` / `in_flight` / `pending`:
   - `LANDED` (with reconciled hash) → `landed`
   - `REDUNDANT` → `landed` (already counted as a no-diff success)
   - `BLOCKED` → keep blocked; do **not** re-attempt unless
     `--force-rebuild-blocked`.
   - `MERGING`, `BUILDING`, `CHECKING`, `FAILED`, `ELIGIBLE` →
     `in_flight` (need restart).
   - `PENDING` (or absent from journal) → `pending`.
6. `audit_finished` = `RunState.audit_finished AND
   audit_verdict in {"passed","partial","blocked"}`.
7. Surface `unreconciled_landed_ids` as a warning — these claim
   LANDED but git disagrees. Treat as `in_flight` and rebuild.

---

## 3. Pause triggers

| Trigger                                | What state on disk          | Detection at resume                     |
|----------------------------------------|------------------------------|------------------------------------------|
| Explicit pause (SIGTERM via `otto cancel`) | checkpoint.status=paused, cancel marker | `paused` pointer set; ack queue handled  |
| Crash mid-phase (uncaught exception)   | checkpoint.status=in_progress, no run.finished | scan finds active checkpoint            |
| `kill -9` mid-merge                    | `.git/REBASE_HEAD` or `MERGE_HEAD` exists in worktree | `recover_mid_merge_state()` returns kind ≠ "" |
| Network failure mid-audit              | partial `audit.attempt.finished` events, no `audit.finished` | `RunState.audit_finished == False`      |
| Machine reboot                         | same as crash; no live PIDs  | scan finds active checkpoint; PIDs ignored — i2p has no live SDK session to reattach to |

**Important:** i2p phases are stateless w.r.t. SDK sessions — each
build agent dispatch is a fresh subprocess. Unlike the legacy stack,
**we do not need to reattach to a Claude session id**. Resume is
purely "skip what landed, re-run what didn't" — much simpler than
`otto/checkpoint.py:ResumeState.agent_session_id`.

---

## 4. Mid-merge git recovery (already implemented)

`otto/spec_state.py:recover_mid_merge_state(worktree_dir)` is the
canonical entry point. It:

1. Resolves `.git` (handling submodule / linked-worktree gitdir files).
2. If `.git/REBASE_HEAD`, `rebase-merge/`, or `rebase-apply/` exists
   → `git rebase --abort`.
3. If `.git/MERGE_HEAD` exists → `git merge --abort`.
4. Else if `git status --porcelain` shows `UU`/`AA`/`DD`/etc. → `git
   reset --merge`.
5. Returns `MidMergeRecovery(kind, restart_required, detail)`.

**Resume protocol for the merge phase:**

```text
for component in plan.components_in_flight:
    worktree = worktree_for_component(project_dir, component)
    rec = recover_mid_merge_state(worktree)
    if rec.restart_required:
        emit(session_dir, "slice.merge.eligible", slice_id=component.id,
             detail=f"resume: cleaned {rec.kind} state — {rec.detail}")
    # else: nothing was stuck; component just hadn't started its merge
    # → standard merge_queue dispatch picks it up
```

The resume plan only needs to call `recover_mid_merge_state` against
each `in_flight` Component's worktree (and against the integration
worktree itself, in case the merge was killed mid-rebase on `main`).
The rest of `merge_queue.run_merge_queue` handles re-dispatch via its
existing eligibility logic.

---

## 5. Identifying the paused session

**Reuse the legacy convention.** `otto/paths.py` already defines:

- `PAUSED_POINTER = "paused"` (line 61) — symlink at
  `otto_logs/paused → sessions/<id>` (or a `paused.txt` fallback for
  symlink-hostile filesystems).
- `paths.set_pointer` / `paths.resolve_pointer` / `paths.clear_pointer`.

**Writer integration:** `runner.run_pipeline` should call
`paths.set_pointer(project_dir, PAUSED_POINTER, session_id)` on entry
to the build phase, and `paths.clear_pointer(project_dir,
PAUSED_POINTER)` on `run.finished` (success OR failure — partial /
blocked are still terminal). A SIGTERM handler installed by the CLI
preserves the pointer.

**Reader integration:** `paths.resolve_pointer(project_dir,
PAUSED_POINTER)` returns the session dir to resume into. If the
pointer is broken (target missing), surface
`missing_paused_session_path` in the error like
`checkpoint.py:_missing_paused_session_path` already does.

**Multiple paused sessions** are not supported in v1 (the pointer is
one-deep). If a second `otto run` is started while one is paused, the
new run gets its own session id and the pointer is **stolen** to the
new session — same behaviour as legacy. Document this.

---

## 6. CLI surface

### v1 proposal

| Command                          | Behaviour                                                |
|----------------------------------|----------------------------------------------------------|
| `otto run --resume`              | Auto-detect via `paused` pointer; replay + continue.     |
| `otto run --resume <session-id>` | Force-resume the named session even if pointer is stale. |
| `otto run --resume --force`      | Resume despite spec-hash mismatch / fingerprint mismatch.|
| `otto run` (no flag)             | Fresh run; if a paused session exists, **warn and clear** the pointer (legacy behaviour, keeps testing painless). |
| `otto build --resume`            | Alias of `otto run --resume` once `otto build` is the i2p entry point. Until then: dispatches via `resolve_pipeline_choice` — legacy path keeps `otto/checkpoint.py` semantics; i2p path uses this design. |
| `otto certify --resume`          | **Not supported in v1.** Certify is brownfield + audit-only. Pause window is too small to be worth the complexity. Errors out with "certify is not resumable; rerun." |
| `otto improve --resume`          | Supported — same model as `run`. Spec hash includes the focus hint so changing focus invalidates resume. |

### Auto-detect default

Match legacy: `--resume` with no session id consults
`paths.resolve_pointer`. If absent, fall back to scanning `sessions/`
for the most recent `checkpoint.json` with `status in {paused,
in_progress}` (already implemented in `checkpoint.py:_scan_active_session_checkpoint`
— factor that helper out to a shared spot rather than duplicating).

---

## 7. Edge cases

### 7.1 Spec was re-edited mid-run

`spec.json` SHA changed. **v1: refuse**. Resume requires byte-identical
spec. Print:

```
Spec at otto_logs/sessions/<id>/spec/spec.json was modified after the run paused
(hash changed: <old8> → <new8>). Cannot surgically resume — Components built
against the old spec may now reference nonexistent Features.
Re-run fresh, or pass --force to resume against the new spec at your own risk.
```

`--force` resumes anyway but logs a `spec.regenerated` event for
forensics.

### 7.2 Component succeeded checks but crashed before merge

`spec.merge.eligible` was emitted; no `slice.merge.landed`. Replay
classifies as `ELIGIBLE` → `in_flight`. The merge phase's existing
`eligible_components` logic re-evaluates eligibility (deps still
satisfied since their LANDED events ARE on the journal) and dispatches
the merge. **No rebuild required** — the Component's worktree is
already in place. (Verify: `worktree_for_component` does not delete
on entry. If it does, that's a bug and resume is broken — flag in the
test plan.)

### 7.3 Audit succeeded but render crashed

`audit.finished` is on the journal; `run.finished` is not, AND
`html_path`/`json_path` from `render_run` are missing on disk. Replay
sees `audit_finished=True`, skips audit, jumps to render. Render is
deterministic and cheap.

### 7.4 Layer 2 repair was mid-flight

`audit.finished` with non-PASS verdict, no `run.finished`. Replay
re-runs the entire repair phase (it's idempotent — fix_agent gets
fresh worktree state). Cost is at most one extra repair attempt per
failing Feature.

### 7.5 `--no-build` paused after compile

Compile ran, exited cleanly (`sys.exit(0)`), no checkpoint was
written. Nothing to resume. `otto run --resume --from-spec ...`
is the right escape hatch — already supported.

### 7.6 Unreconciled LANDED events (Pattern A)

`replay(project_dir=…)` returns
`unreconciled_landed_ids`. These claim a commit hash that isn't in
`git log`. **Treat as in-flight**; the journal lied (almost certainly
a prior crash between commit and journal flush — or a force-push
since). Re-running the merge is safe.

---

## 8. Out of scope for v1

- Resume across machines (different host, same project). Worktree
  state is local.
- Resume after a long pause (>7 days). Filesystem rot, dep cache
  invalidation, base branch drift. Refuse with a clear error.
- Resume mid-compile or mid-seed. Both are atomic and cheap; rerun.
- Reattaching to a live SDK agent session. i2p doesn't have one.
- Multiple concurrent paused sessions per project.
- Resume of `otto certify` (brownfield, no build = nothing to resume).
- Resume after the worktree's git base branch has moved (`main`
  fast-forwarded since pause). v1 detects via `git_sha` mismatch
  and errors out unless `--force`.

---

## 9. Implementation plan (ordered)

1. **`otto/resume.py` (NEW, ~150 LOC)**
   - `@dataclass ResumePlan` with the fields from §2.
   - `def plan_resume(project_dir: Path, *, session_id: str | None) -> ResumePlan | None`
     — load checkpoint, hash spec, replay journal, classify Components.
   - `def reject_if_incompatible(plan, current_fingerprint, *, force: bool) -> None`
     — analogue of `checkpoint.py:enforce_resume_command_match`.

2. **`otto/spec_state.py` (extend, ~30 LOC)**
   - Public re-export of `MidMergeRecovery` + `recover_mid_merge_state`
     already done. No changes needed; verify imports.
   - Add `def component_classification(state: RunState) -> tuple[set[str], set[str], set[str]]`
     returning `(landed, in_flight, pending)` — pure helper, used by
     `plan_resume`.

3. **`otto/runner.py:run_pipeline` (modify)**
   - New kwarg `resume_plan: ResumePlan | None = None`.
   - If `resume_plan is not None`: skip compile (use `plan.spec`),
     skip seed (already done — replay shows `seed.finished`), pass
     `landed_components` into `run_build` so it filters its dispatch
     list, pass `audit_finished` to short-circuit the audit phase if
     true. Render always runs.
   - Carry `plan.cost_so_far` into the shared `BuildBudget`.

4. **`otto/build.py:run_build` (modify)**
   - New kwarg `skip_components: set[str] = frozenset()` — skip
     dispatch entirely; produce a synthesized `SliceResult` marked
     `phase=LANDED, cost=0, wall=0, source="resume"` so render's
     accounting stays honest about prior runs.
   - Before each `_run_slice`: call
     `recover_mid_merge_state(worktree)` and emit a journal event if
     anything was cleaned. Cheap; safe on clean worktrees.

5. **`otto/merge_queue.py:run_merge_queue` (modify)**
   - Same `skip_components` plumbing. Eligibility logic already reads
     `slice.merge.landed` events from the journal, so already-landed
     Components are skipped naturally; the kwarg is belt-and-braces.
   - Before integration merge: `recover_mid_merge_state(integration_worktree)`.

6. **`otto/audit.py:run_audit` (modify)**
   - New kwarg `skip_if_finished: bool = False`. When set, check the
     journal for `audit.finished`; if present, return a synthesized
     `AuditResult` reconstructed from `audit.finished.extra` + the
     persisted feature-verdicts JSON. Avoids a $0.50–$2 re-audit when
     all we lost was the render step.

7. **`otto/cli_run.py` (modify)**
   - Add `--resume` / `--resume <id>` / `--force` flags to `otto run`,
     `otto build`, `otto improve`.
   - Before allocating a fresh session id: call
     `resume.plan_resume()`. If a plan exists and `--resume` was
     passed, reuse `plan.session_id` and pass `resume_plan=plan` to
     `run_pipeline`.
   - Print a banner mirroring `checkpoint.py:print_resume_status`
     (but i2p-shaped: "Resuming session <id>: 3/7 Components landed,
     2 in-flight, 2 pending; audit not yet run.").

8. **`paths.set_pointer` / `clear_pointer` calls in `runner.py`**
   - Set `PAUSED_POINTER` on entering build (first phase that can
     pause meaningfully). Clear on `run.finished` regardless of verdict.

9. **Heartbeat / cancel ack integration**
   - `runs/lifecycle.py:_make_atomic_terminal_callback` already
     polls cancel commands. Wire it through to `run_pipeline` so
     `otto cancel <id>` triggers a clean pause that survives resume.
     (Bulk of this exists; need to confirm it's called from the i2p
     entry points, not just legacy.)

---

## 10. Test plan

### Unit tests (new — `tests/test_resume.py`)

- `test_plan_resume_no_checkpoint_returns_none`
- `test_plan_resume_classifies_landed_in_flight_pending`
- `test_plan_resume_rejects_spec_hash_mismatch_without_force`
- `test_plan_resume_treats_unreconciled_landed_as_in_flight`
- `test_plan_resume_treats_redundant_as_landed`
- `test_component_classification_handles_blocked_terminal`

### Integration tests (new — `tests/integration/test_resume_flow.py`,
restored with i2p semantics — file existed pre-C.1f, recreate with
new contract)

- **Mid-build kill:** spawn a fake build agent that sleeps; SIGTERM
  the process between Component 2 and 3; resume; assert Components
  1–2 are not re-dispatched and 3+ are; final verdict matches a
  baseline single-shot run.
- **Mid-merge kill:** kill during a deliberately-conflicting rebase;
  assert `recover_mid_merge_state` returns kind=`rebase`; resume
  re-dispatches the merge; final integration commit lands.
- **Audit-only resume:** simulate `audit.finished` written then
  crash before render; assert resume runs only render and emits a
  fresh `run.finished`.
- **Spec-edited refusal:** mutate `spec.json` between pause and
  resume; assert resume aborts with the spec-hash-mismatch error.
- **`--force` overrides spec mismatch:** same setup, `--force`
  flag; assert run proceeds and emits `spec.regenerated`.
- **Resume across `--resume` flag round-trip:** start, pause,
  `otto run --resume`, complete, then `otto run --resume` again —
  asserts the second `--resume` says "nothing to resume" (pointer
  cleared on completion).

### Smoke test (manual, run once before merge)

- A small greenfield project; `^C` mid-build; `otto run --resume`;
  verify the build completes without redoing landed Components and
  without exceeding ~$1.20 of incremental spend (vs. ~$1.00 baseline).

---

## 11. Open questions for the user

1. **Cost-carry semantics.** Should `cost_so_far` from the prior
   attempt count against the shared `BuildBudget` cap, or should
   resume reset the budget for the second attempt? Argument for
   counting: matches user intent ("$30 total"). Argument for
   resetting: makes resume more forgiving when the first attempt
   blew most of the budget. **Default proposal: count it** (honest);
   add `--reset-budget` for the escape hatch.

2. **Should `otto certify --resume` be supported?** §6 proposes no
   (atomic-ish, cheap to rerun). But a real audit can be 5–10 min /
   $2; if a user pauses 8 min in, redoing is annoying. Cost of
   support: §9 step 6 is needed regardless; certify-resume is just
   plumbing the same flag through `orchestrate_certify`.

3. **Spec-edit policy.** §7.1 refuses by default and offers
   `--force`. Alternative: **surgically** resume by classifying
   Components as "spec-affected" (any Feature they own changed) vs
   "spec-stable" and rebuilding only the affected ones. Strictly
   better UX, materially more code (Feature-level diff of
   `spec.json`, dependency-aware invalidation). Worth doing in v2,
   but is it worth doing in v1?
