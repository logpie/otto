# Success Criteria

Scenario metadata:

- kind: web
- budget_seconds: 2400
- max_parallel: 4
- tier: modular
- boot_smoke: true
- smoke_path: /
- smoke_port_var: PORT

The product is successful when:

- `./start.sh` serves the app on `$PORT` and the backend answers on its port.
- `python tests/run_acceptance.py` passes from a clean checkout and exercises
  the full cross-service journey: register, login, create project under the
  free-plan limit, hit the limit, upgrade plan, create more, read audit log.
- The audit log contains one event per write across all services.
- Plan-limit enforcement actually blocks creation past the free limit until
  the billing service records an upgrade.
- `CHARTER.md` documents the four-service ownership and the nested subtree.

Graph-shape acceptance (the point of this scenario):

- Max task-graph depth is >= 3 (root -> backend-platform -> service leaves).
- The backend-platform task has `decomposition == emit` with grandchildren.
- Every grandchild branch reaches its parent integration and then `main`;
  no `merge_blocked` at root, no lost branches, no `unverified` leaves.
