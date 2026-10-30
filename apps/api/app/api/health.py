"""Health check: the one route that needs no permission."""

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    version: str


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", version=__version__)
