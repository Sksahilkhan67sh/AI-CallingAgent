"""Tiny app the P2-6 tests serve through the real launcher: reports its PID, can be slow."""

import os
import time

from fastapi import FastAPI

app = FastAPI()


@app.get("/pid")
def pid() -> dict[str, int]:
    time.sleep(0.05)  # keep the worker busy long enough for connections to spread
    return {"pid": os.getpid()}


@app.get("/slow")
def slow(seconds: float = 2.0) -> dict[str, float]:
    time.sleep(seconds)
    return {"slept": seconds}
