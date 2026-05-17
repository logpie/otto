# Expected Shape

Forced tier: `modular`. The driver passes `--tier modular`.

Expected graph: **depth 3+ recursion on TWO sibling branches concurrently**.
Root emits scaffold, ingestion, transformation, and serving children. BOTH
the ingestion child AND the transformation child must independently emit
their own nested subtrees (3 connector leaves; 3 stage leaves). Correct
shape has two children with `decomposition == emit`, two separate sets of
grandchildren, max depth >= 3, and the two nested subtrees integrating in
parallel.

It would be a FAILURE of this scenario if:
- either multi-subsystem child stays inline / flat (depth 2);
- the two nested subtrees collide on shared files or ports during parallel
  integration;
- a grandchild branch from one subtree lands on the wrong parent
  integration branch (cross-subtree branch leakage);
- any `merge_blocked` at root, lost branch, or `unverified` leaf;
- serving is built before the shared record/stage contracts exist.

This is the maximum-stress recursion+concurrency scenario: it specifically
targets cross-subtree branch isolation and parallel nested integration,
which is where the historic brittleness class bit hardest.
