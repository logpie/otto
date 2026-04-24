# Round 1 - Production Dead Code, Dedup, Slop

Target: `otto/web`, `otto/mission_control`, `otto/merge`.

## Candidates

1. NOTE - deferred - cleanup action copy still needs stronger product wording.
   - Evidence: successful/merged queue rows expose cleanup/requeue actions in
     detail, but the UI does not fully explain whether cleanup affects queue
     bookkeeping, worktrees, or both.
   - Decision: defer as product-copy work; not a correctness blocker.

2. NOTE - deferred - long-running provider work needs budget/time affordances.
   - Evidence: current overview shows active count and elapsed rows but not a
     budget forecast, run cap, or provider-specific spend warning.
   - Decision: defer to product roadmap; not needed for the TS/production-ready
     hardening pass.

## Invalid / No Action

- `# type: ignore[arg-type]` in `filters_from_params` is a narrow Literal typing
  bridge after runtime validation; no behavior risk found.
- `or []` and `or {}` patterns in serializers and React render paths are normal
  defensive handling for partially populated run records.
- Duplicate merge invocation strings were intentionally kept visible in both
  action execution and preview code, with tests covering that they match.
