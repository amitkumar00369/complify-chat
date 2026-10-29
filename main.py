from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.config.settings import get_settings
from app.db.database import init_db
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_timing import RequestTimingMiddleware
from app.utils.exceptions import register_exception_handlers
from app.utils.logger import setup_logging

settings = get_settings()
setup_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

# last added = outermost: timing wraps rate-limit so 429s get logged too
app.add_middleware(
    RateLimitMiddleware,
    max_requests=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    enabled=settings.rate_limit_enabled,
)
app.add_middleware(RequestTimingMiddleware)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "storage": settings.storage_backend, "model": settings.openai_model}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.environment == "dev")
