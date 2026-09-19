"""Queue job payload -- Checkpoint 03 Step 3, Step 27.

Deliberately small: just enough to resolve current state from
PostgreSQL, which remains the source of truth. No Contact/Campaign
record is duplicated into Redis.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass
class DialJob:
    job_id: str
    campaign_id: str
    contact_id: str
    attempt_number: int
    idempotency_key: str
    enqueued_at: str
    trace_id: str

    @classmethod
    def new(
        cls, *, campaign_id: uuid.UUID, contact_id: uuid.UUID, attempt_number: int
    ) -> "DialJob":
        idempotency_key = f"{campaign_id}:{contact_id}:{attempt_number}"
        return cls(
            job_id=str(uuid.uuid4()),
            campaign_id=str(campaign_id),
            contact_id=str(contact_id),
            attempt_number=attempt_number,
            idempotency_key=idempotency_key,
            enqueued_at=datetime.now(UTC).isoformat(),
            trace_id=str(uuid.uuid4()),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "DialJob":
        return cls(**json.loads(data))
