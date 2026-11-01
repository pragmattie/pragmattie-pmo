"""Serve the standards traceability map read-only. Needs `standards:read`, which every role has.

The map is one small document, not a growing list, so it is returned whole rather than paged.
It is loaded once per process (`get_standards`). It needs a login, so only the browser may cache
it (`private`), never a shared cache.
"""

from fastapi import APIRouter, Depends, Response

from app.authz import Action, require
from app.standards import StandardsMap, get_standards

router = APIRouter(tags=["standards"], dependencies=[Depends(require(Action.STANDARDS_READ))])

CACHE_CONTROL = "private, max-age=3600"


@router.get("/standards", response_model=StandardsMap)
def read_standards(response: Response) -> StandardsMap:
    response.headers["Cache-Control"] = CACHE_CONTROL
    return get_standards()
