# research: "can the artifact run from clean state?" checks

Date: 2026-05-12

## Why this doc

The FE compile block we just hit (`tsc: command not found`) is a missing
`npm install` before `npm run build`. The proposed fix is one line. But
the broader pattern — we keep adding individual checks each time a
specific clean-state assumption is violated — is what I want to inspect
before another patch lands.

This doc maps every existing "can it run cleanly?" check, what state it
assumes, and where they overlap or diverge.

## Inventory

Eight preflight check `kinds` exist in `otto/v5_preflight.py`. Five are
structural graph checks; three are runtime/clean-state checks.

### Structural (graph-shape) checks

| Kind | What it checks | Failure cost |
|---|---|---|
| `architect_sub_decomposed` | architect tasks don't emit subtasks | n/a (cheap) |
| `charter_missing` | architect-pass implies CHARTER.md exists | n/a (cheap) |
| `dag_cycle` | task graph has no cycles | n/a (cheap) |
| `duplicate_task_id` | task IDs unique | n/a (cheap) |
| (in `filter_blocked_descendants`) | descendants of failing architect don't dispatch | n/a (cheap) |

Pure-function checks over the task graph. Not the subject of this doc.

### Runtime / clean-state checks

| Function | Kinds emitted | Where invoked | What it does |
|---|---|---|---|
| `check_scaffold_compiles` | `scaffold_compile_failed/_timeout/_skipped` | `v5_runner.py:401`, after architect-pass | `npm run build` (or `python -m py_compile`) in source dir |
| `smoke_start_services` | (legacy, no current callers in v5 path) | (legacy) | in-place: run `start.sh`, wait 5s, check ports |
| `smoke_clean_deploy` | `clean_deploy_port_busy`, `_copy_failed`, `_start_failed`, `_ports_not_listening`, `_smoke_error` | `v5_runner.py:807`, before integration | copy project to temp dir, run `start.sh`, poll ports |
| (in agent prompt) `lead.md:184` | n/a — agent self-verify | architect leaf | `npm run build && npx tsc --noEmit`, in agent's own worktree (with `node_modules`) |

Four implementations of "run command X and see if it works". Each makes
different state assumptions about what's installed and where.

## State assumptions per check

For the FE-compile case specifically, here's where each call lives in
state-space:

```
                              node_modules?    npm install runs?    runs start.sh?
─────────────────────────────────────────────────────────────────────────────────
architect leaf verify         YES (just ran)   YES (its session)    NO
check_scaffold_compiles       MAYBE (stale)    NO ← bug source       NO
smoke_clean_deploy            NO (clean copy)  YES (via start.sh)   YES
```

The architect runs `npm run build` AFTER its own `npm install`, sees it
pass, declares `verdict: pass`. The preflight runs the SAME COMMAND in
the SAME DIR but with `node_modules` potentially gone (git-ignored, not
preserved across the session handoff), and gets exit 127. The
clean-deploy smoke runs in a tempdir and goes through `start.sh`, which
does install correctly.

**Three checks of one command, three different state preconditions.
Two agree; the middle one fails because its precondition (deps
installed) isn't stated explicitly anywhere — it's assumed because the
architect "just verified the same command".**

## Where state escapes between checks

The architect's verification state (its working dir with `node_modules`
populated) doesn't survive to the preflight. There's no protocol for
state handoff between the architect and any downstream verifier. So:

- Architect verifies under state A → pass
- Preflight verifies under state B → fail
- Clean-deploy verifies under state C → may pass

All three say "is `npm run build` working?" and get three different
answers, because they're answering three different questions:

- A = "does it build given the deps I just installed?"
- B = "does it build given whatever the working dir happens to contain?"
- C = "does it build given a fresh clone + start.sh's install logic?"

C is the most meaningful answer for "will this product run for a real
user". A is meaningless for that purpose. B is currently the loudest
because it blocks dispatch, but it's the least useful — it's testing
neither in-session nor from-clean.

## Where the checks overlap

```
                              architect leaf  scaffold_compile  clean_deploy_smoke
─────────────────────────────────────────────────────────────────────────────────
verify code compiles          ✓                ✓                  ✓ (via start.sh)
verify deps install            ✓ (assumed)     ✗                  ✓
verify services start          ✗               ✗                  ✓
verify ports bind              ✗               ✗                  ✓
verify from CLEAN state        ✗               ✗ (intended, broken) ✓
shift-left (early in run)      ✓ (earliest)    ✓ (mid)            ✗ (latest)
```

`clean_deploy_smoke` is the strongest check — it makes the fewest
assumptions about installed state. It is also the latest, running
right before integration (so a scaffold-stage bug spends an entire
build phase before being surfaced).

`check_scaffold_compiles` was added to shift-left this catch — discover
"build doesn't compile from clean" right after the architect, not 30
min later. But its implementation doesn't actually clean the state — it
runs in source-dir as-is. So it shifted-left in *time* but not in
*scope*: it's a different (and weaker) check than the one we wanted.

The architect's self-verify (`lead.md:184`) is a third version, running
with the architect's own installed state. Useful as correctness-of-its-
own-output, useless as "will this survive handoff".

## Three options

### A. Patch: add `npm install` to `check_scaffold_compiles`

```python
if not (pkg.parent / "node_modules" / ".bin").exists():
    subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=pkg.parent)
subprocess.run([npm, "run", "build", "--silent"], cwd=pkg.parent, ...)
```

- Cost: ~5 LOC, ~30-60s added per architect-pass
- Fixes: the specific symptom we just hit
- Doesn't fix: the same divergence in the architect's own verify (it
  still tests under different state than preflight), and any future
  install-related state issue elsewhere
- Net: 8 checks, 3 of them subtly different versions of the same
  question. Patch trajectory continues.

### B. Retreat: delete `check_scaffold_compiles`, rely on clean-deploy

- Cost: ~50 LOC removed; loses shift-left advantage (~20-30 min later
  feedback)
- Fixes: removes the inconsistency by removing the weaker check
- Reverses an earlier decision

### C. Protocol: collapse to one `verify_from_clean(scope)` primitive

One function, parameterized by scope. Same sequence at each invocation:

1. Copy project (or relevant subdir) to a temp dir, excluding stateful
   dirs (`node_modules`, `.venv`, `dist`, `.git`, `otto_logs`).
2. If a manifest exists (`package.json`, `pyproject.toml`), install
   deps using the lockfile (`npm ci`, `uv sync`).
3. If a build script exists (`npm run build`, etc.), run it.
4. If `start.sh` exists AND scope >= "subtree", run it and probe
   declared ports.
5. Return one verdict + evidence.

Then:

- Architect's self-verify → calls `verify_from_clean(scope="scaffold")`
  (build only, no start.sh)
- Post-architect preflight → same call, same answer (currently a
  divergence)
- Pre-integration smoke → calls `verify_from_clean(scope="subtree")`
  or `"full"`
- All three reach the same verdict on the same code under the same
  state assumptions, because they use the same code.

The architect's verdict and the preflight's verdict are now guaranteed
to agree, eliminating the "architect passed but preflight blocked"
class entirely.

- Cost: ~150 LOC of refactor (one new primitive, three callsites
  updated). Architect prompt update to call the primitive rather than
  ad-hoc commands.
- Fixes: the protocol-level inconsistency, not just this symptom.
- Side benefit: future "can X run cleanly?" needs (subtree integration
  nodes wanting to verify before declaring pass) can reuse the same
  primitive.

## Recommendation

Option C, scoped to the "runtime / clean-state" checks only. Leave the
structural graph checks alone — they don't share this pathology.

If C is too much work for one sitting, the honest interim is **A with
an explicit budget**: do the `npm install` patch, log the conscious
deferral, and revisit after the next 1-2 patches in this area. The
risk of A-without-budget is that we keep adding patches without ever
doing C, until someone has to debug six near-identical clean-state
checks under time pressure.

## Open questions

- Does the architect's self-verify need to actually *match* preflight,
  or is it enough that they agree on the final verdict? (I think yes —
  if architect says "pass" and preflight says "block" on the same input,
  one of them is lying.)
- Should the clean-state primitive also include `git status` / dirty-
  worktree checks? Some failures come from uncommitted state leaking
  between sessions.
- At what scope-level does "real browser test" enter the primitive? Or
  is it a separate layer that runs AFTER `verify_from_clean` passes?
  (Suggest: separate. Live-stack discipline at the integration prompt
  is the right place for it.)
