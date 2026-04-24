# Production Bug Hunter

## Fixed

1. Failed queue requeue id collision
   - Severity: important
   - Location: `otto/mission_control/actions.py`
   - Root cause: failed queue tasks remain in `.otto-queue.yml`, but retry reconstruction reused `--as <original-id>`.
   - Fix: generate a deduplicated retry id before reconstructing the queue command.

2. Stale legacy queue rows stayed actionable as active runs
   - Severity: important
   - Location: `otto/mission_control/adapters/queue.py`, `otto/mission_control/service.py`, `otto/mission_control/model.py`
   - Root cause: legacy queue rows discarded stale overlays and did not preserve child writer identity.
   - Fix: preserve child process identity, classify abandoned in-flight tasks as `stale`, expose stale overlays, and allow removal.

3. Failed queue rows could disappear from detail while landing still linked to them
   - Severity: important
   - Location: `otto/mission_control/model.py`
   - Root cause: old terminal legacy queue rows were dropped by live retention after five minutes.
   - Fix: retain failed/cancelled/interrupted legacy queue rows while queue state still references them.

4. Detail lookup could be hidden by list filters
   - Severity: medium
   - Location: `otto/mission_control/service.py`
   - Root cause: `/api/runs/{id}` reused current list filters when resolving selected detail.
   - Fix: resolve detail against an unfiltered state snapshot.

5. Removed selected rows could leave false 404 error banners
   - Severity: medium
   - Location: `otto/web/client/src/App.tsx`
   - Root cause: detail and log pollers kept targeting removed rows after cleanup/removal.
   - Fix: ignore stale log 404s and clear stale selected detail on detail 404.

6. Overview undercounted queue failures
   - Severity: medium
   - Location: `otto/web/client/src/App.tsx`
   - Root cause: `Needs attention` counted failed history and stale live rows only.
   - Fix: de-duplicate attention rows across landing, live, and history.

## Rejected Candidates

- Broad `except Exception` blocks in Mission Control model/service/adapter code were reviewed. Most are boundary guards around corrupt local state files, optional manifests, or git/queue state reads; changing them in this pass would reduce resilience rather than improve correctness.
