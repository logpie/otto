from __future__ import annotations

import secrets


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"
