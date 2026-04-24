Seeded multi-tenant task app with intentionally weak visible tests around CSV
import. Use this fixture to validate that Otto's certifier and hidden tests catch
tenant isolation and import-invariant failures that a superficial build can miss.
Maintain a multi-tenant task API with CSV import support.

CSV import changes must preserve tenant isolation, validate imported data, and
return useful API errors. Visible tests cover the happy path, but hidden
invariants check malformed input, auth boundaries, and import consistency.
