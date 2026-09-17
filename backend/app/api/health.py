"""Health check endpoint.

Reports basic service liveness. Does not check downstream dependencies
(database, telephony/STT/LLM/TTS providers) -- those checks belong to a
readiness probe added when those integrations exist. Checkpoint 00 scope
is liveness only.
"""

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["Health"])


@router.get("/health")
def get_health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }
