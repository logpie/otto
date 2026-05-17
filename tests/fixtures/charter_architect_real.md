# iTracker — Project Charter

## Stack

| Concern | Choice |
|---|---|
| Backend | Python 3.11 · FastAPI · Uvicorn |
| Database | SQLite (via `sqlite3`) · WAL mode · FK on |
| Auth | JWT (python-jose HS256) · session cookie `itracker_session` |
| Passwords | bcrypt (passlib) |
| Frontend | React 18 · TypeScript · Vite · Tailwind CSS v3 |
| Routing | React Router v6 |
| State | Zustand |
| Data fetching | TanStack React Query v5 |
| HTTP client | axios |
| Realtime | WebSocket `/api/ws?workspace_id=<id>` |

## Ports & Paths

- Backend: `http://127.0.0.1:8000` (env `BACKEND_PORT`)
- Frontend: `http://localhost:5173` (env `FRONTEND_PORT`)
- DB path: `./data/itracker.db` (env `DB_PATH`)
- Uploads: `./data/uploads/`
- Venv: `.venv/` at repo root (managed with `uv`)

## Starting the App

```
./start.sh
```

## Information Architecture Contract

```json
{
  "registration_isolation": {
    "policy": "file-local",
    "shared_registry_files": [
      "frontend/src/routes/index.ts"
    ],
    "leaf_extension_globs": [
      "frontend/src/features/*/routes.tsx",
      "backend/routers/*.py"
    ],
    "leaf_edit": false
  },
  "feature_owned_paths": {
    "v5-ec6a245d76bf": {
      "description": "Issues, Teams & Core CRUD",
      "may_add": [
        "backend/routers/issues.py",
        "backend/routers/teams.py",
        "backend/routers/workspaces.py",
        "backend/routers/labels.py",
        "frontend/src/features/issues/**",
        "frontend/src/features/teams/**",
        "frontend/src/features/workspaces/**"
      ]
    },
    "v5-8790a6b95ead": {
      "description": "Cycles & Kanban",
      "may_add": [
        "backend/routers/cycles.py",
        "frontend/src/features/cycles/**"
      ]
    },
    "v5-5e9904afe378": {
      "description": "Comments, Search & Real-time",
      "may_add": [
        "backend/routers/comments.py",
        "backend/routers/search.py",
        "backend/routers/notifications.py",
        "frontend/src/features/comments/**",
        "frontend/src/features/search/**",
        "frontend/src/features/inbox/**"
      ]
    },
    "v5-10d588cb7026": {
      "description": "Auth Polish, Settings & Admin",
      "may_add": [
        "backend/routers/settings.py",
        "backend/routers/tokens.py",
        "backend/routers/webhooks.py",
        "backend/routers/uploads.py",
        "frontend/src/features/settings/**",
        "frontend/src/features/profile/**"
      ]
    }
  }
}
```

## Foundation Contracts

```json
[
  {
    "path": "backend/database.py",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": ["get_connection", "db", "init_db", "DB_PATH"]
  },
  {
    "path": "backend/auth.py",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": [
      "hash_password",
      "verify_password",
      "create_token",
      "verify_token",
      "get_current_user",
      "get_current_verified_user",
      "SESSION_COOKIE"
    ]
  },
  {
    "path": "backend/main.py",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "semantic",
    "behavior_probes": [
      "GET /api/health returns {\"status\":\"ok\"}",
      "Routers in backend/routers/*.py are auto-discovered and included",
      "CORS allows http://localhost:5173 with credentials",
      "Rate limit: 429 + Retry-After header on excess"
    ]
  },
  {
    "path": "backend/websocket_manager.py",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": ["manager", "WebSocketManager"],
    "behavior_probes": [
      "manager.connect(ws, workspace_id) accepts the WebSocket",
      "manager.broadcast(workspace_id, event) sends JSON to all room members",
      "manager.disconnect(ws, workspace_id) removes from room"
    ]
  },
  {
    "path": "backend/email_service.py",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": ["send_email"]
  },
  {
    "path": "frontend/src/api/client.ts",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": ["default (axios instance)"],
    "behavior_probes": [
      "baseURL = /api",
      "withCredentials = true",
      "401 response redirects to /login"
    ]
  },
  {
    "path": "frontend/src/store/auth.ts",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": ["useAuthStore", "User"],
    "behavior_probes": [
      "fetchMe() calls GET /api/auth/me and sets user",
      "logout() calls POST /api/auth/logout and redirects to /login"
    ]
  },
  {
    "path": "frontend/src/store/ws.ts",
    "owner_task_id": "v5-0bc5625cf971",
    "check": "literal",
    "required_exports": ["useWsStore"],
    "behavior_probes": [
      "connect(workspaceId) opens WebSocket to /api/ws?workspace_id=<id>",
      "Reconnects with exponential backoff on close",
      "Sets reconnecting=true after 5s of disconnect",
      "subscribe(handler) returns unsubscribe function"
    ]
  }
]
```

## Database Schema

All tables created by `backend/database.py:init_db()` on startup. See schema string in that file for full DDL.

Key entities: `users`, `workspaces`, `workspace_members`, `teams`, `issues`, `cycles`, `comments`, `labels`, `notifications`, `activity_log`, `saved_views`, `uploads`.

## WebSocket Protocol

- Connect: `ws://localhost:8000/api/ws?workspace_id=<id>`
- Messages: JSON objects `{ "type": string, "payload": any }`
- Feature leaves call `manager.broadcast(workspace_id, event_dict)` from their routers to push real-time events.

## Full-Text Search Strategy

SQLite FTS5 on `issues(title, description)` and `comments(body)`. Feature leaf `v5-5e9904afe378` owns the FTS index creation and search router.

## Rate Limits

- Per-IP: 200 req/min
- Per-token (session cookie prefix): 60 req/min
- Response: 429 + `Retry-After: 60` header
