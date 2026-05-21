# Otto — Project Instructions

Read these before any non-trivial Otto work. The Hard Rules at the top of
this file are the load-bearing ones — the rest is detail / rationale / how-to.

## Hard Rules — non-negotiable

1. **Debug from real logs, never guess.** `otto_logs/sessions/<id>/build/narrative.log`
   is the source of truth for what the agent did. Read it before forming
   hypotheses. Read it AGAIN before claiming a fix worked.

2. **Verify before claiming.** After every edit, grep the project for the
   pattern you just changed. After 10+ messages in a conversation, re-read
   any file before editing it (context compression silently drops content).
   If a fix is "done," the evidence is in the code AND a re-run, not a
   diff that looks right.

3. **`otto_logs/` paths NEVER leak.** Not into agent prompts, not into
   git commits, not into spec content. All path construction goes through
   `otto/paths.py` — no hardcoded `"otto_logs/..."` literals elsewhere.

4. **Prompt edits are stack-agnostic.** No run IDs, no session IDs, no
   project names, no specific paths from a particular run. Stack-shape
   examples ("for SQLAlchemy: shared declarative Base") are fine because
   they map to whatever stack the user has. Project-specific examples
   make prompts overfit and rot.

5. **File-on-disk == what's shipped.** If two audiences need different
   content, SPLIT the file (e.g. `lead.md` + `lead-architect.md`). Don't
   add invisible runtime conditionals, marker comments, or string surgery
   in the renderer — source-rendered drift hides bugs and makes tests lie.

6. **Trust the agent — full data or skip.** Never truncate / cap / sample
   what you hand an agent. Either it gets the full evidence packet or
   it skips that step entirely. Half-data is worse than no data.

7. **Centralize and delete > patching.** When you find a bug, look for
   the duplicated/related code first. A 70-LOC deletion of a duplicate is
   worth more than 7 patches across 7 files. When 3+ patches accumulate
   around one bug class, step back and design a protocol (or a structural
   check); don't write patch #4.

8. **No `git add -A` / `git add .`.** Stage explicit product paths only.
   Worktree safety: NEVER mix worktree and main-repo git ops in the same
   session. Verify `pwd && git branch --show-current` before every git
   write.

9. **Behavioral verification beats text-search verification.** The
   integration Lead's chrome-devtools / curl journey self-verify is the
   authoritative behavioral signal. Text-search checks
   (`page_has_ia_route`, `entity_has_empty_state`, `action_has_test`) are
   advisory linting. Never let a doc-vs-doc string match demote a
   live-verified `pass`.

10. **Structural enforcement beats prompt enforcement.** Prompts can be
    rationalized past by the LLM ("This violates the isolation principle
    but for a tiny 2-feature webapp it's the pragmatic path" — actual
    linkboard root Lead). Add a compile-time check whenever a prompt rule
    is load-bearing. Phase B's `foundation_seeded_feature_path` finding
    is the canonical example.

## Otto-the-product — key architectural invariants

### v5 verifier semantics (post-2026-05 hardening)

- **`CHECK_KINDS` is the demote whitelist.** `local_scope_check` and
  `verdict_consistency` demote; everything else is advisory. The
  producer-set `required` flag is unreliable (defaults to True); do NOT
  gate on it. See `otto/v5_verification_plan.py:289`.
- **The integration Lead self-verifies journeys in one continuous session**
  via `mcp__chrome-devtools__*` + Bash/curl. No separate verifier and
  no repair-agent handoff. `journey_verdict_sink.agent_self_verified_executor_results`
  pulls those records into the fail-closed sink. See `lead-integration.md`.
- **`check: literal` contracts gate; `check: semantic` trusts the owner.**
  When a `semantic` contract has no `required_exports` / `behavior_probes`,
  the union guard exempts (trust); when probes are declared, they must
  be satisfied. Literal contracts (route registries) still demand exact
  line preservation.
- **Shared path with NO foundation_contract → advisory, not demote.**
  The architect's missing declaration surfaces via
  `_integration_union_undeclared_shared_paths` (operator-visible). Phase B's
  foundation-gate check catches the declared cases before features dispatch.

### v5 verdict propagation (worst-wins)

`aggregate_verdict` rolls children up to the parent:
`catastrophic > merge_blocked > unverified > partial > pending_children > pass`.
The integration session can produce verdict=pass while the aggregate
remains partial if any child is stale-partial. When debugging a "verdict
says X but everything passed" mismatch, ALWAYS check
`read_graph(project_dir)["tasks"]` for individual verdicts.

### Resume mechanism (Phase 1.2-A, 2026-05-19)

`partial` and `merge_blocked` are resumable; only `pass` and
`catastrophic` are terminal. Fast loop:

```bash
otto recover plan-resume                              # preview
otto recover reset-verdict --task <id> --to unverified  # clear bogus verdicts
otto run "<intent>"                                   # NOT --fresh → resume
```

Resume skips compile + decompose + child rebuild; re-runs integration
only (~5 min, ~$1.20 on linkboard). If you need to re-verify a specific
child whose verdict was bogus: `reset-verdict` it, then resume.

### Ownership / partition rules

- The root Lead writes child intents via `submit_subtask(intent=...)`.
  Children inherit ownership only through the intent text — `lead.md`
  doesn't tell them where their boundaries are. Every intent MUST include
  stack, owned paths, imported contracts, and forbidden paths (audit F-7).
- `feature_owned_paths` (CHARTER) and `leaf_extension_globs`
  (`registration_isolation`) are parallel — the runtime treats
  `feature_owned_paths` as canonical. Both should agree; mismatches are
  CHARTER bugs the architect should fix.
- Foundation MUST NOT seed feature-owned files. Use an aggregator
  (re-exporting index / globbing loader / lazy-import-with-fallback). The
  loader's absent-feature-file tolerance is the design — pre-seeding stubs
  is what hits the union guard 25 min later.

## Anti-patterns learned the hard way

### Prompt edits

- **Don't write run IDs into prompts.** Stack-agnostic examples scale;
  project-specific examples rot. (Reverted commit `235a8de62` → `8295dea05`.)
- **Don't bury Hard Rules.** Put load-bearing invariants in the TOP of a
  prompt. Anything past line ~150 sits in the "lost in the middle" zone
  where instruction-following degrades. (Phase A restructure.)
- **Don't render conditionally with markers.** If two audiences see
  different content, split the file. `lead.md` + `lead-architect.md`,
  not `<!-- BLOCK_START -->` + render-time strip. (Commit `85bfdb52f`.)

### Verification

- **Don't have a separate "verifier agent" + "repair agent" pair.** That's
  the "deterministic fixture for Playwright" anti-pattern. Unify: one
  agent session does build + merge + verify + fix + re-verify, all live.
- **Don't demote on doc-vs-doc text matches.** P0a, then the real fix
  (`bd89feb96`): gate demote on `kind in CHECK_KINDS`, not on a producer
  flag.
- **Don't trust your own theory before tracing the full chain.** When
  the integration verdict was `pass` but the aggregate was `partial`,
  the immediate theory ("children stuck") was right BUT the underlying
  cause (foundation seeded feature-owned paths) needed separate
  investigation. Always trace verdict propagation through every layer.

### Architectural surgery

- **Don't ship a big refactor when a narrow surgical change captures
  most of the value.** Phase E almost shipped a sweeping union-guard
  rewrite; the actual fix was 20 LOC + an advisory helper.
- **Don't use destructive operations as a shortcut.** `git reset --hard`,
  blowing away worktrees, deleting otto_logs — investigate first, ask
  before acting. Almost every "let's just nuke and restart" instinct
  loses real work.
- **Don't half-finish.** Either fully delete the dead code (1500 LOC
  of `journey_ui_executor.py` in commit `b49518d53`) OR don't touch it.
  Half-deleted code with orphan callers is worse than untouched code.

## Otto-dev workflows

### Patches → protocols

When you've written 3+ patches around one bug class, pause and ask: is
there a protocol/check/structural invariant that would have prevented
ALL of these? Ship that instead of patch #4. Examples:
- Foundation-seeded feature paths: patches in the union guard → structural
  check at foundation_gate (`d357e6db4`).
- Brittle predicates: CHECK_KINDS whitelist (`bd89feb96`) instead of
  whack-a-mole on individual demote-on-failure paths.

### Pure-function extraction for testability

When adding a check that depends on git/CHARTER/runtime state, split it:
- A pure function that takes the inputs as args (`_compute_foundation_seeded_findings`)
- A thin wrapper that gathers the inputs and calls the pure function

The pure function is trivially unit-testable with hand-built fixtures;
the wrapper is exercised by live runs only. This was a 6-test win in
`d357e6db4`.

### Live validation cadence

- **Unit tests catch function-level correctness.** Always required for
  any new logic.
- **Integration tests catch component-boundary bugs.** Required when
  changing how components talk (verifier ↔ sink ↔ CLI).
- **Live runs are the only thing that catches multi-agent failure
  modes.** Budget ~6-10 live product runs per architecture validation.
  Prompt changes need ≥4 diverse projects before claiming they hold.
- **Linkboard fast-repro** lives at `/tmp/fastrepro_linkboard_intent.txt`
  and runs in ~25-30 min for ~$4 (Sonnet). Use it for whole-pipeline
  smoke validation.
- **Resume validates faster.** ~$1.20 / ~5 min if integration alone is
  what changed. See Resume mechanism above.

### Don't `--no-verify`. Don't bypass.

Hooks fail for a reason. Investigate the failure; don't skip it. If a
git operation hangs, find the hung process; don't `git reset --hard`.
If a test fails, fix it or document why it's pre-existing — don't
silence it.

## Codex collaboration on otto-dev

Codex is a peer for correctness-critical work. Use it proactively:

- **Codex codes**: concurrency, locking, race conditions, state
  management, systematic refactors, bug fixes where root-cause analysis
  matters.
- **Claude codes**: architecture & system design, UI/UX, rapid
  prototyping, integration work, codebase navigation.
- **Both code independently, compare & merge** for ambiguous problems,
  and testing (Claude writes unit, Codex writes edge-case).
- **When Codex finds bugs, Codex fixes them** (new
  `mcp__codex__codex` call with `sandbox: "workspace-write"`). Claude
  must NOT fix Codex-found bugs — the same blind spot that missed the
  bug shapes the fix.
- **Skip Codex-gate for**: small/mechanical/test-only/doc fixes (waste
  of time). Gate only non-trivial / correctness-critical / concurrency /
  false-pass work.
- **`approval-policy: "never"`** for all MCP calls. Any other value
  hangs forever waiting for approval that nobody can provide.
- Don't ask Codex to create branches or PRs — those can hang in the
  Codex sandbox. Do branch/PR work yourself.

See `/Users/yuxuan/work/cc-autonomous/codex-learnings.md` for
Codex-specific gotchas (sandbox behavior, web-client build/test races,
token accounting). That file is the Codex-side companion to this one;
keep them in sync conceptually.

## Debugging & Log Analysis

When debugging otto runs, ALWAYS read real logs. Never guess.

### Quick diagnosis

```bash
otto run "<intent>"                                   # canonical i2p pipeline
otto proof list                                       # Run history with results
otto proof open                                       # Open latest proof report
otto recover plan-resume                              # Preview what resume would do
otto recover status                                   # Current v5 pipeline state
cat otto_logs/cross-sessions/history.jsonl            # Machine-readable history
readlink otto_logs/latest                             # Most recent session
readlink otto_logs/paused                             # Resumable session (if any)
```

### Per-session layout (`otto_logs/sessions/<session-id>/`)

Every `otto run` invocation creates one session dir.
Session id format: `<yyyy-mm-dd>-<HHMMSS>-<6hex>`.

| File / dir | What it tells you |
|------|-------------------|
| `summary.json` | Completed-session summary: verdict, cost, duration |
| `checkpoint.json` | Resume state — exists only while running/paused |
| `checkpoint.events.jsonl` | Event log: compile_done, decompose_done, integration_done |
| `intent.txt` | Archival copy of the intent at session start |
| `spec/spec.json` | Compiled flat spec consumed by build/audit/merge |
| `lead/narrative.log` | Root Lead's narrative trace (decomp reasoning) |
| `build/narrative.log` | Streamed event log: tool calls, results, thinking, VERDICT markers |
| `build/messages.jsonl` | Lossless normalized SDK event stream |
| `integration/verdict.json` | Integration Lead's self-verified verdict + journeys[] |
| `integration/verification_plan.json` | Runner's check matrix (agent_verdict vs final_verdict, checks, advisories) |
| `integration/screenshots/*.png` | Live UI evidence from chrome-devtools journey |
| `proof-packet.html` / `proof-packet.json` | Rendered proof packet |

### Common debugging patterns

**"Why did the build fail?"**
→ Read `otto_logs/latest/build/narrative.log` — scan for `VERDICT:` markers.
→ Programmatic: `jq -c '.' otto_logs/latest/build/messages.jsonl`.

**"Did the integration Lead self-verify journeys?"**
→ Read `integration/verdict.json` — should have `journeys[]` with `passed` + `detail` (≥40 chars) + `evidence` paths.

**"Why did the verdict get demoted?"**
→ `integration/verification_plan.json` → compare `agent_verdict` to `final_verdict`. If they differ, the runner check matrix is the cause.
→ `checks[]` failed with `kind in CHECK_KINDS` → legitimate demote. Else → bug; the `required` flag isn't authoritative.

**"What's the task graph state?"**
→ `python3 -c "from otto.queue.task_graph import read_graph; from pathlib import Path; print(read_graph(Path('.')))"`.

**"Live tail during a run?"**
→ `tail -f otto_logs/latest/build/narrative.log`.

### Launch pattern for background otto runs

```bash
nohup bash -lc "cd $PROD && .venv/bin/otto run --model claude-sonnet-4-6 \
  \"\$(cat /tmp/intent.txt)\"" > $LOG 2>&1 &
```

Plus `Bash(run_in_background=true)` so the task system can stream
output. Do NOT wrap in `python3 -c "Popen(...)"` — the wrapper exits
and the otto child detaches, breaking monitoring.

Log path **OUTSIDE** the product worktree (don't pollute the product's
git tree).

## Key Principles (concise)

- `system_prompt` must use `{"type": "preset", "preset": "claude_code"}` — NEVER `None`. Blanks Claude Code's defaults.
- Trust the agent — give full data or skip entirely. Never truncate/cap.
- Prompts live in `otto/prompts/*.md` — edit without touching Python code.
- The certifier reports symptoms, not fixes. The coding agent diagnoses.
- `otto_logs/` paths must NEVER leak into agent prompts or git commits.
- All path construction goes through `otto/paths.py`.
- Otto in-process MCP breaks with Agent tool — external MCP subprocess is required.
- Agent SDK doesn't stream ToolResultBlocks for MCP tools — use file side-channels.

## Test commands

- `.venv/bin/python3 -m pytest tests/<file> -x --no-header -q` — fastest iteration loop.
- `uv run python scripts/test_tiers.py smoke` — minimal confidence gate.
- `uv run python scripts/test_tiers.py fast` — day-to-day non-browser gate.
- `uv run python scripts/test_tiers.py web` — TypeScript + Mission Control tests.
- `uv run ruff check otto scripts tests` — Python lint.
- `npm run web:typecheck` — type-check the web client.

When a test fails, FIRST check if it's pre-existing (`git stash &&
pytest ... && git stash pop`). If pre-existing, flag it in the PR
notes but don't claim you broke it.

## When to stop and ask

- Destructive ops (force push, `git reset --hard`, branch deletion, rm -rf).
- Hard-to-reverse ops (amending published commits, dropping packages).
- Actions visible to others (push, PR creation, Slack/email).
- Uploading code/content to third parties.
- When the user's request seems like an XY problem — flag the underlying
  goal before executing.
- When 3+ patch attempts have failed on the same bug — stop and write
  `debug.md` (per `~/.claude/CLAUDE.md` debugging protocol).
