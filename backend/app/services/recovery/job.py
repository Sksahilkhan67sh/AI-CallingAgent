"""Recovery job payload -- Checkpoint 05 §14. Deliberately small: no
transcript, no working memory, no large conversation history. The
recovery worker loads authoritative state from PostgreSQL.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime


@dataclass
class RecoveryJob:
    job_id: str
    attempt_id: str  # the PREVIOUS attempt -- memory is restored from here
    contact_id: str
    campaign_id: str
    attempt_number: int  # the NEXT attempt number to create
    recovery_type: str  # "RECONNECT"
    idempotency_key: str
    scheduled_at: str
    trace_id: str

    @classmethod
    def new(
        cls,
        *,
        attempt_id: uuid.UUID,
        contact_id: uuid.UUID,
        campaign_id: uuid.UUID,
        attempt_number: int,
    ) -> "RecoveryJob":
        return cls(
            job_id=str(uuid.uuid4()),
            attempt_id=str(attempt_id),
            contact_id=str(contact_id),
            campaign_id=str(campaign_id),
            attempt_number=attempt_number,
            recovery_type="RECONNECT",
            idempotency_key=f"{campaign_id}:{contact_id}:{attempt_number}",
            scheduled_at=datetime.now(UTC).isoformat(),
            trace_id=str(uuid.uuid4()),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "RecoveryJob":
        return cls(**json.loads(data))
