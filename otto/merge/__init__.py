"""Otto merge subsystem (Phase 4 of plan-parallel.md).

Python-driven merge orchestration. The Python loop owns `git merge`; the
agent is invoked **only** for actual conflicts (one agent call per
conflict, scoped to the conflict files). A separate triage agent runs
once at the end to produce a verification plan, then the certifier runs
on the must-verify subset.

`--fast` does no LLM work at all (pure git, bail on first conflict).

See plan-parallel.md §5 Phase 4 (steps 4.1-4.6).
"""
