"""FastAPI application entrypoint."""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api.routes.admin_auth import router as admin_auth_router
from app.api.routes.admin_call_attempts import router as admin_call_attempts_router
from app.api.routes.admin_campaigns import router as admin_campaigns_router
from app.api.routes.admin_contacts import router as admin_contacts_router
from app.api.routes.admin_dashboard import router as admin_dashboard_router
from app.api.routes.call_analysis import router as call_analysis_router
from app.api.routes.campaigns import router as campaigns_router
from app.api.routes.contacts import router as contacts_router
from app.api.routes.health import router as health_router
from app.api.routes.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError

settings = get_settings()

app = FastAPI(title=settings.app_name)

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

app.include_router(health_router)
app.include_router(contacts_router)
app.include_router(campaigns_router)
app.include_router(webhooks_router)
app.include_router(call_analysis_router)
app.include_router(admin_auth_router)
app.include_router(admin_dashboard_router)
app.include_router(admin_campaigns_router)
app.include_router(admin_contacts_router)
app.include_router(admin_call_attempts_router)


@app.exception_handler(NotFoundError)
def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": exc.message})


@app.exception_handler(ConflictError)
def handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message})


@app.exception_handler(ValidationError)
def handle_validation_error(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.message})


@app.exception_handler(IntegrityError)
def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    # A database constraint caught something the service layer didn't
    # (e.g. a race between two concurrent requests past the app-level
    # dedup check) -- surface it as a conflict rather than a 500.
    return JSONResponse(
        status_code=409, content={"detail": "The request conflicts with existing data"}
    )
