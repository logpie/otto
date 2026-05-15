# Expected Shape

Expected shape is a compact decomp with shared contracts: scaffold/content
model, rendering/templates, index/tag/search/RSS outputs, and integration tests.
This scenario is meant to surface file-ownership and cross-child contract bugs.
It would be wrong if children all edit the same template and acceptance files
without coordination, or if the final integration forgets to propagate one
generated output type.
