"""CP11 -- request body size cap (pure ASGI, so it works before FastAPI reads,
buffers and parses the body, and applies to every route including unauthenticated
ones such as login and the provider webhooks).

Two ways a body can be oversized, both handled:
* it declares a Content-Length over the cap  -> 413 immediately, body never read;
* it streams (chunked / no or lying Content-Length) -> bytes are counted as they
  are received and the request is aborted with 413 the moment the cap is crossed,
  so memory is bounded by the cap, not by what the client chooses to send.

Caps (settings): MAX_REQUEST_BODY_BYTES (default 1 MiB) for everything, and the
larger MAX_IMPORT_BODY_BYTES (default 6 MiB: a 5 MiB CSV + multipart overhead)
for the one upload route. The import route additionally enforces its own 5 MiB
*file* limit and a 10,000-row cap.
"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

_IMPORT_PATHS = frozenset(
    {"/api/v1/campaigns/import", "/api/v1/admin/suppressions/import"}
)


class _BodyTooLarge(Exception):
    pass


def _limit_for(path: str) -> int:
    settings = get_settings()
    if path.rstrip("/") in _IMPORT_PATHS:
        return settings.max_import_body_bytes
    return settings.max_request_body_bytes


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = _limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send)
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                # FastAPI catches any exception raised while reading the body and answers
                # 400 "error parsing the body". Whatever the app tries to say once the cap
                # was crossed is replaced by one honest 413.
                if not response_started:
                    response_started = True
                    await self._reject(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        if exceeded and not response_started:  # app raised/returned without answering
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)
