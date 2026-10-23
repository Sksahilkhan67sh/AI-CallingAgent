"""Single-host load smoke for the API (P2-8). NOT a Dograh test and NOT a capacity claim.

Starts the API the way production does (`python -m app.serve`, or plain `uvicorn` for a
checkout that predates the launcher), drives N concurrent clients for T seconds against ONE
endpoint, and prints status counts, pool-timeout counts and p50/p95/p99 latency as one line.

    cd backend
    python ../ops/loadtest/loadtest.py --endpoint slow --slow-ms 500 --concurrency 50 \
        --seconds 20 --workers 2 [--backend-dir PATH] [--env KEY=VALUE ...]

Endpoints: read (GET /api/v1/campaigns), write (POST /api/v1/campaigns), slow (a route that holds
a database connection for --slow-ms; served from ops/loadtest/slow_app.py).
The mutation rate limiter is lifted for the run, otherwise the write test measures HTTP 429s.
"""

import argparse
import asyncio
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent

p = argparse.ArgumentParser()
p.add_argument("--endpoint", choices=["read", "write", "slow"], default="read")
p.add_argument("--slow-ms", type=int, default=500)
p.add_argument("--concurrency", type=int, default=10)
p.add_argument("--seconds", type=float, default=10)
p.add_argument("--workers", type=int, default=2)
p.add_argument("--port", type=int, default=8021)
p.add_argument("--backend-dir", default=".")
p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="server env")
p.add_argument("--label", default="")
a = p.parse_args()

backend = Path(a.backend_dir).resolve()
BASE = f"http://127.0.0.1:{a.port}"
sys.path.insert(0, str(backend))
from app.services.admin.auth import AdminPrincipal, create_access_token  # noqa: E402

_TOKEN = create_access_token(AdminPrincipal(username="load", role="admin"))[0]
HDR = {"Authorization": f"Bearer {_TOKEN}"}


def server_command() -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ, WEB_CONCURRENCY=str(a.workers))
    env.setdefault("MUTATION_RATE_LIMIT_PER_MINUTE", "100000000")
    env.setdefault("API_RATE_LIMIT_PER_MINUTE", "100000000")
    for item in a.env:
        key, value = item.split("=", 1)
        env[key] = value
    app_path = "app.main:app"
    if a.endpoint == "slow":
        app_path = "slow_app:app"
        env["PYTHONPATH"] = os.pathsep.join([str(HERE), str(backend), env.get("PYTHONPATH", "")])
    if (backend / "app" / "serve.py").exists():
        cmd = [sys.executable, "-m", "app.serve", "--app", app_path, "--host", "127.0.0.1"]
        cmd += ["--port", str(a.port)]
    else:  # a checkout from before the P2-6 launcher
        cmd = [sys.executable, "-m", "uvicorn", app_path, "--host", "127.0.0.1"]
        cmd += ["--port", str(a.port), "--workers", str(a.workers), "--log-level", "warning"]
    return cmd, env


async def worker(c: httpx.AsyncClient, deadline: float, out: list, n: int) -> None:
    i = 0
    while time.monotonic() < deadline:
        t = time.monotonic()
        i += 1
        try:
            if a.endpoint == "read":
                r = await c.get(BASE + "/api/v1/campaigns?limit=50")
            elif a.endpoint == "write":
                r = await c.post(BASE + "/api/v1/campaigns",
                                 json={"name": f"load-{n}-{i}-{time.monotonic_ns()}"})
            else:
                r = await c.get(BASE + f"/_db_slow?ms={a.slow_ms}")
            out.append((r.status_code, time.monotonic() - t))
        except Exception as exc:  # noqa: BLE001
            out.append((type(exc).__name__, time.monotonic() - t))


async def main() -> None:
    # Kept open and read back after the run, so not a `with` block.
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)  # noqa: SIM115
    cmd, env = server_command()
    # Started before any load is applied, so blocking here costs nothing.
    srv = subprocess.Popen(cmd, cwd=backend, env=env, stdout=log, stderr=log)  # noqa: ASYNC220
    out: list = []
    try:
        limits = httpx.Limits(max_connections=a.concurrency + 5)
        async with httpx.AsyncClient(timeout=120, headers=HDR, limits=limits) as c:
            for _ in range(120):
                try:
                    if (await c.get(BASE + "/health", timeout=1)).status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.5)
            deadline = time.monotonic() + a.seconds
            await asyncio.gather(*[worker(c, deadline, out, n) for n in range(a.concurrency)])
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(60)
        except subprocess.TimeoutExpired:
            srv.kill()
    log.flush()
    text = Path(log.name).read_text()
    lat = sorted(d for _, d in out)

    def q(f: float) -> float:
        return lat[min(len(lat) - 1, int(f * len(lat)))] * 1000

    codes = Counter(str(s) for s, _ in out)
    five = sum(v for k, v in codes.items() if k.startswith("5"))
    shed = codes.get("503", 0)
    other = sum(v for k, v in codes.items() if k[0] not in "2345")
    pool_to = len(re.findall(r"QueuePool limit|database_unavailable", text))
    print(
        f"{a.label or 'run'} endpoint={a.endpoint}"
        f"{'(' + str(a.slow_ms) + 'ms)' if a.endpoint == 'slow' else ''} workers={a.workers} "
        f"conc={a.concurrency} secs={a.seconds:g} requests={len(out)} rps={len(out)/a.seconds:.0f} "
        f"codes={dict(codes)} 5xx={five} (503={shed}, other5xx={five - shed}) "
        f"client_errors={other} pool_timeouts={pool_to} "
        f"p50={q(.5):.0f}ms p95={q(.95):.0f}ms p99={q(.99):.0f}ms max={lat[-1]*1000:.0f}ms"
    )


asyncio.run(main())
