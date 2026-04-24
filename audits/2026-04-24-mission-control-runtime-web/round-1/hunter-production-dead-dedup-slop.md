# Production Dead/Dedup/Slop Hunter

## Fixed

- Split runtime health and diagnostics out of `otto/mission_control/service.py` into `otto/mission_control/runtime.py`.
  - This keeps the FastAPI/client-neutral service focused on orchestration and keeps process ownership, flock probing, queue file diagnostics, and command backlog inspection together.

## Deferred

- Review packet assembly still lives in `service.py`. It is cohesive enough for this change, but if evidence review grows further it should move to a `review_packet.py` module with typed return shapes.
- Runtime diagnostics currently returns plain dictionaries to match the existing serializer style. A typed dataclass layer would improve editor feedback, but it would be a broader API-shape refactor.

## Rejected

- No TUI cleanup was attempted in this pass. The requested scope was web Mission Control single-user readiness, and removing TUI code now would increase merge risk without directly improving the web path.
