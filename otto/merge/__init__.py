"""Otto merge subsystem (Phase 4 of plan-parallel.md).

Python-driven merge orchestration. The Python loop owns `git merge`; the
agent is invoked only for actual conflicts, and the current resolver
uses one consolidated agent session over the union of unresolved files.
After merging, one certifier call verifies the merged story union; the
merge-context preamble lets the certifier skip unaffected stories or flag
genuine cross-branch contradictions inline.

`--fast` does no LLM work at all (pure git, bail on first conflict).

See plan-parallel.md §5 Phase 4 (steps 4.1-4.6).
"""
