# TODO Web App

Build a small full-stack TODO web app with a FastAPI backend and a minimal
React frontend.

Required behavior:

- Backend: FastAPI app with JSON-file persistence, no external database.
- API endpoints:
  - `GET /api/todos`
  - `POST /api/todos` with `{ "text": "..." }`
  - `PATCH /api/todos/{id}` with `{ "done": true|false }`
  - `DELETE /api/todos/{id}`
- Frontend: React UI for adding, listing, completing, filtering, and deleting
  todos.
- Filters: all, active, completed.
- Empty states and error states should be visible and understandable.
- Include `start.sh` at the repo root. It must make the user-facing app
  available on `$PORT`. If a separate backend port is needed, use `$API_PORT`
  or `$BACKEND_PORT`.
- Include `tests/run_acceptance.py` that verifies API CRUD, persistence across
  process restart or file reload, and the built/served frontend shell.

Keep the implementation intentionally small: one backend service, one frontend
surface, local files only.
