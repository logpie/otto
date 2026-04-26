# Production Dead/Dedup/Slop Hunter Notes

Findings considered:

- `App.tsx` remains the largest frontend file. A full component split is risky in one pass.
- Log-buffer helper logic is pure and low-risk to extract because browser log-buffer tests cover the behavior.
- `otto/mission_control/service.py` has obvious future extraction boundaries: product handoff, proof reports, review packets, and queue failure excerpts.
- `Runner.run_async()` still mentions the old Textual dashboard path. This was not removed in this pass because external callers are unknown; it should be evaluated separately.

Implemented:

- Extracted log buffer constants/types/helpers from `App.tsx` into `otto/web/client/src/logBuffer.ts`.
- Rebuilt the committed web bundle and build stamp.

Deferred:

- Large component splits such as `JobDialog`, `TaskBoard`, `History`, and inspector panes.
- Service helper-module extraction.
- Queue runner maintenance deduplication.
