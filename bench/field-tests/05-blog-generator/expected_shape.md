# Expected Shape

Forced tier: `modular`. The driver must pass `--tier modular`.

Expected graph shape: recursive decomposition. The root should start
architecture-first, then at least one substantial child should decompose again
for the static-generation pipeline. A useful shape is architect/scaffold, a
content model/parser subtree, a rendering/template subtree, index/tag/search/RSS
output leaves, and acceptance/integration coverage. This scenario is meant to
surface file-ownership, nested branch propagation, and cross-child contract
bugs. It would be wrong if the entire generator stays inline, if every child
edits the same template and acceptance files without coordination, or if final
integration forgets to propagate one generated output type.
