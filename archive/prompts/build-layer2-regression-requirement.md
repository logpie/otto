**Regression-test requirement**:

If this repo has a test suite or contract test surface, add the smallest
repo-native regression test that would fail before your repair and pass after
it, unless doing so is genuinely impossible. Cover the exact acceptance examples
and edge/error cases named in the audit detail or original intent. If
you touch parsing, normalization, validation, or error handling, include an
invalid/error input that exercises the same changed path, not only a generic
invalid value. When the audit or intent says invalid input should be preserved,
assert the result is exactly equal to the original input, including punctuation/separators.
Run the targeted test or explain why it could not be run.

Do NOT change expected test values to match your current implementation when
that would contradict the user intent or audit detail. Existing repo test files
are in scope for focused regression tests unless the repository itself forbids
editing tests; docstring examples are not a substitute unless the native test
command runs doctests.
