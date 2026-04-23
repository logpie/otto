from __future__ import annotations


def login_page() -> str:
    return "<h1>Sign in</h1><p>POST /login with email and password.</p>"


def account_page(email: str) -> str:
    return f"<h1>Account</h1><p>{email}</p>"
