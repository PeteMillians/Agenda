"""Local API boundary: CORS, Host header, and the per-launch bearer secret.

Middleware order matters. Starlette runs the last-added middleware first, so
install_boundary adds them innermost-first:
  CORS (answers preflight) -> request id -> Host check -> bearer check -> routes
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import error_response
from app.config import Settings

TAURI_ORIGINS = ["tauri://localhost", "http://tauri.localhost"]  # macOS/Linux, Windows
DEV_ORIGINS = ["http://localhost:1420"]  # Vite under `tauri dev`

# The OAuth callback is opened by the system browser, which has no launch secret; it is
# protected by a one-time state instead (spec §07).
PUBLIC_PATHS = {"/health/live", "/health/ready", "/api/v1/integrations/google/callback"}
DEV_PUBLIC_PATHS = {"/docs", "/openapi.json"}


def install_boundary(app: FastAPI, settings: Settings) -> None:
    expected_host = f"127.0.0.1:{settings.port}"
    expected_auth = f"Bearer {settings.token}".encode()
    public = PUBLIC_PATHS | (DEV_PUBLIC_PATHS if settings.dev else set())

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.headers.get("host") != expected_host:  # DNS-rebinding guard
            return error_response(request, 400, "BAD_HOST", "Unexpected Host header.")
        if request.url.path not in public:
            supplied = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(supplied, expected_auth):
                return error_response(request, 401, "UNAUTHORIZED", "Missing or invalid launch secret.")
        return await call_next(request)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = f"req_{secrets.token_hex(8)}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=TAURI_ORIGINS + (DEV_ORIGINS if settings.dev else []),
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
        expose_headers=["X-Request-ID"],
    )
