"""Bearer token authentication middleware."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from tbc_common.config import settings


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Validate Authorization: Bearer <token> header on every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Allow health check without auth
        if request.url.path in ("/health", "/"):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[len("Bearer "):]
        expected = (
            settings.mcp_bearer_token.get_secret_value()
            if settings.mcp_bearer_token
            else None
        )
        if expected is None or token != expected:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid bearer token"},
            )

        return await call_next(request)
