Seeded FastAPI task app for a realistic Mission Control operator session. Use
this fixture to validate live standalone runs, queue runs, cancellation, history
inspection, editor launch, selected-row merge, and post-merge verification.
Maintain a minimal FastAPI task service used to exercise Mission Control.

The API should grow task list, create, and delete endpoints while the operator
uses Mission Control to inspect live runs, switch logs, cancel one queued task,
open artifacts, and merge a selected successful queue row.
