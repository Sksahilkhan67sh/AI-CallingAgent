"""Health endpoints -- Checkpoint 09.

  /health, /health/live  LIVENESS: the process is up. Never touches a
                         dependency, so a database blip cannot make an
                         orchestrator kill a healthy process.
  /health/ready          READINESS: PostgreSQL and Redis (the dependencies
                         required to serve traffic) answer. 503 otherwise.
                         Reports component names and ok/fail only -- never
                         hostnames, URLs, credentials or exception text.
"""

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.redis_client import get_redis

router = APIRouter(tags=["Health"])


@router.get("/health")
@router.get("/health/live")
def get_health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }


def _postgres_ok() -> bool:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _redis_ok() -> bool:
    try:
        return bool(get_redis().ping())
    except Exception:
        return False


@router.get("/health/ready")
def get_readiness(response: Response) -> dict:
    checks = {"postgres": _postgres_ok(), "redis": _redis_ok()}
    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "checks": {name: "ok" if ok else "fail" for name, ok in checks.items()},
    }
