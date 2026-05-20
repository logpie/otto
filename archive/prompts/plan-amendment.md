You are repairing only Otto's v5 plan metadata, not rebuilding the product.

Allowed work:
- Edit CHARTER.md ownership metadata.
- Mutate Otto task-graph ownership fields for affected child tasks.
- Keep the existing scaffold and product code intact.

Goal:
- Clear the supplied foundation/feature ownership overlap.
- Preserve foundation_contract ownership for shared/contract files.
- Give each feature child a disjoint, file-local owned_paths set under the
  declared registration_isolation.leaf_extension_globs.

Do not re-run or rewrite the architect scaffold. Do not widen the repair beyond
the packet's allowed_paths.
