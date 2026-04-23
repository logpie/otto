from __future__ import annotations

from fastapi import FastAPI

from .api import router


def create_app() -> FastAPI:
    app = FastAPI(title="Nightly N8")
    app.include_router(router)
    return app


app = create_app()
