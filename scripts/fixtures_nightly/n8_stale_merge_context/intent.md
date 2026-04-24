Seeded service app for rename-plus-stale-context merge handling. Use this fixture
to validate that Otto can merge queue branches when later branches were produced
against stale paths and need reconciliation after an earlier graduation.
Maintain a small billing service app with visible quote behavior.

The service layer may be renamed while other branches update pricing logic and
tests. Merges must use current repository state rather than stale branch context
so imports, service names, and weekend pricing rules stay coherent.
