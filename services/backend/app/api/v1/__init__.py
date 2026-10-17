"""Version 1 of the local API, mounted at /api/v1."""

from fastapi import APIRouter

from app.api.v1 import core

router = APIRouter(prefix="/api/v1")
router.include_router(core.router)
