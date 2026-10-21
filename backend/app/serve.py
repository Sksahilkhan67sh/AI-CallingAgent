"""Start the API through uvicorn with the P2-6 settings (`python -m app.serve`).

`uvicorn --workers` was chosen over gunicorn + uvicorn workers (docs/hardening-p2/SHUTDOWN.md): it
needs no new dependency and already provides multiple processes, a graceful-shutdown timeout and
a keep-alive timeout. Each worker process imports the app, so each has its own engine and pool.
"""

import argparse

import uvicorn

from app.core.config import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the API with the configured concurrency.")
    parser.add_argument("--app", default="app.main:app", help="ASGI app (default: the API)")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104  (container: published by compose)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    s = get_settings()
    uvicorn.run(
        args.app,
        host=args.host,
        port=args.port,
        workers=s.web_concurrency,
        timeout_graceful_shutdown=s.web_graceful_shutdown_seconds,
        timeout_keep_alive=s.web_keep_alive_seconds,
    )


if __name__ == "__main__":
    main()
