from __future__ import annotations


def render_home() -> str:
    return "<h1>Team Tasks</h1><p>Use /login then /tasks or /labels.</p>"


def render_login(username: str) -> str:
    return f"<h1>Logged in</h1><p>{username}</p>"
