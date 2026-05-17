# iTracker CHARTER

Authoritative architecture and contract document. All feature agents read this before building.

---

## Operational Facts

| Resource | Value |
|---|---|
| REST API port | 8000 (`API_PORT`) |
| WebSocket port | 8001 (`WS_PORT`) |
| Frontend dev port | 5173 (`FRONTEND_PORT`) |
| DB path | `./backend/itracker.db` |
| Uploads dir | `./uploads/` |
| Start command | `./start.sh` |
| Install (backend) | `cd backend && uv venv .venv --python python3.11 && uv pip install -e ".[dev]" --python .venv/bin/python` |
| Install (frontend) | `cd frontend && npm install` |

---

## Full-Text Search Strategy

SQLite FTS5 virtual tables — built by P3 feature leaf (`v5-e1b099554ce2`).

- `issues_fts` — content table over `issues(title, description)` plus denormalized team identifier and team number.
- `comments_fts` — content table over `comments(body)`.

**Sync**: INSERT/UPDATE/DELETE triggers on `issues` and `comments` keep FTS tables current with zero application-layer overhead.

```sql
-- Example trigger (actual DDL in P3 migration)
CREATE TRIGGER issues_ai AFTER INSERT ON issues BEGIN
  INSERT INTO issues_fts(rowid, title, description) VALUES (new.rowid, new.title, new.description);
END;
```

---

## WebSocket Event Protocol

All events are JSON objects with this shape:

```json
{"type": "event_type", "payload": {...}, "workspace_id": "uuid"}
```

**Event types**:

| Type | Emitted when |
|---|---|
| `issue.created` | New issue saved |
| `issue.updated` | Issue fields changed |
| `issue.commented` | Comment posted |
| `cycle.completed` | Cycle marked complete |

**Auth**: Connect with `?token=JWT` query param. The WS server decodes the JWT (`sub` = user_id). Workspace scoping is derived from the token or the first `workspace_id` in the event stream.

**Client reconnect**: Exponential backoff starting at 1 s, capped at 30 s. On reconnect, feature stores re-fetch stale data via REST.

---

## Email Dev Strategy

All emails are logged to stdout. No SMTP in dev.

Format:

```
[EMAIL ts=ISO8601] to=recipient@example.com subject=Subject line
---HTML---
<html>...full HTML body...</html>
---TEXT---
Plain text version of the email.
```

The `send_email(to, subject, html_body, text_body)` function in `backend/app/email_service.py` implements this. Feature leaves call it directly.

---

## Rate Limiting

- **Per-IP**: 100 requests/minute (global default via slowapi middleware in `main.py`)
- **Per-token**: 1000 requests/hour (applied to authenticated endpoints)
- Library: `slowapi` (ASGI middleware, no Redis required)

---

## Foundation Contracts

Machine-readable contract listing all scaffold-owned shared files that feature leaves must import but never create or edit.

```json
[
  {"path": "backend/app/models.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["User","Workspace","Team","Issue","Comment","Notification","Cycle","Label","SavedView","Webhook","ActivityEntry"]},
  {"path": "backend/app/database.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["get_db","AsyncSession","Base"]},
  {"path": "backend/app/auth.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["get_current_user","create_access_token","verify_password","get_password_hash"]},
  {"path": "backend/app/ws_manager.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["WSManager","manager"]},
  {"path": "backend/app/email_service.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["send_email"]},
  {"path": "backend/tests/conftest.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["async_client","db_session","test_user","auth_headers","test_workspace","test_team"]},
  {"path": "frontend/src/api/client.ts", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["apiClient"]},
  {"path": "frontend/src/store/", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/components/Toast.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/components/Skeleton.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/components/MarkdownRenderer.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/components/Modal.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/components/EmptyState.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/components/ThemeProvider.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/layouts/AppLayout.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
  {"path": "frontend/src/hooks/useWebSocket.ts", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"}
]
```

---

## Information Architecture Contract

```json
{
  "registration_isolation": {
    "policy": "auto-discovery",
    "shared_registry_files": [
      {"path": "backend/app/main.py", "role": "router-loader", "leaf_edit": false},
      {"path": "frontend/src/router.tsx", "role": "route-composer", "leaf_edit": false}
    ],
    "leaf_extension_globs": [
      "backend/app/routers/router_*.py",
      "frontend/src/features/*/routes.tsx",
      "frontend/src/features/*/store.ts",
      "frontend/src/features/*/api.ts",
      "backend/tests/test_*.py"
    ]
  },
  "foundation_contracts": [
    {"path": "backend/app/models.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["User","Workspace","Team","Issue","Comment","Notification","Cycle","Label","SavedView","Webhook","ActivityEntry"]},
    {"path": "backend/app/database.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["get_db","AsyncSession","Base"]},
    {"path": "backend/app/auth.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["get_current_user","create_access_token","verify_password","get_password_hash"]},
    {"path": "backend/app/ws_manager.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["WSManager","manager"]},
    {"path": "backend/app/email_service.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["send_email"]},
    {"path": "backend/tests/conftest.py", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["async_client","db_session","test_user","auth_headers","test_workspace","test_team"]},
    {"path": "frontend/src/api/client.ts", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic", "required_exports": ["apiClient"]},
    {"path": "frontend/src/store/", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/components/Toast.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/components/Skeleton.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/components/MarkdownRenderer.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/components/Modal.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/components/EmptyState.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/components/ThemeProvider.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/layouts/AppLayout.tsx", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"},
    {"path": "frontend/src/hooks/useWebSocket.ts", "owner_task_id": "v5-9f2ed79891e7", "check": "semantic"}
  ],
  "feature_owned_paths": {
    "v5-d55970a39e2f": {
      "description": "P1: Auth & User Management",
      "new_files": [
        "backend/app/routers/router_auth.py",
        "backend/app/routers/router_users.py",
        "backend/app/routers/router_workspaces.py",
        "backend/tests/test_auth.py",
        "backend/tests/test_users.py",
        "frontend/src/features/auth/",
        "frontend/src/features/workspace/"
      ]
    },
    "v5-08988ce82303": {
      "description": "P2: Core Issue Tracker (Teams, Issues, Comments, Labels, Notifications, Activity)",
      "new_files": [
        "backend/app/routers/router_teams.py",
        "backend/app/routers/router_issues.py",
        "backend/app/routers/router_comments.py",
        "backend/app/routers/router_labels.py",
        "backend/app/routers/router_notifications.py",
        "backend/tests/test_issues.py",
        "backend/tests/test_comments.py",
        "frontend/src/features/issues/",
        "frontend/src/features/notifications/"
      ]
    },
    "v5-e1b099554ce2": {
      "description": "P3: Cycles, Search, Saved Views, Webhooks & Settings",
      "new_files": [
        "backend/app/routers/router_cycles.py",
        "backend/app/routers/router_search.py",
        "backend/app/routers/router_saved_views.py",
        "backend/app/routers/router_webhooks.py",
        "backend/app/routers/router_settings.py",
        "backend/tests/test_cycles.py",
        "backend/tests/test_search.py",
        "frontend/src/features/cycles/",
        "frontend/src/features/search/",
        "frontend/src/features/settings/"
      ]
    },
    "v5-79c37b00542a": {
      "description": "P4: Real-time, UI Polish & Production UX",
      "new_files": [
        "frontend/src/features/kanban/",
        "frontend/src/features/realtime/",
        "frontend/src/features/keyboard/"
      ]
    }
  }
}
```

---

## Backend Router Convention

Each `backend/app/routers/router_<feature>.py` must:

1. Create a `router = APIRouter(prefix="/...", tags=["..."])`.
2. The `prefix` is the canonical URL prefix for that feature (e.g. `/auth`, `/issues`).
3. Import `get_db` from `app.database` and `get_current_user` from `app.auth`.
4. Import models from `app.models` and schemas from `app.schemas` (add feature-specific schemas inline or in a `schemas_<feature>.py`).
5. Emit WS events via `from app.ws_manager import manager; await manager.dispatch_event(...)`.

## Frontend Feature Convention

Each `frontend/src/features/<feature>/` directory should contain:

- `routes.tsx` — exports `routes: RouteObject[]` (required for route auto-discovery)
- `api.ts` — Axios calls using `apiClient` from `src/api/client.ts`
- `store.ts` — Zustand slice for feature state (optional)
- Page/component files as needed

Feature stores should call `useUIStore().registerSidebarItems([...])` and `useUIStore().registerCommandActions([...])` at mount time to inject into the shared sidebar and command palette.
