Maintain a small multi-user FastAPI task tracker with SQLite persistence.

Users can log in, create tasks, list their own tasks, and view label-related
pages. Keep user data isolated, preserve the visible task-label behavior, and
avoid adding expensive query patterns as the label workflow evolves.
