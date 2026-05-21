# Otto — Project Instructions

Read top-to-bottom at session start. Re-read the Hard Rules whenever unsure.
Codex follows the same playbook — `AGENTS.md` holds Codex-only addenda.

## Hard Rules

These are load-bearing. If a rule conflicts with intuition, the rule wins.

| # | Rule | Why |
|---|---|---|
| 1 | **Read real artifacts before claiming anything.** | LLMs hallucinate; logs don't. |
| 2 | **After every edit, grep the whole project for what you changed — not just the file.** | Duplicates rot elsewhere. The #1 repeated bug. |
| 3 | **When audiences need different content, split the file. No render-time strip.** | Source-on-disk must equal what's shipped, or tests lie. |
| 4 | **Back every load-bearing prompt rule with a structural code check.** | LLMs rationalize past text; code-level invariants they can't. |
| 5 | **Verify behavior live (browser / curl). Text-search is advisory.** | Text matches succeed without the behavior they imply. |
| 6 | **Delete duplicates instead of patching them in parallel.** | Three patches around one bug = the bug is duplication. |
| 7 | **When closing a late-stage fix, ship the earlier check that would have caught it.** | Bug cost is ~10× per pipeline stage. |
| 8 | **Full evidence packet or skip the step. Never half-data.** | Half-data → confident wrong answers; no-data → honest failure. |
| 9 | **In shared docs, stack-shape examples only. No run IDs / sessions / project paths / commit hashes.** | Specifics rot; principles scale. |
| 10 | **Ask before any destructive or irreversible action.** | Trust isn't recoverable; saved minutes < lost work. |

## Decision triggers

When the trigger fires, do the action. Don't deliberate.

| When you... | Do this |
|---|---|
| Finish editing a file | Grep the project for the changed pattern. Fix every match. Grep again to confirm zero. |
| Find a bug | Grep for siblings first. 3+ siblings → design a structural check, don't write patch #4. |
| Need to delete code | Grep `otto/` + `tests/` for callers. Delete dead tests too. |
| Want a prompt rule to actually hold | Add a runtime or compile-time check. Prompts alone don't enforce. |
| Want one agent role to see content others don't | Split the prompt file by audience; compose in the renderer. No markers, no strip logic. |
| Build prompt content for an agent | Stack-shape examples only. No project names, sessions, commits, paths. |
| Encounter a failing test | `git stash && pytest && git stash pop` to baseline. Flag pre-existing; don't claim authorship. |
| Finish a late-stage bugfix | Ask: "what cheaper check would have caught this?" Ship it. |
| Hit a verdict mismatch | Trace the chain: session `summary.json` → `read_graph()` per-task. Worst-wins. |
| Get a "this is working now" feeling | Don't claim done. Run the test or the live flow first. |
| Touch a file after 10+ messages in session | Re-read it. Context compression drops content. |
| Hit a hook failure | Read the message; fix the root cause. Never bypass. |
| About to do something destructive | Stop. Describe it. Wait for explicit confirmation. |
| Send a child task via `submit_subtask(intent=...)` | Include in the intent: stack, owned paths, imported contracts, forbidden paths. |
| About to run any git write | First run `pwd && git branch --show-current`. Confirm worktree + branch are right. |
| Adopting a Codex fix into a commit | Credit it: `Co-Authored-By: Codex`. |

## NEVER

- Never `git add -A` or `git add .` — stage explicit paths only.
- Never `--no-verify`, `--no-gpg-sign`, `-c commit.gpgsign=false` — investigate the hook failure.
- Never `git reset --hard`, `git checkout --theirs/--ours` on whole files, `git push --force`, branch deletion, or `rm -rf` without confirmation.
- Never mix worktree + main-repo git ops in one session. Stay in the worktree the user gave you.
- Never reference `otto_logs/` paths in agent prompts, spec content, CHARTER, or git commits. Use `otto/paths.py`.
- Never use `system_prompt=None` — blanks Claude Code's defaults. Use `{"type": "preset", "preset": "claude_code"}`.
- Never set MCP `approval-policy` to anything but `"never"` — anything else hangs forever waiting for approval.
- Never ask Codex to create branches or PRs — `git checkout -b` can hang in its sandbox.
- Never truncate, cap, or sample evidence going to an agent.
- Never `pkill` a live otto run while a fix→resume is mid-flight. Let it finish or use `otto recover` to coordinate.
- Never delete `otto_logs/` between tests. Let the code's TTL handle it; manual deletes lose evidence.

## How Otto works (just enough)

**Pipeline**

```
compile spec → root Lead decomposes → children build in parallel worktrees
            → integration Lead merges + self-verifies live journeys → verdict
```

**Verdict aggregation**

Worst-wins: `catastrophic > merge_blocked > unverified > partial > pending_children > pass`.
A passing integration session can aggregate to `partial` if any child verdict
is stale. When a verdict mismatch confuses you, trace the chain.

**Behavioral authority**

The integration Lead. Drives every behavior journey via
`mcp__chrome-devtools__*` + Bash/curl in one continuous session — no
separate verifier, no repair-agent handoff. Its `verdict.json` carries
`journeys[]` with credible detail (≥40 chars) and evidence paths;
`journey_verdict_sink` consumes them.

**Demote whitelist**

`CHECK_KINDS = (local_scope_check, verdict_consistency)`. Only these
kinds demote the agent's `pass`. Producer-set `required` flags are
unreliable — don't gate on them.

**Contract semantics**

| `check:` | Behavior |
|---|---|
| `literal` | Exact line preservation required (route registries) |
| `semantic` + probes | Probes must satisfy in final text |
| `semantic` no probes | Trust the owner; no line check |
| (no contract) | Advisory finding, not demote |

**Partition rules**

- Children inherit ownership ONLY through the intent text the root Lead writes.
- Foundation MUST NOT seed files inside any feature's `feature_owned_paths`.
- Loaders tolerate absent feature files — use aggregator pattern
  (re-exporting index / globbing loader / lazy-import-with-fallback).
- `feature_owned_paths` (CHARTER) is canonical; `leaf_extension_globs` is
  advisory; both should agree.

**Resume**

`partial` and `merge_blocked` are resumable; `pass` and `catastrophic` are
terminal.

```
otto recover plan-resume                                # preview
otto recover reset-verdict --task <id> --to unverified  # clear bogus verdicts
otto run "<intent>"                                     # no --fresh → resume
```

Skips compile + decompose + child rebuild. Re-runs integration only.

**Session layout** (`otto_logs/sessions/<id>/`)

| Path | What it tells you |
|---|---|
| `summary.json` | verdict, cost, duration |
| `checkpoint.json` | resume state (running/paused only) |
| `checkpoint.events.jsonl` | compile_done, decompose_done, integration_done |
| `build/narrative.log` | live agent trace (`tail -f` during runs) |
| `build/messages.jsonl` | lossless SDK event stream |
| `integration/verdict.json` | integration Lead's self-verified journeys |
| `integration/verification_plan.json` | runner check matrix (agent_verdict vs final_verdict) |
| `integration/screenshots/*.png` | live UI evidence |
| `proof-packet.html` / `.json` | rendered proof |

## How to work on Otto

**Architecture invariants enforced in code (don't fight these):**

- Prompts live in `otto/prompts/*.md`. Edit those, not Python strings.
- Path construction goes through `otto/paths.py`.
- In-process MCP breaks with the Agent tool. External MCP subprocess required.
- Agent SDK doesn't stream ToolResultBlocks for MCP tools — use file side-channels.

**Pure-function extraction for testability**

When adding a check that depends on git / CHARTER / runtime state:

- Pure function takes inputs as args (unit-testable with hand-built fixtures)
- Thin wrapper gathers inputs and calls the pure function (exercised by live)

**Testing posture**

| Test type | When | What it catches |
|---|---|---|
| Unit | Always, for any new logic | Function-level correctness |
| Integration | When changing component boundaries | Boundary contract bugs |
| Live runs | Architecture validation | Multi-agent failure modes |
| Multi-project (≥4 diverse) | Prompt changes | Project-specific overfit |

**Codex collaboration triggers**

| When the work is... | Who codes |
|---|---|
| Concurrency / locking / race conditions / state management / systematic refactor | Codex |
| Bug fix where root-cause analysis matters | Codex |
| Architecture / system design | Claude |
| UI/UX / web client | Claude |
| Codebase navigation / discovery | Claude |
| Rapid prototyping / integration glue | Claude |
| Ambiguous / both have ideas | Both, independently → compare → merge |

Other Codex rules:
- When Codex finds a bug during review, Codex fixes it (new `mcp__codex__codex` call, `sandbox: "workspace-write"`). The blind spot that missed the bug shapes the fix.
- Skip Codex-gate for small / mechanical / test-only / doc fixes (waste of time).

## Reference

**Quick diagnosis**

```
otto run "<intent>"                          # canonical pipeline
otto proof list                              # run history
otto proof open                              # open latest proof
otto recover status                          # current v5 pipeline state
otto recover plan-resume                     # preview resume
readlink otto_logs/latest                    # most recent session dir
```

**Debugging recipes**

| Question | Where |
|---|---|
| Why did the build fail? | `otto_logs/latest/build/narrative.log` — scan `VERDICT:` markers |
| Did the integration Lead self-verify? | `integration/verdict.json` — `journeys[]` with `passed`, `detail≥40`, `evidence` |
| Why did the verdict demote? | `integration/verification_plan.json` — `agent_verdict` vs `final_verdict`, `kind in CHECK_KINDS`? |
| Task graph state? | `python -c "from otto.queue.task_graph import read_graph; from pathlib import Path; print(read_graph(Path('.')))"` |

**Launch a background otto run**

```bash
nohup bash -lc "cd $PROD && .venv/bin/otto run --model claude-sonnet-4-6 \
  \"\$(cat /tmp/intent.txt)\"" > $LOG 2>&1 &
```

Plus `Bash(run_in_background=true)` for streaming. Log path OUTSIDE the
product worktree. Don't wrap in `python -c "Popen(...)"` — the wrapper
exits and the child detaches, breaking monitoring.

**Test commands**

```
.venv/bin/python3 -m pytest tests/<file> -x --no-header -q  # fast iteration
uv run python scripts/test_tiers.py smoke                   # minimal gate
uv run python scripts/test_tiers.py fast                    # non-browser gate
uv run python scripts/test_tiers.py web                     # TS + Mission Control
uv run ruff check otto scripts tests                        # lint
npm run web:typecheck                                       # TS check
```

## Pointers

- **`~/.claude/CLAUDE.md`** — global user conventions (permissions, dotfile sync, codex-collab).
- **`AGENTS.md`** (this dir) — Codex-only addenda (sandbox modes, build/test races, MC posture).
- **`/Users/yuxuan/work/cc-autonomous/codex-learnings.md`** — persistent Codex memory for Otto.
- **`otto/prompts/`** — agent prompts. Stack-agnostic. Hard Rules at the top of each.
- **`docs/`** — design notes, active project plans.
