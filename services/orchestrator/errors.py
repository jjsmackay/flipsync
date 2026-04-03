"""Custom error handling for spec-compliant error responses.

Spec format: {"error": "snake_case", "message": "Human-readable.", "detail": {}}
FastAPI's default HTTPException wraps under {"detail": {...}} — this module
provides a custom exception that returns the flat format directly.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Raise to return a spec-compliant error response."""

    def __init__(self, status_code: int, error: str, message: str, detail: dict | None = None):
        self.status_code = status_code
        self.error = error
        self.message = message
        self.detail = detail or {}


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "message": exc.message, "detail": exc.detail},
    )
