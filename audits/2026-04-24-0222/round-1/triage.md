# Round 1 - Triage

| Candidate | Severity | Status | Notes |
| --- | --- | --- | --- |
| Merge id collision in same Python process | IMPORTANT | fixed | `new_merge_id()` now includes random suffix; uniqueness test added. |
| `merge --all` re-includes already merged done tasks | IMPORTANT | fixed | Resolver skips prior merged branches; E2E confirmed web merge processed one branch. |
| Generic no-branches message after all done tasks already merged | IMPORTANT | fixed | Resolver now reports "no unmerged done branches to merge". |
| `--fast` clean merge still certifies | IMPORTANT | fixed | Fast mode now skips certification; Mission Control passes `--no-certify`. |
| Mission Control action children inherit web server process group | IMPORTANT | fixed | `start_new_session=True`; action tests assert it. |
| Native `window.confirm` blocks inspectable web automation | IMPORTANT | fixed | Replaced with in-app confirmation modal. |
| Overview active count inflated by done compatibility rows | IMPORTANT | fixed | Active count now derives from watcher task counts. |
| Cleanup action copy ambiguity | NOTE | deferred | Product wording follow-up, not a correctness blocker. |
| Long-running budget/time affordances | NOTE | deferred | Product roadmap item. |
| Best-effort `pass` blocks in cleanup/parsing | NOTE | invalid | Expected cleanup semantics. |
| Protocol `...` methods | NOTE | invalid | Type declarations. |
| Literal `type: ignore[arg-type]` filters | NOTE | invalid | Runtime validation precedes Literal bridge. |

Candidate counts:

- fixed: 7
- deferred: 2
- duplicate: 0
- invalid: 3
- needs-more-evidence: 0
