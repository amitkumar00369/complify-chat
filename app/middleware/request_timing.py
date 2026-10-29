import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("app.request")


class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Adds X-Request-Id / X-Process-Time headers and logs each request."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Process-Time"] = f"{elapsed_ms:.1f}ms"
        logger.info(
            "%s %s -> %s (%.1fms) id=%s",
            request.method, request.url.path, response.status_code, elapsed_ms, request_id,
        )
        return response
