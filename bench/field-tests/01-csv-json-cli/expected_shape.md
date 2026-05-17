# Expected Shape

Forced tier: `solo`. The driver must pass `--tier solo`.

Expected graph shape: one root Lead that calls `begin_inline`, builds the CLI
directly, and finishes with one committed product change on `main`. A useful
result is one small CLI plus an acceptance test, with no child decomposition and
no merge or integration complexity. It would be wrong if Otto emits an
architect/leaf tree, creates a full web app, or spends time splitting ownership
for a one-file CLI.
