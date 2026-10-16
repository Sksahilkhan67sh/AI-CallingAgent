"""HF2 2.1 -- pin when ``get_db`` commits relative to the HTTP response.

Newer FastAPI releases changed *when* the exit code of a dependency with ``yield`` runs
(0.106 moved it before the response is sent; 0.118 moved it after, with an opt-in
``scope="function"`` to get the earlier behaviour back). ``get_db`` commits in that exit code,
so a version bump could silently turn

  * "the response is only sent once the work is persisted" into "the client may read before the
    commit lands", and
  * "a commit failure is an error response" into "a 2xx was already sent, the failure is only
    logged".

These tests drive the REAL app and the REAL ``get_db`` (no dependency override) through a raw
ASGI harness, so they observe the exact moment ``http.response.start`` is emitted and, from a
separate connection, what is committed at that moment. They were written against the
pre-upgrade behaviour and must hold unchanged after the upgrade.
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign
from app.models.retry_policy import RetryPolicy
from tests.conftest import _bearer, test_engine


def _committed_campaigns(name: str) -> int:
    """Rows visible to an independent connection, i.e. only what was truly COMMITTED."""
    with Session(test_engine) as other_connection:
        return len(
            other_connection.execute(select(Campaign).where(Campaign.name == name)).scalars().all()
        )


class _Observation:
    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.persisted_when_first_response_started: int | None = None
        self.body = b""
        self.exception: BaseException | None = None


def _post_campaign(name: str, probe_name: str) -> _Observation:
    payload = json.dumps({"name": name}).encode()
    headers = {"host": "testserver", "content-type": "application/json"} | _bearer("admin")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/campaigns",
        "raw_path": b"/api/v1/campaigns",
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        + [(b"content-length", str(len(payload)).encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    seen = _Observation()

    async def run() -> None:
        delivered = False
        disconnected = asyncio.Event()

        async def receive() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                seen.statuses.append(message["status"])
                if seen.persisted_when_first_response_started is None:
                    # The probe runs the instant the response would leave the server.
                    seen.persisted_when_first_response_started = _committed_campaigns(probe_name)
            elif message["type"] == "http.response.body":
                seen.body += message.get("body", b"")

        try:
            await app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted on by the caller
            seen.exception = exc
        finally:
            disconnected.set()

    asyncio.run(run())
    return seen


@pytest.fixture
def campaign_name():
    name = f"hf2-commit-timing-{uuid.uuid4().hex[:10]}"
    yield name
    # Real commits cannot be rolled back by the per-test transaction: remove the campaign and
    # the rows created with it (its retry policy and audit event) explicitly.
    with test_engine.begin() as conn:
        ids = select(Campaign.id).where(Campaign.name == name)
        conn.execute(delete(AuditLog).where(AuditLog.entity_id.in_(ids)))
        conn.execute(delete(RetryPolicy).where(RetryPolicy.campaign_id.in_(ids)))
        conn.execute(delete(Campaign).where(Campaign.name == name))


def test_state_changing_route_is_persisted_before_the_response_is_sent(campaign_name):
    seen = _post_campaign(campaign_name, probe_name=campaign_name)

    assert seen.exception is None
    assert seen.statuses == [201]
    # The decisive assertion: at the moment the 201 was emitted the row was already committed.
    assert seen.persisted_when_first_response_started == 1
    assert _committed_campaigns(campaign_name) == 1


def test_commit_failure_at_request_end_is_never_a_silent_success(campaign_name, monkeypatch):
    """The route work succeeds, then the commit in ``get_db`` fails. The client must get an
    error response -- never a 2xx -- and nothing may be persisted."""

    class _FailingCommitSession(Session):
        def commit(self) -> None:
            raise OperationalError("COMMIT", {}, Exception("simulated commit failure"))

    monkeypatch.setattr(
        "app.core.database.SessionLocal",
        sessionmaker(bind=test_engine, class_=_FailingCommitSession, autoflush=False),
    )

    seen = _post_campaign(campaign_name, probe_name=campaign_name)

    assert seen.statuses, "the app produced no response at all"
    assert not any(200 <= status < 300 for status in seen.statuses), seen.statuses
    assert seen.statuses[0] >= 500
    assert b'"name"' not in seen.body  # the created resource was not echoed back as a success
    assert _committed_campaigns(campaign_name) == 0


def _get_db_dependants(dependant, found):
    for sub in dependant.dependencies:
        if sub.call is get_db:
            found.append(sub)
        _get_db_dependants(sub, found)
    return found


def test_every_get_db_use_is_function_scoped_so_all_share_one_session():
    """A plain ``Depends(get_db)`` anywhere would (a) commit after the response is sent and
    (b) get its own session, because the dependency cache is keyed by scope."""
    from fastapi.routing import APIRoute

    found: list = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            _get_db_dependants(route.dependant, found)

    assert len(found) > 40  # the routes really were walked
    assert {dep.scope for dep in found} == {"function"}
