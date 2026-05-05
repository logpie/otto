# Phase B summary — what's new in the i2p cutover

**Status**: Phase B implementation is complete except for the
bench-gated default flip. All new-stack code is opt-in via `--i2p`.
Legacy paths continue to work and are the current default.

## What's new

The redesign (research.md §2 vocabulary) replaces Otto's monolithic
v3 build/certify/improve loops with a structured pipeline:

```
intent → compile_spec (greenfield or brownfield)
       → run_build (per-Group dispatch with check loop)
       → run_merge_queue (eligibility-gated FIFO)
       → run_audit (LLM judge with built-in retry + fix loop)
       → render_run (proof-packet.html + .json)
```

Concrete user-visible additions:

- **Brownfield compile** — `compile_spec(brownfield=True)` reads an
  existing project (file tree + README + manifest) and emits a
  baseline Spec describing what's already there. This is what
  `otto certify --i2p` and `otto improve --i2p` use to bootstrap a
  spec without requiring a greenfield run.
- **Spec review surface** — Mission Control exposes
  `?view=spec-review&spec=<session_id>` for editing the compiled
  spec before build. POST-edit reconciles user changes with the base
  via stable Feature/Group ids; intent_hash guards against
  concurrent edits.
- **New RunDrawer** — `?view=run-view&session=<session_id>` renders
  the new design's Run view (Features, Groups, Components,
  Guardrails, Stage timeline). The legacy MC `/api/runs/<id>/...`
  surface still works in parallel.
- **`--i2p` flags** — `otto build --i2p`, `otto certify --i2p`,
  `otto improve {bugs,feature,target} --i2p` route through the new
  stack. `--legacy` is the opt-out escape hatch (lands its meaning
  once the default flips).
- **Out-of-scope guard** — intents that mention systems-level
  products (`browser engine`, `linux kernel`, `language compiler`,
  etc.) raise `SpecValidationError` before LLM cost. Override token:
  `override-scope` in intent text.

## How to opt in

Per-command (overrides config every time):

```sh
otto build --i2p "build a doc editor"
otto certify --i2p
otto improve bugs --i2p "fix error handling"
otto run "build a CLI tool"            # always uses new stack
```

Project-wide (set once, applies to build/certify/improve):

```yaml
# otto.yaml
default_pipeline: i2p   # or "legacy" (current default)
```

When `default_pipeline: i2p` is set, omit the `--i2p` flag — it's
implicit. Pass `--legacy` to fall back per-command if needed.

Conflict: passing `--i2p` and `--legacy` together raises
`click.UsageError`. There is no silent preference.

## What's deprecated

The legacy v3 functions emit `DeprecationWarning` on each call,
naming the migration path:

- `otto.pipeline.build_agentic_v3` → use `otto build --i2p` (or
  `otto run`, or set `default_pipeline: i2p`).
- `otto.certifier.run_agentic_certifier` → use `otto certify --i2p`.
- `otto.cli_improve._run_improve` (private helper) — no migration
  warning at the function level; the `otto improve --i2p` user
  surface is the migration path.

These warnings are advisory. Existing code paths still work.

## What to validate before flipping the default

The default flip — changing `default_pipeline` to `"i2p"` in the
shipped config — is a one-line cutover commit. Before it lands,
confirm:

1. **Bench A** — a fixture-intent build under `--i2p` produces a
   passing proof packet, verifies parity vs the legacy v3 path on
   the same intent (criteria in research §12.7: hidden evaluator
   passes, browser private evaluator passes, 0 slices blocked, wall
   ≤ 1.5× legacy baseline, cost ≤ 1.2× baseline, audit verdict
   `passed`).
2. **No active legacy sessions** — `otto history` shouldn't list
   in-flight legacy runs that would be orphaned by the cutover.
3. **MC default switch** — the new RunDrawer becomes the landing
   page; legacy `/api/runs/<id>/...` deletion happens in Phase C.
4. **One quiet cycle** — at least one session window where the
   default is `i2p` and nothing regresses.

After all four hold, flip `otto.yaml`'s `default_pipeline` to
`"i2p"`. The cutover is the change of one line.

## Migration path for legacy users

- Legacy `otto build`/`certify`/`improve` (no `--i2p`) keeps working
  through Phase B. Resume checkpoints, proof-of-work reports,
  history rows, and improve build journals all continue to land in
  their existing layouts.
- `--i2p` is opt-in for the entire Phase B window. Users who hit
  regressions can stay on legacy by simply not passing the flag.
- After the default flip (B.3 cutover commit), `--legacy` becomes
  the escape hatch for one cycle.
- Phase C deletes the legacy code paths (~9,300 LOC across
  `pipeline.py`, `certifier/__init__.py`, `cli_improve.py` legacy
  bodies, `spec.py` if confirmed stale; see
  `docs/phase-c-deletion-audit.md` for the full audit).

## Honest gaps

The new stack has rough edges that the user should know about:

- **`otto improve target --i2p`** maps the metric `goal` to a `focus`
  scope hint. The legacy `target` measured a metric and iterated; the
  new stack treats `goal` as input to brownfield compile + audit, but
  doesn't measure metrics or compare against thresholds. If you rely
  on `target`'s measure-and-iterate loop, stay on `--legacy` for now.
- **`otto certify --i2p`** runs `run_audit` with placeholder
  `BuildResult`/`MergeQueueResult` (no build phase, no fix loop).
  This is honest for a "judge what's there" semantics, but if your
  certify workflow depended on the legacy `run_agentic_certifier`'s
  monolithic agent (which can install deps, start the app, and do
  more than just audit), evaluate whether the new audit-only flow
  meets your needs.
- **Spec review polish** — full markdown rendering, Add Feature
  modal, and diff viewer (wireframes 4b/4c/4d) are tracked in
  progress.md A5.2 as post-cutover work. Tick 34's skeleton
  (textarea + edit toggle) is the current MVP.
- **`--resume`** is not honored in `--i2p` mode — orchestrate_run /
  orchestrate_certify / orchestrate_improve don't read legacy
  checkpoints. If you need resume mid-flight, stay on legacy.
- **Brownfield compile assumes spec-able projects** — out-of-scope
  guard catches systems-level products, but for projects with
  unusual structure (no manifest, no README), the preamble is
  thinner and the agent has less to anchor to.

## Where to get help

- `progress.md` — full A0–C breakdown with substep status.
- `loop-report.md` — tick-by-tick log of what changed and why.
- `research.md` — design rationale for vocabulary choices, audit
  honesty contract, scope decisions.
- `docs/phase-c-deletion-audit.md` — what's slated for deletion in
  Phase C and the prerequisites.
