"""FastAPI application entrypoint."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DisconnectionError, IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.routes.admin_auth import router as admin_auth_router
from app.api.routes.admin_call_attempts import router as admin_call_attempts_router
from app.api.routes.admin_campaigns import router as admin_campaigns_router
from app.api.routes.admin_contacts import router as admin_contacts_router
from app.api.routes.admin_dashboard import router as admin_dashboard_router
from app.api.routes.admin_data import router as admin_data_router
from app.api.routes.admin_kill_switch import router as admin_kill_switch_router
from app.api.routes.admin_provider_halt import router as admin_provider_halt_router
from app.api.routes.admin_spend import router as admin_spend_router
from app.api.routes.admin_suppressions import router as admin_suppressions_router
from app.api.routes.call_analysis import router as call_analysis_router
from app.api.routes.campaigns import router as campaigns_router
from app.api.routes.contacts import router as contacts_router
from app.api.routes.dograh_webhook import router as dograh_webhook_router
from app.api.routes.health import router as health_router
from app.api.routes.webhooks import router as webhooks_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import Settings, get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.request_context import RequestIdMiddleware

# CP10: httpx logs every request URL at INFO, and Dograh's trigger URL embeds the
# trigger UUID (and a transcript_url may be a pre-signed link). Keep those out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

# HF1 1.4: where the interactive docs and the OpenAPI schema (a free map of every route and
# payload) are NOT served.
_NO_DOCS_ENVIRONMENTS = frozenset({"staging", "production"})


def _json_safe(text: str) -> str:
    # A lone surrogate is valid JSON input but cannot be encoded back to UTF-8.
    return text.encode("utf-8", "replace").decode("utf-8")


def handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # CP11: FastAPI's default handler echoes each error's `input` back to the caller.
    # That crashed with HTTP 500 on a lone-surrogate string ("\ud800" -- valid JSON,
    # not encodable as UTF-8) and reflected request data (phone numbers) in the 422.
    # Same {"detail": [{type, loc, msg}]} shape, minus `input`/`ctx`/`url`.
    detail = [
        {
            "type": err["type"],
            "loc": [_json_safe(p) if isinstance(p, str) else p for p in err["loc"]],
            "msg": _json_safe(err["msg"]),
        }
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": exc.message})


def handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message})


def handle_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.message})


def handle_service_unavailable(request: Request, exc: ServiceUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": exc.message})


def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    # A database constraint caught something the service layer didn't
    # (e.g. a race between two concurrent requests past the app-level
    # dedup check) -- surface it as a conflict rather than a 500.
    return JSONResponse(
        status_code=409, content={"detail": "The request conflicts with existing data"}
    )


def handle_database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    # P2-2: a lost connection, a failover or an exhausted pool is the database being briefly
    # unavailable, not a bug: tell the client to retry instead of answering 500, and never echo
    # the driver message (it can carry host names, SQL and parameters).
    logging.getLogger("app.database").warning(
        "database_unavailable", extra={"error_type": type(exc).__name__}
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "The database is temporarily unavailable. Please retry shortly."},
        headers={"Retry-After": str(get_settings().db_retry_after_seconds)},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. HF1 1.4: in staging and production the interactive docs and
    the OpenAPI schema are disabled (404); development and test keep them."""
    settings = settings or get_settings()
    expose_docs = settings.environment not in _NO_DOCS_ENVIRONMENTS
    application = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )

    # CP11: added before CORS so CORS wraps it (a 413 still carries CORS headers).
    application.add_middleware(BodySizeLimitMiddleware)

    # Checkpoint 07: the Next.js admin dashboard is a separate origin
    # (typically :3000 vs the API's :8000). Only the configured admin
    # origins are allowed, and only for the admin surface's own needs --
    # this does not change how any existing non-browser API consumer works.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.admin_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CP11: outermost, so every response (including CORS preflight and 413s) carries a request id.
    application.add_middleware(RequestIdMiddleware)

    for router in (
        health_router,
        contacts_router,
        campaigns_router,
        webhooks_router,
        dograh_webhook_router,
        call_analysis_router,
        admin_auth_router,
        admin_dashboard_router,
        admin_kill_switch_router,
        admin_provider_halt_router,
        admin_suppressions_router,
        admin_data_router,
        admin_spend_router,
        admin_campaigns_router,
        admin_contacts_router,
        admin_call_attempts_router,
    ):
        application.include_router(router)

    application.add_exception_handler(RequestValidationError, handle_request_validation_error)  # type: ignore[arg-type]
    application.add_exception_handler(NotFoundError, handle_not_found)  # type: ignore[arg-type]
    application.add_exception_handler(ConflictError, handle_conflict)  # type: ignore[arg-type]
    application.add_exception_handler(ValidationError, handle_validation_error)  # type: ignore[arg-type]
    application.add_exception_handler(
        ServiceUnavailableError,
        handle_service_unavailable,  # type: ignore[arg-type]
    )
    application.add_exception_handler(IntegrityError, handle_integrity_error)  # type: ignore[arg-type]
    for db_error in (OperationalError, DisconnectionError, PoolTimeoutError):
        application.add_exception_handler(db_error, handle_database_unavailable)
    return application


settings = get_settings()
app = create_app(settings)
