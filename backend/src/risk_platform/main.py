"""API composition root; business dependencies and authentication are explicitly injected."""

from fastapi import FastAPI

from risk_platform.api.routes.health import router as health_router


def create_app(*, entry_services=None, authenticate=None) -> FastAPI:
    app = FastAPI(title="Credit Risk Platform", version="0.1.0")
    app.state.entry_services = entry_services
    app.state.authenticate = authenticate
    from risk_platform.api.routes.entrypoints import router as entry_router
    app.include_router(entry_router)
    app.include_router(health_router)
    return app


app = create_app()
