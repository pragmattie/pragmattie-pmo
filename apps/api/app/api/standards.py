"""Serve the standards traceability map read-only. Permissions arrive with roles in milestone P2.

The map is one small document, not a growing list, so it is returned whole rather than paged.
It is loaded once per process (`get_standards`) and clients may cache it too.
"""

from fastapi import APIRouter, Response

from app.standards import StandardsMap, get_standards

router = APIRouter(tags=["standards"])

CACHE_CONTROL = "public, max-age=3600"


@router.get("/standards", response_model=StandardsMap)
def read_standards(response: Response) -> StandardsMap:
    response.headers["Cache-Control"] = CACHE_CONTROL
    return get_standards()
