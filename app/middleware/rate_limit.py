"""In-memory sliding-window rate limiter.

Key = `X-User-Id` header if sent, else client IP.
NOTE: in-memory => per process. With multiple uvicorn workers / servers,
move this to Redis (same algorithm with a sorted set).
"""
import math
import threading
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

EXEMPT_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")


class SlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.max_requests:
                retry = max(1, math.ceil(q[0] + self.window - now))
                return False, 0, retry
            q.append(now)
            return True, self.max_requests - len(q), 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int, window_seconds: int, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self.limiter = SlidingWindowLimiter(max_requests, window_seconds)

    async def dispatch(self, request: Request, call_next):
        if not self.enabled or request.url.path.startswith(EXEMPT_PATHS):
            return await call_next(request)

        key = request.headers.get("x-user-id") or (
            request.client.host if request.client else "anonymous"
        )
        allowed, remaining, retry_after = self.limiter.hit(key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "rate_limited", "message": "Too many requests"}},
                headers={"Retry-After": str(retry_after)},
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limiter.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
