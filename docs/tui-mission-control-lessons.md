# TUI Mission Control — Session Lessons

What worked, what was slow, and what to avoid next time.

## What worked well

### Plan-Gate before code
- Sent design doc to Codex 3x before any implementation. Each round caught real issues (queue attempt identity, history durability, command protocol ack/replay, suspend/clock-jump grace, repair precedence, missing files in migration plan, adapter god-object risk, mixed-version envelope, agent-vs-human framing, gate exits A-E). Implementation rolled in much smoother because of this.

### Codex fixes Codex-found bugs
- Held the line on this rule throughout. Every Implementation-Gate finding got a fresh Codex `workspace-write` call to fix, never a "Claude will just patch this real quick." When Claude noticed itself about to write the fix, stopped and dispatched. Resulted in cleaner fixes — Codex's blind spot for the original bug also shaped the fix, so the same eye that spotted it informed the patch.

### Per-phase gates, not one big-bang gate
- Phase 1 / 2 / 3 each got their own multi-round Implementation Gate before the next phase started. Catching CRITICAL findings in Phase 1 (dual-writer race, ack-before-durable) before Phase 2 piled UI on top of broken substrate prevented a lot of throwaway viewer code.

### Real LLM E2E (N9) over more unit tests
- 883 unit tests + N9 surfaced 12 real bugs that no unit test caught. Each take cost ~$1-3 and 5-15min — cheaper than the engineering hours those bugs would cost in production. Real-LLM workflow tests are uniquely good at finding integration bugs (PATH pollution, missing `__main__` guards, dirty-tree-preflight gaps).

### Batch discovery refactor (mid-session)
- Soft-asserts + auto-mine post-run audit + subprocess stderr capture transformed the debug loop. Last 3 takes each found multiple bugs per run instead of one. Should've done this from take 1.

## Mistakes to avoid

### Don't let a fixture's `name = "otto"` package shadow your venv
- A daily scenario asked Otto to "build a CLI named otto" with `pyproject.toml` `name = "otto"`. The LLM dutifully ran `pip install -e .` as part of building. That clobbered our cc-autonomous Otto in `.venv/bin/otto`. **3 hours of confusing failures** before tracing it.
- **Permanent fix landed:** harness startup guard verifies `.venv/bin/otto --help` matches the expected marker, aborts with reinstall instructions if shadowed; per-scenario isolated venv prepended to PATH; warning when scenario intent looks like a packaging build.
- **Lesson:** any fixture that might `pip install` from inside it needs an isolated venv. Even if not explicitly invoked, an LLM building a Python CLI will frequently install it as part of testing.

### Fail-fast harness × multi-bug runs = slow iteration
- Took 9 N9 takes ($9, ~75min wait) to find 9 bugs because the harness exited at the first failed assertion. Each subsequent bug was masked by the one before it.
- **Permanent fix:** soft-asserts + auto-mine post-run audit + subprocess stderr capture (commit `6f3c0ab6a`).
- **Lesson:** when a real-LLM E2E run costs $1-3 and 5min, never waste it by short-circuiting on the first failure. Always collect everything and report once.

### Don't `python -m foo.cli` without an `__main__` guard
- `otto/cli.py` had `def main()` but no `if __name__ == "__main__": main()`. `python -m otto.cli --help` exited 0 with empty output. Took 2 takes to spot because the harness only saw "subprocess didn't error" and assumed merge ran.
- **Permanent fix:** added the guard + smoke test (`tests/test_cli_smoke.py`).
- **Lesson:** every `python -m pkg.module` entry point needs the standard guard. Add a smoke test.

### Don't `Path(sys.executable).resolve().with_name("otto")` in venv-aware code
- `.resolve()` follows the venv's python symlink to `/opt/homebrew/.../python3.13`, then looks for `otto` next to that, which doesn't exist. The venv's `python` and `otto` are siblings BEFORE symlink resolution.
- **Lesson:** when looking up venv siblings, drop `.resolve()`. The symlink IS the venv-locator.

### Don't let runtime files leak into `git status`
- The Phase 1 substrate added `.otto-queue-commands.acks.jsonl` + `.otto-queue-commands.jsonl.processing` as project-root durable command state but didn't gitignore them. `otto merge` then refused on the dirty tree.
- **Lesson:** every file Otto writes to project-root must be added to `setup_gitignore.py`'s defaults. Audit at the same time as adding the writer.

### Don't have N9 and N10 do half the workflow each
- I initially split "substrate validation" (N9) and "TUI integration" (N10) into separate scenarios. User correctly pushed back: artificial fragments. Consolidating into one realistic operator session per nightly was simpler AND found more bugs (because phases interact).
- **Lesson:** if a nightly's name is "X workflow," it should drive the whole workflow. Don't split for the test framework's convenience — split only when phases truly are independent.

### Use `--fast` for harness debug, not for nightly fidelity
- Took ~15-30min per N9 take using full Otto (certifier loop, multi-round verification). Discovered `--fast` mode gives the same TUI ↔ substrate signal in 3-5min/$0.50.
- **Lesson:** add a `OTTO_DEBUG_FAST=1` opt-in to harness scripts that drives the LLM portion lighter for iterative debug. Real nightly runs (cron) leave the env var unset for full fidelity. The substrate doesn't care which mode the build was run in.

### Don't forget separate code paths
- `otto build` had been hardened against dirty trees; `otto merge` had its OWN check that wasn't updated. Same kind of bug, different code path. Ditto: standalone build had a checkout-main hook on cancel only — natural success path was unguarded.
- **Lesson:** when you find a bug class (dirty-tree refusal too strict, branch-state assumption broken), grep for all the places that do similar checks. Fix them in one pass with a shared helper.

### Don't skip subprocess stderr
- Multiple debug cycles burned because background subprocesses' stderr went to /dev/null. Adding stderr capture revealed problems instantly that took 3-4 takes to triangulate without it.
- **Lesson:** every subprocess.Popen of an LLM-running otto command should have stderr=PIPE or a file. The cost of capture is zero; the cost of NOT capturing is hours.

## Process patterns worth reusing

### Real-LLM nightly + hidden oracle pattern
- Visible tests must PASS on initial fixture (precondition for Otto's job).
- Hidden tests must FAIL on initial fixture (proves the trap is well-designed).
- After Otto exits, harness runs hidden tests as oracle.
- This catches certifier-blind-spots, semantic correctness, integration bugs.

### "Two backstops" defense pattern
- For every "the user shouldn't do X" concern: defense at the source (e.g., setup_gitignore) AND defense at the consumer (e.g., merge preflight tolerates the file). Either alone leaves a gap; both together survive future code that adds new runtime files.

### Codex MCP for unbiased adversarial review
- Plan Gate before non-trivial implementation. Implementation Gate before merge. Both multi-round, with clear "REVISE / APPROVED" exit. Used `mcp__codex__codex` with `model: gpt-5.4` to avoid model-availability issues that hit `gpt-5.5`.

### "Soft assert" verifier pattern for batch debug
```python
class RunFailures:
    def soft_assert(self, cond, msg) -> bool:
        if not cond: self.failures.append(msg)
        return cond
```
Replace every `raise` / hard-assert in long-running test phases. Collect all failures, report once. Worth ~5x faster debug cycles for any expensive E2E.

### Auto-mine artifact dir post-run
After every run regardless of how it ended, scan known invariants on the side-effect filesystem state. Catches multi-bug snapshots from one expensive run. Free signal that no specific test step exercised.

## Cost summary

- ~$25-30 in real LLM spend across 12 N9 takes + U2 + B/D groups + nightly N4 validation
- ~6 hours wall time, ~3 hours of agent-dispatch work
- 46 commits, +29207 LOC in `otto/`
- 12 real Otto bugs fixed (would've been many more without N9 dogfooding)
- 883 unit tests + N9 + U2 + B/D scenarios all green
