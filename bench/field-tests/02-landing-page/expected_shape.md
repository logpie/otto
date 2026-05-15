# Expected Shape

Forced tier: `auto`. The driver must pass `--tier auto`.

Expected graph shape: this is the control scenario where the root Lead chooses.
It should usually be inline. A tiny two-child decomposition is acceptable if
the root Lead splits structure/styling from interaction/tests, but anything
deeper is likely over-decomposition. It would be wrong if Otto creates a full
React/Vite stack, backend API, database, or multi-branch integration for this
static page.
