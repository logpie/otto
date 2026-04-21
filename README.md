# Otto

Build, certify, and improve software products from natural language.

```bash
otto build "bookmark manager with tags and search"
otto build "bookmark manager" --spec              # preview + approve spec first
otto certify                                      # verify any project works
otto improve bugs                                 # find and fix bugs
otto improve target "latency < 100ms"             # optimize toward a metric
otto queue build "csv export" && otto queue run   # enqueue + run in parallel worktrees
otto merge --all                                  # land done branches into main
```

## How it works

**`otto build`** launches one autonomous agent that plans, builds, tests, certifies, and fixes:

1. **Explore** — reads existing code (if any), runs existing tests
2. **Build** — implements the product, dispatches subagents for parallel work
3. **Test** — writes tests, runs them, fixes failures
4. **Certify** — dispatches a builder-blind certifier agent that tests as a real user
5. **Fix** — if certification fails, reads findings, fixes code, re-certifies
6. **Ship** — commits when certification passes

Optionally, a **spec gate** runs first: `otto build "intent" --spec` generates a short reviewable spec (What It Does / Must Have / Must NOT Have Yet / Success Criteria), pauses for you to approve, then hands the approved spec to both the build agent and certifier — so scope creep and "Must NOT Have" features are flagged by the certifier as FAIL.

**`otto certify`** runs independently on any project — regardless of how it was built:

```
$ otto certify "notes API with auth, CRUD, and search"

    ✓ Full CRUD lifecycle works correctly
    ✓ Users cannot access each other's notes
    ✓ Unauthenticated requests rejected with 401
    ✓ Search by keyword and tag works
    ✓ Edge cases handled

  PASSED — 5/5 stories
  Cost: $1.10  Duration: 164s
  Report: otto_logs/certifier/proof-of-work.html
```

**`otto improve`** iterates on existing code with three modes:

```bash
# Find and fix bugs (adversarial certifier → fix loop)
otto improve bugs
otto improve bugs "error handling"         # focused

# Suggest and implement features (product advisor → implement loop)
otto improve feature
otto improve feature "search UX"           # focused

# Hit a measurable target (measure → optimize → re-measure loop)
otto improve target "response time < 50ms"
otto improve target "test coverage > 90%"
```

Each mode creates an improvement branch, runs up to N rounds, and writes a report with merge instructions.

**`otto queue` + `otto merge`** runs multiple `otto build`/`improve`/`certify` jobs in parallel — each in its own git worktree on its own branch — then lands the successful ones back into the target branch.

```bash
otto queue build "csv export"               # enqueue
otto queue build "settings redesign"        # enqueue another (parallel-safe)
otto queue improve bugs --rounds 3          # enqueue an improve run
otto queue run --concurrent 3               # foreground watcher dispatches up to 3 at a time

otto merge --all                            # land all done tasks into main
```

The watcher spawns each task in `.worktrees/<task-id>/` so they can build, test, and commit in isolation. `otto merge` does Python-driven `git merge --no-ff`; clean merges burn $0. When git can't auto-merge, otto commits all marker-laden merges first, then runs ONE agent session that resolves every conflict globally — full Bash + project test command + cross-branch context. After all branches land, a triage agent emits a verification plan and the certifier re-runs the must-verify subset.

## Why Otto

Not an editor, not an IDE agent. A **reliability harness** around Claude Code (or Codex) for autonomous product building — the scaffolding that turns "run an agent" into "run an agent you can leave unattended and trust the result."

### Evidence-based trust — proof-of-work, not just green checks

Every certification produces a **proof-of-work** artifact: an HTML report with embedded screenshots, a browser-walkthrough video, and per-story JSON. You can audit a 30-minute autonomous run by opening one HTML file. Evidence a human can verify, not just "all tests pass."

### Build → Certify → Fix loop (independent verification)

The certifier is a **builder-blind** agent that tests the product as a real user would — it never sees the build agent's code or reasoning. If certification fails, its findings feed back to a fix pass and re-certify. This is the core trust primitive: generation alone is never enough; you need independent verification with a feedback loop.

### Scope accountability (spec-gate)

Most autonomous tools silently cut scope to finish. Otto makes scope explicit and enforces it:
- `--spec` generates a reviewable `spec.md` (Must Have / **Must NOT Have Yet** / Success Criteria / Open Questions).
- You `[a]pprove / [e]dit / [r]egenerate / [q]uit` before any code is written.
- The approved spec is handed to both the builder **and** the certifier. Features in "Must NOT Have Yet" get flagged `STORY_RESULT: scope-creep-<slug> | FAIL` — you can't hide scope creep in a passing test suite.

### Autonomous but resumable

- **Checkpoint/resume**: every phase writes atomic checkpoints (`spec → spec_review → spec_approved → build → certify → round_complete`). Crashes, Ctrl-C, or budget exhaustion leave a resumable state — `otto build --resume` picks up exactly where it left off.
- **Session preservation**: the agent's `session_id` is captured on timeout/crash, so resumed runs continue the same conversation rather than starting cold.
- **Graceful budget**: `run_budget_seconds` pauses (not fails) when exhausted; partial work is preserved.

### Hill-climb improvement

`otto improve` turns iteration into a measurable loop on an isolated branch:
- **bugs** — adversarial certifier finds issues → fix → re-certify (N rounds)
- **feature** — product advisor identifies gaps → implement → re-evaluate
- **target** — measure metric → compare to goal → optimize → re-measure (stops when target met)

Each run writes a round-by-round journal (action, result, cost) and a final report with merge instructions.

### Cross-run memory (opt-in)

With `memory: true` in `otto.yaml`, the certifier records what it tested and what it found across **every** run — build, certify, improve. Future runs read this to regression-check previously fixed issues, focus on untested areas, and cite specific commits/files. Capped to prevent bloat. Off by default — dev-loop runs don't need it.

### Honest observability

Timestamped, append-only logs across ~7 artifact types per run. Retries preserve prior attempts (no silent overwrite). Errors are logged honestly (no "success" without checking return codes). You can debug a failed autonomous run from logs alone.

### How it compares to other harness frameworks

Peers surveyed (April 2026): [Symphony](https://github.com/openai/symphony) (OpenAI, open), [Devin 2.2](https://cognition.ai/) (Cognition, SaaS), [Cursor background agents](https://cursor.com/) (IDE + cloud VM), [Factory Droid](https://factory.ai/) (enterprise platform). Research harnesses (OpenHands, SWE-agent) and interactive pair-programmers (Aider) are different categories.

Legend: `✓` documented · `~` partial or different approach · `✗` absent or not publicly documented.

#### What otto does that peers don't (yet)

| | Otto | Symphony | Devin | Cursor | Factory |
|---|---|---|---|---|---|
| Builder-blind LLM agent tests the product as a user (not code review, not pre-written CI) | ✓ | ✗ CI runs fixed tests | ✗ same agent self-reviews | ✗ | ~ Review Droid reviews code, not product |
| Spec as enforceable contract — verifier flags features beyond scope as FAIL | ✓ | ✗ | ✗ | ✗ | ✗ |
| Hill-climb to a measurable target (`improve target "metric < X"`) | ✓ | ✗ | ✗ | ✗ | ✗ |
| Phase-level checkpoint + session_id resume on crash / Ctrl-C | ✓ | ~ retry whole run from queue | ~ session pause (human takeover) | ✗ (open community request) | ✗ |
| Graceful pause on budget exhaustion (resumable, not hard-stopped) | ✓ | ✗ | ✗ spend caps hard-stop | ✗ spend caps hard-stop | ✗ |
| Opt-in cross-run certifier memory with commit citations | ✓ | ✗ | ~ parent reads child trajectories | ✗ | ~ static codebase graph |

#### Table stakes — roughly comparable across the category

| | Otto | Symphony | Devin | Cursor | Factory |
|---|---|---|---|---|---|
| Closed fix loop (verify fails → agent fixes → re-verify, autonomous) | ✓ | ✓ | ✓ autofixes review comments | ✓ iterates until tests pass | ✓ TDD-loop droid |
| Some form of proof-of-work artifact | ✓ HTML + video + JSON | ✓ CI + video + PR review | ✓ screen recording (v2.2) | ✓ video (Feb '26) | ✓ DroidShield report |
| Open source | ✓ MIT | ✓ Apache-2.0 | ✗ | ✗ | ✗ |

#### What peers have that otto doesn't

| | Otto | Symphony | Devin | Cursor | Factory |
|---|---|---|---|---|---|
| Multi-agent parallel execution (different tasks or competing solutions) | ✗ single agent | ✓ BEAM concurrency | ✓ "multiple Devins in parallel" | ✓ git-worktree parallel agents | ✓ coordinator dispatches many droids |
| Ticket-tracker integration (Linear / Jira) | ✗ | ✓ Linear-native | ✓ | ~ GitHub-centric | ✓ |
| Auto-apply human reviewer comments from PR | ✗ | ✗ | ✓ "Autofixes Review Comments" | ~ @cursor mention | ~ @droid mention |
| Code-review bot for other humans' PRs | ✗ | ✗ | ✓ Devin Review | ~ BugBot | ✓ Review Droid inline comments |
| Sandboxed cloud VM / Linux desktop per run | ✗ local only | ✗ local | ✓ Linux desktop (v2.2) | ✓ per-agent cloud VM | ✓ cloud runtime |
| IDE / editor integration | ✗ CLI only | ✗ | ~ VSCode plugin + web | ✓ native IDE | ~ plugins + desktop |
| Team dashboards / SSO / secrets UI / cost analytics | ✗ | ~ | ✓ Enterprise | ✓ | ✓ enterprise RBAC |
| Slack / chat integration | ✗ | ✗ | ✓ | ~ | ✓ |
| Persistent codebase Q&A / knowledge index | ✗ | ✗ | ✓ DeepWiki | ~ | ✓ HyperCode |

**Form factor**: otto is a **single developer's CLI** — local, one process, no backend. The six items in the first table are the reliability primitives you buy by giving up the cloud/team surface area. Closest philosophical analog is [Symphony](https://github.com/openai/symphony) (OSS, proof-of-work gate, per-branch agent config) — but Symphony is team/Linear-oriented and assumes the repo is already harness-engineered with hermetic tests and machine-readable docs. Otto brings the harness *to* repos that aren't.

### Certifier deep-dive

Closed fix-loops are table stakes — what differs is **what runs inside the verify step**. The key mechanic: is verification a *separate LLM agent that tests the product as a user*, or the *same agent* running its own tests?

| | Otto | [Devin 2.2](https://cognition.ai/blog/introducing-devin-2-2) | [Cursor CU](https://cursor.com/blog/agent-computer-use) | [Replit Agent 3](https://blog.replit.com/introducing-agent-3-our-most-autonomous-agent-yet) | [Factory Review Droid](https://docs.factory.ai/guides/droid-exec/code-review) |
|---|---|---|---|---|---|
| Separate, builder-blind process | ✓ | ✗ same agent | ✗ same agent | ✗ same agent | ✓ reviews **code**, not product |
| Drives running product as a user | ✓ Playwright / curl / shell | ✓ Linux desktop | ✓ browser + VM | ✓ real browser | ✗ diff review |
| Generates test stories from intent/spec | ✓ | ✗ | ✗ | ✗ | ✗ |
| Adversarial mode (XSS / SQLi / auth bypass) | ✓ `--thorough` | ✗ | ✗ | ✗ | ~ flags diff bugs |
| Scope-creep FAIL (features beyond spec) | ✓ | ✗ | ✗ | ✗ | ✗ |
| Standalone on any project | ✓ `otto certify` | ✗ | ✗ | ✗ | ~ any PR |

Each peer matches otto on one axis: **Devin / Cursor / Replit** all drive the running product via computer-use or browser — but the same agent that built it self-verifies. **Factory** runs an independent reviewer — but reviews the code diff, not the live product. **Symphony** and **Copilot / Amazon Q** use CI as the gate (different category — not LLM-agent verification).

What's specific to otto: a separate LLM agent that generates its own test stories from the approved spec, drives the product as a user, runs adversarial probes on demand, flags features outside the spec as FAIL, and works standalone (`otto certify`) on projects it didn't build.

## Quick start

```bash
# Install
uv pip install -e ".[claude]"

# Build from scratch (greenfield)
cd your-project && git init
otto build "REST API for a todo app with SQLite"

# Build with a reviewable spec (recommended for non-trivial intents)
otto build "bookmark manager with tags" --spec

# Build on existing code (incremental)
otto build "add search by keyword and tag"
otto build "add pagination to listings"

# Verify any project works
otto certify

# Find and fix bugs
otto improve bugs

# Optimize toward a target
otto improve target "all endpoints respond in < 100ms"
```

## What it handles

- **CLI tools** — argparse, Click (runs commands, checks output)
- **REST APIs** — Express, Flask, FastAPI (curl testing)
- **Libraries** — Python, Node.js (import + unit testing)
- **Web apps** — Next.js, React (browser testing, screenshots, video)

## Command Reference

### `otto build`

Build a product from a natural language intent. One agent builds, certifies, and fixes autonomously.

```bash
otto build "bookmark manager with tags and search"
otto build "CLI tool that converts CSV to JSON"
otto build "add dark mode toggle to the settings page"   # incremental
```

| Flag | What it does |
|---|---|
| `--spec` | Generate a reviewable spec first; pause for approval before building |
| `--spec-file PATH` | Use a pre-written spec (e.g. from `/office-hours` or spec-kit); implies `--yes` |
| `--yes` | Auto-approve the generated spec (CI/scripts) |
| `--force` | Discard an active paused spec run and start fresh |
| `--fast` | Fast certification — happy-path smoke test only (the default) |
| `--thorough` | Thorough certification — adversarial edge cases + code review |
| `--no-qa` | Skip certification entirely (just build) |
| `--split` | Python-driven certify→fix loop (vs. single agent session) |
| `--rounds N` / `-n N` | Max certification rounds (default 8) |
| `--resume` | Resume from last checkpoint; intent inherited from checkpoint |

**Certifier mode selection**: CLI flag > `otto.yaml` > `fast` fallback. No flag + no yaml setting → `fast`. Projects that want real verification by default set `certifier_mode: standard` (or `thorough`) in `otto.yaml`.

**Spec gate**: `--spec` generates `otto_logs/runs/<run-id>/spec.md` with sections _Intent / What It Does / Core User Journey / Must Have / Must NOT Have Yet / Success Criteria / Open Questions_. Pauses for `[a]pprove / [e]dit / [r]egenerate / [q]uit`. Approved spec flows into build.md + the certifier prompt — the certifier flags features found in "Must NOT Have Yet" as scope-creep FAILures.

### `otto certify`

Certify any project — independent, builder-blind verification. Tests the product as a real user. Works regardless of how it was built (otto, Claude Code, human).

The intent describes what the product should do. The certifier generates test stories from it.

```bash
otto certify                                            # reads intent.md or README.md
otto certify "notes API with auth, CRUD, and search"    # explicit intent
otto certify --fast                                     # quick smoke test (~30s)
otto certify --thorough                                 # adversarial: edge cases, code review
```

### `otto improve`

Iterate on existing code. Three modes, each with a specialized certifier prompt. Creates an improvement branch for isolation.

#### `otto improve bugs`

Find and fix bugs, edge cases, error handling gaps. Adversarial certifier tries to break the product, then the build agent fixes what it finds.

```bash
otto improve bugs                       # find and fix all bugs
otto improve bugs "error handling"      # focus on error handling
otto improve bugs -n 5                  # up to 5 rounds (default: 3)
otto improve bugs --split               # system-controlled loop (vs agent-driven)
otto improve bugs --resume              # resume from last checkpoint
```

#### `otto improve feature`

Suggest and implement product improvements. Evaluates the product as a real user, identifies missing features and UX gaps, then implements them.

```bash
otto improve feature                    # suggest and implement improvements
otto improve feature "search UX"        # focus on search experience
otto improve feature "mobile layout"    # focus on mobile
otto improve feature -n 5              # up to 5 rounds (default: 3)
```

#### `otto improve target`

Optimize toward a measurable target. Measures a metric, compares to the target, and iterates until met. The goal is a required argument.

```bash
otto improve target "response time < 100ms"
otto improve target "test coverage > 90%"
otto improve target "bundle size < 500kb"
otto improve target "lighthouse score > 95" -n 10    # up to 10 rounds (default: 5)
otto improve target "latency < 50ms" --resume         # resume interrupted run
```

### `otto queue`

Schedule multiple `otto build` / `improve` / `certify` runs as parallel tasks. Each task gets its own git worktree under `.worktrees/<task-id>/` and its own branch (e.g. `build/csv-export-2026-04-20`), so tasks can't stomp on each other's commits.

```bash
otto queue build "add csv export"            # enqueue (auto-slugs id from intent)
otto queue build "settings redesign" --as redesign --after csv-export   # depends on csv-export
otto queue improve bugs --rounds 3            # enqueue an improve run
otto queue certify --thorough                 # enqueue a certify run

otto queue ls                                 # show all tasks + status
otto queue show csv-export                    # full details for one task
otto queue rm csv-export                      # remove a queued task
otto queue cancel csv-export                  # SIGTERM a running task

otto queue run --concurrent 3                 # start foreground watcher (process up to 3 at a time)
otto queue cleanup --done                     # remove worktrees of done tasks
```

The watcher (`otto queue run`) is a foreground process — run it in a tmux pane like a dev server. It picks tasks off `.otto-queue.yml`, spawns one `otto` subprocess per task into the worktree, and reaps results into per-task manifests. It exits cleanly on SIGINT and supports `on_watcher_restart: resume` (default) or `fail` via `otto.yaml`.

| Flag | What it does |
|---|---|
| `--concurrent N` / `-j N` | Max concurrent tasks (default from `queue.concurrent`, fallback 3) |
| `--quiet` | Suppress watcher event lines on stdout |

### `otto merge`

Land queued / built branches into the target branch. Uses Python-driven `git merge --no-ff`; only invokes the LLM conflict-resolution agent when git can't auto-merge — clean merges burn $0.

```bash
otto merge --all                              # merge all done queue tasks into target
otto merge build/csv-export build/redesign    # explicit branches
otto merge --all --target develop             # target other than default_branch
otto merge --all --no-certify                 # skip post-merge story verification
otto merge --all --fast                       # pure git merge, bail on first conflict (no LLM)
otto merge --all --cleanup-on-success         # remove worktrees after successful merge
```

After all branches are merged, a triage agent computes a verification plan (which stories must be re-run vs. skipped vs. flagged for human review), and the certifier runs the must-verify subset against the post-merge tree. Use `--no-certify` to skip this; `--full-verify` to verify the full union.

| Flag | What it does |
|---|---|
| `--all` | Merge all done queue tasks |
| `--target BRANCH` | Target branch (default from `default_branch` in `otto.yaml`) |
| `--fast` | Pure git merge; bail on first conflict (no LLM agent) |
| `--no-certify` | Skip post-merge story verification |
| `--full-verify` | Verify all stories (no skip-likely-safe optimization) |
| `--cleanup-on-success` | Remove worktrees of merged tasks after successful merge |

**Conflict resolution.** When `git` can't auto-merge, otto first commits all marker-laden merges (preserving each branch's merge history), then runs ONE agent session that resolves every conflict globally with full project context — Bash, project test command, all tools. The agent self-corrects within its session (test-driven retry), then a single orchestrator-level validation enforces no out-of-scope edits, no leftover markers, HEAD unchanged. Bench data: ~2× faster and ~30% cheaper than per-conflict resolution on multi-branch merges.

**Manual fallback.** Use `--fast` to bail on the first conflict without invoking the agent. Then resolve manually with `git merge --continue` and run `otto merge` again for any remaining branches. (`--resume` is on the roadmap but not yet implemented — the flag prints a deferred-message and exits non-zero.)

### `otto history`

Show build history with results, cost, and duration.

```bash
otto history             # show recent builds
otto history -n 20       # show last 20 builds
```

### `otto setup`

Generate a `CLAUDE.md` file with project conventions for the coding agent. Reads the project structure and creates instructions automatically.

```bash
otto setup
```

## Configuration (`otto.yaml`)

`otto.yaml` is auto-created on first run (`otto build` or `otto setup`). All settings are optional — otto auto-detects what it can, and the CLI fallback is sane for quick iteration.

```yaml
# Auto-detected (you usually don't need to set these)
default_branch: main
test_command: pytest                   # auto-detected from project files

# Provider — which coding agent to use
provider: claude                       # "claude" (default) or "codex"
model: null                            # override model (e.g. "sonnet", "gpt-5")
                                       # if null, uses the provider's default

# Budget + certification
run_budget_seconds: 3600               # total wall-clock for the whole invocation (primary knob)
certifier_mode: fast                   # fast | standard | thorough
                                       # CLI no-flag default is `fast` (cheap dev loop);
                                       # set this to `standard` or `thorough` for real QA
max_certify_rounds: 8                  # max certify→fix attempts before giving up
spec_timeout: 600                      # cap on the spec-agent call specifically

# Queue + merge (otto queue / otto merge)
queue:
  concurrent: 3                        # default --concurrent for `otto queue run`
  worktree_dir: .worktrees             # where per-task worktrees live (relative to project)
  on_watcher_restart: resume           # resume | fail — when watcher restarts mid-flight
  task_timeout_s: 1800                 # SIGTERM a queue task after N seconds (null disables)
  bookkeeping_files:                   # files queue tasks should NOT commit to their branches
    - intent.md
    - otto.yaml
```

### Timeout semantics

Only two timeout knobs, both orthogonal:

| Knob | Scope | Default |
|---|---|---|
| `run_budget_seconds` | Total wall-clock across the whole `otto build` / `otto certify` / `otto improve` run. If exhausted, the run pauses with a resumable checkpoint. | `3600` (1h) |
| `spec_timeout` | Per-phase cap on the spec-agent call specifically. Applied as `min(run_budget_remaining, spec_timeout)`. | `600` (10m) |

Previous `certifier_timeout` and `agent_timeout` keys have been removed — `run_budget_seconds` replaces them end-to-end.

### Certifier modes

| Mode | Prompt | Use when |
|---|---|---|
| `fast` (default) | `certifier-fast.md` — 3-5 happy paths, inline, ~30s | Dev iteration, quick smoke check |
| `standard` | `certifier.md` — subagents + screenshots, ~2 min | Real verification without adversarial probing |
| `thorough` | `certifier-thorough.md` — adversarial, edge cases, code review, ~5 min | Production-grade QA |

All three modes respect the spec (if `--spec` was used): scope-creep features in "Must NOT Have Yet" are reported as `STORY_RESULT: scope-creep-<slug> | FAIL` in any mode.

### Providers

Otto supports two agent providers:

| Provider | What it uses | Set with |
|----------|-------------|----------|
| `claude` (default) | Claude Code CLI via Agent SDK | `provider: claude` |
| `codex` | OpenAI Codex CLI | `provider: codex` |

Both providers use the same prompts, certification loop, and output parsing. Switch providers by changing one line in `otto.yaml`.

### Auto-detection

On first run, otto detects:
- **Test command** — npm test, pytest, cargo test, go test, etc. (15+ frameworks)
- **Package manager** — npm, pnpm, yarn, bun
- **Default branch** — from git remote

You can override any auto-detected value in `otto.yaml`.

## Common Workflows

### Quick iteration (cheap, seconds-per-loop)

```bash
otto build "CLI that converts CSV to JSON"        # fast certifier by default
otto build --resume                               # if interrupted
```

### Non-trivial product (spec-gated)

```bash
otto build "bookmark manager with tags and share links" --spec
# → spec agent writes otto_logs/runs/<id>/spec.md
# → summary printed: Intent / Must-Have / Must-NOT-Have / Open questions
# → [a]pprove / [e]dit / [r]egenerate / [q]uit
# approve → build runs with spec-aware certifier
```

### Production QA at the project level

Set `certifier_mode: thorough` in `otto.yaml` and let the project run real verification by default. Developers override with `--fast` for quick checks.

### Using an external spec (e.g. from `/office-hours` or Spec Kit)

```bash
otto build --spec-file ./my-product-spec.md    # skips spec agent; implies --yes
```

The file must contain `**Intent:** <line>` plus required sections (`## Must Have`, `## Must NOT Have Yet`, `## Success Criteria`).

### CI / scripted runs

```bash
otto build "intent" --spec --yes --thorough    # spec + auto-approve + real QA
# Non-zero exit if certification failed.
```

### Crash / timeout recovery

```bash
otto build "intent" --spec       # crashes or Ctrl-C
otto build --resume              # picks up from the last checkpoint phase
                                 # (spec / spec_review / spec_approved / build / certify / round_complete)
```

### Parallel features (queue + merge)

Run several `otto build` jobs concurrently in their own worktrees, then land the successful ones together:

```bash
# 1. Enqueue (each gets its own .worktrees/<id>/ and build/<id>-<date> branch)
otto queue build "csv export"
otto queue build "settings redesign"
otto queue improve bugs --rounds 3

# 2. Run the watcher in a tmux pane (foreground)
otto queue run --concurrent 3

# 3. After tasks finish (`otto queue ls` shows status=done), land them
otto merge --all --cleanup-on-success
```

The watcher commits artifacts to per-task branches and writes manifests to `otto_logs/queue/<task-id>/`. `otto merge` runs `git merge --no-ff` against the target branch; the LLM conflict agent is only invoked when git can't auto-merge. The agent gets full project context (Bash, test command) and resolves all branches' conflicts in one session. Use `--fast` for a pure-git merge that bails on the first conflict.

## Logs

```
otto_logs/
  checkpoint.json            Current run state (run_id, phase, cost, session_id)
  runs/<run-id>/
    spec.md                  Approved or in-review spec (spec-gate)
    spec-v1.md, spec-v2.md   Prior versions after regen
    spec-agent.log           Spec agent trace
  builds/<build-id>/
    agent.log                Structured: commits, certifier rounds, verdict
    agent-raw.log            Full agent output
    checkpoint.json          Cost, duration, stories tested/passed
  certifier/<cert-id>/
    proof-of-work.html       Report with embedded screenshots
    proof-of-work.json       Machine-readable results
    evidence/                Screenshots, video recordings
  queue/<task-id>/
    manifest.json            Final task manifest (status, cost, branch, paths)
  merge/
    merge.log                Orchestrator + conflict-agent + triage-agent events
    <merge-id>/state.json    Per-merge state with branch outcomes + verification plan
  run-history.jsonl          One line per build (for otto history)
build-journal.md             Round-by-round tracking (improve mode)
improvement-report.md        Final improve summary with merge instructions

# Queue bookkeeping (gitignored)
.otto-queue.yml              Pending/queued tasks (the queue file itself)
.otto-queue-state.json       Watcher state (per-task status, child PIDs)
.otto-queue-commands.jsonl   Pending commands (rm/cancel) for the watcher
.worktrees/<task-id>/        Per-task git worktree
```

Otto auto-manages `.gitignore` on first touch — the runtime files above (queue bookkeeping, `.worktrees/`, `otto_logs/`) are added so they don't accidentally get committed. Common build artifacts (`__pycache__/`, `node_modules/`, `.pytest_cache/`, `dist/`, etc.) are also added so the merge orchestrator's "no new untracked files" check doesn't bail when the conflict agent runs project tests.

## Project structure

```
otto/                        ~11,000 lines
  pipeline.py                Build pipeline, certify-fix loop
  certifier/__init__.py      Certifier agent
  spec.py                    Spec-gate: run_spec_agent, review_spec, validate_spec
  budget.py                  RunBudget — wall-clock budget tracker
  checkpoint.py              Resume state, phase tracking, atomic writes
  markers.py                 Parse STORY_RESULT/VERDICT/METRIC from agent output
  prompts/                   Editable prompts (spec, build, certifier modes, code, improve, merger-conflict)
  agent.py                   Agent SDK wrapper, AgentCallError, session_id preservation
  cli.py                     CLI: build, certify (+ spec orchestration)
  cli_improve.py             CLI: improve (bugs, feature, target)
  cli_queue.py               CLI: queue (build, improve, certify, run, ls, show, rm, cancel, cleanup)
  cli_merge.py               CLI: merge
  config.py                  Config, intent resolution, helpers
  journal.py                 Build journal for improve rounds
  setup_gitignore.py         Auto-manages .gitignore for runtime + build artifacts
  queue/                     Parallel queue subsystem
    schema.py                .otto-queue.yml + .otto-queue-state.json read/write
    runner.py                Foreground watcher (spawn / reap / cancel / timeout)
    ids.py                   Slug + branch + worktree-path generation
  merge/                     Multi-branch merge subsystem
    orchestrator.py          Sequential and consolidated agent-mode merge drivers
    git_ops.py               Thin git wrappers (merge_no_ff, conflicted_files, …)
    conflict_agent.py        Per-conflict + consolidated LLM resolvers
    triage_agent.py          Post-merge verification-plan generator
    stories.py               Collect stories from merged branches
    state.py                 BranchOutcome + per-merge state.json
tests/                       420 tests, ~6,700 lines
  _helpers.py                Shared init_repo factory used across test files
```

## Requirements

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- Git repository

## License

MIT
