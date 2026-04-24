# Production Dead/Dedup/Slop Hunter

## Fixed

1. Confirmation bodies used implementation phrasing
   - Severity: minor UX
   - Location: `otto/web/client/src/App.tsx`
   - Root cause: detail actions passed generic text such as `Run remove?` after the label plumbing was fixed.
   - Fix: added action-specific confirmation copy such as `Remove this queue task?` and `Requeue this task?`.

## Reviewed

- The TS client action-label plumbing now has one helper for action names and one helper for confirmation bodies. The duplication is small and clearer than overloading one function with both API action names and product copy.
- The new queue retry id helper is called from one place, but it isolates queue-id policy and is directly covered by regression tests.
- Generated static assets are committed intentionally so `otto web` works without requiring Node at runtime.

## Deferred

- Some Mission Control modules still use `Any` for serialized run/queue JSON shapes. This matches the current untyped persisted data boundary; a future schema pass should tighten it separately from this web recovery work.
