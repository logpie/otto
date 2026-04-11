# Plan: otto certify otto

## Goal

Run `otto certify` on otto's own codebase. See if the certifier finds real bugs
in otto itself. This is both a dogfooding exercise and a test of the certifier's
ability to handle a non-trivial Python project (5K LOC, CLI tool, SDK integration).

## Setup

Run in a **separate CC session** to avoid interfering with the dev session.

```bash
# 1. Clone otto into a fresh directory
cd /tmp
git clone https://github.com/logpie/otto.git otto-self-test
cd otto-self-test

# 2. Install otto from the clone's own code
uv venv .venv
uv pip install -e . --python .venv/bin/python

# 3. Run certify
.venv/bin/otto certify "Python CLI tool called otto. Two main commands:

otto build 'intent' — launches one autonomous coding agent that builds a product
from natural language, dispatches a certifier subagent to test it, and fixes issues
found by the certifier. Supports greenfield (empty repo) and incremental (existing
codebase). Writes logs to otto_logs/, proof-of-work reports with screenshots.

otto certify 'intent' — standalone builder-blind product verification. Reads a
project, installs deps, starts the app if needed, tests as a real user with curl/
CLI/agent-browser. Reports PASS/FAIL per story with evidence.

otto history — shows build history from run-history.jsonl.
otto setup — generates CLAUDE.md for a project.

Built with: Python 3.11+, Claude Agent SDK, Click CLI, agent-browser for visual
testing. Config in otto.yaml. Prompts in otto/prompts/*.md."
```

## What to watch for

### The certifier SHOULD test:
1. **CLI commands work**: `otto --help`, `otto build --help`, `otto certify --help`
2. **Config creation**: `otto setup` or first-run creates `otto.yaml`
3. **History on empty project**: `otto history` shows "No build history"
4. **Error handling**: invalid commands, missing arguments, bad project dir
5. **Import chain**: all modules import cleanly

### The certifier probably CAN'T test:
- `otto build` (would spawn an agent inside the certifier — recursive)
- `otto certify` on a real project (same recursion issue)
- SDK integration (needs API key, real LLM calls)

### Interesting questions:
- Does the certifier understand that otto is a CLI tool and test it as one?
- Does it find edge cases we didn't think of?
- Does it find real bugs in our own CLI (arg parsing, error messages, config)?
- How does it handle testing a tool that itself spawns agents?

## Expected outcome

The certifier should be able to test otto's CLI surface (help text, config creation,
history, error handling) without triggering actual LLM calls. If it tries to run
`otto build` inside the certifier, it will either timeout or hit a recursion issue —
that's fine, it should still test everything else.

## Variations to try

### Variation A: Test just the CLI surface
```bash
otto certify "Python CLI tool with commands: build, certify, history, setup.
Test that --help works for all commands, config creation works, history shows
empty state, and invalid inputs are handled gracefully."
```

### Variation B: Test the certifier module
```bash
otto certify "Python library: otto.certifier module. The run_agentic_certifier()
function takes an intent string and project directory, runs a certification agent,
and returns a CertificationReport with outcome, findings, and cost. Test that
imports work, the CertificationReport dataclass has the right fields, and the
prompt loading works."
```

### Variation C: Test the pipeline module
```bash
otto certify "Python library: otto.pipeline module. The build_agentic_v3()
function takes intent, project_dir, config and returns a BuildResult with
passed, build_id, total_cost, journeys. Test imports, BuildResult dataclass
fields, prompt loading from otto/prompts/build.md, and _get_previous_failure()
reads from run-history.jsonl correctly."
```

## How to evaluate results

After running, check:
1. `otto_logs/certifier/proof-of-work.html` — what did it test?
2. `otto_logs/certifier/proof-of-work.json` — per-story pass/fail
3. Did it find any real bugs? Cross-reference with our test suite.
4. Did it false-positive on anything?
5. How long did it take / how much did it cost?

## Notes

- This is an experiment, not a production feature
- Don't run inside the i2p worktree — use a fresh clone
- The certifier may struggle with the SDK dependency (needs API key)
- If it works well, this could become `otto self-test` command
