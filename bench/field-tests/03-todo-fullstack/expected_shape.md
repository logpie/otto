# Expected Shape

Forced tier: `lead`. The driver must pass `--tier lead`.

Expected graph shape: flat decomposition with an architect/scaffold child plus
two or three build leaves: backend API/storage, frontend UI, and possibly
acceptance/integration tests. Children should run in parallel after a small
scaffold. It would be wrong if the root Lead stays fully inline and hand-rolls
everything in one long session, or if it creates a deep tree for such a compact
product.
