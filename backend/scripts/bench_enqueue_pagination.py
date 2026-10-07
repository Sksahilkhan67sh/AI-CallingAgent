"""CP12-C benchmark: enqueue one campaign of N PENDING contacts and report what it cost.

    PRIMARY_DB_URL=... REDIS_URL=redis://localhost:6379/4 \
        python scripts/bench_enqueue_pagination.py 100000 [--tracemalloc]

Creates its own campaign + contacts (so point it at a scratch database / Redis DB), flushes
the Redis DB it is given, runs QueueEnqueueService in-process and prints one result line.
Run each size in a fresh process: peak RSS is only meaningful per process. `--tracemalloc`
adds the Python-level peak but slows the run, so it is reported separately from timing.
Numbers describe THIS machine only; they are not production throughput.
"""

import os
import resource
import sys
import time
import tracemalloc
import uuid

import redis
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.services.queue.enqueue_service import QueueEnqueueService
from app.services.queue.factory import get_queue


def main() -> None:
    count = int(sys.argv[1])
    use_tracemalloc = "--tracemalloc" in sys.argv
    settings = get_settings()
    redis.Redis.from_url(settings.redis_url).flushdb()
    engine = create_engine(settings.primary_db_url)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as s:
        campaign = Campaign(name=f"bench {count}", status=CampaignStatus.ACTIVE)
        s.add(campaign)
        s.flush()
        campaign_id = campaign.id
        block = uuid.uuid4().int % 10**9 * 10**6
        for start in range(0, count, 20_000):
            s.execute(
                Contact.__table__.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "campaign_id": campaign_id,
                        "phone_number": f"{block + i}",
                        "normalized_phone_number": f"+{block + i}",
                        "status": ContactStatus.PENDING,
                        "attempt_count": 0,
                    }
                    for i in range(start, min(start + 20_000, count))
                ],
            )
        s.commit()

    statements = {"n": 0}

    @event.listens_for(Engine, "before_cursor_execute")
    def _count(*_args):
        statements["n"] += 1

    queue = get_queue()
    rss_before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if use_tracemalloc:
        tracemalloc.start()
    started = time.monotonic()
    with session_factory() as s:
        result = QueueEnqueueService(s, queue, actor="bench").enqueue_campaign(campaign_id)
        s.commit()
    elapsed = time.monotonic() - started
    py_peak_mb = tracemalloc.get_traced_memory()[1] / 1e6 if use_tracemalloc else None
    rss_after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    print(
        f"contacts={count} page_size={settings.enqueue_page_size} "
        f"discovered={result.discovered} enqueued={result.enqueued} "
        f"pages={result.pages_processed} complete={result.complete} "
        f"stream_len={queue.redis.xlen(queue.stream_key)} "
        f"sql_statements={statements['n']} duration_s={elapsed:.2f} "
        f"rate_per_s={result.enqueued / elapsed:.0f} "
        f"rss_growth_mb={(rss_after_kb - rss_before_kb) / 1024:.1f} "
        f"py_peak_mb={'n/a' if py_peak_mb is None else f'{py_peak_mb:.2f}'}"
    )


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    main()
