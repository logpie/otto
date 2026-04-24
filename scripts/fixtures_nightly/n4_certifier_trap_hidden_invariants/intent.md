Maintain a multi-tenant task API with CSV import support.

CSV import changes must preserve tenant isolation, validate imported data, and
return useful API errors. Visible tests cover the happy path, but hidden
invariants check malformed input, auth boundaries, and import consistency.
