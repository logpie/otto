Seeded FastAPI todo app with login, SQLite persistence, task labels, and label
pages. Use this fixture to validate that Otto can build a user-visible feature,
find and fix a cross-user isolation bug, then optimize a measurable performance
target without losing the original behavior.
Maintain a small multi-user FastAPI task tracker with SQLite persistence.

Users can log in, create tasks, list their own tasks, and view label-related
pages. Keep user data isolated, preserve the visible task-label behavior, and
avoid adding expensive query patterns as the label workflow evolves.
