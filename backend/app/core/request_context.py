"""CP11 -- request correlation ID.

Every HTTP request gets a `request_id` (the caller's `X-Request-ID` if it is a
safe token, otherwise a fresh UUID4). It is echoed back in the `X-Request-ID`
response header, stored in a contextvar, stamped on every log record, and copied
into security audit rows, so one request can be followed across logs and audit.

The caller-supplied value is untrusted input that ends up in logs, so it is only
accepted when it is short and made of [A-Za-z0-9._-]; anything else is replaced
(never sanitised-and-kept), which rules out log injection via this header.
"""

import logging
import re
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HEADER = "X-Request-ID"

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return request_id_var.get()


def _install_log_record_factory() -> None:
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_cp11_request_id", False):
        return

    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = previous(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    factory._cp11_request_id = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


_install_log_record_factory()


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = dict(scope["headers"]).get(HEADER.lower().encode(), b"").decode("latin-1")
        request_id = supplied if _SAFE_ID.match(supplied) else str(uuid.uuid4())
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((HEADER.lower().encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
