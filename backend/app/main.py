"""FastAPI application entrypoint."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api.routes.admin_auth import router as admin_auth_router
from app.api.routes.admin_call_attempts import router as admin_call_attempts_router
from app.api.routes.admin_campaigns import router as admin_campaigns_router
from app.api.routes.admin_contacts import router as admin_contacts_router
from app.api.routes.admin_dashboard import router as admin_dashboard_router
from app.api.routes.admin_kill_switch import router as admin_kill_switch_router
from app.api.routes.admin_spend import router as admin_spend_router
from app.api.routes.admin_suppressions import router as admin_suppressions_router
from app.api.routes.call_analysis import router as call_analysis_router
from app.api.routes.campaigns import router as campaigns_router
from app.api.routes.contacts import router as contacts_router
from app.api.routes.dograh_webhook import router as dograh_webhook_router
from app.api.routes.health import router as health_router
from app.api.routes.webhooks import router as webhooks_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.request_context import RequestIdMiddleware

settings = get_settings()

# CP10: httpx logs every request URL at INFO, and Dograh's trigger URL embeds the
# trigger UUID (and a transcript_url may be a pre-signed link). Keep those out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title=settings.app_name)

# CP11: added before CORS so CORS wraps it (a 413 still carries CORS headers).
app.add_middleware(BodySizeLimitMiddleware)

# Checkpoint 07: the Next.js admin dashboard is a separate origin
# (typically :3000 vs the API's :8000). Only the configured admin
# origins are allowed, and only for the admin surface's own needs --
# this does not change how any existing non-browser API consumer works.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.admin_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CP11: outermost, so every response (including CORS preflight and 413s) carries a request id.
app.add_middleware(RequestIdMiddleware)

app.include_router(health_router)
app.include_router(contacts_router)
app.include_router(campaigns_router)
app.include_router(webhooks_router)
app.include_router(dograh_webhook_router)
app.include_router(call_analysis_router)
app.include_router(admin_auth_router)
app.include_router(admin_dashboard_router)
app.include_router(admin_kill_switch_router)
app.include_router(admin_suppressions_router)
app.include_router(admin_spend_router)
app.include_router(admin_campaigns_router)
app.include_router(admin_contacts_router)
app.include_router(admin_call_attempts_router)


def _json_safe(text: str) -> str:
    # A lone surrogate is valid JSON input but cannot be encoded back to UTF-8.
    return text.encode("utf-8", "replace").decode("utf-8")


@app.exception_handler(RequestValidationError)
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


@app.exception_handler(NotFoundError)
def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": exc.message})


@app.exception_handler(ConflictError)
def handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message})


@app.exception_handler(ValidationError)
def handle_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.message})


@app.exception_handler(ServiceUnavailableError)
def handle_service_unavailable(request: Request, exc: ServiceUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": exc.message})


@app.exception_handler(IntegrityError)
def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    # A database constraint caught something the service layer didn't
    # (e.g. a race between two concurrent requests past the app-level
    # dedup check) -- surface it as a conflict rather than a 500.
    return JSONResponse(
        status_code=409, content={"detail": "The request conflicts with existing data"}
    )
