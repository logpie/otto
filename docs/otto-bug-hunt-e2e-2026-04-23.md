# Otto Bug Hunt and E2E Audit - 2026-04-23

Worktree: `.worktrees/codex-provider-i2p`  
Branch: `fix/codex-provider-i2p`  
Scope: post-main-merge audit of Mission Control, queue/merge CLI surfaces, and
Claude/Codex provider behavior.

## Goal

Run multiple bug-hunting rounds, fix critical and important bugs, then operate
Otto like a user through the TUI and CLI flows. Both Claude and Codex providers
must remain usable for build/certify/cancel/queue scenarios.

## Bug Hunt Rounds

### Round 1: Static + Unit-Level Audit

Commands:

```bash
uv run --extra dev python -m compileall -q otto scripts
uv run --extra dev python -m pytest
```

Result:

```text
896 passed in 142.13s
```

Fixed findings:

| Severity | Finding | Fix |
| --- | --- | --- |
| Critical | Codex cancellation could leave the provider subprocess alive and hang the parent Otto command. | `otto/agent.py` now terminates the provider process group and retries with SIGKILL if cleanup wait is cancelled or times out. |
| Important | `merge --cleanup-on-success` leaked merged worktrees when queue session artifacts were missing. | Cleanup now still removes the merged worktree when artifact lookup raises missing-session errors, while preserving collision protection. |
| Important | Mission Control detail pane made important paths hard to inspect because long paths clipped horizontally. | Detail metadata now promotes branch, worktree, selected log, and artifacts near the top and folds long paths at slash boundaries. |
| Important | Mission Control had no copy/yank action for exact run/log metadata. | Added `y` action using `pbcopy`, `wl-copy`, or `xclip`. Detail view copies selected log contents when readable, otherwise row metadata. |
| Important | Empty Mission Control state did not tell users what command to run next. | Empty state now lists `otto build`, `otto improve bugs`, `otto certify`, and `otto queue build <task-id>`. |
| Important | Dashboard and as-user E2E scripts were stale after Mission Control replaced the old QueueApp UI. | Updated behavioral, real-PTY, and as-user harnesses to the current Mission Control UI. |
| Important | Mouse-capture audit treated focus tracking (`?1004h/l`) as mouse capture. | `scripts/cast_utils.py` now recognizes actual mouse modes only. |
| Important | B13 branch fixture was date-sensitive and would fail after 2026-04-20. | E2E runner now discovers the current `build/only-*` branch dynamically. |

### Round 2: Mission Control Behavioral + PTY Audit

Commands:

```bash
uv run --extra dev python scripts/e2e_dashboard_behavioral.py
uv run --extra dev python scripts/e2e_dashboard_pexpect.py
```

Result:

```text
behavioral: pass
pexpect: S1-S10 pass
```

Fixed findings:

| Severity | Finding | Fix |
| --- | --- | --- |
| Important | Real-PTY audit hard-coded old queue text and selected task assumptions. | Tests now derive the selected task and validate current `queue: <task>` detail titles. |
| Important | Malformed queue state scenario expected an obsolete parse-error banner. | Test now verifies graceful render/recovery in the current model. |
| Important | Default dashboard mode was incorrectly treated as mouse-enabled in tests. | U1 verifies default no-mouse behavior; U9 explicitly opts into `--dashboard-mouse`. |

### Round 3: Synthetic CLI + Queue Coverage

Command:

```bash
uv run --extra dev python scripts/e2e_runner.py A B
```

Result:

```text
24/24 passed
```

Coverage:

- Core build/certify fixture paths.
- Queue commands and branch handling.
- Cleanup and merge-adjacent compatibility scenarios.
- Current Mission Control compatibility through the updated dashboard scripts.

## Codex Cancellation Debug Loop

### Observe

Real Codex `U2` initially failed. The cancel command was acknowledged and the
run printed the expected pause/resume guidance, but the parent process did not
exit within the 30s harness timeout. The traceback showed cancellation while
`_query_codex` was awaiting `process.wait()`, followed by an un-reaped
subprocess when the event loop closed.

### Hypothesize

The cleanup path was running inside the same cancelled task. Once cancellation
interrupted `await process.wait()`, Otto could skip complete subprocess-group
cleanup and leave Codex or its children alive.

### Experiment

Changed the provider cleanup path to:

- signal the provider process group with SIGTERM;
- wait with a short grace period;
- send SIGKILL on timeout;
- send SIGKILL again if the cleanup wait itself is cancelled;
- fall back to direct process terminate/kill when no pgid is available.

Added regression coverage with a process whose `wait()` is cancelled during
cleanup.

### Conclude

The fix is validated by unit coverage and a real Codex `U2` rerun. Codex now
exits cleanly after Mission Control cancel/resume-path exercise.

## Real Provider E2E

### Codex

Commands:

```bash
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario A1,A4,U2 --scenario-delay 0 --keep-failed-only --bail-fast
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario U2 --scenario-delay 0 --keep-failed-only --bail-fast
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario B2 --scenario-delay 0 --keep-failed-only --bail-fast
```

Results:

| Scenario | Result | Notes |
| --- | --- | --- |
| A1 | PASS | Build/certify path completed. |
| A4 | PASS | Provider path completed. |
| U2 | FAIL before fix, PASS after fix | Exposed and validated the Codex cancellation cleanup bug. |
| B2 | PASS | Queue/cancel path completed with Codex provider. |

Run ids:

- Initial Codex A1/A4/U2: `2026-04-23-232346-c584d7`
- Fixed Codex U2: `2026-04-23-233051-999614`
- Codex B2: `2026-04-23-233337-e4b027`

Provider accounting observation:

- Codex reports token usage but does not emit USD cost through the CLI stream.
- The observed A1 summary had `cost_usd: 0.0` plus token usage:
  `645,164 input`, `598,912 cached input`, `5,303 output`.
- Treat `$0.00` in machine summaries as "USD not reported by provider" when
  token usage exists. Human-facing certify/run-summary paths already show token
  counts instead of implying free execution.

### Claude

Commands:

```bash
uv run --extra dev python scripts/otto_as_user.py --provider claude --scenario A1,A4,U2 --scenario-delay 0 --keep-failed-only --bail-fast
uv run --extra dev python scripts/otto_as_user.py --provider claude --scenario B2 --scenario-delay 0 --keep-failed-only --bail-fast
```

Results:

| Scenario | Result | Notes |
| --- | --- | --- |
| A1 | PASS | Build/certify path completed. |
| A4 | PASS | Provider path completed. |
| U2 | PASS | Mission Control cancel flow completed. |
| B2 | PASS | Queue/cancel path completed. |

Run ids:

- Claude A1/A4/U2: `2026-04-23-233109-575b60`
- Claude B2: `2026-04-23-233532-cb0033`

Provider accounting observation:

- Claude A1 reported USD cost: `0.2547513`.
- Claude summary included provider cost and token breakdown.

## Mission Control As-User Audit

Command:

```bash
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario U1,U3,U4,U5,U6,U7,U8,U9 --scenario-delay 0 --keep-failed-only --bail-fast
```

Result:

```text
8/8 passed
run id: 2026-04-23-231804-d9904d
```

Scenarios covered:

- U1: default Mission Control open/quit without mouse capture.
- U3: keyboard navigation and help surface.
- U4/U5: detail pane, queue detail titles, folded path fragments, log/artifact
  inspection.
- U6/U7/U8: degraded/empty/recovery states.
- U9: explicit mouse mode enables and disables actual mouse capture sequences.

## Provider Quality Notes

Codex:

- Passed the real provider scenarios after the cancellation fix.
- Certifier behavior remains stricter about real UI interactions based on the
  previous prompt audit; no new certification-integrity regression was observed
  in this round.
- Slower than Claude in the observed A1 run: Codex A1 took about `190.9s` vs
  Claude A1 about `74.7s`.
- Main remaining gap is USD accounting from Codex CLI output. Token usage is
  preserved and should be the source of truth until a pricing map is added.

Claude:

- Passed the same real provider scenarios.
- Faster in the observed A1 run and reported USD cost.
- Previous certifier-integrity issue was prompt-level discipline: some Claude
  walkthrough actions used JavaScript shortcuts while reporting live UI events.
  No new prompt changes were made in this round, so that earlier hardening
  remains the relevant mitigation.

## Remaining Risks

- Codex machine-readable `summary.json` still stores `cost_usd: 0.0` when USD
  is not reported. Consumers should inspect breakdown token usage and avoid
  interpreting the zero as free execution.
- I did not run the full long queue-drain scenario with two real builds for both
  providers in this round. B2 covers queue cancel with real providers; synthetic
  A/B coverage covers queue/merge compatibility more broadly.
- I did not run a full no-shortcut visual certification audit after every older
  certifier prompt hardening change. This round focused on post-merge TUI,
  provider cancellation, queue, and CLI regressions.
- Mission Control has strong PTY and in-process coverage, but a manual long
  interactive terminal soak is still useful before merging if visual polish is a
  merge gate.

## Files Changed

- `otto/agent.py`
- `otto/merge/orchestrator.py`
- `otto/tui/mission_control.py`
- `scripts/cast_utils.py`
- `scripts/e2e_dashboard_behavioral.py`
- `scripts/e2e_dashboard_pexpect.py`
- `scripts/e2e_runner.py`
- `scripts/otto_as_user.py`
- `tests/test_agent.py`
- `tests/test_merge_orchestrator.py`
- `tests/test_mission_control_tui.py`
- `tests/test_otto_as_user.py`

## Verification Summary

```text
compileall otto/scripts: pass
pytest: 896 passed
Mission Control behavioral E2E: pass
Mission Control real-PTY E2E: S1-S10 pass
synthetic A/B E2E runner: 24/24 pass
Codex real provider: A1/A4/B2 pass, U2 pass after cleanup fix
Claude real provider: A1/A4/U2/B2 pass
Codex Mission Control as-user: U1/U3/U4/U5/U6/U7/U8/U9 pass
```
