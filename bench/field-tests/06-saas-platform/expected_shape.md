# Expected Shape

Forced tier: `modular`. The driver passes `--tier modular`.

Expected graph: **depth 3+ recursive decomposition**. The root emits a
shared-contracts/scaffold child, a backend-platform child, and a frontend
child. The backend-platform child MUST NOT build inline — it must itself
emit a nested subtree of four service leaves (auth, billing, audit-log,
core resource API). Correct shape therefore has a child whose
`decomposition == emit` with its own grandchildren, max tree depth >= 3.

It would be a FAILURE of this scenario if:
- the tree stays depth 2 (root -> flat leaves), i.e. the backend platform
  was flattened into the root instead of owning a nested subtree;
- one inline leaf tries to own all four services;
- services reach into each other's tables instead of using shared contracts;
- the nested integration loses a grandchild's branch before it reaches the
  backend-platform integration, or the platform's branch before it reaches
  root.

This scenario exists specifically to exercise grandchild SESSION_DIR
isolation, nested integration, and multi-level branch propagation under the
post-hardening code.
