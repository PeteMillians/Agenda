"""Version 1 of the local API, mounted at /api/v1."""

from fastapi import APIRouter

from app.api.v1 import core, events, jobs, lists, tasks

router = APIRouter(prefix="/api/v1")
for module in (core, tasks, lists, events, jobs):
    router.include_router(module.router)
