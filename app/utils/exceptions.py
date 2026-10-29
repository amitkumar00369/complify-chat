"""Typed application errors + FastAPI handlers -> {"error": {"code", "message"}}."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedFileError(AppError):
    status_code = 415
    code = "unsupported_file"


class ConfigError(AppError):
    status_code = 500
    code = "config_error"


class StorageError(AppError):
    status_code = 502
    code = "storage_error"


class AIServiceError(AppError):
    status_code = 502
    code = "ai_service_error"


def _err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}, headers=headers
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        if exc.status_code >= 500:
            logger.error("%s: %s", exc.code, exc.message)
        return _err(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return _err(422, "validation_error", "; ".join(msgs))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error")
        return _err(500, "internal_error", "Something went wrong")
