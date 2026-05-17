# iTracker Charter

Linear-lite team issue tracker. Production-grade, full-stack, single-SQLite-file.

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | Node.js 18+ · Express 4 · better-sqlite3 · jsonwebtoken · bcrypt · ws · multer · nanoid |
| Frontend | React 18 · TypeScript · Vite 5 · Zustand · TailwindCSS · React Router v6 · react-markdown + rehype-highlight · @hello-pangea/dnd |
| DB | SQLite (better-sqlite3) |
| Email | Console-logged (structured JSON to stdout) |

## Port Allocation

| Service | Default Port | Env Var |
|---------|-------------|---------|
| API (Express) | 3001 | `API_PORT` |
| WebSocket | 3002 | `WS_PORT` |
| Frontend (Vite) | 5173 | `FRONTEND_PORT` |

## DB File

`backend/data/tracker.db` — configurable via `DB_PATH` env var.

## Email Strategy

All emails are console-logged as structured JSON (`_type: "email"`). The `backend/lib/email.js`
module provides typed helpers. In production, swap `sendEmail()` for an SMTP transport.

## Search Strategy

Basic keyword + operator search via SQLite LIKE queries. Implemented in `backend/lib/search.js`.
Supported operators: `status:`, `priority:`, `assignee:me`, `assignee:<name>`, `label:<name>`,
`-label:<name>`, `"exact phrase"`, plus free-text keywords. Documented as "keyword search" in UI.

---

## Information Architecture Contract

```json
{
  "registration_isolation": {
    "policy": "file_local_auto_discovery",
    "description": "Backend routes are auto-loaded from backend/routes/*.js (each exports { router, prefix }). Feature leaves add a new route file — they do NOT edit app.js. Frontend routes are auto-discovered via import.meta.glob('./features/*/routes.tsx') — feature leaves add frontend/src/features/<name>/routes.tsx exporting default RouteEntry[], they do NOT edit frontend/src/routes.tsx.",
    "shared_registry_files": [
      {
        "path": "frontend/src/routes.tsx",
        "owner": "v5-352ed203135e",
        "leaf_edit": false
      }
    ],
    "leaf_extension_globs": [
      "backend/routes/*.js",
      "frontend/src/features/**/*.tsx",
      "frontend/src/features/**/*.ts",
      "backend/tests/*.test.js"
    ]
  },
  "foundation_contracts": [
    {
      "path": "backend/app.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "semantic",
      "required_exports": ["app (default Express instance)"],
      "behavior_probes": ["GET /api/health returns {ok:true}", "auto-loads routes/*.js"]
    },
    {
      "path": "backend/db.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["getDb"],
      "behavior_probes": ["getDb() returns singleton better-sqlite3 instance with schema applied"]
    },
    {
      "path": "backend/schema.sql",
      "owner_task_id": "v5-352ed203135e",
      "check": "semantic",
      "behavior_probes": ["All 20 tables present", "Foreign keys enabled"]
    },
    {
      "path": "backend/ws-server.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["startWsServer", "broadcast", "broadcastToUser"]
    },
    {
      "path": "backend/middleware/auth.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["requireAuth", "requireWorkspaceAdmin", "signToken", "JWT_SECRET"]
    },
    {
      "path": "backend/middleware/rateLimit.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "semantic",
      "required_exports": ["default (Express middleware)"]
    },
    {
      "path": "backend/middleware/logger.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "semantic",
      "required_exports": ["default (Express middleware)"]
    },
    {
      "path": "backend/lib/email.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["sendVerificationEmail", "sendPasswordResetEmail", "sendWorkspaceInviteEmail", "sendMentionEmail"]
    },
    {
      "path": "backend/lib/webhook.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["dispatch"]
    },
    {
      "path": "backend/lib/search.js",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["searchIssues", "parseQuery"]
    },
    {
      "path": "frontend/src/api/client.ts",
      "owner_task_id": "v5-352ed203135e",
      "check": "semantic",
      "required_exports": ["apiClient (axios instance with auth interceptors)"]
    },
    {
      "path": "frontend/src/store/authStore.ts",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["useAuthStore", "User (type)"]
    },
    {
      "path": "frontend/src/store/wsStore.ts",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["useWsStore"]
    },
    {
      "path": "frontend/src/hooks/useWebSocket.ts",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["useWebSocket"]
    },
    {
      "path": "frontend/src/components/ui/Skeleton.tsx",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["SkeletonLine", "SkeletonCard", "SkeletonList"]
    },
    {
      "path": "frontend/src/components/ui/Toast.tsx",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["ToastContainer", "useToast", "useToastStore"]
    },
    {
      "path": "frontend/src/components/ui/Modal.tsx",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["Modal"]
    },
    {
      "path": "frontend/src/components/ui/Button.tsx",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["Button"]
    },
    {
      "path": "frontend/src/components/ui/Badge.tsx",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["Badge"]
    },
    {
      "path": "frontend/src/components/ui/Avatar.tsx",
      "owner_task_id": "v5-352ed203135e",
      "check": "literal",
      "required_exports": ["Avatar"]
    }
  ],
  "feature_owned_paths": {
    "v5-cc77b060a4e7": {
      "description": "Auth, Users & Workspaces",
      "paths": [
        "backend/routes/auth.js",
        "backend/routes/users.js",
        "backend/routes/workspaces.js",
        "backend/routes/pats.js",
        "backend/tests/auth.test.js",
        "frontend/src/features/auth/**",
        "frontend/src/features/workspace/**"
      ]
    },
    "v5-df9a661d152e": {
      "description": "Teams, Issues, Kanban Board & Backlog",
      "paths": [
        "backend/routes/teams.js",
        "backend/routes/issues.js",
        "backend/routes/labels.js",
        "backend/tests/issues.test.js",
        "frontend/src/features/teams/**",
        "frontend/src/features/issues/**",
        "frontend/src/components/ui/Sidebar.tsx"
      ]
    },
    "v5-b524d3799823": {
      "description": "Comments, Notifications & Inbox",
      "paths": [
        "backend/routes/comments.js",
        "backend/routes/notifications.js",
        "backend/routes/upload.js",
        "backend/lib/mentions.js",
        "backend/tests/comments.test.js",
        "frontend/src/features/comments/**",
        "frontend/src/features/inbox/**",
        "frontend/src/store/notifStore.ts"
      ]
    },
    "v5-00056f08449f": {
      "description": "Cycles, Search, Saved Views, Webhooks & Settings",
      "paths": [
        "backend/routes/cycles.js",
        "backend/routes/search.js",
        "backend/routes/saved-views.js",
        "backend/routes/webhooks.js",
        "backend/routes/activity.js",
        "backend/tests/cycles.test.js",
        "frontend/src/features/cycles/**",
        "frontend/src/features/search/**",
        "frontend/src/features/settings/**"
      ]
    }
  }
}
```
