# Otto — Project Instructions

Read once at session start. The Principles section is the load-bearing part;
everything below is application and reference. Codex follows the same
playbook — see `AGENTS.md` for Codex-only addenda.

## Principles

These derive from how LLMs and codebases actually behave, not from convention.
Apply them all; they reinforce each other.

1. **LLMs hallucinate. Real artifacts don't.** Your memory of what the code
   does is less reliable than reading the code; your prediction of what a fix
   does is less reliable than running it. Debug from `otto_logs/.../narrative.log`
   and `messages.jsonl`, not from theory. After 10+ messages in a conversation,
   re-read any file before editing it (context compression silently drops content).

2. **Verify before claiming.** After every edit, grep the project for the
   pattern you just changed — not just the file. "Done" requires evidence in
   the code AND in a passing run, not a diff that looks right.

3. **File-on-disk == what's shipped.** If two audiences need different
   content, split the file. No invisible markers, no render-time string
   surgery, no comments that strip themselves. Source-rendered drift hides
   bugs and makes tests lie about what agents actually see.

4. **LLMs rationalize past text.** A rule buried in a 400-line prompt will be
   ignored when the agent finds a "pragmatic shortcut." When a prompt rule is
   load-bearing, add a structural check that fires regardless of prompt
   compliance. Hard invariants live in code, not in markdown.

5. **Behavioral verification beats text-search.** A text match can succeed
   without the behavior it implies — agents and graders alike. The
   integration Lead drives chrome-devtools and curl through real journeys;
   that's the authoritative signal. `page_has_ia_route` and friends are
   advisory linting, not gates.

6. **Centralize and delete > patching.** Duplicates drift apart. A 70-LOC
   deletion of a duplicated helper is worth more than 7 patches keeping
   copies in sync. When 3+ patches accumulate around one bug class, the
   bug is the duplication; design a protocol or structural check that
   prevents the whole class, not patch #4.

7. **Shift left.** A bug caught at compile time costs orders of magnitude
   less than the same bug caught at integration. Whenever you find a bug
   that surfaced late, ask what cheaper check would have caught it
   earlier, and ship that check too.

8. **Full data or skip.** Never truncate, cap, or sample what an agent
   receives. Half-data is worse than no data — it produces confident
   wrong answers. Either the agent gets the whole evidence packet or
   that step is skipped.

9. **Specifics rot. Principles scale.** Shared documents (prompts,
   CLAUDE.md, contracts) must be stack-agnostic and example-shape
   agnostic — no run IDs, no session IDs, no project paths from one
   particular run. Stack-shape examples ("for SQLAlchemy: shared
   declarative Base") are fine; they map to whatever the user has.

10. **When in doubt, ask. When destructive, always ask.** Force push,
    `git reset --hard`, branch deletion, `rm -rf`, dropping
    dependencies, modifying CI, third-party uploads, anything visible
    to others — confirm first. Almost every "let's just nuke and
    restart" instinct loses real work. Hooks fail for reasons; don't
    `--no-verify`.

## How Otto works (just enough to be useful)

Otto runs a multi-agent pipeline: compile spec → root Lead decomposes →
children build in parallel worktrees → integration Lead merges and
self-verifies live behavior journeys → verdict.

**Verdict propagation is worst-wins.** `aggregate_verdict` rolls a
task's children up:
`catastrophic > merge_blocked > unverified > partial > pending_children > pass`.
A passing integration session can still aggregate to `partial` if any
child verdict is stale. When debugging a verdict mismatch, ALWAYS trace
the full chain: per-session `summary.json` → task graph
(`read_graph(project_dir)`).

**The integration Lead is the behavioral authority.** Post-Phase-1
(2026-05), it self-verifies every journey via chrome-devtools / curl
in one continuous session — no separate verifier or repair agent. Its
`verdict.json` carries `journeys[]` with credible detail (≥40 chars)
and evidence paths; `journey_verdict_sink` consumes these.

**Demote whitelist is `CHECK_KINDS`**, not the producer-set `required`
field. `CHECK_KINDS = (local_scope_check, verdict_consistency)` —
those are the only kinds that demote agent's pass. Everything else is
advisory.

**Partition rules.** Children inherit ownership through the intent
text the root Lead writes for them (NOT through any shared prompt).
Every `submit_subtask(intent=...)` MUST include stack, owned paths,
imported contracts, forbidden paths. CHARTER's `feature_owned_paths`
is the canonical ownership map; foundation MUST NOT seed files inside
it (loaders tolerate absent feature files — use aggregator pattern).

**Resume is the fast loop.** `partial` and `merge_blocked` are
resumable; only `pass` and `catastrophic` are terminal. Reset bogus
verdicts (`otto recover reset-verdict --task <id> --to unverified`),
then re-run without `--fresh`. Skips compile + decompose + child
rebuild (~26 min on linkboard); re-runs integration only (~5 min).

**Per-session layout** at `otto_logs/sessions/<id>/`:
- `summary.json` — verdict, cost, duration
- `checkpoint.json` — resume state (running/paused only)
- `checkpoint.events.jsonl` — `compile_done`, `decompose_done`, `integration_done`
- `intent.txt` — intent snapshot
- `spec/spec.json` — compiled flat spec
- `build/narrative.log` — streamed agent trace (tail -f during run)
- `build/messages.jsonl` — lossless SDK event stream
- `integration/verdict.json` — integration Lead's self-verified journeys
- `integration/verification_plan.json` — runner check matrix (agent_verdict vs final_verdict, checks, advisories)
- `integration/screenshots/*.png` — live UI evidence
- `proof-packet.html` / `proof-packet.json` — rendered report

## How to work on Otto

**Otto invariants enforced by code (don't fight these):**
- Prompts live in `otto/prompts/*.md`. Edit those, not Python strings.
- Path construction goes through `otto/paths.py`. No hardcoded
  `"otto_logs/..."` literals elsewhere.
- `otto_logs/` paths must NEVER leak into agent prompts or git commits.
- `system_prompt` uses `{"type": "preset", "preset": "claude_code"}`,
  never `None` (None blanks Claude Code's built-in defaults).
- In-process MCP breaks with the Agent tool. External MCP subprocess
  required. Agent SDK doesn't stream ToolResultBlocks for MCP tools;
  use file side-channels.

**Worktree safety (critical):**
- Never mix worktree and main-repo git ops in the same session.
- Before every git write: `pwd && git branch --show-current`.
- Stay in the worktree the user gave you; don't `cd` to a different
  repo to run git writes without explicit permission.

**Testing posture:**
- Unit tests for function-level correctness (always).
- Integration tests when crossing component boundaries.
- Live runs are the only thing that catches multi-agent failure modes.
  Budget 6-10 live runs per architecture validation.
- Prompt changes need ≥4 diverse projects before claiming they hold
  (single-project tuning overfits).
- Before flagging a test failure as your fault: `git stash && pytest
  ... && git stash pop` to check if it's pre-existing.

**Codex collaboration:**
- Codex codes correctness-critical work (concurrency, locking, race
  conditions, systematic refactors).
- Claude codes architecture, UI, codebase navigation, integration.
- When Codex finds a bug during review, Codex writes the fix (new
  `mcp__codex__codex` call with `sandbox: "workspace-write"`). The
  same blind spot that missed the bug shapes the fix — don't let
  Claude fix Codex-found bugs.
- Skip Codex-gate for small/mechanical/test-only/doc fixes.
- `approval-policy: "never"` for all MCP calls. Any other value hangs
  forever.
- Don't ask Codex to create branches/PRs — `git checkout -b` can hang
  in its sandbox.

## Reference

**Quick diagnosis:**
```bash
otto run "<intent>"                                   # canonical pipeline
otto proof list                                       # run history
otto proof open                                       # open latest proof
otto recover status                                   # current v5 pipeline state
otto recover plan-resume                              # preview resume
otto recover reset-verdict --task <id> --to unverified
readlink otto_logs/latest                             # most recent session
```

**Common debugging recipes:**

*"Why did the build fail?"* → `otto_logs/latest/build/narrative.log`,
scan for `VERDICT:` markers.

*"Did the integration Lead self-verify journeys?"* → `integration/verdict.json`,
look for `journeys[]` with `passed`, `detail` (≥40 chars), and `evidence`
paths.

*"Why did the verdict get demoted?"* → `integration/verification_plan.json`,
compare `agent_verdict` to `final_verdict`. Failed checks with `kind in
CHECK_KINDS` are legitimate demotes. Anything else is a bug.

*"What's the task graph state?"* →
```python
from otto.queue.task_graph import read_graph
from pathlib import Path
print(read_graph(Path(".")))
```

**Launch a background otto run:**
```bash
nohup bash -lc "cd $PROD && .venv/bin/otto run --model claude-sonnet-4-6 \
  \"\$(cat /tmp/intent.txt)\"" > $LOG 2>&1 &
```
With `Bash(run_in_background=true)` so the task system streams output.
Log path OUTSIDE the product worktree.

**Test commands:**
- `.venv/bin/python3 -m pytest tests/<file> -x --no-header -q` — fast iteration
- `uv run python scripts/test_tiers.py smoke` — minimal gate
- `uv run python scripts/test_tiers.py fast` — day-to-day non-browser gate
- `uv run python scripts/test_tiers.py web` — TS + Mission Control
- `uv run ruff check otto scripts tests` — lint
- `npm run web:typecheck` — TS check

## Pointers

- **Global CLAUDE.md** (`~/.claude/CLAUDE.md`) — user's cross-project
  conventions: permissions, dotfile sync, codex-collab protocols, etc.
- **`AGENTS.md`** — Codex-specific addenda (sandbox modes, build/test
  races, Mission Control posture).
- **`codex-learnings.md`** at `/Users/yuxuan/work/cc-autonomous/` —
  persistent Codex memory for Otto-specific gotchas.
- **Prompts** — `otto/prompts/lead.md`, `lead-architect.md`,
  `lead-integration.md`, `autopilot-pilot.md`. Stack-agnostic. Hard
  Rules at the top of each.
- **`docs/`** — historical design notes and active project plans.
