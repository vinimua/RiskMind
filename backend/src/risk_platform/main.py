"""Minimal API process entry point; business routes will be added by phase."""

from fastapi import FastAPI

from risk_platform.api.routes.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="Credit Risk Platform", version="0.1.0")
    app.include_router(health_router)
    return app


app = create_app()
