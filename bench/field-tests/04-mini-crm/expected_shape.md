# Expected Shape

Forced tier: `modular`. The driver must pass `--tier modular`.

Expected graph shape: architecture-first root decomposition. The root should
emit a concise architect/scaffold child that establishes the shared schema,
ports, API conventions, and shell, followed by two or three vertical build
leaves such as companies, contacts, and deals/dashboard. This is the small
version of the shape that larger issue trackers should hit. It would be wrong
if Otto stays inline, skips the architect/scaffold contract, splits only by
technical layer, or lets child ownership cause schema and route drift.
