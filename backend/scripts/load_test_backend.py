"""Synthetic BACKEND-capacity load test (Checkpoint 09 follow-up, Phase 5).

Measures our own code path -- queue, admission, dialer worker, PostgreSQL,
webhook ingestion, recovery scheduling -- against a FAKE Dograh that answers
instantly (or after --dograh-latency-ms). It says NOTHING about real Dograh
capacity or telephony/carrier capacity: no real call is ever placed.

Usage (from backend/, against a THROWAWAY database -- it TRUNCATES every table):
    PYTHONPATH=. PRIMARY_DB_URL=... REDIS_URL=... python scripts/load_test_backend.py \
        --contacts 100000 --workers 8 --webhook-threads 8

Every number printed is measured by this run; nothing is estimated.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

os.environ.setdefault("CALLING_ENGINE", "dograh")
os.environ.setdefault("DOGRAH_WEBHOOK_SECRET", "load-test-secret-not-real")
# Rate limiting is covered by its own tests; the load run must not be throttled by it.
os.environ.setdefault("WEBHOOK_RATE_LIMIT_PER_MINUTE", "100000000")
os.environ.setdefault("CIRCUIT_BREAKER_ERROR_THRESHOLD", "1000000")

import httpx  # noqa: E402
import redis  # noqa: E402
from sqlalchemy import create_engine, func, insert, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models.base import Base  # noqa: E402
from app.models.call_attempt import CallAttempt  # noqa: E402
from app.models.campaign import Campaign  # noqa: E402
from app.models.contact import Contact  # noqa: E402
from app.models.enums import CampaignStatus, ContactStatus  # noqa: E402
from app.models.retry_policy import RetryPolicy  # noqa: E402
from app.services.queue.admission_controller import AdmissionController  # noqa: E402
from app.services.queue.dialer_worker import process_claimed_job  # noqa: E402
from app.services.queue.job import DialJob  # noqa: E402
from app.services.queue.redis_queue import RedisStreamQueue  # noqa: E402
from app.services.recovery.dispatch import dispatch_due_recovery_jobs  # noqa: E402
from app.services.recovery.scheduler import RecoveryScheduler  # noqa: E402
from app.services.telephony.circuit_breaker import CircuitBreaker  # noqa: E402
from app.services.telephony.dograh_client import DograhTriggerResult  # noqa: E402
from app.services.telephony.mock_provider import MockTelephonyProvider  # noqa: E402

BIG = 10_000_000


class StageReport(dict):
    """Persists the report as each stage lands, so a crash or an environment
    reset mid-run never loses completed stages."""

    path = ""

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        print(f"STAGE {key}: {json.dumps(value)}", flush=True)
        if self.path:
            with open(self.path, "w") as fh:
                json.dump(self, fh, indent=2)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def lat(values: list[float]) -> dict:
    return {
        "p50_ms": round(pct(values, 0.50) * 1000, 2),
        "p95_ms": round(pct(values, 0.95) * 1000, 2),
        "p99_ms": round(pct(values, 0.99) * 1000, 2),
    }


class FakeDograh:
    def __init__(self, latency_s: float) -> None:
        self.latency_s = latency_s
        self.calls = 0
        self._lock = threading.Lock()
        self._next = 50_000_000

    def trigger_call(self, *, phone_number: str, initial_context: dict) -> DograhTriggerResult:
        if self.latency_s:
            time.sleep(self.latency_s)
        with self._lock:
            self.calls += 1
            self._next += 1
            run_id = self._next
        return DograhTriggerResult(run_id, f"run-{run_id}")


class QueueSampler(threading.Thread):
    """Samples depth (undelivered + pending) and oldest-unprocessed age."""

    def __init__(self, r: redis.Redis, stream: str, group: str) -> None:
        super().__init__(daemon=True)
        self.r, self.stream, self.group = r, stream, group
        self.stop_flag = threading.Event()
        self.max_depth = 0
        self.max_oldest_age_s = 0.0

    def run(self) -> None:
        while not self.stop_flag.wait(0.5):
            try:
                info = next(g for g in self.r.xinfo_groups(self.stream) if g["name"] == self.group)
                depth = int(info.get("lag") or 0) + int(info["pending"])
                self.max_depth = max(self.max_depth, depth)
                oldest_ms = None
                unread = self.r.xrange(self.stream, "(" + info["last-delivered-id"], "+", count=1)
                if unread:
                    oldest_ms = int(unread[0][0].split("-")[0])
                if info["pending"]:
                    pend = self.r.xpending_range(self.stream, self.group, "-", "+", 1)
                    if pend:
                        pms = int(pend[0]["message_id"].split("-")[0])
                        oldest_ms = pms if oldest_ms is None else min(oldest_ms, pms)
                if oldest_ms is not None:
                    age = time.time() - oldest_ms / 1000
                    self.max_oldest_age_s = max(self.max_oldest_age_s, age)
            except Exception:  # sampling must never break the run
                pass


def db_writes(engine) -> int:
    with engine.connect() as c:
        row = c.execute(
            text(
                "SELECT tup_inserted + tup_updated + tup_deleted "
                "FROM pg_stat_database WHERE datname = current_database()"
            )
        ).scalar_one()
    return int(row)


def main() -> int:  # noqa: C901 - a linear benchmark script
    ap = argparse.ArgumentParser()
    ap.add_argument("--contacts", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--webhook-threads", type=int, default=8)
    ap.add_argument("--dograh-latency-ms", type=float, default=0.0)
    ap.add_argument("--connect-rate", type=float, default=0.35)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    engine = create_engine(os.environ["PRIMARY_DB_URL"], pool_size=args.workers + 4)
    Session = sessionmaker(bind=engine)
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    r.flushdb()
    with engine.begin() as c:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        c.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")

    out = StageReport()
    out.path = args.json
    out["config"] = vars(args)
    n = args.contacts

    # ---- stage 1: campaign/contact load + queue ingestion ------------------
    with Session() as s:
        campaign = Campaign(name="load test", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        from datetime import time as dtime

        s.add(RetryPolicy(campaign_id=campaign.id, window_start=dtime(0, 0), window_end=dtime.max))
        s.commit()
        campaign_id = str(campaign.id)

    t0 = time.perf_counter()
    with Session() as s:
        for start in range(0, n, 5000):
            rows = [
                {
                    "campaign_id": campaign_id,
                    "phone_number": f"+1415{i:07d}",
                    "normalized_phone_number": f"+1415{i:07d}",
                    "status": ContactStatus.PENDING,
                }
                for i in range(start, min(n, start + 5000))
            ]
            s.execute(insert(Contact), rows)
        s.commit()
    contact_secs = time.perf_counter() - t0
    out["1_contact_load"] = {"contacts": n, "secs": round(contact_secs, 2),
                             "contacts_per_s": round(n / contact_secs)}

    with Session() as s:
        contact_ids = [str(x) for x in s.execute(select(Contact.id)).scalars()]
    queue = RedisStreamQueue(r, "load:calls", "load:workers")
    enq_lat: list[float] = []
    t0 = time.perf_counter()
    for cid in contact_ids:
        a = time.perf_counter()
        queue.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))
        enq_lat.append(time.perf_counter() - a)
    enq_secs = time.perf_counter() - t0
    out["2_queue_ingestion"] = {"jobs": n, "secs": round(enq_secs, 2),
                                "jobs_per_s": round(n / enq_secs), **lat(enq_lat)}

    # ---- stage 3: admission control (isolated) ------------------------------
    admission = AdmissionController(
        r, global_cps_limit=BIG, campaign_cps_limit=BIG, provider_cps_limit=BIG,
        global_concurrency_limit=BIG, campaign_concurrency_limit=BIG,
        provider_concurrency_limit=BIG,
    )
    adm_n = min(n, 20000)
    adm_lat = []
    t0 = time.perf_counter()
    for _ in range(adm_n):
        a = time.perf_counter()
        res = admission.try_admit(campaign_id=campaign_id, provider_name="dograh")
        admission.release(campaign_id=campaign_id, provider_name="dograh")
        adm_lat.append(time.perf_counter() - a)
        assert res.admitted
    adm_secs = time.perf_counter() - t0
    out["3_admission"] = {"ops": adm_n, "ops_per_s": round(adm_n / adm_secs), **lat(adm_lat)}
    r.delete("concurrency:global", f"concurrency:campaign:{campaign_id}",
             "concurrency:provider:dograh")

    # ---- stage 4: worker claim throughput (isolated, no processing) --------
    claim_q = RedisStreamQueue(r, "load:claimonly", "load:claimonly:g")
    claim_n = min(n, 20000)
    for cid in contact_ids[:claim_n]:
        claim_q.enqueue(DialJob.new(campaign_id=campaign_id, contact_id=cid, attempt_number=1))
    claim_lat: list[float] = []
    t0 = time.perf_counter()
    for _ in range(claim_n):
        a = time.perf_counter()
        got = claim_q.read_one("claim-bench", 10)
        claim_q.ack(got[0])
        claim_lat.append(time.perf_counter() - a)
    claim_secs = time.perf_counter() - t0
    out["4_worker_claim"] = {"claims": claim_n, "claims_per_s": round(claim_n / claim_secs),
                             **lat(claim_lat)}

    # ---- stage 5: full dial pipeline with N concurrent workers -------------
    fake = FakeDograh(args.dograh_latency_ms / 1000)
    import app.services.telephony.factory as factory

    factory.get_dograh_client = lambda: fake  # type: ignore[assignment]
    provider = MockTelephonyProvider()
    job_lat: list[float] = []
    lat_lock = threading.Lock()
    busy = [0.0] * args.workers
    errors = [0]
    done = [0]
    stop = threading.Event()
    sampler = QueueSampler(r, "load:calls", "load:workers")
    sampler.start()
    wr0 = db_writes(engine)

    def worker(idx: int) -> None:
        breaker = CircuitBreaker(r, "dograh")
        while not stop.is_set():
            got = queue.read_one(f"w{idx}", 200)
            if got is None:
                if done[0] + errors[0] >= n:
                    return
                continue
            a = time.perf_counter()
            try:
                with Session() as db:
                    process_claimed_job(db, queue, admission, provider, breaker, *got)
            except Exception:
                with lat_lock:
                    errors[0] += 1
                continue
            d = time.perf_counter() - a
            busy[idx] += d
            with lat_lock:
                job_lat.append(d)
                done[0] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    while done[0] + errors[0] < n and any(t.is_alive() for t in threads):
        time.sleep(1)
    dial_secs = time.perf_counter() - t0
    stop.set()
    for t in threads:
        t.join()
    sampler.stop_flag.set()
    dial_writes = db_writes(engine) - wr0

    with Session() as s:
        attempts = s.execute(select(func.count()).select_from(CallAttempt)).scalar_one()
        distinct_runs = s.execute(
            select(func.count(func.distinct(CallAttempt.provider_call_id)))
        ).scalar_one()
        distinct_pairs = s.execute(
            text("SELECT count(*) FROM (SELECT DISTINCT contact_id, attempt_number "
                 "FROM call_attempt) x")
        ).scalar_one()
    pending_left = r.xpending("load:calls", "load:workers")["pending"]
    out["5_dial_pipeline"] = {
        "jobs": n, "workers": args.workers, "secs": round(dial_secs, 2),
        "jobs_per_s": round(n / dial_secs, 1), **lat(job_lat),
        "worker_utilization_pct": round(100 * sum(busy) / (dial_secs * args.workers), 1),
        "db_writes_per_s": round(dial_writes / dial_secs),
        "max_queue_depth": sampler.max_depth,
        "max_oldest_queue_age_s": round(sampler.max_oldest_age_s, 1),
        "failed_jobs": errors[0], "error_rate_pct": round(100 * errors[0] / n, 3),
        "unacked_pending_after": pending_left,
        "call_attempts": attempts, "fake_dograh_triggers": fake.calls,
        "duplicate_attempts": attempts - distinct_pairs,
        "duplicate_runs": attempts - distinct_runs,
        "duplicate_dials": fake.calls - attempts,
    }

    # ---- stage 6: webhook ingestion against a REAL uvicorn server ----------
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port),
         "--log-level", "warning"],
        env=os.environ.copy(),
    )
    try:
        base = f"http://127.0.0.1:{args.port}"
        for _ in range(60):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        with Session() as s:
            rows = s.execute(select(CallAttempt.id, CallAttempt.provider_call_id)).all()
        n_connected = int(len(rows) * args.connect_rate)
        statuses = (["user_hangup"] * n_connected
                    + ["no_answer"] * (len(rows) - n_connected))
        work = list(zip(rows, statuses, strict=True))
        headers = {"Authorization": f"Bearer {os.environ['DOGRAH_WEBHOOK_SECRET']}"}
        wh_lat: list[float] = []
        wh_err = [0]
        idx = [0]
        lock = threading.Lock()
        wr0 = db_writes(engine)

        def sender() -> None:
            with httpx.Client(timeout=30) as c:
                while True:
                    with lock:
                        if idx[0] >= len(work):
                            return
                        (attempt_id, run_id), status = work[idx[0]]
                        idx[0] += 1
                    body = {"call_attempt_id": str(attempt_id), "workflow_run_id": int(run_id),
                            "call_status": status}
                    a = time.perf_counter()
                    try:
                        resp = c.post(f"{base}/api/v1/webhooks/dograh/call-completed",
                                      json=body, headers=headers)
                        ok = resp.status_code == 200
                    except httpx.HTTPError:
                        ok = False
                    d = time.perf_counter() - a
                    with lock:
                        wh_lat.append(d)
                        if not ok:
                            wh_err[0] += 1

        threads = [threading.Thread(target=sender) for _ in range(args.webhook_threads)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wh_secs = time.perf_counter() - t0
        wh_writes = db_writes(engine) - wr0

        # replay protection under load: re-send a sample, expect already_processed
        replay = work[: min(500, len(work))]
        replay_ok = 0
        with httpx.Client(timeout=30) as c:
            for (attempt_id, run_id), status in replay:
                resp = c.post(
                    f"{base}/api/v1/webhooks/dograh/call-completed",
                    json={"call_attempt_id": str(attempt_id), "workflow_run_id": int(run_id),
                          "call_status": status},
                    headers=headers,
                )
                replay_ok += int(resp.status_code == 200
                                 and resp.json()["outcome"] == "already_processed")
    finally:
        server.terminate()
        server.wait(timeout=10)

    with Session() as s:
        processed = s.execute(text("SELECT count(*) FROM processed_event")).scalar_one()
        analysis_audit = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'analysis.queued'")
        ).scalar_one()
        recov_audit = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'recovery.scheduled'")
        ).scalar_one()
    out["6_webhook_ingestion"] = {
        "events": len(work), "threads": args.webhook_threads, "secs": round(wh_secs, 2),
        "events_per_s": round(len(work) / wh_secs, 1), **lat(wh_lat),
        "db_writes_per_s": round(wh_writes / wh_secs),
        "errors": wh_err[0], "error_rate_pct": round(100 * wh_err[0] / len(work), 3),
        "processed_event_rows": processed,
        "replay_sample": len(replay), "replay_idempotent": replay_ok,
        "analysis_enqueued": analysis_audit, "retries_scheduled": recov_audit,
        "note": "HTTP over loopback to a single uvicorn worker sharing 1 CPU with the load "
                "generator, Postgres and Redis",
    }

    # ---- stage 7: recovery dispatch (retry scheduling -> dial queue) -------
    scheduler = RecoveryScheduler(r)
    scheduled = r.zcard("recovery:scheduled")
    rq = RedisStreamQueue(r, "load:retry", "load:retry:g")
    # dispatch_due_recovery_jobs handles at most 50 jobs per call (the worker
    # calls it every 10th loop iteration), so drain by calling it repeatedly.
    future = datetime.now(UTC) + timedelta(seconds=45)
    dispatched = calls = 0
    t0 = time.perf_counter()
    while True:
        got = dispatch_due_recovery_jobs(scheduler, rq, now=future)
        calls += 1
        if got == 0:
            break
        dispatched += got
    disp_secs = time.perf_counter() - t0
    with Session() as s:
        per_contact_max = s.execute(
            text("SELECT max(c) FROM (SELECT count(*) c FROM call_attempt GROUP BY contact_id) x")
        ).scalar_one()
    out["7_recovery_dispatch"] = {
        "scheduled_retries": scheduled, "dispatched": dispatched, "calls": calls,
        "secs": round(disp_secs, 2),
        "drain_rate_per_s": round(dispatched / disp_secs) if disp_secs else None,
        "dispatch_loop_cap_per_s_when_idle": "derived from code, not measured: 50 jobs per call, "
        "one call per 10 worker-loop iterations, each idle iteration blocks up to 1s => ~5/s",
        "attempts_per_contact_max_so_far": per_contact_max,
    }

    print(json.dumps(out, indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
