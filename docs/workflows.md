# Otto Workflows

Recipes for the common ways people use Otto. Each section lists the user
goal, the commands, what Otto does, where the evidence lands, and how to
recover when something goes wrong.

For a deeper architecture reference, see [`architecture.md`](architecture.md).
For the original i2p design, see [`intent-to-product-design.md`](intent-to-product-design.md).

---

## 1. Greenfield Build (i2p Groups)

**Goal:** turn a product intent into a working app from scratch.

```bash
otto run "REST API for a todo app with SQLite"
otto run "expense approval portal" --budget 3600
otto run --project-kind cli "a small linter"
otto run --review-gate "build a markdown notebook"
```

What happens:

1. Otto compiles the intent into a spec (project kind, groups, owned
   paths, checks, done criteria).
2. Optional spec review gate (`--review-gate`) — operator approves or
   regenerates via CLI or Mission Control.
3. Groups build in parallel on per-group branches under `.worktrees/`.
4. The merge lane integrates groups in dependency order.
5. The integrated product is audited; failed features feed the repair
   loop.
6. A proof packet renders to `otto_logs/sessions/<session-id>/`.

Evidence: `proof-packet.html`, `proof-packet.json`, `summary.json`,
`spec/spec.json`, per-phase logs.

Recovery: `otto run --resume`, `--force`, `--reset-budget`.

---

## 2. Greenfield Build (v5 Hierarchical Lead)

**Goal:** turn a multi-subsystem product intent into a working app via
recursive Lead decomposition.

```bash
otto v5 run "URL shortener with admin dashboard and analytics" --tier modular
otto v5 run "tiny CLI tool that watches a directory" --tier solo
otto v5 run "multi-feature SPA" --review-first-decomp
```

Tiers:

| Tier | Use when |
| --- | --- |
| `solo` | Single-scope product; one Lead can hold the whole thing in context. |
| `lead` | Multi-area product; allow root Lead to emit subtasks. |
| `modular` | Multi-subsystem product; require architect-first decomposition. |
| `auto` | Let the root Lead choose (default). |

What happens:

1. `spec_compile_flat` produces `flat-spec.json` (intent + behavior
   journeys).
2. The root Lead reads CHARTER.md / decisions.md if present, then either:
   - **Inline**: `mcp__otto__begin_inline`, writes code + tests, calls
     `mcp__otto__verify`.
   - **Decompose**: `mcp__otto__submit_subtask` per subsystem; the root
     authors `CHARTER.md` + an empty `decisions.md` at the repo root.
3. Pre-flight checks run on the task graph (architect-inline, CHARTER
   exists, no cycles, scaffold compiles, smoke clean-deploy passes).
4. Children dispatch up to `--max-parallel`, honoring `depends_on`.
   Each child gets its own worktree on `i2p/build/<task-id>`.
5. When all children of a parent resolve, an **integration Lead** runs
   on `i2p/integ/<parent-task-id>`. It cross-stack-tests the merged
   tree, applies structured merge drivers for runtime config files,
   and calls `verify` once more.
6. The root's verdict + `summary.json` are written.

Evidence: per-Lead `verdict.json`, `task-graph.json`, `verify-result.json`,
agent message logs, root `summary.json`.

Recovery:

- `otto v5 list-pending` shows tasks awaiting review or dispatch.
- `otto v5 review approve <task-id>` resumes a `--review-first-decomp` pause.
- Child crashes don't kill the run; the parent still integrates against
  surviving children with verdict `catastrophic` recorded for the
  crashed child.
- `--tree-budget-usd` caps total cost.

---

## 3. Brownfield: Bug Hunt

**Goal:** find and fix bugs in an existing project.

```bash
otto improve bugs "look for auth and data isolation bugs"
otto improve bugs "find broken recovery paths" --rounds 3
```

What happens:

1. Otto skips spec-compile (the existing project IS the spec).
2. An audit pass identifies failing or risky features.
3. The repair loop attempts targeted fixes.
4. A proof packet renders showing what was changed and why.

Useful flags: `--rounds`, `--budget`, `--certifier-effort high`.

---

## 4. Brownfield: Feature Addition

**Goal:** add a feature to an existing project.

```bash
otto improve feature "make the review workflow clearer"
otto improve feature "add CSV export to the dashboard"
```

What happens: identical to bug hunt except the audit looks for the
described capability and the fix loop builds it.

---

## 5. Brownfield: Target Verification

**Goal:** verify a target invariant holds.

```bash
otto improve target "all API tests pass and p95 latency < 100ms"
otto improve target "no secrets in any committed file"
```

Useful when the target is a concrete predicate rather than a feature.

---

## 6. Independent Certification

**Goal:** audit an existing project against a stated capability without
touching code.

```bash
otto certify "admin users can approve or reject expenses" --standard
otto certify "release candidate" --thorough
```

Tiers:

| Tier | Behavior |
| --- | --- |
| `--fast` | Quick journey-level checks. |
| `--standard` | Full feature-level audit (default). |
| `--thorough` | Adversarial / regression-style audit. |

Certify never edits code; its output is the proof packet.

---

## 7. Parallel Queue

**Goal:** run multiple build/improve/certify jobs in parallel without
mixing files.

```bash
# Enqueue
otto queue build "add saved filters" --as saved-filters
otto queue improve bugs "audit error handling" -- --rounds 3
otto queue certify "release candidate" -- --standard

# Inspect
otto queue ls
otto queue show <task-id>

# Run the watcher (foreground process)
otto queue run --concurrent 3
otto queue run --concurrent 3 --exit-when-empty

# Cleanup
otto queue rm <task-id>
otto queue clean
```

Each queued task:

1. Gets its own branch and worktree under `.worktrees/<task-id>/`.
2. Runs as a child of the watcher process, tracked by PID/PGID.
3. Writes its own session dir under `otto_logs/sessions/`.
4. Is recoverable via the watcher's command journal.

Mission Control exposes the same actions: enqueue, cancel, resume,
cleanup.

---

## 8. Mission Control (Web)

**Goal:** supervise Otto runs from a browser.

```bash
otto web --port 9000
otto web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher
otto web --project-launcher --projects-root ~/otto-projects
```

What it gives you:

- Project switcher and project launcher.
- Launch forms for build, improve, certify, run.
- Queue watcher start/stop.
- Live run drawer with phase timeline, logs, proof links.
- Historical run inspection.
- Spec review and approval UI.
- Pause / resume / abort / retry / cleanup actions.
- Project history and system health.

Defaults to localhost. `--allow-remote` is required to bind externally.

---

## 9. Proof Inspection

**Goal:** see what a run produced.

```bash
otto proof list
otto proof open                 # latest session
otto proof open <session-id>
otto proof path <session-id>
otto proof render <session-id>  # re-render from messages
otto proof cleanup <run-id>
```

The `proof-packet.html` is the human-facing artifact. `proof-packet.json`
is machine-readable for automation. Both live in
`otto_logs/sessions/<session-id>/`.

---

## 10. Debugging A Run

**Goal:** figure out what an agent actually did.

```bash
# Re-render readable narrative from raw messages
otto debug narrative <session-id>

# Compatibility alias
otto replay <session-id>

# Tail an active run
tail -f otto_logs/latest/build/narrative.log
```

For machine consumption:

```bash
jq -c 'select(has("blocks")) | .blocks[]' \
  otto_logs/latest/build/messages.jsonl
```

Useful artifacts when debugging:

| Question | Where to look |
| --- | --- |
| Why did the build fail? | `build/narrative.log`, scan for `STORY_RESULT:` and `VERDICT:` markers |
| What did the audit test? | `certify/proof-of-work.json` (i2p) or per-Lead `verify-result.json` (v5) |
| Did the repair loop trigger? | `build/narrative.log` → `CERTIFY_ROUND:` markers |
| How much did it cost? | `summary.json` → `cost_usd`, or `otto history` |
| Live tail during a run | `tail -f otto_logs/latest/build/narrative.log` |
| Replay programmatically | `messages.jsonl` — one JSON object per normalized SDK message |

---

## 11. Recovery From Failure

**Goal:** resume work after a crash, budget exhaustion, or pause.

i2p:

```bash
otto run --resume                       # resume the most recent paused session
otto run --resume --auto-approve        # auto-approve spec review on resume
otto run --resume --reset-budget        # ignore prior spend
otto run --resume --force               # bypass spec-hash validation
```

v5:

```bash
otto v5 list-pending                    # see what's waiting
otto v5 review approve <task-id>        # release a review-paused task
otto v5 review reject <task-id>         # cancel a pending task
```

Queue:

```bash
otto queue ls --include-finished
otto queue show <task-id>
otto queue resume <task-id>
otto queue clean
```

Mission Control surfaces these as buttons.

---

## 12. Project Setup

**Goal:** prepare an existing repo for Otto.

```bash
otto setup                          # write CLAUDE.md with conventions
otto setup --no-overwrite           # don't replace existing CLAUDE.md
```

`otto setup` writes project conventions Otto uses when invoking
agents. Run once per project, re-run after major repo restructures.

For greenfield runs, `otto run` will scaffold a directory if needed.

---

## 13. Provider Switching

**Goal:** use a different provider for one phase or one run.

Global override:

```bash
otto run "..." --provider claude --model sonnet-4-5
```

Phase-specific override (i2p):

```bash
otto run "..." \
  --build-provider codex-app-server \
  --certifier-provider claude \
  --fix-provider codex
```

v5 currently defaults to `claude` because it relies on the Claude Agent
SDK's `create_sdk_mcp_server`. Other providers can be selected with
`--provider`, but MCP-tool flows require provider parity.

---

## Where Everything Lives

```text
otto_logs/sessions/<session-id>/    canonical per-session directory
otto_logs/latest                    symlink to most-recent session
otto_logs/paused                    symlink to paused session (if any)
otto_logs/cross-sessions/           history.jsonl, certifier-memory.jsonl

.worktrees/<task-id>/               queue + v5 child worktrees
.otto-queue.yml                     queued task definitions
.otto-queue-state.json              task status
.otto-queue-commands.jsonl          watcher command journal

CHARTER.md                          v5 root-authored design doc (project tree)
decisions.md                        v5 append-only boundary decisions
otto.yaml                           project config
CLAUDE.md                           agent instructions (written by `otto setup`)
intent.md                           optional canonical product description
```

Otto runtime files under `otto_logs/`, `.worktrees/`, and queue state files
must never leak into agent prompts or git commits. See
`otto/setup_gitignore.py` and `setup_gitattributes.py` for the guards.
