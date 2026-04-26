# Round 1 Triage

| Candidate | Status | Notes |
| --- | --- | --- |
| Add explicit test tiers | fixed | Added `scripts/test_tiers.py`, pytest markers, package scripts, README/AGENTS docs. |
| Make day-to-day verification faster | fixed | `smoke` is ~13s; `fast` is ~62s; default full remains available. |
| Browser README stale command | fixed | Added required `-m browser -p playwright`. |
| Duplicate `_string_list` breaks product handoff scalar fields | fixed | Split permissive `_coerce_string_list` from strict request `_string_list`; added regression. |
| Extract safe frontend helper from `App.tsx` | fixed | Moved log-buffer helpers to `logBuffer.ts`; targeted browser log tests pass. |
| Split `App.tsx` into major components | deferred | Valuable but too risky for this pass. Recommended order: launcher/history/log pane, then task board and dialogs. |
| Split `mission_control/service.py` helper clusters | deferred | Pure helper modules are the next backend cleanup, but not required for the test-speed fix. |
| Remove `Runner.run_async()` stale TUI compatibility | deferred | Needs external API decision. |
