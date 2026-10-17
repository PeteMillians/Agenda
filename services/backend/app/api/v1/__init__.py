"""Version 1 of the local API, mounted at /api/v1."""

from fastapi import APIRouter

from app.api.v1 import core, dashboards, events, integrations, jobs, lists, tasks

router = APIRouter(prefix="/api/v1")
for module in (core, dashboards, tasks, lists, events, jobs, integrations):
    router.include_router(module.router)
