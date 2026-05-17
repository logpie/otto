# Multi-Service SaaS Platform

Build a small but genuinely multi-service SaaS backend with a thin web UI.

This is a FORCED-RECURSION field-test scenario. The backend is not one
service — it is a platform composed of four independent services that own
separate data models, separate endpoints, and separate tests. A single inline
leaf cannot coherently own the whole backend; the child that owns the backend
platform MUST itself decompose into one independent leaf per service. Recursive
sub-decomposition is required, not optional.

## Top-level shape (must be honored)

- A shared contracts/scaffold child: shared API envelope, error shape, auth
  token format, storage choice, port conventions, `CHARTER.md`.
- A backend **platform** child that is itself multi-service and MUST emit a
  nested subtree — one leaf per service below.
- A thin frontend SPA child that exercises the services through the UI.

## Backend platform — four independent services (each its own nested leaf)

1. **Auth service**: register, login, issue + verify bearer tokens, `/auth/me`.
   Owns the users table and password hashing. No other service writes users.
2. **Billing service**: subscription plans (free/pro/enterprise), a tenant's
   current plan, plan changes, and a usage counter. Owns the billing tables.
3. **Audit-log service**: append-only event log; every write in the other
   services emits an audit event through a shared contract. Owns the audit
   table; exposes `/audit?tenant=` (read-only, newest-first).
4. **Core resource API**: tenant-scoped CRUD for "projects" (name, status,
   owner). Enforces auth tokens, checks plan limits via the billing service,
   and emits audit events. Owns the projects table.

Services communicate only through the shared contracts defined by the
scaffold child (in-process function contracts or local HTTP — your choice,
documented in `CHARTER.md`). They must not reach into each other's tables.

## Required deliverables

- `start.sh` at repo root serving the user-facing app on `$PORT`; backend on
  `$API_PORT`/`$BACKEND_PORT` if separate.
- Persist in SQLite or local JSON. No external services.
- `tests/run_acceptance.py` that, from a clean checkout, exercises one full
  cross-service journey: register → login → create project (under free plan
  limit) → hit the plan limit → upgrade plan via billing → create more →
  read the audit log and assert events for each write.
- `CHARTER.md` documenting the shared contracts, the four service ownership
  boundaries, and the nested decomposition.

Keep each service deliberately small but real. No third-party billing, no
email, no OAuth — local only.
