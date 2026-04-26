# Local Notes

The goal was to reduce the cost of ordinary development loops without deleting coverage or weakening pre-merge confidence.

The practical result is a tiered verification model:

- Use `smoke` for quick sanity after tiny edits.
- Use `fast` for normal non-browser work.
- Use `web` for Mission Control backend/client changes.
- Use browser smoke or targeted browser tests for UI behavior.
- Use full pytest before broad merges or pushes.

Default pytest remains broad by design. This avoids surprising CI or hiding queue/merge/resume regressions from the existing command.
