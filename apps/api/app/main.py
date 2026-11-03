"""FastAPI application entry point."""

from fastapi import APIRouter, Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app import __version__
from app.api import (
    audit,
    auth,
    gates,
    health,
    intake,
    portfolio,
    risk_library,
    risks,
    standards,
    users,
)
from app.config import get_settings
from app.db import is_lock_conflict
from app.headers import BodyLimitMiddleware, SecurityHeadersMiddleware

LOCK_CONFLICT = "the data changed while you were saving; try again"


async def _lock_conflict(request: Request, exc: Exception) -> JSONResponse:
    """A deadlock or lock timeout is a clash with a parallel request, not a crash: 409. The
    session is rolled back when the request's `get_db` closes it. Any other database error is
    raised again and stays a 500."""
    if isinstance(exc, OperationalError) and is_lock_conflict(exc):
        return JSONResponse({"detail": LOCK_CONFLICT}, status_code=status.HTTP_409_CONFLICT)
    raise exc


def create_app() -> FastAPI:
    settings = get_settings()
    # The interactive docs and the OpenAPI schema are served only in dev and test.
    docs = settings.app_env in ("dev", "test")
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last, so they run first: an oversized body is refused before CORS or any route sees
    # it, and the security headers go on every response, the 413 included.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(OperationalError, _lock_conflict)
    # Every unsafe method under /api/v1 needs the CSRF header (login excepted). Every route also
    # needs a guard from app/authz.py, except those on OPEN_PATHS; a test checks this.
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(auth.require_csrf)])
    api.include_router(health.router)
    api.include_router(auth.router)
    api.include_router(audit.router)
    api.include_router(intake.router)
    api.include_router(gates.router)
    api.include_router(portfolio.router)
    api.include_router(risk_library.router)
    api.include_router(risks.router)
    api.include_router(risks.escalations_router)
    api.include_router(standards.router)
    api.include_router(users.router)
    app.include_router(api)
    return app


app = create_app()
