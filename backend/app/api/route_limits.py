"""CP11 per-principal rate-limit budgets for authenticated routes.

One place so the budgets read as a table. All are keyed on the VERIFIED token
subject (not IP / headers) and counted per budget, not per route: e.g. every
mutation route draws from the same `mutation` bucket.

| budget   | default/min | on Redis failure            | used by                              |
|----------|-------------|-----------------------------|--------------------------------------|
| enqueue  | 5           | fail CLOSED (503)           | POST /campaigns/{id}/enqueue         |
| import   | 5           | fail CLOSED (503)           | POST /campaigns/import               |
| mutation | 120         | fail open + audit           | create/update/associate/remove/      |
|          |             |                             | deactivate, status, kill switch      |
| analysis | 120         | fail open + audit           | the three analysis GETs              |

Enqueue and import are the abuse-critical, expensive ones, so an unreadable
limiter blocks them (user-decided CP11 policy). Ordinary DB mutations and reads
stay available if the limiter's Redis is down, with a throttled audit row.
"""

from fastapi import Depends

from app.api.admin_deps import principal_identity
from app.core.config import get_settings
from app.core.rate_limit import rate_limit_dependency

ENQUEUE_LIMIT = Depends(
    rate_limit_dependency(
        limit=lambda: get_settings().enqueue_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="enqueue",
        fail_closed=True,
        identity=principal_identity,
    )
)
IMPORT_LIMIT = Depends(
    rate_limit_dependency(
        limit=lambda: get_settings().import_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="import",
        fail_closed=True,
        identity=principal_identity,
    )
)
MUTATION_LIMIT = Depends(
    rate_limit_dependency(
        limit=lambda: get_settings().mutation_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="mutation",
        identity=principal_identity,
    )
)
ANALYSIS_LIMIT = Depends(
    rate_limit_dependency(
        limit=lambda: get_settings().analysis_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="analysis",
        identity=principal_identity,
    )
)
