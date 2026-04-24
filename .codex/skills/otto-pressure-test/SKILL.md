---
name: otto-pressure-test
description: "Run deep Otto validation beyond quick smoke tests: multi-round bug hunting, deterministic E2E, Mission Control TUI audits, queue/merge CLI scenarios, and real Claude/Codex provider comparisons. Use when the user asks for pressure testing, thorough bug hunting, release-level validation, or 'treat yourself as a user'; not for a quick unit-test-only check."
---

# Otto Pressure Test

## Purpose

Pressure-test Otto as a product, not just as a Python package. The goal is to
find and fix important bugs, then leave an evidence trail that explains what
was tested, what broke, what was fixed, and what remains risky.

This skill is wired to the current Otto repo shape. Use the available harnesses
under `scripts/`, `tests/`, and `scripts/fixtures_nightly/`; do not assume old
benchmark trees or Claude-only automation commands exist.

## When To Use

Use this for:

- release-level validation
- large TUI, provider, queue, merge, or orchestration changes
- "do thorough bug hunting and fix loops"
- "run real E2E real-world tests"
- "audit both providers"
- "treat yourself as a user"

Do not use this for a narrow code change that only needs focused unit tests.
For user-surface smoke only, use `$otto-as-user`.

## Operating Rules

- Work in the requested worktree. Confirm `pwd`, branch, and dirty state before
  editing or launching long runs.
- Fix critical and important bugs inline. Do not just report them.
- Do not revert user changes. If the worktree is dirty, separate your changes.
- Use `uv run --extra dev python ...` for repo commands unless the environment
  requires `.venv/bin/python`.
- For non-trivial failures, use an observe -> hypothesize -> experiment ->
  conclude loop before patching.
- Prefer small, high-signal real-provider batches before expensive full runs.
- When comparing providers, run the same scenario set with `--provider codex`
  and `--provider claude`, then compare result, duration, artifacts, token/cost
  reporting, and certification integrity.

## Round Structure

### Round 0: Orient

```bash
pwd
git branch --show-current
git status --short
rg --files | sed -n '1,120p'
```

Read the changed areas and the relevant test harnesses before launching broad
runs. Identify the likely blast radius: provider adapter, Mission Control,
queue, merge, certifier, reporting, or harness.

### Round 1: Static and Unit Bug Hunt

Run fast deterministic checks:

```bash
uv run --extra dev python -m compileall -q otto scripts
uv run --extra dev python -m pytest
```

For focused changes, add targeted tests first and run the narrow subset while
iterating, then run the full suite after fixes.

Bug classes to look for:

- cancellation and orphan subprocesses
- stale state, bad history snapshots, or missing command acks
- path/branch/worktree metadata lying to the user
- provider-specific usage, cost, or subagent normalization bugs
- TUI clipped content, stale selection, blocking actions, or broken keybinds
- tests that passed only because fixtures were stale

### Round 2: Deterministic E2E

Run current deterministic harnesses:

```bash
uv run --extra dev python scripts/e2e_dashboard_behavioral.py
uv run --extra dev python scripts/e2e_dashboard_pexpect.py
uv run --extra dev python scripts/e2e_runner.py A B
```

Use these to catch UI compatibility, PTY rendering, queue, merge, and CLI
regressions before spending provider tokens.

### Round 3: Mission Control As User

Use the as-user harness for real TUI operations:

```bash
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario U1,U3,U4,U5,U6,U7,U8,U9 --scenario-delay 0 --keep-failed-only --bail-fast
```

Add `--provider claude` or rerun a smaller matching set when provider parity is
part of the ask.

Inspect `recording.cast`, `debug.log`, `verify.json`, live run records, command
acks, and `history.jsonl` for any failure or suspicious pass.

### Round 4: Real Provider Parity

Start with a small provider matrix:

```bash
uv run --extra dev python scripts/otto_as_user.py --provider codex --scenario A1,A4,U2,B2 --scenario-delay 0 --keep-failed-only --bail-fast
uv run --extra dev python scripts/otto_as_user.py --provider claude --scenario A1,A4,U2,B2 --scenario-delay 0 --keep-failed-only --bail-fast
```

Compare:

- pass/fail and failure type
- duration and retry behavior
- token usage and USD cost reporting
- subagent launch/result handling
- certifier methodology honesty
- artifact completeness
- Mission Control live/history/detail behavior

For Codex, remember that the CLI may report token usage without USD cost. Do
not treat `cost_usd: 0.0` as free execution when token usage exists.

### Round 5: Nightly/Seeded Coverage

Use when the user asks for real-world, hidden-oracle, or long coverage:

```bash
uv run --extra dev python scripts/otto_as_user_nightly.py --dry-run
uv run --extra dev python scripts/otto_as_user_nightly.py --scenario N9
uv run --extra dev python scripts/otto_as_user_nightly.py --scenario N1,N2,N4,N8,N9 --scenario-delay 10
```

`N9` is the Mission Control operator workflow. `N1,N2,N4,N8` cover evolving
features, semantic auth merges, certifier traps, and stale merge context.

Use `OTTO_DEBUG_FAST=1` only for harness debugging. Do not count debug-fast
runs as full-fidelity validation unless the user explicitly accepts that.

## Fix Loop

For each important bug:

1. Record symptom and reproducer.
2. Identify root cause from code/logs.
3. Patch the smallest responsible code path.
4. Add or update a regression test.
5. Rerun the narrow failing scenario.
6. Rerun the broader round that exposed it.
7. Document the result and residual risk.

Commit in reviewable chunks when the user asked for mergeable work.

## Report

Write a report under `docs/`, for example:

```text
docs/otto-pressure-test-YYYY-MM-DD.md
docs/otto-bug-hunt-e2e-YYYY-MM-DD.md
```

Include:

- worktree and branch
- round-by-round command log and results
- bugs found, severity, root cause, fix, and verification
- provider comparison table
- Mission Control user-flow findings
- artifact paths/run ids for expensive runs
- remaining risks and tests not run

Do not bury critical findings behind a summary. Lead with the bugs fixed and
the evidence that the fix held.

## Common Pitfalls

- Running from `main` when the user asked for an isolated worktree.
- Running old Claude-only commands from stale docs.
- Treating a PTY render pass as proof that real provider flows work.
- Treating provider auth/rate-limit as a product failure.
- Treating Codex `cost_usd: 0.0` as actual zero cost when token usage exists.
- Calling Mission Control good without checking live rows, history rows,
  detail/log panes, commands/acks, and cleanup behavior.
- Trusting certifier claims without checking methodology and artifacts.
- Stopping after the first failure in an expensive batch when collecting all
  failures would make the next fix loop faster.
