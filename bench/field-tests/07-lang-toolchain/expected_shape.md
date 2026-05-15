# Expected Shape

Forced tier: `modular`. The driver passes `--tier modular`.

Expected graph: **depth 3+ recursive decomposition** on a non-web (CLI)
product. Root emits a shared-schema/scaffold child, a language-analysis-
engine child, and a CLI child. The analysis-engine child MUST emit its own
nested subtree of four stage leaves (lexer, parser, semantic, evaluator+
aggregator). Correct shape has a child with `decomposition == emit` and
grandchildren; max depth >= 3.

It would be a FAILURE of this scenario if:
- the tree stays depth 2 (engine flattened into root-level leaves);
- one inline leaf owns the whole engine;
- a stage imports another stage's internals instead of the shared schema;
- a grandchild (stage) branch is lost before reaching the engine
  integration, or the engine branch before reaching root;
- the CLI is built before the engine contract exists (ordering/`depends_on`
  must be honored).

This is the CLI/non-web counterpart to the SaaS recursion scenario — it
checks that recursion + nested integration work without a boot-smoke HTTP
surface, so the merge/verdict path is the only oracle.
