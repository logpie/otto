# Codex Provider Audit and Fix Notes

This note records the Codex provider fixes, certifier prompt hardening, and E2E
observations from the `fix/codex-provider-i2p` worktree.

## Current Integration Shape

Otto still invokes Codex through the Codex CLI:

```text
codex exec --json ...
```

It does not use an OpenAI Agents SDK integration. The adapter reads Codex JSONL
events from `codex exec --json` and normalizes them into Otto's internal
`AssistantMessage`, `ToolUseBlock`, `ToolResultBlock`, and `ResultMessage`
stream.

## Root Causes

### Codex Subagents Were Not Normalized

Codex can launch subagents in this environment, but its raw stream uses:

- `item.type = "collab_tool_call"`
- `item.tool = "spawn_agent"` for dispatch
- `item.tool = "wait"` for result collection
- `receiver_thread_ids` for child thread IDs
- `agents_states[child_id].message` for child final text

Otto previously handled Codex `agent_message` and `command_execution` only. That
made Codex look like a shell/text provider: subagent dispatches were not surfaced
as Otto `Agent` tool calls, wait results were not surfaced as tool results, and
child sessions were not captured.

### Codex Effort Overrides Were Ignored

Otto config exposes provider/model/effort. Codex should inherit the user's local
Codex config when effort is unset, but explicit Otto effort (`--effort` or
`otto.yaml`) should be passed to Codex. `_codex_command` previously ignored
`AgentOptions.effort`.

### Certifier Prompt Had Parallel Browser Weak Spots

Claude's certifier timeout was not a provider crash. It was a 599s timeout caused
by browser-command churn and shared/default `agent-browser` session contention.
After adding named sessions for parallel subagents, Claude completed the same
fixture, but still showed command discipline issues:

- It generated invalid `agent-browser --ref e1` and `--text` commands.
- It retried many failed browser actions.
- It used state-changing JavaScript (`element.value = ...`, `dispatchEvent`,
  `.click()`) while reporting `methodology=live-ui-events`.

The prompt already forbade JavaScript state injection, but it did not explicitly
warn against the invalid ref flags and did not restate that visual screenshots
must be produced from real UI state or already verified sessions.

### Codex Cost Was Misreported

Codex CLI emits token usage (`input_tokens`, `cached_input_tokens`,
`output_tokens`) but not actual USD cost. Otto treated missing USD cost as
`$0.00`, which implied a free run. Reports now show "Cost: not reported by
provider" plus token usage when USD cost is unavailable.

## Fixes

### Adapter

- Normalize Codex `spawn_agent` collab calls into `ToolUseBlock(name="Agent")`.
- Normalize Codex `wait` collab completions into `ToolResultBlock` values.
- Track Codex child thread IDs from `receiver_thread_ids`.
- Add a Codex prompt compatibility prelude mapping Otto's "Agent tool" wording
  to Codex `spawn_agent` and wait behavior.
- Pass explicit Codex reasoning effort with
  `-c model_reasoning_effort="<effort>"`.
- Preserve user Codex config inheritance when Otto effort is unset.
- Map Otto `effort=max` to Codex `xhigh`.

### Certifier Parser

- Preserve fenced command blocks inside `STORY_EVIDENCE_START/END`.
- Continue ignoring evidence-looking markers inside unrelated frontmatter or
  fenced blocks.

### Prompt Hardening

- Require unique `agent-browser --session <story-id>` sessions for parallel web
  subagents.
- Keep `main`/shared sessions as the serial default where appropriate.
- Require the parent visual walkthrough to use its own named session such as
  `visual`.
- Teach valid `agent-browser` ref syntax: use `@e1`, not `--ref e1`, `--text`,
  or bare `e1`.
- Explicitly forbid state-changing JavaScript including `element.value = ...`,
  `dispatchEvent(...)`, `requestSubmit()`, `.click()`, storage mutation, focus
  mutation, app function calls, and Playwright `page.evaluate` bypasses.
- State that JS-created screenshot state is `javascript-eval`, not
  `live-ui-events`, and cannot certify a user-facing UI flow by itself.

### Token Reporting

- Preserve `cached_input_tokens` in session phase usage.
- Include token usage in PoW JSON and Markdown/HTML.
- Show `Cost: not reported by provider` when USD cost is missing but token usage
  exists.
- Update standalone certify terminal output to show token usage for Codex.
- Update run summary totals to show compact token counts instead of `$0.00`
  when cost is not reported.

## E2E Results

### Standalone Certify, Codex vs Claude

Fixture: static browser todo app in temp git repos.

Fresh run after prompt hardening:

| Provider | Result | Duration | Browser Calls | Tool Errors | Stories |
| --- | ---: | ---: | ---: | ---: | ---: |
| Codex | PASS | 3m52s | 23 | 0 | 5/5 |
| Claude | PASS | 9m23s | 104 | 19 | 4/4 |

Codex followed the browser-event rules more closely. Claude completed after the
session-topology fix but remained slower because of browser command churn and
invalid command syntax retries.

### Build Only, Codex vs Claude

Same static todo intent with `--no-qa --budget 600`:

- Codex: success in 4m07s, generated a multi-file app with a working `npm test`.
- Claude: success in 1m24s, generated a working app and custom `node test.js`,
  but left `npm test` as the npm-init placeholder, so `npm test` failed.

### Full Codex Build + QA Loop

Command shape:

```text
otto build <todo intent> --standard --provider codex --budget 1200
```

Result:

- Project: `/tmp/otto-codex-full-e2e`
- SUCCESS in 11m18s.
- Build commit: `eb1c360 Build static todo app`.
- Certification reached round 2 and passed 8/8 stories.
- Covered source/baseline tests, first load, lifecycle, filters, persistence,
  direct file opening, input edge cases, and visual walkthrough evidence.

This validates Codex through the full Otto build + certification loop, not only
standalone `certify`.

### Mission Control Complex Repo, Codex Retry, and Merge

Project: `/private/tmp/otto-complex-web-20260424-093052`

- Queued a real Codex task from Mission Control against a fresh Otto repo clone.
- The first run failed in 11 seconds because Codex emitted a JSONL stdout line
  larger than asyncio's default subprocess stream limit.
- Otto now creates the Codex subprocess with a 16 MiB stream reader limit.
- Requeued the task from the web UI; Codex completed build and certification.
- Certifier passed 5/5 stories, including web shell load, filtered detail
  inspectability, and requeue action execution.
- Mission Control showed usage as `1.3M in / 5.2K out` for the successful retry.
- Web merge then exposed and fixed a branch-target bug: remote default branch
  names containing slashes were truncated. `origin/fix/codex-provider-i2p` now
  resolves to `fix/codex-provider-i2p`.
- Retried web merge succeeded into `fix/codex-provider-i2p`, and the restarted
  UI displayed the same target.

### Patched Codex Token Reporting

Fast standalone Codex certify after the token-reporting patch:

```text
RUN SUMMARY: certify=1:22 (1 round), total=280.1K in/3.4K out 1:22
Cost: not reported by provider; Tokens: 280,052 input (254,848 cached), 3,359 output
```

The PoW JSON includes:

```json
"token_usage": {
  "cached_input_tokens": 254848,
  "input_tokens": 280052,
  "output_tokens": 3359
}
```

## Verification

Latest full test run:

```text
npm run web:typecheck
npm run web:build
uv run pytest -q --maxfail=10
924 passed, 18 deselected in 105.58s
```
