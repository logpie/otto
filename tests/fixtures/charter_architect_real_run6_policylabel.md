# iTracker CHARTER

## Stack Decisions

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Backend framework | FastAPI (Python 3.11) | Async-native, OpenAPI auto-gen, type annotations align with SQLModel |
| ORM / DB model | SQLModel (SQLAlchemy + Pydantic) | Single class for both DB schema and Pydantic validation |
| Database | SQLite (dev) via Alembic migrations | Zero-dependency for dev; Alembic enables clean upgrades |
| Auth | JWT (access + refresh) in httpOnly cookies + Bearer for API/PAT | Secure browser auth + programmatic API access |
| Password hashing | bcrypt | Industry standard, resistant to GPU cracking |
| Full-text search | SQLite FTS5 virtual tables (see below) | In-process, zero dependencies, fast for <10M docs |
| WebSocket | FastAPI WebSocket endpoint at `/ws/{room}` | Same process/port, no extra service |
| Frontend framework | React 18 + TypeScript + Vite | Fast HMR, strong typing |
| State management | Zustand | Minimal boilerplate, no Provider wrapping |
| Data fetching | TanStack Query v5 | Caching, optimistic updates, stale-while-revalidate |
| Styling | Tailwind CSS v3 + @tailwindcss/typography | Utility-first, dark mode via class |
| Markdown | react-markdown + rehype-highlight | GitHub-flavored MD + syntax highlighting |
| Drag & drop | @dnd-kit/core + @dnd-kit/sortable | Accessible, pointer/touch, no jQuery |
| Command palette | cmdk | Headless, composable |
| Toasts | react-hot-toast | Lightweight, stacks correctly |

## Port Conventions

| Service | Port | Env Var |
|---------|------|---------|
| Backend API | 8000 | `API_PORT` |
| WebSocket | 8000 | `WS_PORT` (same server, path `/ws/{room}`) |
| Frontend dev | 5173 | `FE_PORT` |

Vite proxies `/api` and `/ws` to `localhost:$API_PORT`, so the browser sees a single origin.

## Auth Approach

- **Browser clients**: JWT access token stored in `access_token` httpOnly cookie (60 min TTL), refresh token in `refresh_token` httpOnly cookie (30 day TTL). Token refresh happens transparently via axios interceptor.
- **API / script clients**: Personal Access Token (PAT) prefixed `itrk_`, sent as `Authorization: Bearer itrk_...`. PATsare SHA-256 hashed in the DB.
- **`get_current_user`** dependency in `backend/auth.py` reads from either cookie or Bearer header.

## Route Auto-Discovery

### Backend
`backend/main.py` uses `pkgutil.iter_modules` to discover all `*.py` files in `backend/routers/`. Any file that defines a module-level `router = APIRouter()` is automatically `include_router`-ed. Feature leaves add new router files to `backend/routers/` and never edit `main.py`.

### Frontend
`frontend/src/App.tsx` uses Vite's `import.meta.glob('./features/*/routes.tsx')` to discover route modules. Each feature leaf adds a `frontend/src/features/<name>/routes.tsx` that default-exports a React component rendering `<Route>` elements. The glob runs at build time; no central registry to edit.

## Full-Text Search Index Strategy

SQLite FTS5 virtual tables power full-text search over issues and comments.

### Tables
```sql
CREATE VIRTUAL TABLE issues_fts USING fts5(
    issue_id UNINDEXED,   -- filtered via SQL JOIN, not FTS5
    team_id UNINDEXED,
    workspace_id UNINDEXED,
    title,
    description,
    tokenize='unicode61 remove_diacritics 1'
);

CREATE VIRTUAL TABLE comments_fts USING fts5(
    comment_id UNINDEXED,
    issue_id UNINDEXED,
    body,
    tokenize='unicode61 remove_diacritics 1'
);
```

### Sync triggers
INSERT/UPDATE/DELETE triggers on `issues` and `comments` keep FTS5 in sync automatically (see `0001_initial.py` migration).

### Query operator mapping
| Operator | Handling |
|----------|---------|
| `plain terms` | FTS5 MATCH across title+description |
| `"exact phrase"` | FTS5 quoted phrase `"exact phrase"` |
| `status:open` | SQL `WHERE status = 'open'` |
| `priority:high` | SQL `WHERE priority = 'high'` |
| `assignee:me` | SQL `WHERE assignee_id = <current_user_id>` |
| `label:bug` | SQL `EXISTS` join on issue_labels + labels |
| `-label:wontfix` | SQL `NOT EXISTS` join |
| `team:ENG` | SQL `WHERE teams.identifier = 'ENG'` |

Compound queries combine a FTS5 `MATCH` clause with SQL `WHERE` clauses joined via `JOIN`. Ranking uses FTS5's built-in `bm25()` function.

Implementation: `backend/search_index.py`.

## Email Template Approach

`backend/email_service.py` renders HTML + text templates from a `TEMPLATES` dict and logs the full rendered email to stdout (human-readable). Templates: `verify_email`, `reset_password`, `workspace_invite`, `mention_notification`. In production, replace the `send()` function body with an SMTP/SES call while keeping the same interface.

---

## Information Architecture Contract

```json
{
  "registration_isolation": {
    "policy": "auto-discover",
    "description": "Leaf tasks add new files; they never edit shared registry files. Backend router discovery uses pkgutil.iter_modules over backend/routers/. Frontend route discovery uses import.meta.glob over features/*/routes.tsx.",
    "shared_registry_files": [
      {
        "path": "backend/main.py",
        "owner": "v5-a45d349c5e54",
        "leaf_edit": false,
        "note": "Auto-discovers routers via pkgutil. Leaf adds router file, not this file."
      },
      {
        "path": "frontend/src/App.tsx",
        "owner": "v5-a45d349c5e54",
        "leaf_edit": false,
        "note": "Auto-discovers routes via import.meta.glob. Leaf adds routes.tsx, not this file."
      }
    ],
    "leaf_extension_globs": [
      "backend/routers/*.py",
      "frontend/src/features/*/routes.tsx",
      "frontend/src/features/*/index.tsx"
    ]
  },
  "foundation_contracts": [
    {
      "path": "backend/main.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["app"]
    },
    {
      "path": "backend/database.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["engine", "get_session", "create_db_and_tables"]
    },
    {
      "path": "backend/auth.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["get_current_user", "get_pat_or_jwt_user", "hash_password", "verify_password", "create_access_token", "create_refresh_token"]
    },
    {
      "path": "backend/config.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["get_settings", "Settings"]
    },
    {
      "path": "backend/logger.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["get_logger", "configure_logging"]
    },
    {
      "path": "backend/models/__init__.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "note": "All domain models imported here to register with SQLModel metadata"
    },
    {
      "path": "backend/websocket_manager.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["manager", "ConnectionManager"]
    },
    {
      "path": "backend/email_service.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["send"]
    },
    {
      "path": "backend/search_index.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["search_issues", "parse_query", "ensure_fts_tables"]
    },
    {
      "path": "backend/rate_limiter.py",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["rate_limit", "SlidingWindowLimiter"]
    },
    {
      "path": "frontend/src/lib/api.ts",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["api", "setAuthToken"]
    },
    {
      "path": "frontend/src/lib/websocket.ts",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["wsClient"]
    },
    {
      "path": "frontend/src/store/auth.ts",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["useAuthStore", "AuthUser"]
    },
    {
      "path": "frontend/src/store/workspace.ts",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["useWorkspaceStore", "WorkspaceInfo", "TeamInfo"]
    },
    {
      "path": "frontend/src/store/theme.ts",
      "owner_task_id": "v5-a45d349c5e54",
      "check": "literal",
      "required_exports": ["useThemeStore"]
    }
  ],
  "feature_owned_paths": {
    "v5-d36d418332d0": {
      "description": "Auth, Users & Workspace Management",
      "backend_routers": [
        "backend/routers/auth.py",
        "backend/routers/users.py",
        "backend/routers/workspaces.py"
      ],
      "frontend_features": [
        "frontend/src/features/auth/",
        "frontend/src/features/auth/routes.tsx",
        "frontend/src/features/settings/"
      ],
      "owned_globs": [
        "backend/routers/auth.py",
        "backend/routers/users.py",
        "backend/routers/workspaces.py",
        "frontend/src/features/auth/**",
        "frontend/src/features/settings/**"
      ]
    },
    "v5-019db6109793": {
      "description": "Issues, Teams, Labels, Comments & Core Views",
      "backend_routers": [
        "backend/routers/teams.py",
        "backend/routers/issues.py",
        "backend/routers/labels.py",
        "backend/routers/comments.py",
        "backend/routers/activity.py"
      ],
      "frontend_features": [
        "frontend/src/features/issues/",
        "frontend/src/features/issues/routes.tsx",
        "frontend/src/features/inbox/",
        "frontend/src/features/inbox/routes.tsx"
      ],
      "owned_globs": [
        "backend/routers/teams.py",
        "backend/routers/issues.py",
        "backend/routers/labels.py",
        "backend/routers/comments.py",
        "backend/routers/activity.py",
        "frontend/src/features/issues/**",
        "frontend/src/features/inbox/**"
      ]
    },
    "v5-b2b3684e0a70": {
      "description": "Cycles, Search, Saved Views & Notifications",
      "backend_routers": [
        "backend/routers/cycles.py",
        "backend/routers/search.py",
        "backend/routers/saved_views.py",
        "backend/routers/notifications.py"
      ],
      "frontend_features": [
        "frontend/src/features/cycles/",
        "frontend/src/features/cycles/routes.tsx",
        "frontend/src/features/search/",
        "frontend/src/features/search/routes.tsx",
        "frontend/src/features/notifications/"
      ],
      "owned_globs": [
        "backend/routers/cycles.py",
        "backend/routers/search.py",
        "backend/routers/saved_views.py",
        "backend/routers/notifications.py",
        "frontend/src/features/cycles/**",
        "frontend/src/features/search/**",
        "frontend/src/features/notifications/**"
      ]
    },
    "v5-d07509b9fc1c": {
      "description": "Live Updates, Polish & Production Features",
      "backend_routers": [
        "backend/routers/websocket.py",
        "backend/routers/webhooks.py",
        "backend/routers/export.py"
      ],
      "frontend_features": [
        "frontend/src/features/shell/CommandPalette.tsx",
        "frontend/src/features/shell/KeyboardShortcuts.tsx"
      ],
      "owned_globs": [
        "backend/routers/websocket.py",
        "backend/routers/webhooks.py",
        "backend/routers/export.py",
        "frontend/src/features/shell/CommandPalette.tsx",
        "frontend/src/features/shell/KeyboardShortcuts.tsx"
      ],
      "note": "May also enhance existing components for drag UX, skeletons, mobile layout, theme polish. Coordinates with other leaves for WS integration."
    }
  }
}
```
