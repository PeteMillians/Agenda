"""Errors the application raises; the API turns each into the spec §08 error envelope."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, details: dict[str, Any] | None = None, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code


class NotFound(AppError):
    """Missing, or in a space the caller cannot access. Always 404, never 403 (spec §08)."""

    status, code = 404, "NOT_FOUND"

    def __init__(self, what: str = "Resource"):
        super().__init__(f"{what} not found.")


class Forbidden(AppError):
    status, code = 403, "FORBIDDEN"


class Conflict(AppError):
    status, code = 409, "CONFLICT"


class RevisionConflict(Conflict):
    code = "REVISION_CONFLICT"

    def __init__(self, resource_id: str, current_revision: int):
        super().__init__(
            "The resource changed after this version was loaded.",
            {"resource_id": resource_id, "current_revision": current_revision},
        )


class Invalid(AppError):
    """Schema-valid input that breaks a domain rule."""

    status, code = 422, "INVALID"


class PreconditionRequired(AppError):
    status, code = 428, "PRECONDITION_REQUIRED"


class Unavailable(AppError):
    status, code = 503, "NOT_READY"
