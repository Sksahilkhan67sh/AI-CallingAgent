"""Wiring for the recovery scheduler -- mirrors app/services/queue/factory.py."""

from app.core.redis_client import get_redis
from app.services.recovery.scheduler import RecoveryScheduler


def get_recovery_scheduler() -> RecoveryScheduler:
    return RecoveryScheduler(get_redis())
