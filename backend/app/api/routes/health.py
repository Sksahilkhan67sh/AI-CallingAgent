"""Health check endpoints -- Checkpoint 00 (liveness), hardened in
Checkpoint 09 §9 with a separate readiness probe.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.schemas.admin import ComponentHealth
from app.services.admin.system_service import check_postgres, check_redis

router = APIRouter(tags=["Health"])


@router.get("/health")
def get_health() -> dict:
    """Liveness: the process is up and can respond -- never fails
    merely because a downstream dependency (Postgres, Redis) is
    temporarily unavailable. An orchestrator restarting the process
    over a transient DB blip would make an outage worse, not better."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }


@router.get("/ready")
def get_readiness(response: Response, db: Session = Depends(get_db)) -> dict:
    """Readiness: are this process's actually-required dependencies
    reachable right now. Scoped to Postgres and Redis -- the two
    dependencies every request path needs regardless of
    calling_engine. Deliberately does NOT make a live call to Dograh
    here: that would make a frequently-polled probe slow and add an
    external-network dependency to something an orchestrator hits
    every few seconds; Dograh's own reachability is instead surfaced
    through its circuit breaker state and the admin dashboard's system
    page (Checkpoint 07), not this probe. Reuses the same component
    checks the admin dashboard's system page already uses -- not
    duplicated.
    """
    components = [check_postgres(db), check_redis()]
    # CP10: with calling_engine=dograh a missing credential means no call can
    # ever be placed -- report it here (no network call, just the settings)
    # instead of letting the first dial discover it.
    settings = get_settings()
    if settings.calling_engine == "dograh":
        configured = bool(settings.dograh_api_key and settings.dograh_trigger_uuid)
        components.append(
            ComponentHealth(
                name="Dograh configuration",
                status="ok" if configured else "degraded",
                detail=None if configured else "DOGRAH_API_KEY / DOGRAH_TRIGGER_UUID not set",
            )
        )
    all_ok = all(c.status == "ok" for c in components)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if all_ok else "not_ready",
        "components": [{"name": c.name, "status": c.status} for c in components],
    }
