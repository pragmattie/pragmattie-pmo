"""FastAPI application entry point."""

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import audit, auth, health, portfolio, standards
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Every unsafe method under /api/v1 needs the CSRF header (login excepted).
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(auth.require_csrf)])
    api.include_router(health.router)
    api.include_router(auth.router)
    api.include_router(audit.router)
    api.include_router(portfolio.router)
    api.include_router(standards.router)
    app.include_router(api)
    return app


app = create_app()
