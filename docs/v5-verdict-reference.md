# Otto v5 Verdict Reference

A v5 task ends with one of six terminal verdicts. The verdict is computed
deterministically from the verifier's structured output (per plan-v5 §13);
the Lead's text claim is NEVER trusted unless verify ran and returned a
matching result.

## Verdicts

| Verdict | Meaning | What you should do |
|---|---|---|
| `pass` | All in-scope behavior journeys passed verification. The task's diff is in the integration branch. | Trust it. The product works for the journeys verified. |
| `partial` | Built and integrated, but some declared journeys did not pass within retry budget. Honest list in the proof packet. | Read the proof packet; decide which journeys to follow up on. The code is in the branch. |
| `pending_children` | A parent task whose children haven't all resolved yet. Not terminal. | Wait. Otto's runner will spawn the integration Lead when children complete. |
| `unverified` | Code committed, but the verifier failed/timed-out, or the Lead never called `mcp__otto__verify`. | Manually inspect or re-run with longer budget. The committed code MAY be correct but Otto cannot certify. |
| `merge_blocked` | The task built fine but its branch couldn't be merged into the parent's integration branch (conflict, retries exhausted). Worktree preserved. | Resolve the conflict manually or with `otto run --resume`. Sibling tasks were unaffected. |
| `catastrophic` | Infrastructure failure (provider auth/credits, disk full, etc.). Not a code problem. | Fix the infra issue; resume. Provider auth: check API keys. Credits: configure `fallback_provider` for next time. |

## Severity ordering

When a parent's verdict aggregates from its children:

```
pass < pending_children < partial < unverified < merge_blocked < catastrophic
```

The parent's verdict is **at least as severe as** its worst child's. A
parent never claims `pass` if any child is `partial` or worse.

This is a philosophy invariant (plan-v5 §14) — verdicts never lie.

## Project-level state

Otto v5 does NOT have a project-level state machine (`green` / `degraded` /
`quarantined`). Plan-v5 explicitly drops these because they are de-facto
hard blocks. Instead, a project's MC dashboard shows:

- Recent task verdicts (last 20).
- Open `merge_blocked` count.
- Open `regression_unfixable` count (post-merge audit failures, if any).
- In-flight tasks count.
- Total spend last 24h.

You read the dashboard; you decide. Otto never pauses dispatching new
tasks because of past verdicts (per plan-v5 §13: "advance always").

## How verdicts are computed

For an inline task:
1. Build agent + test agent run via Task dispatch.
2. Lead calls `mcp__otto__verify`.
3. Verifier writes `<session_dir>/verify/verify-result.json`.
4. Lead returns; render layer reads `verify-result.json`.
5. If `verify-result.json` exists with `verdict=pass` and matches the Lead's
   claim → `pass`. Else → downgrade.

For a parent with emitted children:
1. Children complete; each writes its own verdict.
2. Otto's runner spawns the integration Lead at the parent.
3. Integration Lead runs `mcp__otto__verify` against the integrated
   worktree at parent's semantic level.
4. Integration verdict + `aggregate_verdict()` of children = parent's
   final verdict (worst-of-all).

## What a proof packet shows

Every task has a `summary.json` and (when rendered) a proof packet:
- The verdict.
- The original intent.
- The decomposition decision (`inline` or `emit`).
- Per-journey pass/fail from the verifier.
- Cost (`cost_usd` plus `cost_attempts[]` if provider fallback ran).
- Duration.
- Evidence paths (test output logs, screenshots, etc.).
- Failure reason if applicable.

The proof packet is your source of truth, not the Lead's text output.
